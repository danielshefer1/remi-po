"""The GOWA webhook parser: every documented shape, and garbage at every level.

Like the Meta parser it runs after signature verification, so it must store what it can,
count what it cannot, and never raise -- an exception answers 500 and the gateway redelivers.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from personal_organizer.interfaces.channel import DeliveryStatus, SenderRef
from personal_organizer.messaging.groups import gate
from personal_organizer.providers.channel.gowa import parser
from personal_organizer.providers.channel.gowa.parser import jid_user, parse_webhook, phone_jid
from tests.fixtures.gowa_payloads import load
from tests.fixtures.payloads import SENDER_PHONE


def _parse(payload: object, device_id: str | None = None) -> Any:
    return parse_webhook(payload, device_id=device_id)


def _only_message(name: str) -> Any:
    batch = _parse(load(name))
    assert batch.skipped == 0
    (message,) = batch.messages
    return message


class TestMessageTypes:
    def test_text(self) -> None:
        message = _only_message("text")
        assert message.message_type == "text"
        assert message.text == "Oncology appointment with Dr Meyer"
        assert message.provider_message_id == "3EB0C127D7BACC83D6A1"
        assert message.sent_at == datetime(2026, 9, 21, 14, 13, 20, tzinfo=UTC)
        assert message.raw["from"] == "31612345678@s.whatsapp.net"

    def test_a_reply_carries_its_context_and_an_offset_is_normalised(self) -> None:
        message = _only_message("reply")
        assert message.context_message_id == "3EB0C127D7BACC83D6A1"
        assert message.sent_at == datetime(2026, 9, 21, 14, 15, tzinfo=UTC)

    def test_an_image_keeps_its_caption_as_text(self) -> None:
        message = _only_message("image")
        assert message.message_type == "image"
        assert message.text == "My prescription"
        assert message.media_id is None

    def test_a_voice_note_is_audio_without_text(self) -> None:
        """Synthetic until replaced by a real staging voice note (Task 7)."""
        message = _only_message("audio")
        assert message.message_type == "audio"
        assert message.text is None
        assert message.provider_message_id == load("audio")["payload"]["id"]

    def test_a_list_selection_is_an_interactive_reply(self) -> None:
        message = _only_message("selection")
        assert message.message_type == "interactive"
        assert message.reply_id == "slot-3"
        assert message.text == "Thursday 10:00"

    def test_the_gateways_own_summary_is_not_passed_on_as_text(self) -> None:
        """``body`` on a location is GOWA's rendering, not words the user typed."""
        message = _only_message("location")
        assert message.message_type == "location"
        assert message.text is None

    def test_no_body_and_no_known_kind_is_unknown(self) -> None:
        payload = load("text")
        del payload["payload"]["body"]
        (message,) = _parse(payload).messages
        assert message.message_type == "unknown"
        assert message.text is None


class TestSenderIdentity:
    def test_the_sender_is_the_phone_number_in_e164(self) -> None:
        """The allowlist holds ``+31612345678``; the gateway sends a JID."""
        message = _only_message("text")
        assert message.sender == SenderRef(user_id=None, phone=SENDER_PHONE)
        assert message.sender.key == f"tel:{SENDER_PHONE}"

    def test_a_known_phone_wins_over_the_lid_for_the_identity_key(self) -> None:
        """The same person must have the same key on every channel, or the per-person
        invite-only mute would let a stranger collect one reply per channel."""
        message = _only_message("text")
        assert message.sender.user_id is None

    def test_a_lid_only_sender_is_kept_by_lid(self) -> None:
        message = _only_message("lid_only")
        assert message.sender == SenderRef(user_id="251556368777322@lid", phone=None)

    def test_a_device_suffix_is_dropped(self) -> None:
        payload = load("text")
        payload["payload"]["from"] = "31612345678:12@s.whatsapp.net"
        (message,) = _parse(payload).messages
        assert message.sender.phone == SENDER_PHONE

    def test_jid_helpers_round_trip(self) -> None:
        assert phone_jid(SENDER_PHONE) == "31612345678@s.whatsapp.net"
        assert jid_user(phone_jid(SENDER_PHONE)) == "31612345678"


class TestReceipts:
    def test_delivered_receipts_become_updates_for_every_id(self) -> None:
        batch = _parse(load("ack_delivered"))
        assert [update.provider_message_id for update in batch.updates] == [
            "3EB0AAAAAAAAAAAAAAAA01",
            "3EB0AAAAAAAAAAAAAAAA02",
        ]
        assert {update.status for update in batch.updates} == {DeliveryStatus.DELIVERED}
        assert batch.updates[0].at == datetime(2026, 9, 21, 14, 22, tzinfo=UTC)

    def test_a_read_receipt_is_read(self) -> None:
        payload = load("ack_delivered")
        payload["payload"]["receipt_type"] = "read"
        assert {update.status for update in _parse(payload).updates} == {DeliveryStatus.READ}

    def test_our_own_account_reading_a_message_is_skipped(self) -> None:
        assert _parse(load("ack_read_self")).updates == ()

    def test_an_unknown_receipt_type_is_skipped(self) -> None:
        payload = load("ack_delivered")
        payload["payload"]["receipt_type"] = "played"
        batch = _parse(payload)
        assert batch.updates == ()
        assert batch.skipped == 1


