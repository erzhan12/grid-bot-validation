# Grid Bot Validation

Grid trading bot validation and backtesting framework.

## Project Structure

```
grid-bot-validation/
├── packages/
│   └── gridcore/          # Pure grid trading strategy logic (zero dependencies)
├── bbu_reference/         # Reference implementation from bbu2-master
├── backtest_reference/    # Backtest reference implementation
├── docs/                  # Documentation and feature plans
├── tests/                 # Integration tests
└── pyproject.toml         # uv workspace configuration
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) package manager

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd grid-bot-validation

# Sync the workspace with uv
uv sync

# Install gridcore package in editable mode
uv pip install -e packages/gridcore
```

### Running Tests

```bash
# Run all gridcore tests
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Run specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

## Packages

### gridcore

Pure grid trading strategy logic with **zero exchange dependencies**.

- **Location**: `packages/gridcore/`
- **Description**: Core grid trading strategy implementation extracted from bbu2-master
- **Test Coverage**: 89%
- **Dependencies**: None (production), pytest + pytest-cov (dev)

See [`packages/gridcore/README.md`](packages/gridcore/README.md) for detailed documentation.

## Development Workflow

This project follows a structured workflow defined in `CLAUDE.md`:

1. Define task clearly
2. Research codebase and RULES.md
3. Create plan and get confirmation
4. Implement with testing
5. Update RULES.md with learnings
6. Verify and commit

See [`CLAUDE.md`](CLAUDE.md) for full workflow details and [`RULES.md`](RULES.md) for project-specific guidelines.

## Package Management with uv

This project uses [uv](https://github.com/astral-sh/uv) for fast, reliable Python package management.

### Common Commands

```bash
# Sync all dependencies
uv sync

# Add a dependency to workspace dev dependencies
uv add --dev <package>

# Add a dependency to a specific package
cd packages/gridcore
uv add <package>

# Run tests
uv run pytest packages/gridcore/tests/

# Run Python scripts
uv run python script.py
```

### Why uv?

- **Fast**: 10-100x faster than pip
- **Reliable**: Deterministic dependency resolution with lockfile
- **Modern**: Built-in workspace support for monorepos
- **Compatible**: Works with existing pip/PyPI ecosystem

## Features

### Phase B: Core Library Extraction ✅ COMPLETED

**Status**: Completed 2025-12-30

Extracted pure strategy logic from `bbu2-master` into `gridcore` package with zero exchange dependencies.

- ✅ Grid calculations (from greed.py)
- ✅ Event-driven strategy engine (from strat.py)
- ✅ Position risk management (from position.py)
- ✅ 89% test coverage (37 tests passing)
- ✅ Zero exchange dependencies verified

See [`docs/features/0001_IMPLEMENTATION_SUMMARY.md`](docs/features/0001_IMPLEMENTATION_SUMMARY.md) for full details.

## License

[Specify license here]

## Contributing

[Specify contribution guidelines here]
