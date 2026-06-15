# Changelog

All notable changes to `discord-agent-secretary` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-06-15

### Changed

- **Autopilot digest weekends**: the daily digest now skips Saturday and Sunday,
  then posts a Monday catch-up labelled with the weekend dates. The completed
  bucket covers work since the previous successful digest so weekend completions
  are not lost.

## [0.4.0] - 2026-06-08

> Modernization bundle (research-driven). Every item is opt-in; default config
> reproduces 0.3.0 behaviour exactly.

### Added

- **Components V2 task cards** (`cards.py`, `DISCORD_CARDS_ENABLED`): render the
  `/task` confirmation as a colour-accented Discord Components V2 card (heading +
  ref + priority + optional description) instead of a plain-text line. Accent
  colour signals priority (urgent=red, high=orange, medium=blurple, low=green).
  Pure builders — the card is a `discord.ui.LayoutView`, unit-tested by
  introspecting the view tree. Requires `discord.py >= 2.6` (floor bumped from
  2.3).

- **Passive secretary observer** (`observer.py`, `DISCORD_OBSERVER_ENABLED`):
  watch `DISCORD_WATCH_CHANNELS` for trigger-prefixed messages
  (`/task`, `!task`, `задача:`, `task:` — configurable), parse them with the
  regex-first `parsers.parse_task` (RU+EN, no LLM), and post a **human-in-the-loop**
  ✅/❌ confirmation. Only ✅ creates the task (and opens its thread); only the
  message author can confirm. Enabling it turns ON the privileged MESSAGE CONTENT
  intent (`build_intents(enable_message_content=...)`) — documented in
  `docs/threat-model.md`. Default off.
- `threads.announce_task_thread` — a shared high-level helper now used by both the
  `/task` slash command and the observer to open + populate the task thread.
- **Bidirectional thread↔issue sync** (`sync.py`, `thread_map.py`,
  `message_router.py`, `DISCORD_THREAD_SYNC_ENABLED`): mirror comments both ways —
  a Multica `comment_created` webhook is posted into the mapped task thread, and a
  human reply inside a task thread is added as an issue comment
  (`backend.add_comment`, attributed via `DISCORD_MEMBER_MAP`). A persistent
  issue↔thread map (`thread_map.py`, atomic JSON) is recorded when a thread opens.
  Echo-loop guard: outbound comments carry a `[via-discord]` marker that the
  inbound router skips; bot-authored thread posts are ignored by the outbound
  handler. A single `on_message` dispatcher (`message_router.py`) lets the observer
  and sync coexist. Adds `IssueBackend.add_comment` (Multica:
  `issue comment add … --content`; stubs raise NotImplementedError). Reading thread
  replies needs the MESSAGE CONTENT intent (shared with the observer gate).
  Default off.

- **Scoped Discord MCP server** (`mcp_server/`, optional `[mcp]` extra, entrypoint
  `discord-agent-secretary-mcp`): gives an LLM agent a *minimal* Discord surface —
  `list_threads`, `read_thread`, `post_message` (mentions suppressed) — and
  nothing else. No Administrator, no privileged intents, token via
  `DISCORD_MCP_BOT_TOKEN` (never hard-coded). The dependency-light core
  (`DiscordThreadGateway`) is fully unit-tested without the `mcp` package; the
  FastMCP wiring lazy-imports `mcp`. Directly answers the deep-research finding
  that the popular third-party Discord MCP servers recommend Administrator and
  privileged intents (OWASP MCP01/MCP03).

### Changed

- Dependency floor: `discord.py>=2.6,<3` (was `>=2.3,<3`) for Components V2.

## [0.3.0] - 2026-06-08

### Added

