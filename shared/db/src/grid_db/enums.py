from enum import StrEnum

class RunType(StrEnum):
    """Run execution mode."""
    LIVE = "live"
    BACKTEST = "backtest"
    SHADOW = "shadow"
