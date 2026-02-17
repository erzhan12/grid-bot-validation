# 0007 Review — Phase H: Testing & Validation (Re-Review)

## Findings (Ordered by Severity)

### [P3] One remaining lint issue in integration test file
`Order` is imported but never used in the new EventSaver DB integration test module.

Evidence:
- `tests/integration/test_eventsaver_db.py:19`
- Repro: `uv run ruff check tests/integration/test_eventsaver_db.py`

Impact:
- Low risk; no runtime behavior impact, but adds avoidable lint noise.

---

### [P3] Root `pytest` invocation is still not usable due `ImportPathMismatchError`
Even with updated `testpaths`, running all tests via root `pytest` still fails because multiple package-level `tests/conftest.py` files collide.

Evidence:
- Repro: `uv run pytest -q`
- Error observed: `_pytest.pathlib.ImportPathMismatchError`

Impact:
- Developer ergonomics issue: contributors must continue using `make test`/per-directory commands rather than root `pytest`.

## Resolved Since Previous Review
- `apps/event_saver` strict-warning issue resolved: `uv run pytest apps/event_saver/tests/test_main.py -q -W error` now passes.
- `apps/gridbot` strict-warning issue resolved: `uv run pytest apps/gridbot/tests/test_main.py -q -W error` now passes.
- CLI async test contract issue resolved (`AsyncMock` + coroutine cleanup added).
- EventSaver DB integration scope now includes `OrderWriter` end-to-end tests:
  - `tests/integration/test_eventsaver_db.py:404`
- WebSocket coverage target remains exceeded:
  - `packages/bybit_adapter/src/bybit_adapter/ws_client.py`: 90%
- Integration suite remains green under strict warnings:
  - `uv run pytest tests/integration/ -q -W error` passes.

## Verification Commands Run
- `uv run pytest packages/bybit_adapter/tests/test_rest_client.py packages/bybit_adapter/tests/test_ws_client.py -q -W error`
- `uv run pytest apps/event_saver/tests/test_main.py -q`
- `uv run pytest apps/event_saver/tests/test_main.py -q -W error`
- `uv run pytest apps/gridbot/tests/test_main.py -q`
- `uv run pytest apps/gridbot/tests/test_main.py -q -W error`
- `uv run pytest apps/event_saver/tests/test_public_collector.py apps/event_saver/tests/test_private_collector.py -q -W error`
- `uv run pytest tests/integration/ -q -W error`
- `uv run ruff check apps/event_saver/tests/test_main.py apps/event_saver/tests/test_public_collector.py apps/event_saver/tests/test_private_collector.py apps/gridbot/tests/test_main.py tests/integration/test_engine_to_executor.py tests/integration/test_runner_lifecycle.py tests/integration/test_eventsaver_db.py`