- **Venture thread-per-task** (`threads.py`): when `DISCORD_THREAD_ENABLED=true`,
  a successful `/task` opens a dedicated Discord thread named
  `"<ticket-id> <short title>"` and pings the participants **inside the thread**
  — the main channel keeps only the short confirmation, so it never gets
  cluttered and every task becomes a jump-able sub-space. Participants pinged:
  the creator, the assignee (when given as a member UUID resolvable through
  `DISCORD_MEMBER_MAP` — zero backend coupling), and the configured standing
  watchers. Public threads are attached to the announcement message; private
  threads (`DISCORD_THREAD_PRIVATE=true`) are standalone, members pulled in by
  mention. Thread mentions are scoped with `AllowedMentions` to exactly the
  resolved ids (`everyone=False`) — no mass-ping is possible even if task text
  contained one. Thread creation is best-effort: any Discord error is logged and
  swallowed, so a missing permission never fails task creation.
  New settings: `DISCORD_THREAD_ENABLED`, `DISCORD_THREAD_PRIVATE`,
  `DISCORD_THREAD_AUTO_ARCHIVE_MINUTES` (validated ∈ {60,1440,4320,10080}),
  `DISCORD_THREAD_NAME_MAX_WORDS`, `DISCORD_THREAD_PING_USER_IDS`,
  `DISCORD_THREAD_PING_ROLE_IDS`. Default off — existing deployments are
  unchanged.

### Fixed

- Type-checking under mypy 2.0 (`asyncio.Task[None]`, `Button[Any]`,
  `from_custom_id` annotations, pinned float/str return types) — the `mypy src`
  CI gate is green again after the unpinned `mypy>=1.10` resolved to 2.0.

## [0.2.7] - 2026-05-29

### Changed

- **Shared CLI helpers** (`_cli.py`): `strip_preamble`, `text_or_none`, and
  `run_cli_json` replace 7 + 4 + several duplicated copies across the workers.
  The shared `run_cli_json` reaps the child on timeout, fixing a zombie-process
  leak in the mention scanner and digest worker.
- **Round-robin reviewers**: automated review routing now rotates across all
  configured `MULTICA_AUTOMATED_REVIEWERS` instead of always using the first.
- **Worker resilience**: each worker loop now counts consecutive failures,
  escalates the log WARNING→ERROR past a threshold, and backs off exponentially
  (capped) instead of treating every error as a transient retry.
- **Per-worker liveness**: `/readyz` now reports `last_ok` for the review poller,
  digest, and mention scanner (not just the poller).
- **Member-map cache**: the mention scanner caches the workspace member→Discord
  map for `MENTION_MEMBER_MAP_TTL` seconds (default 300) instead of fetching it
  every poll cycle.
- Approval-button failures now show a cause-specific message (timeout vs the
  issue having changed).

### Added

- Webhook endpoint rate limiting (`WEBHOOK_RATE_LIMIT`, default 30 POSTs per
  client IP per 10s; 0 disables).

### Fixed

- Mention scanner no longer relies on `issue.updated_at` as the only comment
  activity gate; Multica comments can be newer than the issue timestamp, so the
  scanner now reads active issue comments and uses the comment seen-set plus the
  previous issue timestamp as the flood guard.
- Mention scanner now resolves both Multica `member.id` and `member.user_id`
  values in `mention://member/<uuid>` links.

### Removed

- Dead `anthropic_api_key` / `anthropic_model` config and the unused `[llm]`
  optional-dependency extra (the LLM parser fallback was never implemented).

## [0.2.6] - 2026-05-29

### Added

- **Mention scanner** (`MentionScanWorker`): polls active issues for
  `mention://member/<id>` in comments, maps member IDs to Discord users via
  `DISCORD_MEMBER_MAP`, and pings `<@discord_id>` in the review channel.
  Per-issue `updated_at` gates re-scans; comment-id seen-set deduplicates.
  Config: `MENTION_SCAN_ENABLED`, `MENTION_SCAN_STATUSES`.
- **Start-of-work approval gate** (`[approval-request:start]` marker): agent posts
  the marker and the bot renders 🟢 "Разрешить начать" (→ `in_progress` +
  `[start-approved]` comment) / 🔴 "Отклонить" (→ `blocked`) buttons. The waiting
  agent polls the machine-readable comment before proceeding.

### Fixed

- Approval callback calls `defer()` before invoking the CLI so the 3-second
  Discord interaction window cannot expire mid-subprocess.
- `apply_human_verdict` now enforces a CLI timeout; a bounded subprocess can no
  longer hang indefinitely.
- Mention-scanner comment snippets are `escape_markdown`-sanitized; `AllowedMentions`
  blocks `@everyone` / `@here` / role pings injected via Multica comment text.
- Seen-set eviction in the mention scanner is now insertion-ordered (was
  lexicographic on UUIDs, which silently dropped recent IDs on overflow).
