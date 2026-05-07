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
│  _safe_invoke() — error sanitization    │
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

1. `UnsafePermissionsError` aborts boot if ADMINISTRATOR or MANAGE_GUILD detected.
2. No `shell=True` anywhere — subprocess calls use the list form of argv.
3. Backend stderr is logged server-side only; users see only sanitized messages.
4. No Message Content intent — the bot cannot read channel messages in v0.1.
