# venchur-secretary

Discord bot that bridges slash commands (`/task`, `/status`, `/assign`) and
channel conversations to Multica issues.

## Status

P1-P4 landed: skeleton/config/logs, Multica CLI wrapper, regex parser, Discord
slash-command handlers (`/task`, `/status`, `/assign`). 76 tests passing.
P5 (secretary mode — passive channel observation with LLM task proposals) is
the next phase.

## Running the bot

```bash
# one-time setup
uv venv .venv
source .venv/bin/activate
uv pip install -e '.[dev]'

# run (needs .env populated — see `.env.example`)
python -m bot.main
```

Guild-scoped slash commands sync on `on_ready`. Global commands take up to an
hour to propagate; guild-scoped are instant.

## Inviting the bot to another server

Application is set to `bot_public: false` (private), so Discord no longer
generates the install URL automatically. To add the bot to a new server, open
this manually-constructed OAuth2 link in a browser where you're a guild admin
of the target server:

```
https://discord.com/oauth2/authorize
  ?client_id=<APPLICATION_ID>
  &scope=bot%20applications.commands
  &permissions=2147503168
```

The permissions bitmask `2147503168` grants: `VIEW_CHANNEL`, `SEND_MESSAGES`,
`EMBED_LINKS`, `ADD_REACTIONS`, `USE_APPLICATION_COMMANDS`. The fail-safe
permission check refuses to boot if the bot holds `ADMINISTRATOR` or
`MANAGE_GUILD`, so don't elevate beyond that bitmask.

## Layout

```
src/bot/
  config.py          pydantic-settings, loads .env
  logging_setup.py   stdlib logging + JSON formatter, structlog when installed
  multica_client.py  async wrapper around the `multica` CLI
  parsers.py         regex-first RU/EN field extractor for slash commands
  discord_client.py  Discord client factory + fail-safe permission check
  handlers.py        slash-command handlers bridging Discord → MulticaClient
  main.py            entrypoint (loads settings, wires components, runs bot)

tests/
  conftest.py                      shared fixtures (frozen date, mock subprocess)
  fixtures/multilingual_cases.yaml RU/EN golden cases
  unit/                            unit tests (parsers, config, logs,
                                   discord_client, handlers)
  integration/                     integration tests (CLI wrapper, commands)
```

## Running tests

```bash
python3 -m pytest tests/ -v
python3 -m pytest tests/ --cov=src --cov-report=term-missing
```

Full dev deps (for structlog, discord.py, dpytest, freezegun):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

## Design rules

- **Regex-first parser.** Follows `rules/common/llm-text-processing.md` — regex
  pre-extracts structured fields; LLM is only called on the ambiguous residual
  when confidence < 0.9.
- **CLI over HTTP.** All Multica writes go through the `multica` CLI, never
  direct HTTP. The CLI handles auth; we stay thin.
- **No secrets in code.** Everything sensitive lives in `.env`
  (see `.env.example` for the full list).
