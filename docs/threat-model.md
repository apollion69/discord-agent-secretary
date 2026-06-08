# Threat model

## Scope

`discord-agent-secretary` v0.1 — slash-command mode only. P5 (passive channel
observation) is out of scope and will receive its own threat model in v0.2.

---

## Assets

| Asset | Sensitivity | Notes |
|---|---|---|
| `DISCORD_TOKEN` | Critical | Full bot account access if leaked |
| Backend credentials (API tokens, CLI auth) | High | Issue creation / modification in tracker |
| Issue content entered via slash commands | Medium | Visible to Discord server members already |
| Bot DMs / followup messages | Low | Ephemeral Discord interactions |

---

## Threat actors

| Actor | Goal | Capability |
|---|---|---|
| Malicious server member | Spam issues / exhaust quota | Can invoke slash commands |
| Discord server admin | Elevate bot permissions | Can modify bot role |
| Supply-chain attacker | Inject malicious backend code | Compromise pip dependencies |

---

## Controls

### Permission fail-safe (CRITICAL)
`assert_safe_permissions()` in `discord_client.py` aborts the bot on `on_ready`
if any of these permissions are detected: ADMINISTRATOR, MANAGE_GUILD,
MANAGE_ROLES, MANAGE_CHANNELS, MANAGE_WEBHOOKS, BAN_MEMBERS, KICK_MEMBERS,
MENTION_EVERYONE. If the bot's guild membership cannot be resolved
(`get_member` miss + `fetch_member` failure), startup is also aborted —
better than running with unverified authority. The bot cannot be tricked
into running with elevated permissions; it refuses to start and `main()`
returns a non-zero exit so orchestrators see the failure.

### No shell injection
All subprocess calls use `asyncio.create_subprocess_exec` with an explicit
`argv` list. No string interpolation into shell commands.

### No secret leakage to Discord
`handlers._safe_invoke()` catches all `BackendError` subclasses and sends only
a generic sanitized message to the Discord channel — and that error reply is
ephemeral, visible only to the invoking user. Backend stderr (which may
contain paths, exit codes, or token fragments) is logged server-side only.

### Defense-in-depth: log redactor
`SecretRedactingFilter` in `logging_setup.py` is wired in `main.py` with the
full set of secret-shaped settings (`DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`,
`LINEAR_API_KEY`, `JIRA_API_TOKEN`, `ANTHROPIC_API_KEY`). If any of those
values appears verbatim in a log message or `extra` field, it's replaced
with `***REDACTED***` before the formatter runs. This catches accidental
inclusion through e.g. `logger.exception` rendering of an exception that
embedded a secret.

### Per-user rate limit
`handlers.RateLimiter` (token-bucket, 5-burst / 1 token per 2 s) is keyed by
`(guild_id, user_id)`. A spamming member is told ephemerally to slow down
and never reaches the backend, capping subprocess churn / API quota burn.
State is in-process; multi-replica deployments need a shared store.

### Circuit breaker + bounded retry
`backends.base.CircuitBreaker` (closed / open / half-open) wraps the
Multica backend's CLI invocations. After
`BACKEND_CIRCUIT_FAILURE_THRESHOLD` consecutive failures the circuit opens
for `BACKEND_CIRCUIT_RESET_TIMEOUT` seconds, fast-failing every call with
`CircuitOpenError` (handler shows a dedicated ephemeral message). Idempotent
methods (`get_issue`, `update_status`, `assign_issue`) wrap one bounded
retry via `with_retry`; `create_issue` does not, to avoid duplicates.

### Output cap on subprocess CLI
`MULTICA_CLI_OUTPUT_BYTE_LIMIT` (default 10 MiB) is enforced per call.
A misbehaving CLI that produces gigabytes is killed (`SIGKILL`) and reaped
before its output can consume the bot's process memory.

### Optional healthcheck server
`HEALTHCHECK_PORT` (default `0`, disabled) starts a stdlib HTTP server in
a daemon thread exposing `/livez` and `/readyz`. The server has no auth
and answers anyone who can reach the bind address — keep it bound to
loopback or the cluster network, never to the public internet.

### Message Content intent — opt-in only (passive observer, v0.4)
By default the bot requests only `guilds` and `guild_messages` intents and
cannot read arbitrary message bodies — only slash command payloads directed at
it explicitly. The privileged **MESSAGE CONTENT** intent is enabled *only* when
`DISCORD_OBSERVER_ENABLED=true` (`build_intents(enable_message_content=...)`).
When on, the observer reads bodies *only* in `DISCORD_WATCH_CHANNELS`, acts only
on trigger-prefixed messages, and never auto-creates a task — every creation is
behind an author-scoped ✅ confirmation button. Enabling it requires toggling the
intent in the Dev Portal and, past 100 guilds, Discord verification. `members`
and `presences` stay OFF regardless.

### Timeout on every external call
`MulticaBackend._invoke()` enforces `timeout=settings.multica_cli_timeout`
(default 8 s, configurable 0.5–60 s) on every CLI call. Hung subprocesses
are `.kill()`ed and reaped via `await proc.wait()` so they cannot linger
as zombies.

---

## Out-of-scope risks (v0.1)

| Risk | Mitigation status |
|---|---|
| Passive observer — reading channel messages | Opt-in (`DISCORD_OBSERVER_ENABLED`), scoped to watch channels, human-confirmed creation (v0.4) |
| Multi-guild token reuse | Single-tenant deployment assumed in v0.1 |
| Distributed (multi-replica) rate limiting | Out of scope; current limiter is in-process only |

---

## Deployment recommendations

- Run as a non-root system user with no shell.
- Store `DISCORD_TOKEN` and backend credentials in environment variables or a
  secrets manager — never in a `.env` file committed to git.
- Set `DISCORD_GUILD_ID` to a single guild in production (limits blast radius
  if the token is compromised).
- Pin the Docker image or package version and review changelogs before upgrading.
