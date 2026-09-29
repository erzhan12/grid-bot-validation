"""Recorded-data quality errors shared by every consumer of recorded rows."""


class RecordedDataQualityError(Exception):
    """Recorded rows are incomplete in a way that makes arithmetic unsafe.

    Raised when a consumer (replay, backtest, comparator, live-check) meets a
    recorded value that is UNKNOWN rather than zero — e.g. a
    ``PrivateExecution.closed_pnl`` that is NULL because the row came from a
    REST backfill whose endpoint does not report per-execution PnL. Callers
    that validate recordings turn it into a SKIP; it must never be coerced
    to ``Decimal("0")``.
    """
