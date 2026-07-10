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
   (For the optional thread-per-task feature, also grant
   `Create Public Threads` + `Send Messages in Threads` — and
   `Create Private Threads` if you set `DISCORD_THREAD_PRIVATE=true`.)
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

#### Default two-squad routing

When a secretary `/task` has no explicit `assignee`, the Multica backend creates
a lead/audit parent issue and a child execution issue:

```dotenv
MULTICA_DEFAULT_ASSIGNEE=Claude
MULTICA_EXECUTION_ASSIGNEE=GPT-5.5
```

The parent issue is assigned to `MULTICA_DEFAULT_ASSIGNEE`; the child issue is
created with `--parent <parent-id>` and assigned to
`MULTICA_EXECUTION_ASSIGNEE`. A coordination comment on the parent records the
handoff contract: lead/audit owns review and user closeout, execution posts
commands, actions, findings, blockers, and evidence back to the parent.

If a user explicitly passes `assignee`, the bot keeps the old single-issue
behavior and does not create the execution child.

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

#### Thread-per-task (Venture secretary)

Set `DISCORD_THREAD_ENABLED=true` and every `/task` additionally opens a
dedicated Discord **thread** for the new issue, then pings the participants
**inside the thread** — the main channel keeps only the short confirmation, so
it never gets cluttered and each task becomes a jump-able sub-space.

```dotenv
DISCORD_THREAD_ENABLED=true
# Public threads attach to the announcement message (default). Private threads
# are standalone; members are pulled in by the intro mention.
DISCORD_THREAD_PRIVATE=false
# Discord allows only 60 / 1440 / 4320 / 10080 minutes.
DISCORD_THREAD_AUTO_ARCHIVE_MINUTES=4320
# Thread name = "<ticket-id> <first N title words>", hard-capped at 100 chars.
DISCORD_THREAD_NAME_MAX_WORDS=6
# Standing watchers always pinged inside a new task thread (CSV of Discord IDs).
DISCORD_THREAD_PING_USER_IDS=111111111111111111
DISCORD_THREAD_PING_ROLE_IDS=
```

- **Thread name** — `"<identifier> <short title>"` (e.g. `VEN-128 fix login
  redirect`), built from the issue's generated identifier plus the first
  `DISCORD_THREAD_NAME_MAX_WORDS` words of the title, hard-capped at Discord's
  100-char limit.
- **Who gets pinged** — the creator, the assignee (only when `assignee` is a
  member UUID resolvable through `DISCORD_MEMBER_MAP` — no extra backend call),
  and the configured standing watchers (`DISCORD_THREAD_PING_USER_IDS` /
  `_ROLE_IDS`).
- **Mention hygiene** — pings are scoped with `AllowedMentions` to exactly the
  resolved ids (`everyone=False`); no mass-ping is possible even if the task
  title/description contained `@everyone`.
- **Best-effort** — if the bot lacks the thread permission (or Discord errors),
  the failure is logged and swallowed; task creation and the main-channel
  confirmation are never affected.
- **Permissions** — needs `Create Public Threads` + `Send Messages in Threads`
  on the channel (plus `Create Private Threads` when `DISCORD_THREAD_PRIVATE`).
  None of these are in the bot's refused-permission allow-list, so the feature
  keeps the minimal-privilege posture.

---

#### Automated review routing

Agent-assigned issues with `origin_type=autopilot` (cron/autopilot tasks) are
suppressed from the corporate Discord review channel and routed to configured
reviewer agents instead — summarized once a day by the digest rather than pinged
per task. Requires the Multica server to expose `origin_type` in the issue list
(added 2026-05). Human/operator review tasks (no autopilot origin) are still
notified normally.

The digest posts only on weekdays in `TZ`. Saturday and Sunday are skipped; the
next Monday digest is labelled with the weekend dates and counts completed
autopilot work since the previous successful digest.

```dotenv
DISCORD_REVIEW_CHANNEL_ID=1234567890
MULTICA_REVIEW_ROUTING_MODE=off          # off | subscribe | assign
MULTICA_REVIEW_DRY_RUN=true              # rollback switch: true disables Multica mutations
MULTICA_AUTOMATED_REVIEWERS=checker-agent
MULTICA_REWORK_STATUS=todo
MULTICA_REVIEW_STATE_PATH=/opt/discord-secretary/review-routing.json
```

Use at least two cross-model reviewer ids in `MULTICA_AUTOMATED_REVIEWERS`.
The router removes the current producer from the eligible pool. Reviewers post
one protocol-v2 comment in this exact form and do not mutate issue state:

```text
[automated-review-verdict-v2] {"action":"approve|rework","summary":"evidence-based verdict"}
```

The secretary continuously reconciles routed `in_review` issues. `approve`
moves the issue to `done`; `rework` moves it to `MULTICA_REWORK_STATUS` and
reassigns the recorded producer. The persisted state and authoritative routing
and verdict comments make the operation idempotent across restarts.

Deploy with `MULTICA_REVIEW_DRY_RUN=true` first. Evidence commands:

```bash
multica issue list --status in_review --output json
pytest tests/unit/test_review_routing.py tests/unit/test_review_router.py tests/unit/test_stale_review.py -q
```

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

## Scoped Discord MCP server (optional)

For AI-agent chatops, the package ships a **minimal-permission** Model Context
Protocol server (`discord-agent-secretary-mcp`) that exposes only three tools —
`list_threads`, `read_thread`, `post_message` (mentions always suppressed). It
needs no Administrator and no privileged intents; the bot token comes from
`DISCORD_MCP_BOT_TOKEN`. This is deliberately narrower than the popular
third-party Discord MCP servers, which recommend Administrator + privileged
gateway intents (an OWASP MCP01/MCP03 risk).

```bash
pip install "discord-agent-secretary[mcp]"
DISCORD_MCP_BOT_TOKEN=... discord-agent-secretary-mcp
```

Grant the MCP bot only `View Channels`, `Read Message History`, and
`Send Messages in Threads`.

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
