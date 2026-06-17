"""Intent executor for converting strategy intents to Bybit API calls.

The executor is the bridge between the pure strategy logic (gridcore)
and the exchange. It handles the actual order placement and cancellation.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Callable, Optional

from bybit_adapter.rest_client import BybitRestClient
from bybit_adapter.error_codes import (
    INSUFFICIENT_BALANCE,
    ORDER_LINK_ID_DUPLICATE,
    ORDER_QTY_TRUNCATED_TO_ZERO,
)
from gridcore.intents import PlaceLimitIntent, CancelIntent
from gridcore.position import DirectionType
from gridbot.order_link_id import make_order_link_id
from gridbot.safety_caps import SafetyCaps


logger = logging.getLogger(__name__)

# Feature 0079 (issue #182) — throttle the C4 rate-limit WARNING so a sustained
# rate-limit (every queued/re-dispatched intent rejected) does not flood the log.
_RATE_LIMIT_WARN_THROTTLE_SEC = 60.0

# Bybit error codes that indicate auth/permission problems (not retryable)
AUTH_ERROR_CODES = {10003, 10004, 10005, 33004}

# Matches both our _check_response format [NNNNN] and pybit's native format (ErrCode: NNNNN)
_ERR_CODE_RE = re.compile(r"(?:\[(\d+)\]|\(ErrCode:\s*(\d+)\))")


def is_truncate_error(error: Optional[str]) -> bool:
    """Return True if an error string carries Bybit ErrCode 110017.

    110017 ("orderQty will be truncated to zero") means a reduce-only qty was
    clamped to zero against a smaller exchange-side position — the local mirror
    diverged. Module-level (not a method) so callers branch on it without
    routing through a mock executor instance (feature 0064). Reuses the shared
    ``_ERR_CODE_RE`` so there is one regex for both wire formats.
    """
    if not error:
        return False
    match = _ERR_CODE_RE.search(error)
    if match:
        code = int(match.group(1) or match.group(2))
        return code == ORDER_QTY_TRUNCATED_TO_ZERO
    return False


def is_insufficient_balance(error: Optional[str]) -> bool:
    """Return True if an error string carries Bybit ErrCode 110007.

    110007 ("available balance not enough for new order") means the account's
    free margin cannot cover an OPEN order. Module-level (mirrors
    ``is_truncate_error``) so the runner branches on it without routing through
    a mock executor instance — feature 0066 / issue #159. Reuses the shared
    ``_ERR_CODE_RE`` for both wire formats.
    """
    if not error:
        return False
    match = _ERR_CODE_RE.search(error)
    if match:
        code = int(match.group(1) or match.group(2))
        return code == INSUFFICIENT_BALANCE
    return False


# Feature 0069 (issue #151) — narrow lowercased-token test for transient
# network/transport failures surfaced as the str(exception) from execute_place.
# NARROW on purpose: must NOT match a bare "error" or a digit, so it never
# swallows a real ErrCode classification. "connection" covers ConnectionError;
# "readtimeout"/"timeout" cover pybit's ReadTimeout.
_NETWORK_ERROR_TOKENS = (
    "timeout",
    "connection",
    "temporarily unavailable",
    "readtimeout",
)


def is_network_error(error: Optional[str]) -> bool:
    """Return True if an error string looks like a transient network failure.

    Counts toward the state-divergence detector's sustained placement-failure
    UNION (feature 0069 / issue #151) alongside ``is_truncate_error`` (110017)
    and ``is_duplicate_link_error`` (110072). Module-level for isolated
    testability — no inline substring checks at the call site. Deliberately
    narrow: a bare ``"error"`` or a stray digit must NOT match.
    """
    if not error:
        return False
    lowered = error.lower()
    return any(token in lowered for token in _NETWORK_ERROR_TOKENS)


def is_duplicate_link_error(error: Optional[str]) -> bool:
    """Return True if an error string carries Bybit ErrCode 110072.

    110072 ("OrderLinkedID is duplicate") means a re-sent order reused a still-
    cached orderLinkId, or a REST retry landed an order whose first ack never
    arrived via WS. Matches the numeric code (via the shared ``_ERR_CODE_RE``
    for both wire formats) OR the literal wording. Module-level (mirrors
    ``is_truncate_error``) so the runner branches without a mock executor —
    feature 0069 / issue #151. Replaces the inline 110072 substring checks.
    """
    if not error:
        return False
    if "orderlinkedid is duplicate" in error.lower():
        return True
    match = _ERR_CODE_RE.search(error)
    if match:
        code = int(match.group(1) or match.group(2))
        return code == ORDER_LINK_ID_DUPLICATE
    return False


@dataclass
class OrderResult:
    """Result of order placement attempt."""

    success: bool
    order_id: Optional[str] = None
    order_link_id: Optional[str] = None
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


@dataclass
class CancelResult:
    """Result of order cancellation attempt."""

    success: bool
    error: Optional[str] = None
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)


class IntentExecutor:
    """Executes trading intents against Bybit API.

    Converts PlaceLimitIntent and CancelIntent objects from gridcore
    into actual API calls to Bybit.

    In shadow mode, logs intents without executing them.

    Example:
        client = BybitRestClient(api_key="...", api_secret="...", testnet=True)
        executor = IntentExecutor(client, shadow_mode=False)

        result = executor.execute_place(intent)
        if result.success:
            print(f"Order placed: {result.order_id}")
    """

    def __init__(
        self,
        rest_client: BybitRestClient,
        shadow_mode: bool = False,
        position_idx_long: int = 1,
        position_idx_short: int = 2,
        max_auth_failures: int = 5,
        on_cooldown_entered: Optional[Callable[[], None]] = None,
        safety_caps: Optional[SafetyCaps] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Initialize executor.

        Args:
            rest_client: Bybit REST client for API calls.
            shadow_mode: If True, log intents without executing.
            position_idx_long: Position index for long direction (hedge mode).
            position_idx_short: Position index for short direction (hedge mode).
            max_auth_failures: Consecutive auth errors before entering cooldown.
            on_cooldown_entered: Callback fired when auth cooldown activates.
            safety_caps: Optional shared ``SafetyCaps`` (feature 0079 / issue
                #182). The orchestrator passes the SAME instance it gives the
                StrategyRunner so the C4 rate-limit window and the runner's
                C1/C2/C3 share one source of truth. When None (direct/test
                callers) C4 is inert.
            clock: Monotonic clock for the C4 window; MUST be the same callable
                the shared SafetyCaps uses (the orchestrator passes one value to
                both). Defaults to ``time.monotonic``; injectable for tests.
        """
        self._client = rest_client
        self._shadow_mode = shadow_mode
        self._position_idx_long = position_idx_long
        self._position_idx_short = position_idx_short
        self._max_auth_failures = max_auth_failures
        self._auth_failure_count = 0
        self._auth_cooldown = False
        self._on_cooldown_entered = on_cooldown_entered
        # Feature 0079 (issue #182) — C4 max-orders-per-minute rate limit. The
        # executor is the single live-submit choke point (catches retry-queue
        # re-dispatch, which bypasses the runner). Inert when safety_caps is None.
        self._safety_caps = safety_caps
        self._clock = clock
        self._rate_limit_warn_last: float = 0.0

    @property
    def shadow_mode(self) -> bool:
        """Whether executor is in shadow mode."""
        return self._shadow_mode

    @property
    def auth_cooldown(self) -> bool:
        """Whether executor is in auth error cooldown."""
        return self._auth_cooldown

    @property
    def auth_failure_count(self) -> int:
        """Number of consecutive auth failures."""
        return self._auth_failure_count

    def reset_auth_cooldown(self) -> None:
        """Reset auth cooldown state. Called by orchestrator after cooldown expires."""
        self._auth_failure_count = 0
        self._auth_cooldown = False
        logger.info("Auth cooldown reset, resuming order execution")

    @staticmethod
    def _is_auth_error(error: str) -> bool:
        """Check if error string contains a Bybit auth error code."""
        match = _ERR_CODE_RE.search(error)
        if match:
            code = int(match.group(1) or match.group(2))
            return code in AUTH_ERROR_CODES
        return False

    def _handle_error(self, error: str) -> None:
        """Track auth errors and enter cooldown if threshold reached."""
        if self._is_auth_error(error):
            self._auth_failure_count += 1
            logger.warning(
                f"Auth error ({self._auth_failure_count}/{self._max_auth_failures}): {error}"
            )
            if self._auth_failure_count >= self._max_auth_failures and not self._auth_cooldown:
                self._auth_cooldown = True
                logger.error(
                    f"Auth cooldown activated after {self._auth_failure_count} consecutive failures"
                )
                if self._on_cooldown_entered:
                    self._on_cooldown_entered()
        else:
            self._auth_failure_count = 0

    def execute_place(self, intent: PlaceLimitIntent) -> OrderResult:
        """Execute a place order intent.

        Args:
            intent: PlaceLimitIntent from strategy.

        Returns:
            OrderResult with success status and order_id if successful.
        """
        unique_link_id = intent.order_link_id or make_order_link_id(
            intent.client_order_id
        )

        if self._shadow_mode:
            logger.info(
                f"[SHADOW] Would place {intent.side} order: "
                f"{intent.symbol} qty={intent.qty} price={intent.price} "
                f"reduce_only={intent.reduce_only} "
                f"client_id={intent.client_order_id} link_id={unique_link_id}"
            )
            return OrderResult(
                success=True,
                order_id=f"shadow_{intent.client_order_id}",
                order_link_id=unique_link_id,
            )

        # Feature 0079 (issue #182) — C4 rate limit. Checked AFTER the shadow
        # early-return (so shadow placements never consume the window) and
        # BEFORE the real submit, on EVERY caller (runner first-dispatch AND
        # retry-queue re-dispatch), so a rate-limited order never reaches Bybit.
        # Returns the non-retryable "safety_cap_rate_limit" sentinel: the runner
        # drops it on first dispatch (never enqueues — see _execute_place_intent);
        # an already-enqueued item that trips this on re-dispatch is not
        # re-submitted and simply exhausts its bounded retry budget.
        if self._safety_caps is not None:
            now = self._clock()  # read once: same instant gates the window + throttle
            if self._safety_caps.rate_limited(now):
                if (now - self._rate_limit_warn_last) >= _RATE_LIMIT_WARN_THROTTLE_SEC:
                    self._rate_limit_warn_last = now
                    logger.warning(
                        f"Safety cap rate limit: dropping {intent.side} order "
                        f"{intent.symbol} qty={intent.qty} price={intent.price} "
                        f"link_id={unique_link_id} (not submitted, not enqueued)"
                    )
                return OrderResult(
                    success=False,
                    order_link_id=unique_link_id,
                    error="safety_cap_rate_limit",
                )

        try:
            # Determine position index based on direction
            position_idx = self._get_position_idx(intent.direction)

            # HOTFIX 2026-05-08: Bybit caches orderLinkId past order lifetime
            # (~1-2h after cancel/fill), so re-placing the same logical intent
            # triggers ErrCode 110072 "OrderLinkedID is duplicate" in a tight loop.
            # The runner assigns one wire id per placement lifecycle so retries
            # remain idempotent; direct callers fall back to the generated id above.
            result = self._client.place_order(
                symbol=intent.symbol,
                side=intent.side,
                order_type="Limit",
                qty=str(intent.qty),
                price=str(intent.price),
                reduce_only=intent.reduce_only,
                position_idx=position_idx,
                order_link_id=unique_link_id,
                # Feature 0066 (issue #159): maker-only for chase-close orders.
                # Default GTC == today's implicit behavior for every other order.
                time_in_force="PostOnly" if intent.post_only else "GTC",
            )

            order_id = result.get("orderId")
            logger.info(
                f"Placed {intent.side} order: {intent.symbol} "
                f"qty={intent.qty} price={intent.price} "
                f"order_id={order_id} link_id={unique_link_id}"
            )

            self._auth_failure_count = 0
            # Feature 0079 — count this accepted real submission toward the C4
            # trailing-60s window (only real successes, never shadow).
            if self._safety_caps is not None:
                self._safety_caps.record_accepted_submission(self._clock())
            return OrderResult(
                success=True,
                order_id=order_id,
                order_link_id=unique_link_id,
            )

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            self._handle_error(str(e))
            return OrderResult(
                success=False,
                order_link_id=unique_link_id,
                error=str(e),
            )

    def execute_cancel(self, intent: CancelIntent) -> CancelResult:
        """Execute a cancel order intent.

        Args:
            intent: CancelIntent from strategy.

        Returns:
            CancelResult with success status.
        """
        if self._shadow_mode:
            logger.info(
                f"[SHADOW] Would cancel order: {intent.symbol} "
                f"order_id={intent.order_id} reason={intent.reason}"
            )
            return CancelResult(success=True)

        try:
            success = self._client.cancel_order(
                symbol=intent.symbol,
                order_id=intent.order_id,
            )

            if success:
                logger.info(
                    f"Cancelled order: {intent.symbol} "
                    f"order_id={intent.order_id} reason={intent.reason}"
                )
                self._auth_failure_count = 0
            else:
                logger.warning(
                    f"Cancel returned False: {intent.symbol} "
                    f"order_id={intent.order_id} (may already be filled/cancelled)"
                )

            return CancelResult(success=success)

        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            self._handle_error(str(e))
            return CancelResult(success=False, error=str(e))

    def execute_batch(
        self,
        intents: list[PlaceLimitIntent | CancelIntent],
    ) -> list[OrderResult | CancelResult]:
        """Execute a batch of intents sequentially.

        Args:
            intents: List of PlaceLimitIntent or CancelIntent.

        Returns:
            List of results in same order as intents.
        """
        results = []

        for intent in intents:
            if isinstance(intent, PlaceLimitIntent):
                result = self.execute_place(intent)
            elif isinstance(intent, CancelIntent):
                result = self.execute_cancel(intent)
            else:
                logger.warning(f"Unknown intent type: {type(intent)}")
                continue

            results.append(result)

        return results

    def _get_position_idx(self, direction: str) -> int:
        """Get position index for direction (hedge mode).

        Args:
            direction: 'long' or 'short'

        Returns:
            Position index (1 for long, 2 for short in hedge mode)

        Reference:
            bbu_reference/bbu2-master/bybit_api_usdt.py:275-280
        """
        if direction == DirectionType.LONG:
            return self._position_idx_long
        elif direction == DirectionType.SHORT:
            return self._position_idx_short
        return 0  # One-way mode
