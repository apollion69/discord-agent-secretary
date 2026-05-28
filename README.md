# discord-agent-secretary

> Turn Discord slash commands and channel chatter into tracked issues — across any issue tracker.

`discord-agent-secretary` is a production-ready Discord bot that bridges your team's
Discord server to an issue tracker of your choice via a **pluggable backend protocol**.
Out of the box it ships a full [Multica](https://multica.ai) adapter; GitHub Issues,
Linear, and Jira adapters are documented stubs ready for community contribution.

---

## Why

Teams already live in Discord. Capturing "hey, can someone fix X?" in a real ticket
requires context-switching to Jira/Linear/GitHub. This bot removes that friction:

- `/task` — create an issue from a Discord slash command
- `/status` — update an issue's status
- `/assign` — assign an issue to a team member

The bot uses **zero** dangerous Discord permissions (blocks boot if ADMINISTRATOR or
MANAGE_GUILD are granted).

---

## Architecture

```
Discord server
  ├── Slash commands (/task, /status, /assign)
  │          │
  │          ▼
  │   discord-agent-secretary
  │     ┌────────────┐
  │     │  handlers  │  maps Discord interactions → IssueBackend calls
  │     └─────┬──────┘
  │           │  IssueBackend protocol
  │     ┌─────▼──────────────────────────┐
  │     │ backends/                       │
  │     │  multica.py  ← reference impl  │
  │     │  github.py   ← stub (v0.1)     │
  │     │  linear.py   ← stub (v0.1)     │
  │     │  jira.py     ← stub (v0.1)     │
  │     └─────────────────────────────────┘
  │           │
  │           ▼
  │   Issue tracker (Multica / GitHub / Linear / Jira)
```

The `IssueBackend` protocol lives in `src/discord_agent_secretary/backends/base.py`.
Any class that implements `create_issue`, `get_issue`, `assign_issue`, and
`update_status` is a valid backend — no registration, no base-class inheritance
required (though `IssueBackendBase` ABC is provided as an optional convenience).

---

## Quickstart

### 1. Install

```bash
pip install discord-agent-secretary
# or with uv:
uv pip install discord-agent-secretary
```

### 2. Create a Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications).
2. Create a new application → Bot tab → copy **Token**.
3. OAuth2 → URL Generator: scopes `bot` + `applications.commands`,
   permissions `Send Messages` + `Use Application Commands`.
4. Invite the bot to your server.

### 3. Configure

```bash
cp .env.example .env
# edit .env — at minimum set DISCORD_BOT_TOKEN and BACKEND
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | *(required)* | Bot token from Discord Developer Portal |
| `BACKEND` | `multica` | `multica` \| `github` \| `linear` \| `jira` |
| `DISCORD_GUILD_ID` | `""` | Guild-scoped commands (instant sync); empty = global (1h delay) |

#### Multica backend

```dotenv
BACKEND=multica
MULTICA_CLI_PATH=multica          # path to multica CLI binary
MULTICA_WORKSPACE_ID=your-uuid   # from `multica workspace list`
```

Install the Multica CLI from [github.com/multica-ai/multica/releases](https://github.com/multica-ai/multica/releases)
or via Homebrew: `brew install multica-ai/tap/multica`.

#### Member attribution (`DISCORD_MEMBER_MAP`)

By default, issues created via `/task` are attributed to the bot's token owner.
`DISCORD_MEMBER_MAP` lets you attribute each issue to the actual Discord user who
invoked the command instead.

```dotenv
# JSON object: Discord user ID (string) → Multica member UUID (string)
DISCORD_MEMBER_MAP={"111111111111111111":"<member-uuid>","222222222222222222":"<member-uuid>"}
```

- Unmapped Discord users fall back silently to the token owner (a warning is logged).
- The bot token must belong to an owner or admin member of the workspace for
  attribution to take effect; the Multica server enforces this server-side and
  silently ignores the header for any other caller.
- Member UUIDs are validated at startup; an invalid UUID prevents the bot from booting.
- The full variable format and an example are documented in `.env.example`.

---

#### GitHub Issues backend (stub — contribute!)

```dotenv
BACKEND=github
GITHUB_TOKEN=ghp_...
GITHUB_REPO=owner/repo
```

#### Linear backend (stub — contribute!)

```dotenv
BACKEND=linear
LINEAR_API_KEY=lin_api_...
LINEAR_TEAM_ID=your-team-id
```

#### Jira backend (stub — contribute!)

```dotenv
BACKEND=jira
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_EMAIL=bot@your-org.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=ABC
```

### 4. Run

```bash
discord-agent-secretary
# or:
python -m discord_agent_secretary
```

---

## Commands

| Command | Arguments | Description |
|---|---|---|
| `/task` | `title`, `description?`, `priority?`, `assignee?` | Create a new issue |
| `/status` | `issue_id`, `status` | Update issue status (`todo` / `in_progress` / `in_review` / `done` / `blocked`) |
| `/assign` | `issue_id`, `to` | Assign an issue to a team member |

---

## Threat model

- **No ADMINISTRATOR / MANAGE_GUILD** — boot is aborted if these are present.
- **Slash commands only** — no Message Content intent; the bot cannot read arbitrary messages.
- **No secrets in Discord channels** — backend stderr never reaches Discord users; only sanitized error messages are sent.
- **P5 passive observer mode** (planned for v0.2) — will require explicit opt-in and a separate threat-model review. See `docs/threat-model.md`.

---

## Adding a backend

See [CONTRIBUTING.md](CONTRIBUTING.md) for the five-step guide.

In short: implement `IssueBackendBase` in `src/discord_agent_secretary/backends/your_tracker.py`,
register it in `make_backend()` in `backends/__init__.py`, add a `BackendName` literal,
and open a PR.

---

## Development

```bash
uv venv .venv --python python3.12
uv pip install -e ".[dev]"
pytest tests/ -v
ruff check src tests
mypy src
```

See [`docs/local-development.md`](docs/local-development.md) for smoke-test
recipes that don't require the Multica CLI binary.

---

## Security

To report a vulnerability, see [`SECURITY.md`](SECURITY.md). For deployment
hardening recommendations and the threat model, see
[`docs/threat-model.md`](docs/threat-model.md).

---

## Changelog

[`CHANGELOG.md`](CHANGELOG.md) tracks release notes and the running
`[Unreleased]` section.

---

## License

MIT — see [LICENSE](LICENSE).
