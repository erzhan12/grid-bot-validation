# Project Rules and Guidelines — Index

The full project rules live in **`.claude/rules/`** (feature 0096). Claude Code
loads them natively: unscoped rules load every session; path-scoped rules
auto-load when a file matching their `paths:` globs is read, and are re-injected
after context compaction.

**External reviewers / other tools**: read the files in `.claude/rules/` directly —
this index is only a map.

**Updating rules** (workflow step 6): edit the relevant `.claude/rules/*.md` file.
New cross-cutting pitfalls go to `core-invariants.md`; component specifics go to
that component's file. Keep this index in sync when adding/removing rule files;
every ~10 features, sweep the rule files and prune entries that no longer apply.

| Rule file | Loads | Contents |
|---|---|---|
| `.claude/rules/code-style.md` | always | Coding principles, conventions, safety rules |
| `.claude/rules/core-invariants.md` | always | Project overview, Constraints (do-not), running tests, CI, Dependency / lockfile (`uv sync` local vs `uv sync --locked` in CI, `uv lock` after manifest edits, pre-commit `uv lock --check`), logging levels, cross-package testing + pitfalls, margin-ratio vs positionIM distinction, Common Pitfalls (cross-cutting) |
| `.claude/rules/gridcore.md` | `packages/gridcore/**` | Strategy engine: grid/engine/position modules, 110017 self-heal (0064), state-divergence detector (0069), 110007 preflight + chase-close (0066), safety caps (0079), SAME ORDER, enums, events/intents, persistence, DB snapshots (0047), PnL functions |
| `.claude/rules/grid-db.md` | `shared/db/**` | Multi-tenant DB layer rules, enums, env vars |
| `.claude/rules/bybit-adapter.md` | `packages/bybit_adapter/**` | REST/WS components, event normalization, V5 API status |
| `.claude/rules/event-saver.md` | `apps/event_saver/**`, `apps/recorder/**` | Data capture rules, env vars, private WS disconnect handling |
| `.claude/rules/gridbot.md` | `apps/gridbot/**` | Live bot: architecture, fail-closed startup (0086), health file (0082), exceptions, Telegram, embedded saver, reconciliation invariants, orderLinkId format, WS reconnect (0024) |
| `.claude/rules/backtest.md` | `apps/backtest/**` | Backtest engine architecture, risk multiplier composition, CLI, metrics |
| `.claude/rules/comparator.md` | `apps/comparator/**` | Validation concepts, NormalizedTrade, spike-vs-drift stats (0070) |
| `.claude/rules/recorder.md` | `apps/recorder/**` | Standalone recorder rules + test pitfalls |
| `.claude/rules/replay.md` | `apps/replay/**` | Replay engine, telemetry parity (0034), UTA wallet semantics (0042), fill simulator modes incl. `last_cross` + `event_follower` (0072), test pitfalls |
| `.claude/rules/pnl-checker.md` | `apps/pnl_checker/**` | Live PnL validation rules |
| `.claude/rules/live-check.md` | `apps/live_check/**` | Replay-vs-live reconciliation (0088): read-only DB, verdict gates, freshness |
| `.claude/rules/risk-tiers.md` | tier-related files (see its `paths:`) | Dynamic risk limit tiers: architecture, consumers, caching, drift monitoring, cache format evolution |
