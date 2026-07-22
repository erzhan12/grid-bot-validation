# Code Style & Guardrails

## Workflow
Process steps (define → research → plan → implement → verify → update RULES.md) live in the parent `CLAUDE.md`. Engineering rules on top of that:
1. If >3 files affected, break into smaller tasks
2. Track work in `tasks/todo.md` as checkable items
3. Define success criteria up front; turn the task into a verifiable goal and loop until it's met (e.g. "add validation" → write tests for invalid inputs, then make them pass)
4. After writing code, list what could break and suggest tests
5. Bug fixes start with a reproducing test

## Behavioral Rules
Apply on every task — these curb assumptions, over-engineering, and collateral damage.
- **Think before coding** — don't assume. State assumptions explicitly; if multiple interpretations exist, present them — don't pick silently. If a simpler approach exists, say so. If something is unclear, stop, name it, and ask.
- **Simplicity first** — minimum code that solves the problem, nothing speculative. No features beyond what was asked, no abstractions for single-use code, no "flexibility" that wasn't requested, no error handling for impossible scenarios. If you write 200 lines and it could be 50, rewrite. Test: "would a senior engineer call this overcomplicated?"
- **Surgical changes** — touch only what you must. Don't "improve" adjacent code, comments, or formatting; don't refactor what isn't broken; match existing style even if you'd do it differently. Remove only the imports/vars/functions YOUR change orphaned — leave pre-existing dead code in place (mention it, don't delete). Every changed line should trace directly to the request.
- **Goal-driven execution** — define success criteria up front; turn the task into a verifiable goal and loop until it's met (e.g. "add validation" → write tests for invalid inputs, then make them pass). Strong criteria let you loop independently.

## Conventions
Match existing code style; verify with `make lint` (ruff defaults, line-length 88) before claiming done.

- **Naming:** `snake_case` funcs/vars, `PascalCase` classes, `_leading_underscore` for private methods/attrs, `_UPPER_SNAKE` for module-private constants, `UPPER_CASE` enum members, `snake_case.py` files.
- **Types & models:** full type hints on public funcs; frozen `@dataclass` for immutable domain models (events/intents), Pydantic `BaseModel` for config + validators; **`Decimal` for price/qty — never `float`**; absolute imports; Google-style docstrings on public classes/methods.
- **Frameworks:** `uv` workspace; pytest + pytest-asyncio (`asyncio_mode=auto`); SQLAlchemy/SQLite (`shared/db`); Bybit via `packages/bybit_adapter`.
- **Testing:** tests in per-package `tests/` + `tests/integration/`; files `test_*.py`, classes `Test<Feature>`, functions `test_<scenario>` with a one-line docstring; hermetic — mock all network/DB (`unittest.mock`, shadow mode), never hit real Bybit; run `uv run pytest` (never bare `pytest`); async tests use `try/finally` cleanup; gridcore enforces 80% coverage.
- **Error handling:** raise `ValueError` for validation (esp. gridcore); custom exceptions only for control flow (e.g. `StartupTimeoutError`); detect Bybit error codes via module-level classifier funcs in `executor.py` (not methods); retry transient failures via the exponential-backoff `retry_queue` (owner calls `process_due()`, no bg threads); event handlers log ERROR with `exc_info=True` and **re-raise** — never swallow.
- **Logging:** per-module `logging.getLogger(__name__)`; INFO = state/startup, WARNING = recoverable/divergence, ERROR = failure (+`exc_info=True`), DEBUG = per-op. No new per-tick log spam — the existing per-tick Position-update INFO heartbeat stays (analyzer depends on it).
- **Anti-patterns:** no `float` money math; no `pybit`/network/DB imports in `gridcore` (pure logic); no inline magic time values (use named `_UPPER_SNAKE` constants); don't duplicate `.claude/rules/` (architecture/risk invariants/feature behavior live there; `RULES.md` is the index).

## Constraints
What NOT to do (negative rules — these override positive guidance when they conflict). Project-specific constraints live in `.claude/rules/core-invariants.md`.
- **Treat vendored/reference code and generated dirs as read-only** — don't edit third-party/legacy reference code, build artifacts, recorded data, or DB files; they aren't source.
- **No speculative backward-compat** — don't add shims, fallbacks, or legacy branches for old behavior unless asked.
- **No speculative features/config** — no fields, flags, abstractions, or "flexibility" for hypothetical future needs (see Behavioral Rules → Simplicity first).
- **Don't run tooling against live/production state without explicit ask.**

## Safety Rules
- Never overwrite existing files without confirmation; create `.bak` backup if risky
- No `rm`, `del`, `rmdir`, or `rm -rf` without explicit approval
- Package installs: explain what, why, and impact before proceeding
- Database migrations: always confirm before running; never drop tables without approval
- When in doubt, explain the command in plain language and ask before running