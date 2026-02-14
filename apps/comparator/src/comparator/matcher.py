"""Trade matcher for joining live and backtest trades.

Matches on client_order_id (deterministic SHA256 hash shared by gridbot and backtest).
"""

import logging
from dataclasses import dataclass

from comparator.loader import NormalizedTrade

logger = logging.getLogger(__name__)


@dataclass
class MatchedTrade:
    """A pair of live and backtest trades with the same client_order_id."""

    live: NormalizedTrade
    backtest: NormalizedTrade


@dataclass
class MatchResult:
    """Result of matching live vs backtest trades."""

    matched: list[MatchedTrade]
    live_only: list[NormalizedTrade]
    backtest_only: list[NormalizedTrade]


class TradeMatcher:
    """Joins live and backtest trades on client_order_id."""

    def match(
        self,
        live_trades: list[NormalizedTrade],
        backtest_trades: list[NormalizedTrade],
    ) -> MatchResult:
        """Match live trades against backtest trades.

        Args:
            live_trades: Normalized live trades.
            backtest_trades: Normalized backtest trades.

        Returns:
            MatchResult with matched pairs, live-only, and backtest-only lists.
        """
        # Use (client_order_id, occurrence) as composite key to handle
        # deterministic ID reuse across order lifecycles.
        live_by_key = {
            (t.client_order_id, t.occurrence): t for t in live_trades
        }
        backtest_by_key = {
            (t.client_order_id, t.occurrence): t for t in backtest_trades
        }

        live_keys = set(live_by_key.keys())
        backtest_keys = set(backtest_by_key.keys())

        matched_keys = live_keys & backtest_keys
        live_only_keys = live_keys - backtest_keys
        backtest_only_keys = backtest_keys - live_keys

        matched = [
            MatchedTrade(live=live_by_key[k], backtest=backtest_by_key[k])
            for k in sorted(matched_keys)
        ]

        live_only = [live_by_key[k] for k in sorted(live_only_keys)]
        backtest_only = [backtest_by_key[k] for k in sorted(backtest_only_keys)]

        logger.info(
            "Match result: %d matched, %d live-only, %d backtest-only",
            len(matched),
            len(live_only),
            len(backtest_only),
        )

        return MatchResult(
            matched=matched,
            live_only=live_only,
            backtest_only=backtest_only,
        )
