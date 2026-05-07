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
if ADMINISTRATOR or MANAGE_GUILD permissions are detected. The bot cannot be
tricked into running with elevated permissions — it refuses to start.

### No shell injection
All subprocess calls use `asyncio.create_subprocess_exec` with an explicit
`argv` list. No string interpolation into shell commands.

### No secret leakage to Discord
`handlers._safe_invoke()` catches all `BackendError` subclasses and sends only
a generic sanitized message to the Discord channel. Backend stderr (which may
contain paths, exit codes, or token fragments) is logged server-side only.

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
| Rate limiting / quota abuse via slash command spam | Not implemented; Discord's built-in app rate limits apply |
| P5 passive observer — reading channel messages | Not implemented; will require explicit opt-in + separate threat model |
| Multi-guild token reuse | Single-tenant deployment assumed in v0.1 |

---

## Deployment recommendations

- Run as a non-root system user with no shell.
- Store `DISCORD_TOKEN` and backend credentials in environment variables or a
  secrets manager — never in a `.env` file committed to git.
- Set `DISCORD_GUILD_ID` to a single guild in production (limits blast radius
  if the token is compromised).
- Pin the Docker image or package version and review changelogs before upgrading.
