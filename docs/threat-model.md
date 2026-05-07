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

### No Message Content intent
The bot requests only `guilds` and `guild_messages` intents. It cannot read
arbitrary messages — only slash command payloads directed at it explicitly.

### Timeout on every external call
`MulticaBackend._invoke()` enforces `timeout=settings.multica_cli_timeout`
(default 8 s, configurable 0.5–60 s) on every CLI call. Hung subprocesses
are `.kill()`ed and reaped via `await proc.wait()` so they cannot linger
as zombies.

---

## Out-of-scope risks (v0.1)

| Risk | Mitigation status |
|---|---|
| P5 passive observer — reading channel messages | Not implemented; will require explicit opt-in + separate threat model |
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
