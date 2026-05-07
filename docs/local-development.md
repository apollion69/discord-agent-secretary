# Local development

The project is structured so the test suite never touches Discord's API
or any tracker — every external boundary is mocked. The notes below cover
running tests, smoke-testing without the Multica CLI installed, and
poking at the bot interactively.

## Setup

```bash
uv venv .venv --python python3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Running the test suite

```bash
pytest tests/ -v
ruff check src tests
mypy src
```

Coverage gate is `>= 80 %`; CI fails the build below that. Re-check
locally with:

```bash
pytest tests/ --cov=src/discord_agent_secretary --cov-report=term-missing --cov-fail-under=80
```

## Smoke-testing without the Multica CLI

The bot loads the Multica backend by default (`BACKEND=multica`), which
requires the `multica` binary on `$PATH`. To exercise the slash-command
plumbing on a developer machine that doesn't have Multica installed:

1. Set `BACKEND=multica` and `MULTICA_CLI_PATH=/path/to/fake-multica`,
   where `fake-multica` is a stub script:

   ```bash
   #!/usr/bin/env bash
   # /usr/local/bin/fake-multica
   echo '{"id":"DEV-1","status":"todo","title":"hello"}'
   ```

   `chmod +x` and point `MULTICA_CLI_PATH` at it.

2. Or fork the `MulticaBackend` in `src/discord_agent_secretary/backends/`
   to use HTTP fixtures, returning canned `IssueRef` instances.

The unit test suite in `tests/unit/` and the integration suite in
`tests/integration/test_multica.py` already do this through the
`_FakeProc` helper — read those for canonical patterns.

## Hooking up Discord

For interactive testing you need a real bot user, but you do NOT need a
real tracker:

1. Create a Discord application + bot, copy the token to `.env` as
   `DISCORD_BOT_TOKEN`.
2. Set `DISCORD_GUILD_ID` to a personal test server (instant slash
   command sync; otherwise global registration takes up to an hour).
3. Run with the fake Multica stub described above.

The bot refuses to boot if it has any of the dangerous permissions
listed in `docs/threat-model.md` — when inviting it, grant only the
`Send Messages` + `Use Application Commands` permissions.

## Common pitfalls

- **`get_settings()` is memoized.** Tests must call
  `get_settings.cache_clear()` (the `clean_settings` fixture does this)
  between cases that monkey-patch env variables.
- **`_CsvFriendlyEnvSource` is sensitive to `pydantic-settings`
  internals.** If you bump that dependency past the upper bound in
  `pyproject.toml`, run the full test suite — the unit test
  `test_watch_channels_parsed_from_csv` is the canary.
- **Async test mode is `auto`.** Don't decorate async tests with
  `@pytest.mark.asyncio` — pytest-asyncio's auto mode picks them up.
- **`tree.sync()` rate limits.** During development, set
  `DISCORD_GUILD_ID` so syncs stay guild-scoped (no global quota).

## Debugging tips

- Set `LOG_FORMAT=console` in `.env` for human-readable output.
- Set `LOG_LEVEL=DEBUG` to see structlog's full event payloads.
- The `SecretRedactingFilter` masks any of the configured secret
  values in log output — if you need to see a token in a debug session,
  read it from `settings.discord_bot_token` directly, not via the
  logger.
