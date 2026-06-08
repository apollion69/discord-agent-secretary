"""Runtime configuration loaded from environment / .env file.

All secrets and operational knobs live here. Import `get_settings()` — it's
memoized, so importing modules share one instance.

Backend selection: set `BACKEND=multica|github|linear|jira` (default `multica`).
Each backend reads its own block of variables — unused blocks may stay empty.
"""
from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

_CSV_FIELDS = {
    "discord_watch_channels",
    "multica_automated_reviewers",
    "mention_scan_statuses",
    "discord_thread_ping_user_ids",
    "discord_thread_ping_role_ids",
}

# Discord accepts only these thread auto-archive durations (minutes).
_VALID_AUTO_ARCHIVE: frozenset[int] = frozenset({60, 1440, 4320, 10080})


class _CsvFriendlyEnvSource(EnvSettingsSource):
    """EnvSettingsSource that skips JSON pre-decode for CSV-shaped env vars.

    `pydantic-settings` 2.1 eagerly `json.loads()` any env value whose target
    field is "complex" (list/dict/etc). Our list settings are CSV-shaped,
    which would trip that. Passing the raw string through lets the
    `@field_validator(mode="before")` split it. Dotenv uses the same override
    below because it has the same complex-value pre-decode behavior.
    """

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if field_name in _CSV_FIELDS:
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class _CsvFriendlyDotEnvSource(DotEnvSettingsSource):
    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if field_name in _CSV_FIELDS:
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


