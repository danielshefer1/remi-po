from __future__ import annotations

import json
import os
from typing import Any

import pytest
from pydantic import ValidationError

from personal_organizer.core.errors import MissingDatabaseRoleError
from personal_organizer.db.roles import DatabaseRole
from personal_organizer.settings import Settings


class TestParsing:
    def test_nested_env_vars(self, settings: Settings) -> None:
        assert settings.models.chat_model == "gpt-5-mini"
        assert settings.database.app_url.get_secret_value().startswith("postgresql://")

    def test_dsn_for_role(self, settings: Settings) -> None:
        assert "app_user" in settings.dsn_for(DatabaseRole.APP)
        assert "app_owner" in settings.dsn_for(DatabaseRole.OWNER)

    def test_missing_role_raises(self, settings_factory: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("DATABASE__BOOTSTRAP_URL", raising=False)
        cfg = settings_factory()
        object.__setattr__(cfg.database, "bootstrap_url", None)
        with pytest.raises(MissingDatabaseRoleError):
            cfg.dsn_for(DatabaseRole.BOOTSTRAP)

    def test_the_definer_role_has_no_dsn(self, settings_factory: Any) -> None:
        """app_definer is NOLOGIN and BYPASSRLS: a DSN for it must never be honoured, even
        one someone configured."""
        cfg = settings_factory(DATABASE__DEFINER_URL="postgresql://app_definer:pw@h/db")
        with pytest.raises(MissingDatabaseRoleError, match="NOLOGIN"):
            cfg.dsn_for(DatabaseRole.DEFINER)


class TestSecretsNeverRender:
    """An exception traceback that renders the settings object is a classic leak."""

    @pytest.mark.parametrize("render", [repr, str, lambda c: c.model_dump_json()])
    def test_secret_values_are_hidden(self, settings: Settings, render: Any) -> None:
        text = render(settings)
        assert "pw@localhost" not in text
        assert "test-pepper" not in text

    def test_json_dump_is_still_valid_json(self, settings: Settings) -> None:
        assert isinstance(json.loads(settings.model_dump_json()), dict)


class TestDeployedInvariants:
    """A deployed service that could leak PII must refuse to boot."""

    @pytest.mark.parametrize(
        "override",
        [
            {"LOGGING__ALLOW_RAW_PII": "true"},
            {"LOGGING__RENDERER": "console"},
            {"APP__DEBUG": "true"},
        ],
    )
    def test_unsafe_settings_are_rejected(
        self, settings_factory: Any, override: dict[str, str]
    ) -> None:
        with pytest.raises(ValidationError):
            settings_factory(
                APP__ENV="production",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                **override,
            )

    def test_placeholder_pepper_is_rejected(self, settings_factory: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("LOGGING__PII_PEPPER", raising=False)
        with pytest.raises(ValidationError, match="PII_PEPPER"):
            settings_factory(
                APP__ENV="staging",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                LOGGING__PII_PEPPER="local-dev-pepper-not-secret",
            )

    def test_missing_sentry_dsn_is_rejected(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError, match="SENTRY__DSN"):
            settings_factory(APP__ENV="production")

    def test_staging_without_an_internal_token_is_rejected(self, settings_factory: Any) -> None:
        """The /internal router is mounted whenever env != production, so staging serves it on
        a public URL. Refusing to boot is what keeps it from being open there."""
        with pytest.raises(ValidationError, match="APP__INTERNAL_TOKEN"):
            settings_factory(
                APP__ENV="staging",
                SENTRY__DSN="https://k@o.ingest.sentry.io/1",
                LOGGING__PII_PEPPER="a-real-secret",
            )

    def test_production_needs_no_internal_token(self, settings_factory: Any) -> None:
        """Because the router is not mounted there at all."""
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.app.internal_token is None

    def test_valid_production_config_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.is_deployed

    def test_local_is_permissive(self, settings_factory: Any) -> None:
        cfg = settings_factory(APP__ENV="local", LOGGING__ALLOW_RAW_PII="true")
        assert cfg.logging.allow_raw_pii
        assert not cfg.is_deployed


VERIFY_TOKEN = "v" * 64

_WHATSAPP_CREDENTIALS = {
    "WHATSAPP__APP_SECRET": "app-secret",
    "WHATSAPP__VERIFY_TOKEN": VERIFY_TOKEN,
    "WHATSAPP__ACCESS_TOKEN": "graph-token",
    "WHATSAPP__PHONE_NUMBER_ID": "1234567890",
}

_DEPLOYED = {
    "SENTRY__DSN": "https://k@o.ingest.sentry.io/1",
    "LOGGING__PII_PEPPER": "a-real-secret",
    "APP__INTERNAL_TOKEN": "t",
}


class TestWhatsApp:
    """Off by default and all-or-nothing when on, so that staging can deploy before the Meta
    app exists and a half-configured channel refuses to boot rather than half-working."""

    def test_disabled_by_default_with_nothing_required(self, settings: Settings) -> None:
        assert settings.whatsapp.enabled is False
        assert settings.whatsapp.app_secret is None

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_a_deployed_service_boots_with_whatsapp_disabled(
        self, settings_factory: Any, env: str
    ) -> None:
        """What lets Iteration 02 merge and deploy before the Meta credentials exist."""
        cfg = settings_factory(APP__ENV=env, **_DEPLOYED)
        assert cfg.is_deployed
        assert not cfg.whatsapp.enabled

    def test_enabled_with_nothing_names_every_missing_credential_at_once(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(WHATSAPP__ENABLED="true")
        message = str(excinfo.value)
        for name in _WHATSAPP_CREDENTIALS:
            assert name in message

    def test_enabled_with_every_credential_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ENABLED="true", **_WHATSAPP_CREDENTIALS)
        assert cfg.whatsapp.enabled
        assert cfg.whatsapp.phone_number_id == "1234567890"

    def test_enabled_in_staging_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            APP__ENV="staging", WHATSAPP__ENABLED="true", **_DEPLOYED, **_WHATSAPP_CREDENTIALS
        )
        assert cfg.whatsapp.enabled

    def test_a_short_verify_token_is_rejected(self, settings_factory: Any) -> None:
        """It travels in a query string; below 32 characters the token scrubber misses it."""
        with pytest.raises(ValidationError, match="VERIFY_TOKEN"):
            settings_factory(WHATSAPP__VERIFY_TOKEN="short")

    def test_plain_http_graph_url_is_rejected_when_deployed(self, settings_factory: Any) -> None:
        """The bearer token goes to this URL on every reply."""
        with pytest.raises(ValidationError, match="GRAPH_BASE_URL"):
            settings_factory(
                APP__ENV="production",
                WHATSAPP__ENABLED="true",
                WHATSAPP__GRAPH_BASE_URL="http://graph.facebook.com",
                **_DEPLOYED,
                **_WHATSAPP_CREDENTIALS,
            )

    def test_the_allowlist_is_comma_separated_and_normalised(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ALLOWED_PHONES=" +31 6 1234 5678, 0044 7700 900123 ,")
        assert cfg.whatsapp.allowlist == frozenset({"+31612345678", "+447700900123"})

    def test_an_invalid_allowlist_entry_fails_boot_without_echoing_it(
        self, settings_factory: Any
    ) -> None:
        """Silently dropping a typo'd number would leave its owner locked out with no
        explanation; echoing it would put a phone number in the deploy log."""
        with pytest.raises(ValidationError, match="ALLOWED_PHONES") as excinfo:
            settings_factory(WHATSAPP__ALLOWED_PHONES="+31612345678,0612345678")
        assert "0612345678" not in str(excinfo.value)
        assert "31612345678" not in str(excinfo.value)

    def test_a_boot_failure_never_repeats_a_secret(self, settings_factory: Any) -> None:
        """Pydantic echoes the input in its error by default, and boot failures are printed
        straight into the platform's deploy log."""
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(APP__ENV="production", LOGGING__PII_PEPPER="a-real-secret")
        assert "a-real-secret" not in str(excinfo.value)
        assert "pw@localhost" not in str(excinfo.value)

    def test_an_empty_allowlist_is_empty(self, settings: Settings) -> None:
        assert settings.whatsapp.allowlist == frozenset()

    def test_credentials_never_render(self, settings_factory: Any) -> None:
        cfg = settings_factory(WHATSAPP__ENABLED="true", **_WHATSAPP_CREDENTIALS)
        rendered = repr(cfg) + cfg.model_dump_json()
        for secret in ("app-secret", VERIFY_TOKEN, "graph-token"):
            assert secret not in rendered


GOWA_WEBHOOK_SECRET = "g" * 40

_GOWA_CREDENTIALS = {
    "GOWA__BASIC_AUTH_USER": "po",
    "GOWA__BASIC_AUTH_PASSWORD": "gateway-password",
    "GOWA__WEBHOOK_SECRET": GOWA_WEBHOOK_SECRET,
}


class TestGowa:
    """The QR-code gateway: off by default, all-or-nothing when on, independent of Meta."""

    def test_disabled_by_default_with_nothing_required(self, settings: Settings) -> None:
        assert settings.gowa.enabled is False
        assert settings.gowa.webhook_secret is None

    def test_enabled_with_nothing_names_every_missing_credential_at_once(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(GOWA__ENABLED="true")
        message = str(excinfo.value)
        for name in _GOWA_CREDENTIALS:
            assert name in message

    def test_enabled_with_every_credential_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            GOWA__ENABLED="true", GOWA__BASE_URL="http://gowa:3000/", **_GOWA_CREDENTIALS
        )
        assert cfg.gowa.enabled
        assert cfg.gowa.base_url == "http://gowa:3000"

    @pytest.mark.parametrize("secret", ["secret", "short-but-not-the-default"])
    def test_a_guessable_webhook_secret_is_rejected(
        self, settings_factory: Any, secret: str
    ) -> None:
        """``secret`` is the gateway's documented default."""
        with pytest.raises(ValidationError, match="GOWA__WEBHOOK_SECRET") as excinfo:
            settings_factory(GOWA__WEBHOOK_SECRET=secret)
        assert "short-but-not-the-default" not in str(excinfo.value)

    def test_both_whatsapp_providers_may_run_at_once(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            WHATSAPP__ENABLED="true",
            GOWA__ENABLED="true",
            **_WHATSAPP_CREDENTIALS,
            **_GOWA_CREDENTIALS,
        )
        assert cfg.whatsapp.enabled
        assert cfg.gowa.enabled

    @pytest.mark.parametrize(
        "url", ["http://gowa.railway.internal:3000", "https://gowa.example.com"]
    )
    def test_deployed_base_url_is_private_or_tls(self, settings_factory: Any, url: str) -> None:
        cfg = settings_factory(
            APP__ENV="staging",
            GOWA__ENABLED="true",
            GOWA__BASE_URL=url,
            **_DEPLOYED,
            **_GOWA_CREDENTIALS,
        )
        assert cfg.gowa.base_url == url

    def test_plain_http_over_the_internet_is_rejected_when_deployed(
        self, settings_factory: Any
    ) -> None:
        """The basic-auth credentials go to this URL on every reply."""
        with pytest.raises(ValidationError, match="GOWA__BASE_URL"):
            settings_factory(
                APP__ENV="production",
                GOWA__ENABLED="true",
                GOWA__BASE_URL="http://gowa.example.com",
                **_DEPLOYED,
                **_GOWA_CREDENTIALS,
            )

    @pytest.mark.parametrize("url", ["gowa:3000", "ftp://gowa", "http://user:pw@gowa"])
    def test_a_malformed_base_url_is_rejected(self, settings_factory: Any, url: str) -> None:
        with pytest.raises(ValidationError, match="GOWA__BASE_URL"):
            settings_factory(GOWA__BASE_URL=url)

    def test_the_typing_delay_is_bounded(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError):
            settings_factory(GOWA__TYPING_DELAY_S="30")
        with pytest.raises(ValidationError):
            settings_factory(GOWA__TYPING_DELAY_S="10.5")
        assert settings_factory(GOWA__TYPING_DELAY_S="10").gowa.typing_delay_s == 10.0

    def test_the_typing_jitter_is_bounded(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError):
            settings_factory(GOWA__TYPING_JITTER_S="6")
        with pytest.raises(ValidationError):
            settings_factory(GOWA__TYPING_JITTER_S="-1")

    def test_replies_pause_three_to_five_seconds_by_default(self, settings_factory: Any) -> None:
        gowa = settings_factory().gowa
        assert (gowa.typing_delay_s, gowa.typing_jitter_s) == (3.0, 2.0)

    def test_credentials_never_render(self, settings_factory: Any) -> None:
        cfg = settings_factory(GOWA__ENABLED="true", **_GOWA_CREDENTIALS)
        rendered = repr(cfg) + cfg.model_dump_json()
        for secret in ("gateway-password", GOWA_WEBHOOK_SECRET):
            assert secret not in rendered


LINK_SECRET = "s" * 32

_COMPOSIO = {
    "COMPOSIO__API_KEY": "composio-key",
    "COMPOSIO__CALENDAR_AUTH_CONFIG_ID": "ac_calendar123",
    "APP__PUBLIC_BASE_URL": "https://staging.example.com",
    "ONBOARDING__LINK_SECRET": LINK_SECRET,
}


class TestComposio:
    """Off by default and all-or-nothing when on, like WhatsApp -- except that two of the
    four things it needs live in other sections, so the check is on `Settings`."""

    def test_disabled_by_default_with_nothing_required(self, settings: Settings) -> None:
        assert settings.composio.enabled is False
        assert settings.composio.api_key is None

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_a_deployed_service_boots_with_composio_disabled(
        self, settings_factory: Any, env: str
    ) -> None:
        """What lets every PR in the Iteration 03 stack deploy before Composio exists."""
        cfg = settings_factory(APP__ENV=env, **_DEPLOYED)
        assert not cfg.composio.enabled

    def test_enabled_with_nothing_names_every_missing_setting_at_once(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(COMPOSIO__ENABLED="true")
        message = str(excinfo.value)
        for name in _COMPOSIO:
            assert name in message

    def test_enabled_with_everything_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(COMPOSIO__ENABLED="true", **_COMPOSIO)
        assert cfg.composio.enabled
        assert cfg.composio.calendar_auth_config_id == "ac_calendar123"

    def test_enabled_in_production_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            APP__ENV="production", COMPOSIO__ENABLED="true", **_DEPLOYED, **_COMPOSIO
        )
        assert cfg.composio.enabled

    @pytest.mark.parametrize("wrong", ["ca_connected123", "googlecalendar", "AC_upper"])
    def test_something_other_than_an_auth_config_id_is_rejected(
        self, settings_factory: Any, wrong: str
    ) -> None:
        """A connected-account id or a toolkit slug is the likely paste mistake, and it would
        otherwise first fail at a real user's onboarding."""
        with pytest.raises(ValidationError, match="CALENDAR_AUTH_CONFIG_ID"):
            settings_factory(COMPOSIO__CALENDAR_AUTH_CONFIG_ID=wrong)

    def test_credentials_never_render(self, settings_factory: Any) -> None:
        cfg = settings_factory(COMPOSIO__ENABLED="true", **_COMPOSIO)
        rendered = repr(cfg) + cfg.model_dump_json()
        for secret in ("composio-key", LINK_SECRET):
            assert secret not in rendered


class TestAgent:
    """Off by default. On, it needs its key and Composio, named together like Composio's."""

    def test_disabled_by_default_with_nothing_required(self, settings: Settings) -> None:
        assert settings.agent.enabled is False
        assert settings.agent.api_key is None

    def test_enabled_with_nothing_names_every_missing_setting_at_once(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(AGENT__ENABLED="true")
        message = str(excinfo.value)
        assert "COMPOSIO__ENABLED" in message
        assert "AGENT__API_KEY" in message

    def test_enabled_with_everything_boots(self, settings_factory: Any) -> None:
        cfg = settings_factory(
            AGENT__ENABLED="true",
            AGENT__API_KEY="sk-ant-test",
            COMPOSIO__ENABLED="true",
            **_COMPOSIO,
        )
        assert cfg.agent.enabled
        assert "sk-ant-test" not in repr(cfg) + cfg.model_dump_json()


class TestVoice:
    def test_off_by_default(self, settings: Settings) -> None:
        assert not settings.voice.enabled

    def test_enabled_without_a_key_fails(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError) as excinfo:
            settings_factory(VOICE__ENABLED="true")
        assert "VOICE__API_KEY" in str(excinfo.value)

    def test_enabled_with_a_key_boots_and_hides_it(self, settings_factory: Any) -> None:
        cfg = settings_factory(VOICE__ENABLED="true", VOICE__API_KEY="sk-proj-test")
        assert cfg.voice.enabled
        assert "sk-proj-test" not in repr(cfg) + cfg.model_dump_json()


class TestPublicBaseUrl:
    def test_unset_by_default(self, settings: Settings) -> None:
        assert settings.app.public_base_url is None

    def test_blank_means_unset(self, settings_factory: Any) -> None:
        """A variable created in a dashboard and left empty is "not set", not an error."""
        assert settings_factory(APP__PUBLIC_BASE_URL=" ").app.public_base_url is None

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("https://staging.example.com", "https://staging.example.com"),
            ("https://staging.example.com/", "https://staging.example.com"),
            ("http://localhost:8000", "http://localhost:8000"),
        ],
    )
    def test_an_origin_is_stored_without_a_trailing_slash(
        self, settings_factory: Any, given: str, stored: str
    ) -> None:
        """So that `base + "/connect/..."` never produces a double slash."""
        assert settings_factory(APP__PUBLIC_BASE_URL=given).app.public_base_url == stored

    @pytest.mark.parametrize(
        "bad",
        [
            "staging.example.com",
            "ftp://example.com",
            "https://example.com/app",
            "https://example.com?x=1",
            "https://user:pw@example.com",
        ],
    )
    def test_anything_but_an_origin_is_rejected(self, settings_factory: Any, bad: str) -> None:
        with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
            settings_factory(APP__PUBLIC_BASE_URL=bad)

    def test_plain_http_is_rejected_when_deployed(self, settings_factory: Any) -> None:
        """Every onboarding link carries a bearer token in its path."""
        with pytest.raises(ValidationError, match="PUBLIC_BASE_URL must be https"):
            settings_factory(
                APP__ENV="staging", APP__PUBLIC_BASE_URL="http://staging.example.com", **_DEPLOYED
            )


