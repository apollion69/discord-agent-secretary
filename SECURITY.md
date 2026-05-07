# Security policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

Instead, use GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/apollion69/discord-agent-secretary/security/advisories/new).

If that channel is unavailable, email the maintainer listed in
`pyproject.toml` directly. We aim to acknowledge reports within 72 hours and
to ship a fix or mitigation within 14 days for confirmed high-severity
issues.

## Supported versions

Only the latest minor version receives security fixes. Pin the published
version (e.g. `discord-agent-secretary~=0.1.0`) and review release notes
before upgrading.

## In scope

- The bot itself (`src/discord_agent_secretary/`).
- The default configuration shipped in `.env.example`.
- The Multica reference backend.

Stub backends (GitHub, Linear, Jira) are not yet implemented; they refuse
to start. Any bug in their stubs is in scope only if it allows the bot to
boot in an unsafe state.

## Out of scope

- Bugs in upstream dependencies (report those upstream — we'll bump pinned
  versions once a fix lands).
- The Multica CLI binary itself; report to that project.
- Rate limiting under multi-replica deployments — the in-process token
  bucket is documented as single-replica only; see `docs/threat-model.md`.

## Hardening recommendations

See `docs/threat-model.md` for the full list. Highlights:

- Run as an unprivileged system user with no shell.
- Store `DISCORD_BOT_TOKEN` and backend credentials in a secrets manager,
  not a committed `.env` file.
- Set `DISCORD_GUILD_ID` to a single guild in production to limit blast
  radius if the token is ever exposed.
- Pin the Docker image / package version and review release notes before
  upgrading.