- `DigestWorker._post` returns a bool; the day is marked sent only on successful
  Discord delivery, not on attempted delivery.
- `ApprovalButton` registers on `DISCORD_MEMBER_MAP` alone, not gated on mention
  scan being enabled — the two features are independent.
- Worker done-callbacks log `CRITICAL` when a background worker exits unexpectedly.

### Security

- `DISCORD_*` secrets stripped from the CLI subprocess environment before
  `apply_human_verdict` spawns the Multica CLI — env-var leak path closed.

---

## [0.2.5] - 2026-05-28

### Added

- **Discord approval buttons** (`[approval-request]` marker): when an agent posts
  the marker, the bot renders 🟢 approve (→ `done`) / 🔴 rework (→ `todo`) /
  🔵 open-in-Multica buttons. Click applied as the clicking member via
  act-as-member; only `DISCORD_MEMBER_MAP`-mapped members may click; buttons
  survive restarts (`DynamicItem`).
- **Act-as-member attribution**: `/task` maps the invoking Discord user to a
  Multica member UUID via `DISCORD_MEMBER_MAP` and passes it as `on_behalf_of`,
  so tasks show the real requester as creator instead of the bot's token owner.
  Unmapped invokers fall back to the token owner with a warning.
- **Automated review routing**: autopilot (`origin_type=autopilot`) `in_review`
  issues are routed to a configurable reviewer agent instead of flooding the
  review channel with per-task pings.
- **Stale review scanner** (`discord-agent-secretary-stale-review` entry point):
  detects issues stuck in `in_review` beyond a configurable age and re-notifies.
- **Daily autopilot digest** (`DigestWorker`): one summary per day of autopilot
  issues awaiting review + completed in the last 24 h, replacing per-task pings
  for cron-originated work. Config: `DIGEST_ENABLED`, `DIGEST_HOUR`.

### Fixed

- Review-router (assign mode) now reassigns the issue to the reviewer **before**
  posting the routing comment, so the triggering comment dispatches the reviewer
  agent rather than the producer.
- Automated review classifier gates on `origin_type=='autopilot'` (the
  authoritative field) — the previous `origin_source` check was always inert
  because `origin_source` is not a first-class issue field.

---

## [0.2.4] - 2026-05-22

### Added

- **Webhook endpoint** (`POST /hooks/multica`): healthcheck server gains a
  webhook receiver; Multica `status→in_review` transitions by agents post a
  "✅ Готово к ревью" notification to the Discord review channel.
  Optional HMAC-SHA256 signature verification via `MULTICA_WEBHOOK_SECRET`.
- **Pull-model `in_review` poller** (`PullWorker`): background coroutine polls
  `multica issue list --status in_review` every `MULTICA_POLL_INTERVAL` seconds
  and pings Discord on new agent-assigned issues. First pass seeds silently to
  avoid startup floods. Dedup state persisted to `MULTICA_SEEN_PATH`.
- Clickable `[VEN-NNN](<url>)` Markdown link in the `/task` created reply when
  `MULTICA_APP_URL` is set; falls back to bare identifier or raw UUID.

### Fixed

- Issue URLs in both the pull-worker notification and the `/task` reply now use
  the issue UUID for the Multica frontend route (the frontend routes by UUID, not
  by identifier slug).

---

## [0.2.3] - 2026-05-08

### Fixed

- Permission safety check (`assert_safe_permissions`) now evaluates only the
  bot's explicitly assigned roles, excluding the `@everyone` role (whose
  `guild.id`-keyed permissions are server-wide defaults, not bot-specific grants).
  Eliminates false-positive `REFUSE_PERMS` on servers where `@everyone` carries
  `mention_everyone`.
- `verify_guilds_safe` calls `guild.fetch_member()` (REST) instead of
  `guild.get_member()` (local cache), ensuring the security check always uses
  Discord's authoritative current role state and not stale gateway data.

---

## [0.2.2] - 2026-05-08

### Fixed

- Passes `HTTPS_PROXY` (fallback `HTTP_PROXY`) to `discord.Client` as the
  `proxy=` kwarg — aiohttp does not auto-detect proxy env vars, so WebSocket
  and REST calls now route correctly on firewall-gated deployments.

---

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
