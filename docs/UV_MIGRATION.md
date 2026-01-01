# UV Package Manager Migration

**Date**: 2025-12-30

**Status**: ✅ COMPLETED

## Overview

Migrated the grid-bot-validation project to use [uv](https://github.com/astral-sh/uv) as the primary package manager instead of pip.

## Changes Made

### 1. Root Configuration

Created `/pyproject.toml` with workspace configuration:

```toml
[project]
name = "grid-bot-validation"
version = "0.1.0"
description = "Grid trading bot validation and backtesting framework"
requires-python = ">=3.11"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-cov>=5.0",
]
```

### 2. Package Configuration

Updated `packages/gridcore/pyproject.toml`:
- Changed build backend from `setuptools` to `hatchling`
- Removed `[project.optional-dependencies]` (dev deps now at workspace level)
- Added hatch build configuration for proper package structure

### 3. Virtual Environment

- Created `.venv/` using uv (auto-created on `uv sync`)
- Virtual environment managed by uv with exact dependency versions in `uv.lock`

### 4. Lockfile

- Generated `uv.lock` (45KB) for reproducible builds
- **Important**: `uv.lock` is committed to git (removed from .gitignore)

### 5. Documentation Updates

Updated the following files:
- `README.md` - Added uv quick start and commands
- `packages/gridcore/README.md` - Updated installation and testing sections
- `RULES.md` - Added uv section with common commands
- Created `docs/UV_MIGRATION.md` (this file)

## Usage

### Installation

```bash
# Sync workspace (install all dependencies)
uv sync

# Install gridcore in editable mode
uv pip install -e packages/gridcore
```

### Running Tests

```bash
# All tests with coverage
uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v

# Specific test file
uv run pytest packages/gridcore/tests/test_grid.py -v
```

### Common Commands

```bash
# Add dev dependency to workspace
uv add --dev <package>

# Add dependency to gridcore
cd packages/gridcore && uv add <package>

# Run Python scripts
uv run python script.py

# Update dependencies
uv sync --upgrade
```

## Benefits

1. **Speed**: 10-100x faster than pip for dependency resolution and installation
2. **Reliability**: Deterministic builds with `uv.lock` lockfile
3. **Workspace Support**: Native monorepo support for multiple packages
4. **Compatibility**: Works with existing PyPI packages and pip workflows
5. **Modern**: Written in Rust, actively maintained by Astral

## Verification

### Tests Pass

```bash
$ uv run pytest packages/gridcore/tests/ --cov=gridcore --cov-fail-under=80 -v
...
======================== 37 passed, 7 skipped in 0.80s =========================
Required test coverage of 80% reached. Total coverage: 88.66%
```

### Dependencies Installed

```bash
$ uv sync
...
Installed 7 packages in 92ms
 + coverage==7.13.1
 + iniconfig==2.3.0
 + packaging==25.0
 + pluggy==1.6.0
 + pygments==2.19.2
 + pytest==9.0.2
 + pytest-cov==7.0.0
```

### Package Imports Work

```python
from gridcore import Grid, GridEngine, GridConfig, TickerEvent, PlaceLimitIntent
# ✓ All imports successful
```

## Backwards Compatibility

The project still works with pip if needed:

```bash
# Using pip (from packages/gridcore/)
pip install -e .
pip install pytest pytest-cov
PYTHONPATH=./src pytest tests/
```

However, **uv is now the recommended approach** for development.

## Files Modified

- Created: `pyproject.toml` (root)
- Created: `uv.lock` (lockfile, committed to git)
- Modified: `packages/gridcore/pyproject.toml`
- Modified: `.gitignore` (removed uv.lock, kept .venv/)
- Modified: `README.md`
- Modified: `packages/gridcore/README.md`
- Modified: `RULES.md`
- Created: `docs/UV_MIGRATION.md`
- Created: `.venv/` (not committed, in .gitignore)

## Next Steps

1. Team members should run `uv sync` after pulling these changes
2. CI/CD pipelines should be updated to use uv (if applicable)
3. Consider adding more packages to the workspace as the project grows

## References

- [uv Documentation](https://github.com/astral-sh/uv)
- [uv Workspace Guide](https://docs.astral.sh/uv/concepts/projects/)
- [Python Packaging with uv](https://docs.astral.sh/uv/guides/projects/)