class TestOnboardingLinks:
    def test_defaults(self, settings: Settings) -> None:
        assert settings.onboarding.link_secret is None
        assert settings.onboarding.link_ttl_s == 900

    def test_a_short_link_secret_is_rejected(self, settings_factory: Any) -> None:
        with pytest.raises(ValidationError, match="LINK_SECRET"):
            settings_factory(ONBOARDING__LINK_SECRET="s" * 31)

    def test_the_example_placeholder_is_rejected_when_deployed(self, settings_factory: Any) -> None:
        """Copying .env.example into a deployed environment must not yield forgeable links."""
        with pytest.raises(ValidationError, match="LINK_SECRET must be set to a real secret"):
            settings_factory(
                APP__ENV="production",
                ONBOARDING__LINK_SECRET="local-dev-link-secret-not-a-secret-0000",
                **_DEPLOYED,
            )

    def test_the_example_placeholder_is_fine_locally(self, settings_factory: Any) -> None:
        cfg = settings_factory(ONBOARDING__LINK_SECRET="local-dev-link-secret-not-a-secret-0000")
        assert cfg.onboarding.link_secret is not None

    @pytest.mark.parametrize("ttl", ["59", "3601"])
    def test_the_link_lifetime_is_bounded(self, settings_factory: Any, ttl: str) -> None:
        with pytest.raises(ValidationError):
            settings_factory(ONBOARDING__LINK_TTL_S=ttl)

    def test_the_bot_phone_is_optional_and_normalised(self, settings_factory: Any) -> None:
        assert settings_factory().onboarding.bot_phone is None
        cfg = settings_factory(ONBOARDING__BOT_PHONE="+972 50-123-4567")
        assert cfg.onboarding.bot_phone == "+972501234567"

    def test_an_invalid_bot_phone_fails_boot_without_echoing_it(
        self, settings_factory: Any
    ) -> None:
        with pytest.raises(ValidationError, match="BOT_PHONE") as excinfo:
            settings_factory(ONBOARDING__BOT_PHONE="0501234567")
        assert "0501234567" not in str(excinfo.value)


