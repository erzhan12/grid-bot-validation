# Project Rules and Guidelines

## Phase B: Core Library Extraction (gridcore)

### Completed: 2025-12-30

Successfully extracted pure strategy logic from `bbu2-master` into `packages/gridcore/` with zero exchange dependencies.

### Key Implementation Notes

1. **Zero Exchange Dependencies**
   - NO imports from `pybit`, `bybit`, or any exchange-specific libraries
   - Validation command: `grep -r "^import pybit\|^from pybit" packages/gridcore/src/` should return nothing
   - File: All modules in `packages/gridcore/src/gridcore/`

2. **Grid Module (`grid.py`)**
   - Extracted from: `bbu_reference/bbu2-master/greed.py`
   - Key transformation: Replaced `BybitApiUsdt.round_price()` with internal `_round_price(tick_size)`
   - **IMPORTANT**: `tick_size` must be passed as `Decimal` parameter, not looked up from exchange
   - Removed: `read_from_db()`, `write_to_db()`, `strat` dependency
   - Added: `is_price_sorted()`, `is_greed_correct()` validation methods
   - File: `packages/gridcore/src/gridcore/grid.py`

3. **Engine Module (`engine.py`)**
   - Extracted from: `bbu_reference/bbu2-master/strat.py` (Strat50 class)
   - Event-driven pattern: `on_event(event) → list[Intent]`
   - **CRITICAL**: Engine NEVER makes network calls or has side effects
   - Returns intents (PlaceLimitIntent, CancelIntent), execution layer handles actual orders
   - File: `packages/gridcore/src/gridcore/engine.py`

4. **Events and Intents**
   - Events (`events.py`): Immutable dataclasses representing market data and order updates
   - Intents (`intents.py`): Immutable dataclasses representing desired actions
   - **PITFALL**: All event dataclass fields that extend Event must have default values (Python dataclass inheritance requirement)
   - Files: `packages/gridcore/src/gridcore/events.py`, `packages/gridcore/src/gridcore/intents.py`

5. **Testing**
   - Must maintain ≥80% test coverage
   - Run tests: `cd packages/gridcore && PYTHONPATH=./src pytest tests/ --cov=gridcore --cov-fail-under=80`
   - Current coverage: 88%
   - Test files: `packages/gridcore/tests/test_*.py`

6. **Package Structure**
   ```
   packages/gridcore/
   ├── pyproject.toml           # Zero external dependencies
   ├── src/gridcore/
   │   ├── __init__.py
   │   ├── events.py            # Event models
   │   ├── intents.py           # Intent models
   │   ├── config.py            # GridConfig
   │   ├── grid.py              # Grid calculations (from greed.py)
   │   ├── engine.py            # GridEngine (from strat.py)
   │   └── position.py          # Position risk management
   └── tests/
       ├── test_grid.py         # 17 tests
       ├── test_engine.py       # 13 tests
       ├── test_position.py     # 7 tests
       └── test_comparison.py   # Comparison with original (optional)
   ```

7. **Reference Code Location**
   - Original code: `bbu_reference/bbu2-master/`
   - Keep reference code for comparison tests
   - Never modify reference code

## Package Management with uv

This project uses [uv](https://github.com/astral-sh/uv) for package management.

### Installation and Setup

```bash
# Sync workspace (from project root)
uv sync

# Install gridcore in editable mode
uv pip install -e packages/gridcore
```

### Running Tests

```bash
# Run all gridcore tests with coverage
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Run specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

### Key Files

- `pyproject.toml` (root) - Workspace configuration with dev dependencies
- `uv.lock` - Lockfile for reproducible builds (committed to git)
- `packages/gridcore/pyproject.toml` - Package-specific configuration

## Development Workflow

1. Always follow the CLAUDE.md workflow:
   - Define task clearly
   - Research codebase and RULES.md
   - Create plan and get confirmation
   - Implement with testing
   - Update RULES.md with learnings
   - Verify and commit

2. When working with gridcore:
   - Use `uv run pytest` for running tests
   - Always run tests after changes
   - Verify zero exchange dependencies
   - Check that tick_size is passed as Decimal parameter
   - Ensure event-driven pattern is maintained (no side effects in engine)

## Common Pitfalls to Avoid

1. **DO NOT** import pybit, bybit, or exchange-specific libraries in gridcore
2. **DO NOT** make network calls or database calls in gridcore modules
3. **DO NOT** use BybitApiUsdt.round_price() - use Grid._round_price(tick_size) instead
4. **DO NOT** forget default values in event dataclass fields (Python inheritance requirement)
5. **DO NOT** make GridEngine.on_event() have side effects - it must be pure (except internal state)
6. **ALWAYS** pass tick_size as Decimal parameter to Grid, never look it up from exchange
7. **ALWAYS** run tests before committing changes to gridcore
8. **Grid Rebuild**: `build_greed()` clears `self.greed = []` before building to prevent doubling on rebuilds
9. **Duplicate Orders**: `PlaceLimitIntent.create()` uses deterministic `client_order_id` (SHA256 hash of symbol+side+price+grid_level+direction) so execution layer can detect/skip duplicates

## Next Steps (Future Phases)

- Phase C: Backtesting Framework Integration
- Phase D: Live Trading Integration
- Phase E: Multi-Tenant Support