class TestGroups:
    def test_group_text_keeps_group_and_sender(self) -> None:
        message = _only_message("group_text")
        assert message.group_id == "120363402106000001@g.us"
        assert message.sender.phone == "+972500000002"
        assert message.sender_name == "Noa"
        assert message.text == "hi all"

    def test_direct_message_has_no_group(self) -> None:
        assert _only_message("text").group_id is None

    def test_group_poll_vote(self) -> None:
        message = _only_message("group_poll_vote")
        assert message.message_type == "poll_vote"
        assert message.reply_id == load("group_poll_vote")["payload"]["poll"]["poll_id"]
        assert message.text == "Yes"
        assert message.group_id == "120363402106000001@g.us"

    def test_direct_poll_vote(self) -> None:
        message = _only_message("direct_poll_vote")
        assert message.message_type == "poll_vote"
        assert message.group_id is None

    def test_cleared_vote_has_no_text(self) -> None:
        payload = load("group_poll_vote")
        payload["payload"]["poll"]["selected_options"] = []
        payload["payload"]["poll"]["selected_option_hashes"] = []
        message = _parse(payload).messages[0]
        assert message.message_type == "poll_vote"
        assert message.text is None

    def test_undecryptable_vote_is_plain_poll(self) -> None:
        payload = load("group_poll_vote")
        payload["payload"]["poll"]["resolution_status"] = "decrypt_failed"
        assert _parse(payload).messages[0].message_type == "poll"

    def test_a_poll_creation_is_a_plain_poll(self) -> None:
        payload = load("group_poll_vote")
        payload["payload"]["poll"]["type"] = "creation"
        assert _parse(payload).messages[0].message_type == "poll"


class TestSkipped:
    @pytest.mark.parametrize("name", ["from_me", "reaction"])
    def test_events_the_bot_does_not_answer(self, name: str) -> None:
        """Our own sends echoed back would loop; reactions are not requests."""
        batch = _parse(load(name))
        assert batch.messages == ()
        assert batch.skipped == 1

    @pytest.mark.parametrize("chat_id", ["status@broadcast", "120363000000@newsletter"])
    def test_broadcasts_and_newsletters(self, chat_id: str) -> None:
        payload = load("text")
        payload["payload"]["chat_id"] = chat_id
        assert _parse(payload).skipped == 1

    def test_newsletter_still_skipped(self) -> None:
        payload = load("group_text")
        payload["payload"]["chat_id"] = "120363000000@newsletter"
        assert _parse(payload).skipped == 1

    def test_another_device_is_skipped(self) -> None:
        assert _parse(load("text"), device_id="someone-else").skipped == 1

    def test_the_configured_device_is_kept(self) -> None:
        assert len(_parse(load("text"), device_id="po-bot").messages) == 1

    def test_no_session_id_is_kept_even_with_a_device_configured(self) -> None:
        """The gateway omits ``session_id`` when it cannot map the JID to a device."""
        assert len(_parse(load("reply"), device_id="po-bot").messages) == 1

    def test_a_message_without_an_id_is_skipped(self) -> None:
        payload = load("text")
        del payload["payload"]["id"]
        assert _parse(payload).skipped == 1

    def test_a_message_with_no_sender_at_all_is_skipped(self) -> None:
        payload = load("text")
        del payload["payload"]["from"]
        del payload["payload"]["from_lid"]
        assert _parse(payload).skipped == 1

    def test_an_unreadable_timestamp_becomes_now(self) -> None:
        payload = load("text")
        payload["payload"]["timestamp"] = "yesterday-ish"
        before = datetime.now(UTC)
        (message,) = _parse(payload).messages
        assert before <= message.sent_at <= datetime.now(UTC)


def _garbage_variants() -> list[Any]:
    base = load("text")
    variants: list[Any] = [None, 42, "text", [], {}, {"event": "message"}]
    envelope_junk: tuple[Any, ...] = (None, 7, "x", [], {}, [1, 2])
    field_junk: tuple[Any, ...] = (None, 7, [], {}, "")
    ids_junk: tuple[Any, ...] = (None, 7, "x", [None, 3, ""], {})
    for key in ("event", "device_id", "session_id", "payload"):
        for junk in envelope_junk:
            variant = copy.deepcopy(base)
            variant[key] = junk
            variants.append(variant)
    for key in ("id", "chat_id", "from", "from_lid", "timestamp", "is_from_me", "body"):
        for junk in field_junk:
            variant = copy.deepcopy(base)
            variant["payload"][key] = junk
            variants.append(variant)
    ack = load("ack_delivered")
    for junk in ids_junk:
        variant = copy.deepcopy(ack)
        variant["payload"]["ids"] = junk
        variants.append(variant)
    return variants


@pytest.mark.parametrize("payload", _garbage_variants())
def test_garbage_at_any_level_never_raises(payload: Any) -> None:
    batch = _parse(payload)
    assert len(batch.messages) + len(batch.updates) + batch.skipped >= 1


def test_the_gate_matches_the_parsers_poll_vote() -> None:
    """messaging may not import providers, so the gate keeps its own copy: kept equal here."""
    assert gate.POLL_VOTE == parser.POLL_VOTE