BackendName = Literal["multica", "github", "linear", "jira"]
ReviewRoutingMode = Literal["off", "subscribe", "assign"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _CsvFriendlyEnvSource(settings_cls),
            _CsvFriendlyDotEnvSource(
                settings_cls,
                env_file=getattr(dotenv_settings, "env_file", None),
                env_file_encoding=getattr(dotenv_settings, "env_file_encoding", None),
                case_sensitive=getattr(dotenv_settings, "case_sensitive", None),
                env_prefix=getattr(dotenv_settings, "env_prefix", None),
                env_nested_delimiter=getattr(dotenv_settings, "env_nested_delimiter", None),
                env_nested_max_split=getattr(dotenv_settings, "env_nested_max_split", None),
                env_ignore_empty=getattr(dotenv_settings, "env_ignore_empty", None),
                env_parse_none_str=getattr(dotenv_settings, "env_parse_none_str", None),
                env_parse_enums=getattr(dotenv_settings, "env_parse_enums", None),
            ),
            file_secret_settings,
        )

    # === backend selector ===
    backend: BackendName = Field(default="multica", description="Issue tracker to use")

    # === Discord ===
    discord_bot_token: str = Field(default="", description="Discord bot token from Dev Portal")
    discord_guild_id: int | None = Field(default=None, description="Test/prod guild ID")
    discord_watch_channels: list[int] = Field(
        default_factory=list,
        description="Channel IDs the secretary observes for implicit task extraction",
    )

    # === Venture thread-per-task ===
    discord_thread_enabled: bool = Field(
        default=False,
        description=(
            "Open a Discord thread per /task and ping participants inside it, "
            "keeping the main channel uncluttered. Opt-in; default off preserves "
            "existing behaviour."
        ),
    )
    discord_thread_private: bool = Field(
        default=False,
        description=(
            "Create private threads (members added by mention) instead of public "
            "threads attached to the announcement message."
        ),
    )
    discord_thread_auto_archive_minutes: int = Field(
        default=4320,
        description="Thread auto-archive duration; Discord allows only 60, 1440, 4320, 10080.",
    )
    discord_thread_name_max_words: int = Field(
        default=6,
        ge=0,
        le=50,
        description=(
            "Cap the title portion of a thread name to the first N words (0 = no "
            "word cap); the full name is always hard-capped at 100 chars."
        ),
    )
    discord_thread_ping_user_ids: list[int] = Field(
        default_factory=list,
        description="CSV of Discord user IDs always pinged inside a new task thread.",
    )
    discord_thread_ping_role_ids: list[int] = Field(
        default_factory=list,
        description="CSV of Discord role IDs always pinged inside a new task thread.",
    )

    # === Multica backend ===
    multica_workspace_id: str = Field(
        default="",
        description="Target Multica workspace UUID (required when BACKEND=multica)",
    )
    multica_default_assignee: str = Field(default="", description="Default assignee ID/name")
    discord_member_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Maps a Discord user ID (string) to a Multica member user UUID so /task "
            "is attributed to the real requester via act-as-member. JSON object, e.g. "
            '{"123456789":"<uuid>"}. Unmapped users fall back to the bot token owner.'
        ),
    )
    multica_cli_path: str = Field(default="", description="Absolute path to multica CLI; empty = autodetect")
    multica_cli_timeout: float = Field(default=8.0, ge=0.5, le=60.0)
    multica_cli_output_byte_limit: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=512 * 1024 * 1024,
        description="Per-call cap on combined stdout/stderr bytes; over the limit the CLI is killed.",
    )

    # === Backend resilience knobs (apply to every backend that opts in) ===
    backend_circuit_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Consecutive failures before the per-backend circuit breaker opens.",
    )
    backend_circuit_reset_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
        description="Cool-down (seconds) before the open circuit moves to half-open.",
    )

    # === Healthcheck ===
    healthcheck_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="TCP port for the /livez and /readyz HTTP endpoints; 0 disables.",
    )

    # === Webhook ===
    multica_webhook_secret: str = Field(
        default="",
        description="HMAC-SHA256 secret for Multica webhook signatures (X-Multica-Signature header); empty = no verification.",
    )
    webhook_rate_limit: int = Field(
        default=30,
        ge=0,
        description="Max webhook POSTs per client IP per 10s window; 0 disables rate limiting.",
    )
    discord_review_channel_id: int | None = Field(
        default=None,
        description="Discord channel ID for 'Готово к ревью' notifications.",
    )

    # === Pull poller ===
    multica_poll_interval: float = Field(
        default=30.0,
        ge=5.0,
        le=3600.0,
        description="Seconds between in_review poll cycles (default 30).",
    )
    multica_seen_path: str = Field(
        default="/opt/discord-secretary/seen.json",
        description="Path to the dedup state file for the pull poller.",
    )
    multica_app_url: str = Field(
        default="",
        description="Base URL of the Multica web UI for issue links in Discord messages (e.g. http://ansible-lx1.mgmt.local:3000).",
    )

    # === Autopilot digest ===
    digest_enabled: bool = Field(
        default=True,
        description="Post a once-daily summary of autopilot (cron) issues instead of per-task review pings.",
    )
    digest_hour: int = Field(
        default=9,
        ge=0,
        le=23,
        description="Local hour (in TZ) at which the daily autopilot digest is posted.",
    )
    digest_state_path: str = Field(
        default="/opt/discord-secretary/digest_state.json",
        description="Path to the dedup state file recording the last digest date.",
    )

    # === Automated review routing ===
    multica_automated_reviewers: list[str] = Field(
        default_factory=list,
        description="CSV list of reviewer actor refs/names for automated review routing; issues are assigned round-robin across them.",
    )
    multica_review_routing_mode: ReviewRoutingMode = Field(
        default="off",
        description="Automated review routing mode: off, subscribe, or assign.",
    )
    multica_review_dry_run: bool = Field(
        default=True,
        description="When true, report reviewer routing actions without mutating Multica.",
    )
    multica_rework_status: str = Field(
        default="todo",
        description="Existing Multica status used when a reviewer requests rework.",
    )
    multica_review_state_path: str = Field(
        default="/opt/discord-secretary/review-routing.json",
        description="Idempotent state file for automated review routing and verdicts.",
    )

    # === Mention notifications ===
    mention_scan_enabled: bool = Field(
        default=True,
        description="Scan Multica comments for member @mentions and ping the mapped Discord user.",
    )
    mention_scan_statuses: list[str] = Field(
        default_factory=lambda: ["todo", "in_progress", "in_review", "blocked"],
        description="CSV of issue statuses whose comments are scanned for @mentions.",
    )
    mention_scan_state_path: str = Field(
        default="/opt/discord-secretary/mention-seen.json",
        description="Dedup state for mention notifications (seen comment ids + issue updated_at).",
    )
    mention_member_map_ttl: float = Field(
        default=300.0,
        description="Seconds to cache the workspace member→Discord map before re-fetching (members change rarely).",
    )


    # === GitHub backend ===
    github_token: str = Field(default="", description="GitHub PAT or App-installation token")
    github_repo: str = Field(default="", description="owner/repo target")

    # === Linear backend ===
    linear_api_key: str = Field(default="", description="Linear personal API key")
    linear_team_id: str = Field(default="", description="Linear team UUID")

    # === Jira backend ===
    jira_base_url: str = Field(default="", description="https://{site}.atlassian.net or on-prem URL")
    jira_email: str = Field(default="", description="Jira Cloud account email")
    jira_api_token: str = Field(default="", description="Jira API token / PAT")
    jira_project_key: str = Field(default="", description="Jira project key, e.g. ABC")

    # === Runtime ===
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    tz: str = Field(default="Europe/Moscow", description="IANA timezone for deadline parsing")

    @field_validator("discord_member_map")
    @classmethod
    def _validate_member_map(cls, v: dict[str, str]) -> dict[str, str]:
        """Fail fast at startup if any mapped value is not a valid UUID.

        Without this, a typo'd member UUID is silently rejected by the server
        and the issue is attributed to the token owner with no observable error
        (only a per-call warning) — exactly the bug this feature fixes.
        """
        for discord_id, member_uuid in v.items():
            try:
                uuid.UUID(member_uuid)
            except (ValueError, AttributeError, TypeError) as e:
                raise ValueError(
                    f"discord_member_map[{discord_id!r}] is not a valid UUID: {member_uuid!r}"
                ) from e
        return v

    @field_validator(
        "discord_watch_channels",
        "multica_automated_reviewers",
        "mention_scan_statuses",
        "discord_thread_ping_user_ids",
        "discord_thread_ping_role_ids",
        mode="before",
    )
    @classmethod
    def _split_csv_list(cls, v: object) -> object:
        """Accept int, str, or list — coerce comma-separated env strings.

        pydantic-settings can hand this field a bare `int` when the env value
        parses as a JSON number. A CSV string like `"123,456"` comes in as
        `str`. A pre-parsed list passes through untouched.
        """
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            if not v.strip():
                return []
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("discord_watch_channels")
    @classmethod
    def _coerce_channel_ids(cls, v: list[object]) -> list[int]:
        return [int(str(x)) for x in v]

    @field_validator("discord_thread_ping_user_ids", "discord_thread_ping_role_ids")
    @classmethod
    def _coerce_thread_ping_ids(cls, v: list[object]) -> list[int]:
        return [int(str(x)) for x in v]

    @field_validator("discord_thread_auto_archive_minutes")
    @classmethod
    def _validate_auto_archive(cls, v: int) -> int:
        """Reject any duration Discord won't accept (else create_thread 400s)."""
        if v not in _VALID_AUTO_ARCHIVE:
            raise ValueError(
                "discord_thread_auto_archive_minutes must be one of "
                f"{sorted(_VALID_AUTO_ARCHIVE)}, got {v}"
            )
        return v

    @field_validator("multica_workspace_id")
    @classmethod
    def _validate_workspace_uuid(cls, v: str) -> str:
        """Validate UUID shape only when value is non-empty.

        Empty is allowed because the field may be unused (e.g. BACKEND=github).
        The backend factory raises a clear error if it's needed and missing.
        """
        if not v:
            return v
        try:
            uuid.UUID(v)
        except ValueError as e:
            raise ValueError(
                f"multica_workspace_id must be a valid UUID, got: {v!r}"
            ) from e
        return v

    @field_validator("tz")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        """Reject timezone names ZoneInfo can't resolve.

        Catches typos at boot rather than at the first deadline parse.
        """
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"tz {v!r} is not a known IANA zone") from e
        return v

    @model_validator(mode="after")
    def _validate_review_routing_signature_secret(self) -> Settings:
        if (
            self.discord_review_channel_id is not None
            and self.multica_review_routing_mode != "off"
            and not self.multica_review_dry_run
            and not self.multica_webhook_secret.strip()
        ):
            raise ValueError(
                "MULTICA_WEBHOOK_SECRET is required when automated review routing "
                "can mutate Multica from review webhooks"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the memoized `Settings` instance.

    Tests clear the cache with `get_settings.cache_clear()` before rebuilding
    with monkeypatched env vars.
    """
    return Settings()
