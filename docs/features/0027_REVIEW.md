# 0027 Review

## Findings

::code-comment{title="[P2] Propagate shared ratio to the linked position" body="The implementation computes the bbu2-aligned long.margin / short.margin value, but only assigns it to self.position_ratio. The plan explicitly calls for assigning the shared_ratio to both self and self._opposite, matching bbu2's invariant that both Position instances carry the same value. As written, the opposite manager keeps its previous ratio until its own calculate_amount_multiplier call runs, and if only one side is calculated or any caller inspects the linked object between calls, the two managers disagree despite the comments saying the value is shared. Assign the computed ratio to self._opposite.position_ratio as well when linked, and add a test that checks the opposite manager immediately after a single calculation." file="/Users/erzhan/DATA/PROJ/grid-bot-validation/packages/gridcore/src/gridcore/position.py" start=230 end=230 priority=2 confidence=0.86}

## Notes

- The short-rule ordering now matches the requested bbu2 order: emergency high-liq, moderate-liq hedge, low-margin, ratio martingale, extreme ratio martingale.
- The new regression tests cover the production replay, short moderate-liq precedence, and the martingale-only short case.
- The new `TestPositionRulesBBU2Alignment` class has 9 tests, not the 10-case checklist from the plan. Existing long-side boundary tests cover part of the boundary requirement, but the plan's explicit long-side mirror for the extreme no-upnl-gate case is not present in the new alignment group.

## Verification

- `uv run pytest -q packages/gridcore/tests/test_position.py packages/gridcore/tests/test_comparison.py` — 118 passed, 1 skipped.
- `uv run ruff check packages/gridcore/src/gridcore/position.py` — passed.
- `uv run pytest packages/gridcore/tests/ apps/gridbot/tests/ -q` — 774 passed, 1 skipped.
