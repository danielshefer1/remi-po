"""Application settings.

One ``Settings`` object, assembled from environment variables with ``__`` as the nesting
delimiter: ``DATABASE__APP_URL``, ``MODELS__CHAT_MODEL``, ``LOGGING__PII_PEPPER``.

The important design choice here is ``_enforce_deployed_invariants``: in staging and
production the settings *tighten*, and a service that is misconfigured in a way that could
leak PII refuses to boot. Railway's healthcheck then fails the deploy and rolls back, which
is far better than a service that comes up happily and writes phone numbers to stdout.

Which puts all of the weight on ``APP__ENV``, and it used to default to ``"local"`` in
silence. Omitting one variable therefore disabled every check at once rather than tripping
any of them -- so the gate is now guarded by :func:`_on_platform`, which asks the platform
instead of asking the operator.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from personal_organizer.core.errors import MissingDatabaseRoleError
from personal_organizer.core.phone import normalise_e164
from personal_organizer.db.roles import DEFINER_ROLE_NAME, DatabaseRole

Environment = Literal["local", "ci", "staging", "production"]
Component = Literal["api", "worker", "cli"]

#: Placeholder pepper; rejected by the validator outside local/ci.
_DEV_PEPPER = "local-dev-pepper-not-secret"

#: Placeholder link secret from ``.env.example``; rejected by the validator outside local/ci.
_DEV_LINK_SECRET = "local-dev-link-secret-not-a-secret-0000"  # noqa: S105 - a known placeholder

#: Railway injects its own variables into every container it runs. Their presence is proof
#: of being deployed that does not depend on anyone remembering to say so.
_PLATFORM_PREFIX = "RAILWAY_"


def _on_platform() -> bool:
    return any(name.startswith(_PLATFORM_PREFIX) for name in os.environ)


class AppSettings(BaseModel):
    env: Environment = "local"
    component: Component = "api"
    #: Set from ``RAILWAY_GIT_COMMIT_SHA``. Nothing reads that variable automatically, so the
    #: Railway services map it explicitly -- see ``docs/runbook-iteration-01.md``. Left unset it
    #: reports "dev", which makes every Sentry release indistinguishable.
    release: str = "dev"
    debug: bool = False
    #: Shared secret for the ``/internal`` router. That router is mounted whenever ``env`` is not
    #: ``production``, so on staging it is publicly reachable and enqueues work per call.
    internal_token: SecretStr | None = None
    #: The origin users' browsers reach the api at -- where onboarding links point and where
    #: Composio sends them back to, e.g. ``https://staging.example.com``. An origin and nothing
    #: more, so building a URL is concatenation. Required when ``COMPOSIO__ENABLED``.
    public_base_url: str | None = None

    @field_validator("public_base_url")
    @classmethod
    def _origin_only(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            msg = "APP__PUBLIC_BASE_URL must be an origin such as https://example.com, no path"
            raise ValueError(msg)
        return f"{parts.scheme}://{parts.netloc}"


class DatabaseSettings(BaseModel):
    """DSNs, one per privilege tier. See :mod:`personal_organizer.db.roles`."""

    app_url: SecretStr
    owner_url: SecretStr | None = None
    bootstrap_url: SecretStr | None = None

    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout: float = 10.0
    pool_recycle_seconds: int = 1800
    connect_timeout: float = 10.0
    #: How long a starting api or worker keeps retrying its first connection. Covers a
    #: platform whose private network is not routable for the first seconds of a
    #: container's life; it is not a substitute for a DSN that points somewhere real,
    #: which fails on the first attempt regardless. See ``Database.wait_ready``.
    startup_timeout: float = 30.0
    statement_timeout_ms: int = 15_000
    # A leaked open transaction is a pooled connection stuck with a tenant GUC set, so this
    # is a correctness guard for the RLS design, not just hygiene.
    idle_in_transaction_timeout_ms: int = 30_000
    echo: bool = False


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    renderer: Literal["json", "console"] = "json"
    pii_pepper: SecretStr = SecretStr(_DEV_PEPPER)
    #: Escape hatch for local debugging. The validator makes it unreachable when deployed.
    allow_raw_pii: bool = False


class SentrySettings(BaseModel):
    dsn: SecretStr | None = None
    traces_sample_rate: float = 0.1
    profiles_sample_rate: float = 0.0


class LangfuseSettings(BaseModel):
    public_key: SecretStr | None = None
    secret_key: SecretStr | None = None
    #: EU host by default -- the data inventory for this product is squarely personal.
    host: str = "https://cloud.eu.langfuse.com"

    @property
    def enabled(self) -> bool:
        return self.public_key is not None and self.secret_key is not None


class ModelSettings(BaseModel):
    """Model IDs are settings, per the spec, so a deprecation is a config change.

    ``embedding_dimensions`` is the exception that proves the rule: pgvector fixes
    ``vector(N)`` at DDL time, so changing it is a migration plus a re-embed backfill,
    not a config flip.
    """

    chat_model: str
    fallback_model: str
    embedding_model: str
    embedding_dimensions: int = 1536
    transcription_model: str
    request_timeout_s: float = 60.0
    max_retries: int = 3


class WorkerSettings(BaseModel):
    concurrency: int = 8
    queues: list[str] = Field(default_factory=lambda: ["default", "webhooks", "maintenance"])
    shutdown_graceful_timeout_s: int = 30
    pool_min_size: int = 1
    pool_max_size: int = 4


#: The verify token rides in a query string (``?hub.verify_token=``), which is the one place
#: a secret can reach Sentry's request URL. At this length the ``_LONG_TOKEN`` scrubber
#: recognises it everywhere, so the floor is what keeps it out of logs -- not a nicety.
MIN_VERIFY_TOKEN_LENGTH: Final = 32


class WhatsAppSettings(BaseModel):
    """Meta's WhatsApp Cloud API.

    Off by default, and all-or-nothing when on. The explicit flag is the point: inferring
    "enabled" from whichever secrets happen to be present turns a forgotten
    ``WHATSAPP__APP_SECRET`` into a webhook that silently is not there, instead of a service
    that refuses to boot and names the missing variable. Disabled, the webhook router is not
    mounted at all, so nothing accepts unsigned input while the credentials do not exist yet.
    """

    enabled: bool = False
    #: HMAC key for ``X-Hub-Signature-256`` -- App settings -> Basic -> App secret. api only.
    app_secret: SecretStr | None = None
    #: Chosen by us, typed into Meta's webhook form, echoed back on the GET handshake. api only.
    verify_token: SecretStr | None = None
    #: Graph API bearer token (a System User token; the dashboard's expires in 24h). worker only.
    access_token: SecretStr | None = None
    #: The sending number's id -- not the number itself. worker only.
    phone_number_id: str | None = None
    graph_api_version: str = "v24.0"
    graph_base_url: str = "https://graph.facebook.com"
    send_timeout_s: float = 10.0
    #: Comma-separated E.164 numbers. A ``str`` rather than ``list[str]`` because
    #: pydantic-settings JSON-decodes list fields from the environment, and a plain
    #: comma-separated value is not JSON. From Iteration 03 this is the *invite* list: a number
    #: on it may start onboarding, and ``tenant_identities`` records who actually has. It is
    #: the invite list for every channel -- the GOWA gateway's too -- so it applies whether
    #: or not ``WHATSAPP__ENABLED`` is.
    allowed_phones: str = ""

    @property
    def allowlist(self) -> frozenset[str]:
        """The allowlist, normalised. The validator guarantees every entry normalises."""
        return frozenset(
            phone for entry in self._allowlist_entries() if (phone := normalise_e164(entry))
        )

    def _allowlist_entries(self) -> list[str]:
        return [entry.strip() for entry in self.allowed_phones.split(",") if entry.strip()]

    @model_validator(mode="after")
    def _all_or_nothing(self) -> WhatsAppSettings:
        problems: list[str] = []
        if self.enabled:
            required = {
                "WHATSAPP__APP_SECRET": self.app_secret,
                "WHATSAPP__VERIFY_TOKEN": self.verify_token,
                "WHATSAPP__ACCESS_TOKEN": self.access_token,
                "WHATSAPP__PHONE_NUMBER_ID": self.phone_number_id,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                problems.append(f"{', '.join(missing)} required when WHATSAPP__ENABLED is true")
        token = self.verify_token.get_secret_value() if self.verify_token else None
        if token is not None and len(token) < MIN_VERIFY_TOKEN_LENGTH:
            problems.append(
                f"WHATSAPP__VERIFY_TOKEN must be at least {MIN_VERIFY_TOKEN_LENGTH} characters"
            )
        # Counted, never echoed: an entry that fails to parse is still somebody's number.
        invalid = sum(1 for entry in self._allowlist_entries() if normalise_e164(entry) is None)
        if invalid:
            problems.append(
                f"WHATSAPP__ALLOWED_PHONES has {invalid} entries that are not E.164 numbers"
            )
        if problems:
            msg = "Invalid WhatsApp settings: " + "; ".join(problems)
            raise ValueError(msg)
        return self


#: GOWA's webhook secret defaults to the literal ``secret``; the floor rules that out, and
#: anything else an attacker could guess from the gateway's docs.
MIN_GOWA_WEBHOOK_SECRET_LENGTH: Final = 32

#: Railway's private network. Traffic on it never leaves the project, so plain http to a
#: host here is not the exposure it would be on the internet.
PRIVATE_NETWORK_SUFFIX: Final = ".railway.internal"


class GowaSettings(BaseModel):
    """The GOWA gateway (go-whatsapp-web-multidevice): WhatsApp through a linked device.

    An unofficial alternative to Meta's Cloud API, linked by QR code to a dedicated SIM;
    docs/adr/0004 has why, and what it risks. Off by default and all-or-nothing when on,
    like ``WhatsAppSettings`` -- and independent of it: both may be on at once, each serving
    its own number, and a reply goes out on whichever channel the message came in on.
    """

    enabled: bool = False
    #: Where the worker reaches the gateway's REST API -- on Railway, its private hostname.
    base_url: str = "http://localhost:3000"
    #: The gateway's ``APP_BASIC_AUTH``. Required: its API can send as the linked number.
    basic_auth_user: str | None = None
    basic_auth_password: SecretStr | None = None
    #: HMAC key for the webhook's ``X-Hub-Signature-256`` -- the gateway's
    #: ``WHATSAPP_WEBHOOK_SECRET``. api only.
    webhook_secret: SecretStr | None = None
    #: Sent as ``X-Device-Id`` and matched against the webhook's ``session_id``. Only needed
    #: when the gateway holds more than one device; with one, it is the default.
    device_id: str | None = None
    send_timeout_s: float = 10.0
    #: A typing indicator at least this long precedes every reply. A linked device that
    #: answers in milliseconds, every time, looks like what it is; a ban costs the number.
    typing_delay_s: float = Field(default=3.0, ge=0.0, le=10.0)
    #: A random extra of up to this long on each pause: a fixed one is a fingerprint too.
    typing_jitter_s: float = Field(default=2.0, ge=0.0, le=5.0)

    @field_validator("base_url")
    @classmethod
    def _http_origin(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            msg = "GOWA__BASE_URL must be an http(s) URL such as http://gowa.railway.internal:3000"
            raise ValueError(msg)
        return value.strip().rstrip("/")

    @property
    def on_private_network(self) -> bool:
        host = urlsplit(self.base_url).hostname or ""
        return host.endswith(PRIVATE_NETWORK_SUFFIX)

    @model_validator(mode="after")
    def _all_or_nothing(self) -> GowaSettings:
        problems: list[str] = []
        if self.enabled:
            required = {
                "GOWA__BASIC_AUTH_USER": self.basic_auth_user,
                "GOWA__BASIC_AUTH_PASSWORD": self.basic_auth_password,
                "GOWA__WEBHOOK_SECRET": self.webhook_secret,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                problems.append(f"{', '.join(missing)} required when GOWA__ENABLED is true")
        secret = self.webhook_secret.get_secret_value() if self.webhook_secret else None
        if secret is not None and len(secret) < MIN_GOWA_WEBHOOK_SECRET_LENGTH:
            problems.append(
                f"GOWA__WEBHOOK_SECRET must be at least {MIN_GOWA_WEBHOOK_SECRET_LENGTH} "
                "characters (the gateway's default, 'secret', is not one)"
            )
        if problems:
            msg = "Invalid GOWA settings: " + "; ".join(problems)
            raise ValueError(msg)
        return self


#: Composio's id prefix for an auth config. A connected account id (``ca_``) or a toolkit
#: slug pasted in its place would otherwise surface as a failure at the first onboarding.
AUTH_CONFIG_PREFIX: Final = "ac_"


class ComposioSettings(BaseModel):
    """Composio, which holds users' Google tokens so that we never do (v7 Section 3).

    The tokens are issued to *our own* Google OAuth app: published "In production" and
    unverified, which shows users Google's warning screen and caps the app at 100 users,
    plugged into a Composio custom auth config. There is no cutover to come;
    docs/runbook-iteration-03.md sets both up.

    Off by default and all-or-nothing when on, like WhatsApp. Enabled, the api serves the
    connect pages and Composio's callback, so it also needs ``APP__PUBLIC_BASE_URL`` and
    ``ONBOARDING__LINK_SECRET`` -- which live in other sections, so ``Settings`` checks all
    four together and names every missing one at once.
    """

    enabled: bool = False
    api_key: SecretStr | None = None
    #: The auth config new connections are made under: our custom Google Calendar config,
    #: one per Composio project. Every connection row records the config that made it, so
    #: changing this never breaks an existing connection.
    calendar_auth_config_id: str | None = None
    request_timeout_s: float = 15.0

    @field_validator("calendar_auth_config_id")
    @classmethod
    def _looks_like_an_auth_config(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(AUTH_CONFIG_PREFIX):
            msg = (
                "COMPOSIO__CALENDAR_AUTH_CONFIG_ID must be an auth config id "
                f"({AUTH_CONFIG_PREFIX}...)"
            )
            raise ValueError(msg)
        return value


#: Links are signed with HMAC; a short secret can be brute-forced offline from one link.
MIN_LINK_SECRET_LENGTH: Final = 32


class OnboardingSettings(BaseModel):
    """Signed links that take a user from WhatsApp to one of our pages and back."""

    #: Signs onboarding links and the state Composio hands back to our callback. Rotating it
    #: invalidates every outstanding link, which costs a user one fresh link; nothing else.
    link_secret: SecretStr | None = None
    #: Long enough to switch apps and sign in to Google; short enough that a forwarded
    #: screenshot of the link has gone stale.
    link_ttl_s: int = Field(default=900, ge=60, le=3600)
    #: The number invitees write to, E.164 -- today the GOWA SIM. Only ``po-admin invite``
    #: reads it, to print a ``wa.me`` link; unset, the invite is still recorded.
    bot_phone: str | None = None

    @field_validator("bot_phone")
    @classmethod
    def _bot_phone_is_e164(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        phone = normalise_e164(value)
        if phone is None:
            # Not echoed: hide_input_in_errors covers pydantic's part, and this is ours.
            msg = "ONBOARDING__BOT_PHONE is not an E.164 number"
            raise ValueError(msg)
        return phone

    @model_validator(mode="after")
    def _long_enough(self) -> OnboardingSettings:
        secret = self.link_secret.get_secret_value() if self.link_secret else None
        if secret is not None and len(secret) < MIN_LINK_SECRET_LENGTH:
            msg = f"ONBOARDING__LINK_SECRET must be at least {MIN_LINK_SECRET_LENGTH} characters"
            raise ValueError(msg)
        return self


class AgentSettings(BaseModel):
    """The LLM (docs/plan-iteration-04.md D1): onboarding talks through it.

    Off by default. On, it needs its key and Composio, because the conversation it holds is the
    one that ends in the Google connect; ``Settings`` checks both together. Only the worker
    calls the model, so only the worker sets these.
    """

    enabled: bool = False
    api_key: SecretStr | None = None
    #: Per model call, with the SDK's own retries off: ``INBOUND_RETRY`` is the one retry layer,
    #: and a slow call holds the sender's lock.
    request_timeout_s: float = 20.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
        # A ValidationError otherwise repeats the offending input -- here DSNs, tokens and the
        # WhatsApp allowlist's phone numbers -- and a boot failure is printed to the deploy log.
        # The messages name the variable, which is all an operator needs.
        hide_input_in_errors=True,
    )

    app: AppSettings = AppSettings()
    database: DatabaseSettings
    logging: LoggingSettings = LoggingSettings()
    sentry: SentrySettings = SentrySettings()
    langfuse: LangfuseSettings = LangfuseSettings()
    models: ModelSettings
    worker: WorkerSettings = WorkerSettings()
    whatsapp: WhatsAppSettings = WhatsAppSettings()
    gowa: GowaSettings = GowaSettings()
    composio: ComposioSettings = ComposioSettings()
    onboarding: OnboardingSettings = OnboardingSettings()
    agent: AgentSettings = AgentSettings()
    flags: dict[str, bool] = Field(default_factory=dict)

    @property
    def is_deployed(self) -> bool:
        return self.app.env in ("staging", "production")

    def dsn_for(self, role: DatabaseRole) -> str:
        """Return the DSN for ``role``, or raise if it was never configured.

        ``DEFINER`` is refused outright rather than looked up: ``app_definer`` is ``NOLOGIN``,
        so no DSN for it can work, and a ``DATABASE__DEFINER_URL`` someone adds would be a
        BYPASSRLS login waiting to be enabled.
        """
        if role is DatabaseRole.DEFINER:
            raise MissingDatabaseRoleError(
                role.value,
                reason=f"{DEFINER_ROLE_NAME} is NOLOGIN; it owns functions, nothing connects",
            )
        value: SecretStr | None = getattr(self.database, f"{role.value}_url", None)
        if value is None:
            raise MissingDatabaseRoleError(role.value)
        return value.get_secret_value()

    @model_validator(mode="after")
    def _composio_is_complete(self) -> Settings:
        if not self.composio.enabled:
            return self
        required = {
            "COMPOSIO__API_KEY": self.composio.api_key,
            "COMPOSIO__CALENDAR_AUTH_CONFIG_ID": self.composio.calendar_auth_config_id,
            "APP__PUBLIC_BASE_URL": self.app.public_base_url,
            "ONBOARDING__LINK_SECRET": self.onboarding.link_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            msg = (
                f"Invalid Composio settings: {', '.join(missing)} required when "
                "COMPOSIO__ENABLED is true"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _agent_is_complete(self) -> Settings:
        if not self.agent.enabled:
            return self
        missing = [
            name
            for name, ok in {
                "COMPOSIO__ENABLED": self.composio.enabled,
                "AGENT__API_KEY": self.agent.api_key is not None,
            }.items()
            if not ok
        ]
        if missing:
            msg = (
                f"Invalid agent settings: {', '.join(missing)} required when AGENT__ENABLED is true"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enforce_deployed_invariants(self) -> Settings:
        if not self.is_deployed:
            if _on_platform():
                # Every check below is gated on APP__ENV, which defaults to "local" -- so
                # forgetting it does not mislabel one environment, it silently switches off
                # all of them at once. Observed in production on 2026-09-26: the pepper was
                # allowed to stay at its public placeholder, SENTRY__DSN and
                # DATABASE__OWNER_URL stopped being required (the missing owner URL then
                # surfaced as an obscure failure inside the pre-deploy command instead of a
                # named one at startup), and /internal mounted itself on a public URL with
                # its token check disabled, because that check also defers to is_deployed.
                msg = (
                    f"APP__ENV is {self.app.env!r} on a container the platform is running. "
                    "Set it to 'staging' or 'production': every deployed-environment check "
                    "is gated on it, so an unset value disables all of them together."
                )
                raise ValueError(msg)
            return self
        problems: list[str] = []
        if self.logging.allow_raw_pii:
            problems.append("LOGGING__ALLOW_RAW_PII must be false outside local/ci")
        if self.logging.renderer != "json":
            problems.append("LOGGING__RENDERER must be 'json' when deployed")
        if self.sentry.dsn is None:
            problems.append("SENTRY__DSN is required when deployed")
        if self.app.debug:
            problems.append("APP__DEBUG must be false when deployed")
        if self.logging.pii_pepper.get_secret_value() == _DEV_PEPPER:
            problems.append("LOGGING__PII_PEPPER must be set to a real secret when deployed")
        if self.database.owner_url is None:
            problems.append("DATABASE__OWNER_URL is required when deployed (Alembic)")
        # The /internal router is mounted whenever env != "production", so staging serves it on a
        # public URL. Refusing to boot without a token is what keeps it from being open.
        if self.app.env == "staging" and self.app.internal_token is None:
            problems.append("APP__INTERNAL_TOKEN is required in staging (/internal is mounted)")
        # The access token is sent as a bearer header to this URL on every reply.
        if self.whatsapp.enabled and not self.whatsapp.graph_base_url.startswith("https://"):
            problems.append("WHATSAPP__GRAPH_BASE_URL must be https:// when deployed")
        # Basic-auth credentials ride on every send, so they go over TLS or stay inside
        # Railway's private network -- never plain http across the internet.
        gowa = self.gowa
        if gowa.enabled and not (gowa.base_url.startswith("https://") or gowa.on_private_network):
            problems.append(
                f"GOWA__BASE_URL must be https:// or a *{PRIVATE_NETWORK_SUFFIX} host when deployed"
            )
        # Onboarding links carry a bearer token in their path, and Google will not complete
        # an OAuth flow that returns to a plain-http page.
        base_url = self.app.public_base_url
        if base_url is not None and not base_url.startswith("https://"):
            problems.append("APP__PUBLIC_BASE_URL must be https:// when deployed")
        link_secret = self.onboarding.link_secret
        if link_secret is not None and link_secret.get_secret_value() == _DEV_LINK_SECRET:
            problems.append("ONBOARDING__LINK_SECRET must be set to a real secret when deployed")
        if problems:
            msg = "Invalid settings for a deployed environment: " + "; ".join(problems)
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment


__all__ = [
    "AgentSettings",
    "AppSettings",
    "ComposioSettings",
    "DatabaseSettings",
    "GowaSettings",
    "LangfuseSettings",
    "LoggingSettings",
    "ModelSettings",
    "OnboardingSettings",
    "SentrySettings",
    "Settings",
    "WhatsAppSettings",
    "WorkerSettings",
    "get_settings",
]
