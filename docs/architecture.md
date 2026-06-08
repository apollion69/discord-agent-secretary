# Architecture

## Decision: Core + Adapter pattern

### Context

The bot began as a Multica-specific implementation (`venchur-secretary`). When
considering open-source publication, a key question arose: should it stay
Multica-only (simpler) or support pluggable backends (broader audience)?

### Decision

Ship a **protocol-first adapter pattern**. The core (`handlers.py`) depends only
on the `IssueBackend` protocol from `backends/base.py`. Concrete backends
(Multica, GitHub, Linear, Jira) live in separate modules and are wired by
`make_backend(settings)` at startup.

### Rationale

- Multica becomes the **reference adapter**, showcasing what a full implementation
  looks like — lowering the barrier for GitHub/Linear/Jira contributors.
- The core never imports any backend-specific type; error handling catches
  `BackendTimeoutError / BackendCallError / BackendError` (abstract hierarchy).
- Adding a new tracker = one new file + five lines in `make_backend()`.

### Alternatives considered

| Alternative | Rejected because |
|---|---|
| Multica-only OSS | Narrow audience; forks would reinvent the adapter layer |
| Runtime plugin discovery (`pkg_resources` entry points) | Over-engineered for v0.1; entry points can be added later without breaking changes |

---

## Component diagram

```
┌─────────────────────────────────────────┐
│           Discord Gateway               │
└────────────────┬────────────────────────┘
                 │ slash commands
                 ▼
┌─────────────────────────────────────────┐
│          discord_client.py              │
│  • build_intents()  (min privileges)    │
│  • assert_safe_permissions()            │
└────────────────┬────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────┐
│            handlers.py                  │
│  register_handlers(tree, backend, gid)  │
│  RateLimiter — per-(guild, user) bucket │
│  _safe_invoke() — error sanitization    │
│  _safe_followup() — HTTP-error guard    │
└────────────────┬────────────────────────┘
                 │ IssueBackend protocol
       ┌─────────┴──────────┐
       ▼                    ▼
┌────────────┐    ┌──────────────────────┐
│  multica   │    │  github / linear /   │
│  backend   │    │  jira  (stubs v0.1)  │
└────────────┘    └──────────────────────┘
       │
       │ asyncio.create_subprocess_exec
       ▼
  multica CLI binary
       │
       ▼
  Multica issue tracker

Cross-cutting:
  logging_setup.py — JSON formatter + SecretRedactingFilter
                     (masks every settings-derived secret).
  parsers.py       — regex-first /task parser (RU + EN); used by the
                     P5 passive observer (planned for v0.2).
  config.py        — pydantic-settings, env-driven; tz validated via
                     zoneinfo; CSV-friendly for DISCORD_WATCH_CHANNELS.
```

---

## Error hierarchy

```
BackendError
├── BackendTimeoutError   ← subprocess/HTTP timed out
├── BackendCallError      ← non-zero exit / non-2xx HTTP
└── BackendParseError     ← JSON malformed / missing `id` field
```

Multica-specific subclasses (`MulticaCliError`, `MulticaCliTimeoutError`,
`MulticaParseError`) carry extra context (exit_code, stderr) for log richness
but are caught via the abstract hierarchy in `handlers.py`.

---

## Security invariants

1. `UnsafePermissionsError` aborts boot if any of ADMINISTRATOR,
   MANAGE_GUILD, MANAGE_ROLES, MANAGE_CHANNELS, MANAGE_WEBHOOKS,
   BAN_MEMBERS, KICK_MEMBERS, MENTION_EVERYONE is detected. Bot
   membership that cannot be resolved (`get_member` miss +
   `fetch_member` failure) also aborts boot.
2. `main()` returns a non-zero exit code on any boot refusal, so
   container orchestrators surface the failure instead of silently
   restarting.
3. No `shell=True` anywhere — subprocess calls use the list form of
   argv. The Multica child process is reaped via `proc.wait()` after
   `kill()` on timeout.
4. Backend stderr is logged server-side only and goes through the
   `SecretRedactingFilter`; user-visible error messages are sanitized
   *and* ephemeral (single-user-only).
5. No Message Content intent — the bot cannot read channel messages
   in v0.1.
6. `RateLimiter` per (guild, user) caps subprocess churn / API quota
   burn from a spamming member.

---

## Decision: Thread-per-task (`threads.py`, v0.3.0)

### Context

Teams running the bot in a busy channel wanted each `/task` to spawn its own
discussion space and loop in the right people, without flooding the main
channel with per-task pings.

### Decision

An opt-in (`DISCORD_THREAD_ENABLED`, default off) post-step on the `/task`
success path opens a Discord thread for the new issue and posts a rich intro
that @-pings the participants *inside* the thread. The main-channel reply keeps
its existing short confirmation contract unchanged.

- **`threads.py` is pure-helpers + one best-effort orchestrator.**
  `build_thread_name` / `resolve_thread_pings` / `build_allowed_mentions` /
  `build_thread_intro` are deterministic and unit-tested without Discord;
  `open_task_thread` is the only async, Discord-touching function and it never
  raises into the handler — a thread failure must not fail task creation, which
  already succeeded.
- **Public (default)** threads attach to the announcement message
  (`Message.create_thread`); **private** threads are standalone
  (`TextChannel.create_thread(type=private_thread)`), members pulled in by the
  intro mention.
- **Mention hygiene**: `AllowedMentions` is scoped to exactly the resolved ids
  with `everyone=False`, so untrusted task text can never trigger a mass-ping.
- **Assignee resolution is zero-coupling**: the handler reverses the in-memory
  `DISCORD_MEMBER_MAP` (`member-uuid → discord-id`) — no Multica call is added
  to the backend-agnostic handler. A name-based assignee simply isn't pinged.

### Security note

Thread creation needs `create_public_threads` / `create_private_threads` and
`send_messages_in_threads` — **none** of which are in the refused-permission
allow-list (`MANAGE_CHANNELS` is refused but is *not* required to create
threads), so the feature is fully compatible with the minimal-privilege posture.

### Alternatives considered

| Alternative | Rejected because |
|---|---|
| Forum channel (one post per task) | Larger UX/permission change; a thread on the existing channel is the minimal, reversible step |
| Live Multica member lookup to ping name-based assignees | Couples the backend-agnostic handler to Multica; the `mention_scanner` already pings assignees once @mentioned in a comment |
| `AllowedMentions(users=True)` | Less safe than enumerating exact ids — a stray mention in task text could ping a non-participant |