class TestThePlatformGuard:
    """`APP__ENV` is what every other check is gated on, so it cannot be optional.

    Unset, it defaults to "local", and the failure is silent in the worst direction: no
    check fires, the placeholder pepper is accepted, and `/internal` mounts itself on a
    public URL with its token check disabled. So a container the platform is running is not
    allowed to claim it is a laptop.
    """

    def test_a_platform_container_without_app_env_refuses_to_boot(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.delenv("APP__ENV", raising=False)
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="local")

    def test_any_platform_variable_is_enough(self, settings_factory: Any, monkeypatch: Any) -> None:
        """Detected by prefix: the exact set the platform injects is not ours to depend on."""
        monkeypatch.setenv("RAILWAY_SERVICE_NAME", "api")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="local")

    def test_ci_on_the_platform_is_rejected_too(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        """`ci` is not deployed either, and it is the other value that disables the checks."""
        monkeypatch.setenv("RAILWAY_PROJECT_ID", "p-1")
        with pytest.raises(ValidationError, match="APP__ENV"):
            settings_factory(APP__ENV="ci")

    def test_a_declared_environment_on_the_platform_is_fine(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        cfg = settings_factory(
            APP__ENV="production",
            SENTRY__DSN="https://k@o.ingest.sentry.io/1",
            LOGGING__PII_PEPPER="a-real-secret",
        )
        assert cfg.is_deployed

    def test_off_the_platform_local_stays_permissive(
        self, settings_factory: Any, monkeypatch: Any
    ) -> None:
        """The guard must not reach a laptop or a CI runner, which have no such variables."""
        for name in [key for key in os.environ if key.startswith("RAILWAY_")]:
            monkeypatch.delenv(name, raising=False)
        cfg = settings_factory(APP__ENV="local")
        assert not cfg.is_deployed
