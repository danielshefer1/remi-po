# GOWA group key-stub fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A member's first group message after the group's encryption keys change reaches Remi, instead of being dropped as a duplicate of an empty key stub.

**Architecture:** In a WhatsApp group, a sender's first message after a key change arrives from GOWA as two `message` webhooks with the same `id`. The first carries only `senderKeyDistributionMessage`, so it has no `body` and no known kind; the second is the real message. `channel_inbox` is unique on `(channel, provider_message_id)` (`db/models/channel.py:64`), so the stub, stored as `unknown`, wins and the real one counts as a duplicate. The fix: the GOWA parser skips a **group** payload with no content (returns `None` from `_message`, which `parse_webhook` already counts as `skipped`), so the real payload is the only one stored. Direct chats keep `unknown`.

**Tech Stack:** Python, pytest, FastAPI ingress, Postgres (DB tests).

**Spec:** Diagnosis in this session (2026-10-26). Staging evidence: GOWA `level=warning msg="Unrecognized message type … populated proto fields=[senderKeyDistributionMessage messageContextInfo]"` at 18:09:30, 18:09:48, 18:11:24, 18:13:16 UTC. Each was followed by two forwards with the same id: the API logged `message_count=1`, then `duplicate_count=1`, and `channel_inbox` holds only the `unknown` row.

## Global Constraints

- Branch `fix/gowa-group-key-stub` from `main`; one PR; commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- The parser stays total: it never raises, it skips (`parse_webhook` docstring).
- Direct-chat behaviour is unchanged: `test_no_body_and_no_known_kind_is_unknown` still passes as is.
- Unit tests run with `PYTHONPATH=src uv run pytest`. DB tests need `DATABASE__*` set (see `.env.example`); without it they skip, which doesn't count as passing.
- After code changes: `PYTHONHASHSEED=0 graphify update .`

## Review Focus

1. **Stub arrives after the real message.** The real one is stored with its text and the stub is skipped; there is still one row. The ingress test posts in both orders.
2. **Group media with no caption** (voice note, image) has no `body` but has a kind. It must still be stored as `audio`/`image`, not skipped. Parser test on a group `audio` payload without `body`.
3. **Group poll vote whose options didn't resolve** returns `{"message_type": "poll"}`, so it has a kind and stays stored. Covered by the existing `_content` order; no new test needed.
4. **Direct chat with no content** still stored as `unknown` (existing test, unchanged).
5. **Serving a group.** Today a stub can be what makes a group served (`served_at` was 18:09:48, the time of Hilla's stub). After the fix, the member's real message does that. Covered by the existing group-gate DB tests; run them.

---

### Task 1: Skip contentless group payloads in the GOWA parser

**Files:**
- Create: `tests/fixtures/gowa/group_key_stub.json`
- Modify: `src/personal_organizer/providers/channel/gowa/parser.py` (`_message`, ~L153-175; module docstring "Groups" paragraph)
- Test: `tests/unit/test_gowa_parser.py` (`TestGroups`)
- Test: `tests/db/test_gowa_ingress.py` (`TestGroups`)

**Interfaces:**
- Consumes: `_content(payload) -> _Content`, which returns `{"message_type": "unknown"}` when there's neither `body` nor a known kind.
- Produces: no new names. `_message` returns `None` for a group payload whose `_content` is `unknown`.

- [ ] **Step 1: Add the fixture**

`tests/fixtures/gowa/group_key_stub.json`: a copy of `group_text.json` with `"body"` removed. Set the `"id"` to `"3EB0GROUPTEXT00000001"`, the same as `group_text`, so the two pair as stub and real message. Field set is inferred: staging stored no raw payload.

- [ ] **Step 2: Write the failing parser tests** in `TestGroups` of `tests/unit/test_gowa_parser.py`

```python
def test_a_group_key_stub_is_skipped(self) -> None:
    """A sender's first message after a key change comes first as a stub with no
    content, under the real message's id; storing it would drop the real one."""
    batch = _parse(load("group_key_stub"))
    assert batch.messages == ()
    assert batch.skipped == 1

def test_a_group_voice_note_without_text_is_kept(self) -> None:
    payload = load("group_text")
    del payload["payload"]["body"]
    payload["payload"]["audio"] = load("audio")["payload"]["audio"]
    message = _only_message_from(payload)
    assert message.message_type == "audio"
    assert message.group_id == "120363402106000001@g.us"
```

`_only_message` takes a fixture name. Either add `_only_message_from(payload)` next to it, or have the test assert `(message,) = _parse(payload).messages`, following `test_no_body_and_no_known_kind_is_unknown`.

- [ ] **Step 3: Run them**

Run: `PYTHONPATH=src uv run pytest tests/unit/test_gowa_parser.py -k "key_stub or voice_note_without" -v`
Expected: `test_a_group_key_stub_is_skipped` FAILS (one `unknown` message); the voice-note test passes already.

- [ ] **Step 4: Implement in `_message`**

Build `content = _content(payload)` once. If `group_id` is set and `content["message_type"] == "unknown"`, return `None`, with a one-line comment giving the reason: a key-distribution stub shares the real message's id. Pass `**content` to `InboundMessage`. Add one sentence to the module docstring's **Groups** paragraph saying the same.

- [ ] **Step 5: Run the parser suite**

Run: `PYTHONPATH=src uv run pytest tests/unit/test_gowa_parser.py -v`
Expected: all pass, including `test_no_body_and_no_known_kind_is_unknown`.

- [ ] **Step 6: Write the ingress test** in `TestGroups` of `tests/db/test_gowa_ingress.py`

```python
@pytest.mark.parametrize("order", [("group_key_stub", "group_text"), ("group_text", "group_key_stub")])
async def test_the_real_message_survives_its_key_stub(
    self, http: AsyncClient, owner_conn: Any, order: tuple[str, str]
) -> None:
    for name in order:
        body, headers = gowa.signed(gowa.load(name))
        assert (await http.post(URL, content=body, headers=headers)).status_code == 200

    rows = await owner_conn.fetch("SELECT message_type, body FROM channel_inbox")
    assert [(r["message_type"], r["body"]) for r in rows] == [("text", "hi all")]
```

- [ ] **Step 7: Run the DB tests that touch groups and ingress**

Run: `PYTHONPATH=src uv run pytest tests/db/test_gowa_ingress.py tests/db/test_group_gate.py tests/db/test_group_turn.py -v`
Expected: all pass, and none skipped. Skipped means `DATABASE__*` isn't set: set it and rerun.

- [ ] **Step 8: Full check**

Run: `PYTHONPATH=src uv run pytest tests/unit -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
Expected: all green.

- [ ] **Step 9: Commit, graph, PR**

```bash
git add tests/fixtures/gowa/group_key_stub.json tests/unit/test_gowa_parser.py tests/db/test_gowa_ingress.py src/personal_organizer/providers/channel/gowa/parser.py
git commit -m "fix(gowa): skip a group key stub so the real message isn't dropped as its duplicate"
PYTHONHASHSEED=0 graphify update .
git push -u origin fix/gowa-group-key-stub
gh pr create --title "fix(gowa): a member's first group message after a key change reaches Remi"
```

PR body: the staging evidence from **Spec**, the cause, and a note that the stub fixture's field set is inferred. Also add a staging check: after deploy, add or remove someone in the test group, have a member write "רמי, …" as their first message, and confirm one `text` inbox row and a reply.
