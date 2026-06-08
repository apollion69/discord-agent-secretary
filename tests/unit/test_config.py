"""Unit tests for discord_agent_secretary.config."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from discord_agent_secretary.config import Settings, get_settings


class TestSettingsDefaults:
    def test_defaults_load_without_env(self, clean_settings):
        s = Settings(_env_file=None)
        assert s.backend == "multica"
        assert s.log_level == "INFO"
        assert s.log_format == "json"
        assert s.tz == "Europe/Moscow"
        assert s.multica_cli_timeout == 8.0
        assert s.discord_watch_channels == []

    def test_default_workspace_is_empty(self, clean_settings):
        s = Settings(_env_file=None)
        # Default is empty: backend factory raises a clear error if needed and missing.
        assert s.multica_workspace_id == ""

    def test_backend_aliases_accepted(self, monkeypatch, clean_settings):
        for name in ("multica", "github", "linear", "jira"):
            monkeypatch.setenv("BACKEND", name)
            get_settings.cache_clear()
            s = Settings(_env_file=None)
            assert s.backend == name


class TestSettingsFromEnv:
    def test_env_overrides_defaults(self, monkeypatch, clean_settings):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("LOG_FORMAT", "console")
        monkeypatch.setenv("MULTICA_CLI_TIMEOUT", "15.5")
        s = Settings(_env_file=None)
        assert s.log_level == "DEBUG"
        assert s.log_format == "console"
        assert s.multica_cli_timeout == 15.5

    def test_watch_channels_parsed_from_csv(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_WATCH_CHANNELS", "123, 456 , 789")
        s = Settings(_env_file=None)
        assert s.discord_watch_channels == [123, 456, 789]

    def test_thread_ping_user_ids_parsed_from_csv(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_PING_USER_IDS", "111, 222 , 333")
        s = Settings(_env_file=None)
        assert s.discord_thread_ping_user_ids == [111, 222, 333]

    def test_thread_ping_role_ids_parsed_from_csv(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_PING_ROLE_IDS", "999")
        s = Settings(_env_file=None)
        assert s.discord_thread_ping_role_ids == [999]

    def test_watch_channels_empty_csv_yields_empty_list(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_WATCH_CHANNELS", "")
        s = Settings(_env_file=None)
        assert s.discord_watch_channels == []

    def test_automated_reviewers_parsed_from_csv(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_AUTOMATED_REVIEWERS", "alice, checker-agent")
        s = Settings(_env_file=None)
        assert s.multica_automated_reviewers == ["alice", "checker-agent"]

    def test_automated_reviewers_parsed_from_dotenv_csv(self, tmp_path, clean_settings):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "MULTICA_AUTOMATED_REVIEWERS=alice, checker-agent\n",
            encoding="utf-8",
        )

        s = Settings(_env_file=env_file)

        assert s.multica_automated_reviewers == ["alice", "checker-agent"]

    def test_discord_guild_id_coerced_to_int(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_GUILD_ID", "999888777")
        s = Settings(_env_file=None)
        assert s.discord_guild_id == 999888777

    def test_case_insensitive_env_names(self, monkeypatch, clean_settings):
        monkeypatch.setenv("log_level", "WARNING")
        s = Settings(_env_file=None)
        assert s.log_level == "WARNING"


class TestSettingsValidation:
    def test_invalid_log_level_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("LOG_LEVEL", "TRACE")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_invalid_log_format_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("LOG_FORMAT", "xml")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_invalid_backend_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("BACKEND", "asana")  # not in supported set
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_bad_workspace_uuid_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_WORKSPACE_ID", "not-a-uuid")
        with pytest.raises(ValidationError, match="valid UUID"):
            Settings(_env_file=None)

    def test_empty_workspace_uuid_accepted(self, monkeypatch, clean_settings):
        # Empty allowed: field unused when BACKEND != multica.
        monkeypatch.setenv("MULTICA_WORKSPACE_ID", "")
        s = Settings(_env_file=None)
        assert s.multica_workspace_id == ""

    def test_valid_workspace_uuid_accepted(self, monkeypatch, clean_settings):
        monkeypatch.setenv(
            "MULTICA_WORKSPACE_ID", "12345678-1234-1234-1234-123456789012"
        )
        s = Settings(_env_file=None)
        assert s.multica_workspace_id == "12345678-1234-1234-1234-123456789012"

    def test_timeout_out_of_range_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_CLI_TIMEOUT", "0.1")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_timeout_upper_bound_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_CLI_TIMEOUT", "120")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_unknown_tz_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("TZ", "Mars/Olympus_Mons")
        with pytest.raises(ValidationError, match="IANA"):
            Settings(_env_file=None)

    def test_known_tz_accepted(self, monkeypatch, clean_settings):
        monkeypatch.setenv("TZ", "America/New_York")
        s = Settings(_env_file=None)
        assert s.tz == "America/New_York"

    def test_output_byte_limit_default(self, clean_settings):
        s = Settings(_env_file=None)
        assert s.multica_cli_output_byte_limit == 10 * 1024 * 1024

    def test_output_byte_limit_lower_bound(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_CLI_OUTPUT_BYTE_LIMIT", "100")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_circuit_threshold_bounds(self, monkeypatch, clean_settings):
        monkeypatch.setenv("BACKEND_CIRCUIT_FAILURE_THRESHOLD", "0")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_healthcheck_port_bounds(self, monkeypatch, clean_settings):
        monkeypatch.setenv("HEALTHCHECK_PORT", "70000")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_healthcheck_port_default_disabled(self, clean_settings):
        s = Settings(_env_file=None)
        assert s.healthcheck_port == 0

    def test_review_routing_defaults_are_dry_run(self, clean_settings):
        s = Settings(_env_file=None)
        assert s.multica_automated_reviewers == []
        assert s.multica_review_routing_mode == "off"
        assert s.multica_review_dry_run is True
        assert s.multica_rework_status == "todo"
        assert s.multica_review_state_path == "/opt/discord-secretary/review-routing.json"

    def test_invalid_review_routing_mode_rejected(self, monkeypatch, clean_settings):
        monkeypatch.setenv("MULTICA_REVIEW_ROUTING_MODE", "invalid")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_mutating_review_webhook_routing_requires_secret(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_REVIEW_CHANNEL_ID", "123")
        monkeypatch.setenv("MULTICA_REVIEW_ROUTING_MODE", "subscribe")
        monkeypatch.setenv("MULTICA_REVIEW_DRY_RUN", "false")
        with pytest.raises(ValidationError, match="MULTICA_WEBHOOK_SECRET"):
            Settings(_env_file=None)

    def test_dry_run_review_webhook_routing_allows_missing_secret(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_REVIEW_CHANNEL_ID", "123")
        monkeypatch.setenv("MULTICA_REVIEW_ROUTING_MODE", "subscribe")
        monkeypatch.setenv("MULTICA_REVIEW_DRY_RUN", "true")
        s = Settings(_env_file=None)
        assert s.multica_review_dry_run is True


class TestSettingsMemoization:
    def test_get_settings_is_memoized(self, clean_settings):
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_cache_clear_rebuilds_with_new_env(self, monkeypatch, clean_settings):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        s1 = get_settings()
        assert s1.log_level == "DEBUG"

        get_settings.cache_clear()
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        s2 = get_settings()
        assert s2.log_level == "ERROR"
        assert s1 is not s2


class TestMemberMapValidation:
    """DISCORD_MEMBER_MAP UUID values are validated at load (fail-fast)."""

    def test_valid_uuids_accepted(self, clean_settings):
        s = Settings(
            _env_file=None,
            discord_member_map={"219764926061871104": "aebb6b6f-d07d-4ea0-9cfe-3576987ccfbe"},
        )
        assert s.discord_member_map["219764926061871104"].startswith("aebb6b6f")

    def test_invalid_uuid_rejected(self, clean_settings):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, discord_member_map={"123": "not-a-uuid"})

    def test_empty_map_is_default(self, clean_settings):
        assert Settings(_env_file=None).discord_member_map == {}


class TestThreadSettings:
    """Venture thread-per-task settings: defaults, coercion, validation."""

    def test_defaults_are_off_and_safe(self, clean_settings):
        s = Settings(_env_file=None)
        assert s.discord_thread_enabled is False
        assert s.discord_thread_private is False
        assert s.discord_thread_auto_archive_minutes == 4320
        assert s.discord_thread_name_max_words == 6
        assert s.discord_thread_ping_user_ids == []
        assert s.discord_thread_ping_role_ids == []

    def test_enabled_from_env(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_ENABLED", "true")
        monkeypatch.setenv("DISCORD_THREAD_PRIVATE", "true")
        s = Settings(_env_file=None)
        assert s.discord_thread_enabled is True
        assert s.discord_thread_private is True

    def test_auto_archive_accepts_valid_value(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_AUTO_ARCHIVE_MINUTES", "10080")
        s = Settings(_env_file=None)
        assert s.discord_thread_auto_archive_minutes == 10080

    def test_auto_archive_rejects_invalid_value(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_AUTO_ARCHIVE_MINUTES", "30")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)

    def test_name_max_words_zero_allowed(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_NAME_MAX_WORDS", "0")
        s = Settings(_env_file=None)
        assert s.discord_thread_name_max_words == 0

    def test_name_max_words_rejects_negative(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_THREAD_NAME_MAX_WORDS", "-1")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)
