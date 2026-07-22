---
paths:
  - "apps/pnl_checker/**"
---

## pnl_checker — Live PnL Validation

**Path**: `apps/pnl_checker/`

Read-only tool comparing our PnL/margin calculations against Bybit exchange values.

### Key Rules

- Use `pos.mark_price` (position endpoint) NOT `ticker.mark_price` for unrealized PnL
- Funding data is informational only (no tolerance check)
- Rate limiting: 10 req/sec (well under Bybit's 50)
- `BYBIT_API_KEY`/`BYBIT_API_SECRET` env vars override YAML config
- `liqPrice` can be empty string — use `Decimal(pos.get("liqPrice", "0") or "0")`
- Initial Margin comparison will show FAIL in hedge mode (expected — Bybit UTA uses mark_price + hedge optimization)
- **Division guard constants**: `MIN_POSITION_IM` and `MIN_LEVERAGE` in `calculator.py` prevent division by near-zero values. Warnings are logged when these guards activate.
- **Symbol validation**: `_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{4,20}$")` in `config.py`. Bybit symbols are uppercase alphanumeric only.
- **`get_transaction_log_all()` return type**: Returns `tuple[list[dict], bool]` — the bool indicates whether data was truncated at `max_pages`. Callers must handle the truncation flag.
- **Config redaction**: `_redact_config()` in `reporter.py` replaces API credentials with `[REDACTED]` before writing to JSON output. Never serialize raw `AccountConfig` to files.
- **Tolerance scaling for percentages**: PnL % values are 100x USDT values. Use `PERCENTAGE_TOLERANCE_MULTIPLIER = 100` in `comparator.py` to scale tolerance for ROE comparisons.
- **Workspace dependency**: `pnl-checker` must be in root `pyproject.toml` dev deps AND `tool.uv.sources` for test discovery to work.

---

