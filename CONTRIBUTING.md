# Contributing to discord-agent-secretary

Thank you for your interest in contributing! The most impactful contribution
right now is **implementing a backend adapter** — GitHub Issues, Linear, and
Jira are all stubbed out and waiting.

---

## Adding a backend (good first issue)

Five steps to implement a new backend:

### Step 1 — Create the module

Create `src/discord_agent_secretary/backends/your_tracker.py`.
Inherit from `IssueBackendBase`:

```python
from .base import IssueBackendBase, IssueRef

class YourTrackerBackend(IssueBackendBase):
    def __init__(self, *, api_key: str, ...) -> None:
        ...

    async def create_issue(
        self, title: str, *, description=None, priority=None, assignee=None
    ) -> IssueRef:
        ...

    async def get_issue(self, issue_id: str) -> IssueRef:
        ...

    async def assign_issue(self, issue_id: str, to: str) -> IssueRef:
        ...

    async def update_status(self, issue_id: str, status: str) -> IssueRef:
        ...
```

Use the error hierarchy from `base.py`:
- `BackendTimeoutError` — API call timed out
- `BackendCallError(exit_code, stderr)` — API returned non-2xx / non-zero exit
- `BackendParseError` — response could not be parsed into `IssueRef`

### Step 2 — Add config fields

In `src/discord_agent_secretary/config.py`, add settings under a comment block:

```python
# --- YourTracker ---
your_tracker_api_key: str = Field(default="", alias="YOUR_TRACKER_API_KEY")
```

### Step 3 — Wire the factory

In `src/discord_agent_secretary/backends/__init__.py`, add to `make_backend()`:

```python
if name == "your_tracker":
    from .your_tracker import YourTrackerBackend
    return YourTrackerBackend(api_key=settings.your_tracker_api_key)
```

Also add `"your_tracker"` to the `BackendName` literal in `config.py`.

### Step 4 — Add integration tests

Model your tests on `tests/integration/test_multica.py`. Use `_FakeProc`-style
mocks for HTTP responses rather than hitting the real API in CI.

### Step 5 — Update `.env.example` and README

Add a config block to `.env.example` and a quickstart section to `README.md`.

---

## Development setup

```bash
uv venv .venv --python python3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
pytest tests/ -v
ruff check src tests
mypy src
```

CI requires: `pytest` (≥80% coverage), `ruff` (zero errors), `mypy` (strict).

---

## Pull request checklist

- [ ] Tests added (≥80% coverage on the new module)
- [ ] `ruff check src tests` passes
- [ ] `mypy src` passes
- [ ] `.env.example` updated
- [ ] README quickstart section added
- [ ] Stub backend file updated with `raise NotImplementedError` replaced by real implementation

---

## Coding style

- Python 3.11+ syntax (e.g. `X | None` instead of `Optional[X]`, `datetime.UTC`)
- Type annotations on all public functions
- `asyncio.create_subprocess_exec` (list form, never `shell=True`) for any subprocess
- Explicit timeouts on every external call (see `BackendBase.timeout`)
- Frozen dataclasses for value objects

---

## Questions

Open a [GitHub Discussion](../../discussions) — not an issue — for design questions.
Issues are for confirmed bugs and accepted feature requests.
