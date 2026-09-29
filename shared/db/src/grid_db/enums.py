from enum import StrEnum

class RunType(StrEnum):
    """Run execution mode."""
    LIVE = "live"
    BACKTEST = "backtest"
    SHADOW = "shadow"


class RecoveryStatus(StrEnum):
    """Outcome of a private-stream gap's REST execution recovery (0110)."""
    PENDING = "pending"  # gap recorded, recovery not finished
    RECOVERED = "recovered"  # complete query persisted (zero rows is fine)
    SKIPPED = "skipped"  # gap below the reconcile threshold, not queried
    TRUNCATED = "truncated"  # max_pages hit with a cursor left; not persisted
    # REST/DB/conversion error, no run_id, cancelled or crashed recovery,
    # a dropped Trade row, or a window Bybit cannot query
    FAILED = "failed"
