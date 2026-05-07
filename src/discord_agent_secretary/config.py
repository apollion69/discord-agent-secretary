"""Runtime configuration loaded from environment / .env file.

All secrets and operational knobs live here. Import `get_settings()` — it's
memoized, so importing modules share one instance.

Backend selection: set `BACKEND=multica|github|linear|jira` (default `multica`).
Each backend reads its own block of variables — unused blocks may stay empty.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource


class _CsvFriendlyEnvSource(EnvSettingsSource):
    """EnvSettingsSource that skips JSON pre-decode for CSV-shaped env vars.

    `pydantic-settings` 2.1 eagerly `json.loads()` any env value whose target
    field is "complex" (list/dict/etc). Our `discord_watch_channels` is a CSV,
    which would trip that. Passing the raw string through lets the
    `@field_validator(mode="before")` split it.
    """

    _CSV_FIELDS = {"discord_watch_channels"}

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if field_name in self._CSV_FIELDS:
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


BackendName = Literal["multica", "github", "linear", "jira"]


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
            dotenv_settings,
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

    # === Multica backend ===
    multica_workspace_id: str = Field(
        default="",
        description="Target Multica workspace UUID (required when BACKEND=multica)",
    )
    multica_default_assignee: str = Field(default="", description="Default assignee ID/name")
    multica_cli_path: str = Field(default="", description="Absolute path to multica CLI; empty = autodetect")
    multica_cli_timeout: float = Field(default=8.0, ge=0.5, le=60.0)

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

    # === Anthropic (optional LLM fallback for parser when regex confidence < 0.9) ===
    anthropic_api_key: str = Field(default="", description="Anthropic API key for LLM fallback")
    anthropic_model: str = Field(default="claude-haiku-4-5-20251001")

    # === Runtime ===
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    tz: str = Field(default="Europe/Moscow", description="IANA timezone for deadline parsing")

    @field_validator("discord_watch_channels", mode="before")
    @classmethod
    def _split_channel_list(cls, v: object) -> object:
        """Accept int, str, or list — coerce to list[int].

        pydantic-settings can hand this field a bare `int` when the env value
        parses as a JSON number. A CSV string like `"123,456"` comes in as
        `str`. A pre-parsed list passes through untouched.
        """
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
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
        import uuid

        try:
            uuid.UUID(v)
        except ValueError as e:
            raise ValueError(
                f"multica_workspace_id must be a valid UUID, got: {v!r}"
            ) from e
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the memoized `Settings` instance.

    Tests clear the cache with `get_settings.cache_clear()` before rebuilding
    with monkeypatched env vars.
    """
    return Settings()
