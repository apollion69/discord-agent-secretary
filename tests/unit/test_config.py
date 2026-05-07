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

    def test_watch_channels_empty_csv_yields_empty_list(self, monkeypatch, clean_settings):
        monkeypatch.setenv("DISCORD_WATCH_CHANNELS", "")
        s = Settings(_env_file=None)
        assert s.discord_watch_channels == []

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
