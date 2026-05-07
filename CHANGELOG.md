# Changelog

All notable changes to `discord-agent-secretary` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-05-08

### Added

- **Backend resilience helpers** in `backends/base.py`:
  `with_retry()` (bounded exponential-backoff retry, applied only to
  idempotent methods like `get_issue`, `update_status`, `assign_issue`)
  and `CircuitBreaker` (closed/open/half-open with configurable
  threshold + cool-down). `MulticaBackend` opts in to both. The
  breaker fast-fails with a new `CircuitOpenError` so handlers send a
  dedicated ephemeral message rather than the generic timeout reply.
- **HTTP healthcheck server** (`health.start_healthcheck`) — stdlib
  `http.server` in a daemon thread, exposing `/livez` (always 200 if
  the process responds) and `/readyz` (200 only when
  `discord.Client.is_ready()`). Disabled by default
  (`HEALTHCHECK_PORT=0`); set the env var to a TCP port to enable.
- **Output-size cap** for the Multica CLI (`MULTICA_CLI_OUTPUT_BYTE_LIMIT`,
  default 10 MiB). A misbehaving CLI that pours gigabytes is killed
  before consuming process memory.
- `Operating System :: MacOS :: MacOS X` classifier — code already
  worked on macOS, the metadata now reflects it.
- `SECURITY.md` describing the private vulnerability reporting channel.
- `RateLimiter` (token bucket, 5-burst / 1 token per 2 s, keyed by
  `(guild_id, user_id)`) protects the backend from a single member
  spamming slash commands. State is in-process; multi-replica
  deployments need a shared store.
- `SecretRedactingFilter` masks every secret-shaped setting
  (`DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`,
  `JIRA_API_TOKEN`, `ANTHROPIC_API_KEY`) wherever it appears in a log
  message or string-typed `extra` field.
- `Settings.tz` is validated against `zoneinfo` at boot — typos like
  `Mars/Olympus_Mons` fail fast instead of mid-deadline-parse.
- Slash command parameters now declare `app_commands.Range` length
  bounds (`title` 1–300, `description` 1–2000, `assignee`/`issue_id`/
  `to` 1–200) so Discord rejects pathological inputs before they reach
  the backend.
- Parser accepts `tz="Europe/Moscow"`-style argument so production
  callers can pin "today" to the configured zone instead of system
  local time.
- Parser rolls bare `DD.MM` dates forward to the next year when the
  current-year candidate is already past — the common-sense default
  for "до 5.04" written in November.
- `Programming Language :: Python :: 3.13` classifier.
- `tests/unit/test_main.py` covers `_collect_secrets` and the four
  exit-code paths (missing token, `LoginFailure`, `KeyboardInterrupt`,
  clean shutdown).

### Changed

- `main()` now returns a non-zero exit code when `on_ready` aborts on
  unsafe permissions or on an unresolved bot membership — orchestrators
  see the failure instead of a silent restart.
- `tree.sync()` runs once per process via a `synced` guard, no longer
  burning Discord's global-sync quota on every gateway reconnect.
- `main()` installs `SIGTERM` / `SIGINT` handlers around `client.start()`
  so containers / systemd get a graceful gateway close instead of a
  `SIGKILL` that leaves the bot online for ~30 minutes.
- `MulticaBackend._invoke` awaits `proc.wait()` (bounded at 2 s) after
  `kill()` on timeout, reaping the child instead of leaking zombies.
- `MulticaBackend._invoke` distinguishes `proc.returncode is None`
  from a real exit, surfacing it as a typed CLI error instead of
  silently OK'ing.
- `_strip_preamble` caches `splitlines()` output (one pass, not two).
- `discord_client.REFUSE_PERMS` extends beyond `ADMINISTRATOR` /
  `MANAGE_GUILD` to also block `MANAGE_ROLES`, `MANAGE_CHANNELS`,
  `MANAGE_WEBHOOKS`, `BAN_MEMBERS`, `KICK_MEMBERS`, `MENTION_EVERYONE`.
- Stub backends (`GitHubBackend` / `LinearBackend` / `JiraBackend`)
  raise `NotImplementedError` from their `__init__` — a misconfigured
  `BACKEND=github` deployment now fails at boot, not on the first
  user-visible `/task`.
- `make_backend` now dispatches via a `_BACKEND_BUILDERS` dict instead
  of an if/elif chain — the supported-backends list in error messages
  derives from the registry.
- `_safe_invoke` routes every error reply through ephemeral followups
  so backend health signals don't leak into the public channel.
- `_safe_followup` wraps `interaction.followup.send` with an HTTP-error
  guard so a flaky Discord followup doesn't crash the handler
  mid-error-path.
- All handler logs now include `interaction_id` / `user_id` /
  `guild_id` for ops correlation.
- All runtime dependencies grew upper bounds (`<3` for major-versioned
  libs, `<26`, `<1`, `<7`) so a major upstream release can't silently
  break the bot — `pydantic-settings` in particular drives a private
  API used by `_CsvFriendlyEnvSource`.
- `.env.example` and the README converged on `DISCORD_BOT_TOKEN`
  (the README previously said `DISCORD_TOKEN`).
- `docs/threat-model.md` reflects the expanded `REFUSE_PERMS`, the
  rate limiter, the redactor, the bot-membership refusal, and the
  correct `MULTICA_CLI_TIMEOUT` default (8 s, not 30 s).
- `CONTRIBUTING.md` drops the bogus `alias=` example and clarifies the
  stub-replacement step.

### Removed

- The "Stubs raise on first call rather than at construction" path —
  see *Changed* above. Stubs now refuse to construct.

### Security

- See *Added* (`RateLimiter`, `SecretRedactingFilter`, `tz` validator)
  and *Changed* (`REFUSE_PERMS`, ephemeral error replies, exit codes,
  `SIGTERM` handling, bot-membership refusal).

### Pending — needs maintainer action

- CI matrix bump to include Python 3.13. The push token used by the
  review job lacks GitHub's `workflow` scope, so `.github/workflows/ci.yml`
  could not be modified. One-line change: add `"3.13"` to
  `matrix.python-version`.

## [0.1.0] — initial release

- First public OSS scaffolding (README, LICENSE, CI, docs, code of
  conduct).
- Multica reference backend; GitHub / Linear / Jira documented stubs.
