[X] proceed with 0001_PLAN
[X] bbu2 has a new md file to check the logic
[X] think about grid.py write_to_db, read_from_db and logger , do I need them or not.
[X] continue with 0002_plan implement (fixed db review issues)
[X] Proceed testing Phase D manual s
[X] Add risk calculations to backtest (integrate gridcore.Position risk multipliers with margin=positionValue/walletBalance)
[X] Move margin ratio calculation to gridcore (currently inline in gridbot runner.py:477-478 and pnl_checker calculator.py)
[X] Move position_value calculation in gridbot runner.py to use gridcore.pnl.calc_position_value
[X] Wire order qty computation: engine sets qty=Decimal('0') (gridcore/engine.py:367), expects execution layer to resolve from config `amount` (e.g. "x0.001" wallet fraction) and position.amount_multiplier — currently nothing fills it in, so all orders hit the exchange with qty=0
[ ] 0017 P1: configure REST timeout in packages/bybit_adapter/src/bybit_adapter/rest_client.py:81 (HTTP(...) currently has no timeout). Precondition for removing asyncio.wait_for in orchestrator.py — without it, a hung socket can block the sync main loop indefinitely. Discuss value (~10s query / ~15s order) and how to plumb it through pybit's HTTP wrapper before starting 0017 refactor.
