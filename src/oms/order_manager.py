"""Per-portfolio OMS coordinator.

Lifecycle owned by this module (docs/OMS/OMS_DESIGN.md §5.2):

    process_order()          register a ParentOrder, pick the execution
                             algorithm (TWAP/VWAP/MARKET), generate the
                             ChildOrder schedule, enqueue it
    manage_order()           the pump: release due children from the
                             scheduler, execute them through the injected
                             ``execute_child`` callable, apply fills,
                             retry-once-then-cancel failures, finalize
                             exhausted parents
    on_child_filled()        fill accounting (parent avg price / status)
    cancel_order()           cancel a parent + its queued children

What this module deliberately does NOT do:

  * Position sizing — the executor's ``default_trade_size`` owns
    buying-power/margin math; ``process_order`` receives a final
    ``total_quantity``.
  * Touching an executor directly — execution is injected per pump call as
    ``execute_child`` (see ``manage_order``). This keeps the OMS agnostic of
    the pipeline and, in live trading, avoids attaching per-portfolio state
    to the *shared* ``tradeExecutor`` (which multiple portfolio threads use
    concurrently — see CLAUDE.md "OMS" section before changing this).
  * DB persistence — orders live in memory. The design doc's
    ``oms_parent_orders`` / ``oms_child_orders`` tables (§6) are a future
    layer that should hook ``process_order`` / ``on_child_filled`` /
    ``_finalize_parents``.

The pump is wired in both pipelines: ``BacktestRunner._run_event_loop``
pumps per bar with simulated time; ``RunEngine._run_oms_pump`` (a dedicated
thread, default 5s tick) pumps with wall-clock NY time. Both inject
``executor.execute_child_order`` as the ``execute_child`` callable — it
receives one ``ChildOrder`` and returns
``{"status": "success", "filled_quantity": <float>, "fill_price": <float>}``
on success. The live adapter (``RunEngine._make_child_executor``) fetches
portfolio state fresh per fill; never bind submit-time state into the
callable.
"""

from src.oms.order_structs import (
    AlgoType,
    ChildOrder,
    OrderStatus,
    ParentOrder,
    Side,
    tz,
)
from src.oms.scheduler import Scheduler
from src.oms.sizing import build_algorithm
from datetime import datetime
from typing import Callable, Iterable, Optional

import logging
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# A child order that errors is retried this many times (design doc §5.6:
# "failed child orders are retried once, then marked CANCELLED").
MAX_CHILD_RETRIES = 1

# Child statuses after which the OMS will never execute the child again.
_TERMINAL_CHILD_STATUSES = (
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
)


def _normalize_side(signal_type: Side | str) -> Side:
    if isinstance(signal_type, Side):
        return signal_type
    if isinstance(signal_type, str):
        normalized = signal_type.strip().upper()
        if normalized == "BUY":
            return Side.BUY
        if normalized == "SELL":
            return Side.SELL
    raise ValueError(f"Invalid signal_type: {signal_type}")


def _build_parent(
    portfolio_id: str,
    ticker: str,
    signal_type: Side | str,
    confidence: float,
    total_quantity: float,
    algo_type: AlgoType,
    status: OrderStatus,
    duration_minutes: int,
    arrival_price: float,
    timestamp: Optional[datetime] = None,
) -> ParentOrder:
    parent = ParentOrder()
    parent.portfolio_id = portfolio_id
    parent.ticker = ticker
    parent.signal_type = _normalize_side(signal_type)
    parent.confidence = float(confidence)
    parent.total_quantity = float(total_quantity)
    parent.filled_quantity = 0.0
    parent.algo_type = algo_type
    parent.status = status
    parent.arrival_price = float(arrival_price)
    parent.avg_fill_price = 0.0
    parent.duration_minutes = int(duration_minutes)
    if timestamp is None:
        timestamp = datetime.now(tz)
    parent.created_at = timestamp
    parent.updated_at = timestamp
    return parent


class OrderManager:
    def __init__(self, portfolio_id: str, config: Optional[dict] = None):
        self.portfolio_id = portfolio_id
        self.scheduler = Scheduler()
        self.tracker = logging.getLogger(
            f"{portfolio_id}_tracking_logger"
        )
        # The portfolio's ``OMS`` config block (see design doc §8):
        # default_algo, duration_minutes, twap_num_slices,
        # vwap_bucket_minutes, min_order_notional, fallback_to_market.
        self.config = config or {}
        self.orders: list[ParentOrder] = []
        self.child_orders: list[ChildOrder] = []
        self.parent_order_ids: set[str] = set()
        self.orders_by_id: dict[str, ParentOrder] = {}
        # Every child ever generated for a parent, terminal or not — the
        # authoritative record for finalization and execution-quality
        # reporting (the scheduler's heap only holds *queued* children).
        self.children_by_parent: dict[str, list[ChildOrder]] = {}
        # Warn only once per manager if the pump runs without an execution
        # seam, so a mis-wired engine doesn't spam the log every poll.
        self._warned_no_executor = False
        # In live trading the portfolio thread calls process_order/cancel_order
        # while the engine's OMS pump thread calls manage_order concurrently.
        # Reentrant because manage_order -> on_child_filled both lock. Backtest
        # is single-threaded per portfolio; the uncontended lock is ~free.
        self._lock = threading.RLock()

    def _tracker_info(self, message: str, *args):
        if self.tracker is None:
            return
        if hasattr(self.tracker, "info"):
            self.tracker.info(message, *args)

    # ------------------------------------------------------------------
    # Algorithm selection
    # ------------------------------------------------------------------

    def _resolve_algo_type(
        self,
        requested: Optional[AlgoType],
        ticker: str,
        order_notional: float,
    ) -> AlgoType:
        """Pick the execution algorithm for one order.

        Precedence: explicit per-order ``algo_type`` argument > the
        portfolio's ``OMS.default_algo`` > MARKET. Two downgrades apply on
        top:

          * Orders below ``min_order_notional`` always go MARKET — slicing a
            sub-threshold order just multiplies per-fill overhead.
          * An unknown/unimplemented algo name falls back to MARKET when
            ``fallback_to_market`` is true (the default, and every current
            portfolio config sets it); otherwise it is a hard error so a
            typo in config cannot silently change execution style.
        """
        fallback_to_market = bool(self.config.get("fallback_to_market", True))

        algo_type = requested
        if algo_type is None:
            default_algo = str(self.config.get("default_algo", "MARKET")).upper()
            try:
                algo_type = AlgoType(default_algo)
            except ValueError:
                if not fallback_to_market:
                    raise ValueError(
                        f"Unknown OMS.default_algo '{default_algo}' for "
                        f"portfolio {self.portfolio_id}"
                    )
                logger.warning(
                    "Unknown OMS.default_algo '%s'; falling back to MARKET.",
                    default_algo,
                )
                algo_type = AlgoType.MARKET

        min_order_notional = float(self.config.get("min_order_notional", 0.0))
        if (
            algo_type != AlgoType.MARKET
            and min_order_notional > 0
            and order_notional < min_order_notional
        ):
            logger.info(
                "Order for %s (notional %.2f) below min_order_notional %.2f; "
                "downgrading %s -> MARKET.",
                ticker,
                order_notional,
                min_order_notional,
                algo_type.value,
            )
            algo_type = AlgoType.MARKET

        return algo_type

    def _generate_children(self, parent_order: ParentOrder) -> list[ChildOrder]:
        """Run the execution algorithm; honor ``fallback_to_market`` when the
        selected algo has no implementation yet (LIMIT/STOP)."""
        try:
            algorithm = build_algorithm(parent_order.algo_type, self.config)
        except NotImplementedError:
            if not bool(self.config.get("fallback_to_market", True)):
                raise
            logger.warning(
                "AlgoType.%s has no implementation; executing parent %s as "
                "MARKET (fallback_to_market=true).",
                parent_order.algo_type.name,
                parent_order.order_id,
            )
            parent_order.algo_type = AlgoType.MARKET
            algorithm = build_algorithm(AlgoType.MARKET, self.config)
        return algorithm.generate_schedule(
            parent_order,
            volume_profile=getattr(parent_order, "_volume_profile", None),
        )

    # ------------------------------------------------------------------
    # Order intake
    # ------------------------------------------------------------------

    def process_order(
        self,
        portfolio_id: str,
        ticker: str,
        side: Side | str,
        confidence: float,
        arrival_price: float,
        total_quantity: float,
        timestamp: Optional[datetime] = None,
        algo_type: Optional[AlgoType] = None,
        duration_minutes: Optional[int] = None,
        volume_profile: Optional[list[float]] = None,
    ) -> ParentOrder:
        """Register a parent order from a quantity the caller has already sized,
        generate its child-order schedule, and enqueue it.

        The executor owns position sizing and buying-power constraints; the OMS
        takes the resulting quantity, selects the execution algorithm, slices
        the order into scheduled children, and tracks the lifecycle. Nothing is
        executed here — fills happen when ``manage_order`` releases due
        children. Returns the created ParentOrder (status WORKING).

        ``volume_profile`` is the optional per-bucket weight vector for VWAP
        (see ``sizing/vwap.py`` for the expected shape and the query a future
        provider should use); TWAP/MARKET ignore it.
        """
        if portfolio_id != self.portfolio_id:
            raise ValueError(
                f"OrderManager for portfolio {self.portfolio_id} cannot process orders for {portfolio_id}"
            )
        if duration_minutes is None:
            duration_minutes = int(self.config.get("duration_minutes", 30))

        if arrival_price is None or arrival_price <= 0:
            raise ValueError(f"Invalid arrival_price for {ticker}: {arrival_price}")

        if total_quantity is None or total_quantity <= 0:
            raise ValueError(
                f"Invalid total_quantity for {ticker}: {total_quantity}"
            )

        algo_type = self._resolve_algo_type(
            requested=algo_type,
            ticker=ticker,
            order_notional=float(total_quantity) * float(arrival_price),
        )

        parent_order = _build_parent(
            portfolio_id=portfolio_id,
            ticker=ticker,
            signal_type=side,
            confidence=confidence,
            total_quantity=total_quantity,
            algo_type=algo_type,
            status=OrderStatus.PENDING,
            duration_minutes=duration_minutes,
            arrival_price=arrival_price,
            timestamp=timestamp,
        )
        # Stash the profile on the parent (private, in-memory only) so the
        # algorithm sees it without widening the BaseAlgorithm interface.
        parent_order._volume_profile = volume_profile
        self.tracker.info(
            "Order Created order for %s %s qty=%s timestamp=%s",
            parent_order.order_id,
            parent_order.signal_type.value,
            parent_order.total_quantity,
            parent_order.created_at.isoformat()
        )

        # Registration + scheduling under the manager lock so the live pump
        # thread never observes a half-registered parent.
        with self._lock:
            if parent_order.order_id in self.parent_order_ids:
                raise ValueError(f"Duplicate parent order ID: {parent_order.order_id}")

            self.parent_order_ids.add(parent_order.order_id)
            self.orders.append(parent_order)
            self.orders_by_id[parent_order.order_id] = parent_order

            # Slice the order and queue the schedule. From here on the parent
            # is WORKING and the pump (manage_order) drives it to a terminal
            # state.
            children = self._generate_children(parent_order)
            self.children_by_parent[parent_order.order_id] = children
            self.child_orders.extend(children)
            self.scheduler.schedule_order(parent_order)
            self.scheduler.enqueue_children(children)
            parent_order.status = OrderStatus.WORKING
            parent_order.updated_at = parent_order.created_at

        self.tracker.info(
            "Submitted order %s %s qty=%s algo=%s slices=%d",
            parent_order.order_id,
            parent_order.ticker,
            parent_order.total_quantity,
            parent_order.algo_type.value,
            len(children),
        )

        return parent_order

    # ------------------------------------------------------------------
    # The pump: execute due children
    # ------------------------------------------------------------------

    def manage_order(
        self,
        now: Optional[datetime] = None,
        execute_child: Optional[Callable[[ChildOrder], Optional[dict]]] = None,
    ) -> list[ChildOrder]:
        """Drive working orders toward completion. Call once per engine tick.

        Args:
            now: tz-aware current time (simulated time in backtest,
                wall-clock in live). Defaults to ``datetime.now(tz)``, which
                is only correct for live — backtests MUST pass simulated time
                or every slice fires immediately.
            execute_child: the execution seam. Receives one due
                ``ChildOrder`` and returns a dict with ``filled_quantity``
                (float > 0) and ``fill_price`` (float > 0) on success; any
                other return value (or an exception) counts as a failed
                attempt. The engine follow-up wraps its executor here — e.g.
                ``lambda child: executor.execute_child_order(child)`` — with
                whatever portfolio state (cash/positions) that executor
                needs already bound in.

        Failure policy (design doc §5.6): a failed child is re-enqueued and
        retried on a later tick, at most ``MAX_CHILD_RETRIES`` times, then
        marked CANCELLED. Because ``pop_due`` is called exactly once per
        pump, a same-tick retry loop is impossible.

        Concurrency: the manager lock is taken around bookkeeping but is
        deliberately NOT held across the ``execute_child`` call itself (which
        does price-fetch + DB I/O in live), so a portfolio thread can keep
        submitting orders while a slice executes.

        Returns the children whose execution was attempted this tick (useful
        for tests and telemetry).
        """
        if now is None:
            now = datetime.now(tz)

        if execute_child is None:
            # No execution seam wired: leave the queue intact (popping would
            # silently discard slices) and make the misconfiguration visible.
            if self.scheduler.pending_count() > 0 and not self._warned_no_executor:
                logger.warning(
                    "manage_order called without execute_child for portfolio "
                    "%s; %d queued child orders are NOT being executed. Wire "
                    "an execution callable (see module docstring).",
                    self.portfolio_id,
                    self.scheduler.pending_count(),
                )
                self._warned_no_executor = True
            return []

        attempted: list[ChildOrder] = []
        for child in self.scheduler.pop_due(now):
            with self._lock:
                parent_order = self.orders_by_id.get(child.parent_order_id)
                if parent_order is None or parent_order.is_complete:
                    # Parent finished (e.g. cancelled after this child was
                    # popped-and-retried): drop the slice.
                    child.status = OrderStatus.CANCELLED
                    child.updated_at = now
                    continue

            attempted.append(child)
            fill = None
            try:
                fill = execute_child(child)
            except Exception as e:
                logger.exception(
                    "execute_child raised for child %s (%s %s x%s): %s",
                    child.child_id,
                    child.signal_type.value,
                    child.ticker,
                    child.target_quantity,
                    e,
                )

            filled_qty, fill_price = self._parse_fill(fill)
            if filled_qty > 0 and fill_price > 0:
                self.on_child_filled(child, filled_qty, fill_price)
                continue

            # Failed attempt: retry on a later tick, then give up.
            if child.attempts < MAX_CHILD_RETRIES:
                child.attempts += 1
                child.updated_at = now
                self.scheduler.enqueue_children([child])
                self._tracker_info(
                    "Child %s failed (attempt %d/%d); re-queued.",
                    child.child_id,
                    child.attempts,
                    MAX_CHILD_RETRIES + 1,
                )
            else:
                child.status = OrderStatus.CANCELLED
                child.updated_at = now
                self._tracker_info(
                    "Child %s failed %d times; cancelled.",
                    child.child_id,
                    child.attempts + 1,
                )

        self._finalize_parents(now)
        return attempted

    @staticmethod
    def _parse_fill(fill) -> tuple[float, float]:
        """Extract (filled_quantity, fill_price) from an execute_child result.

        Contract is the dict described in ``manage_order``; ``quantity`` /
        ``exec_price`` are accepted as aliases because the existing executors'
        result dicts use those names. Anything unparseable is (0, 0) — i.e. a
        failed attempt — rather than an exception, so a malformed executor
        response degrades to the retry path instead of killing the pump.
        """
        if not isinstance(fill, dict):
            return 0.0, 0.0
        if fill.get("status") == "error":
            return 0.0, 0.0
        try:
            qty = float(fill.get("filled_quantity", fill.get("quantity", 0.0)))
            price = float(fill.get("fill_price", fill.get("exec_price", 0.0)))
        except (TypeError, ValueError):
            return 0.0, 0.0
        return qty, price

    def _finalize_parents(self, now: datetime):
        """Close out parents whose schedule is exhausted.

        A parent is exhausted when every generated child is terminal and
        nothing for it remains in the scheduler (a retrying child is
        non-terminal *and* queued, so it blocks finalization on both counts).
        Fully filled parents were already marked FILLED by on_child_filled;
        anything else that exhausted its schedule becomes EXPIRED, keeping
        whatever partial fill it achieved on the books.
        """
        with self._lock:
            for parent_order in self.orders:
                if parent_order.is_complete:
                    continue
                if parent_order.status not in (
                    OrderStatus.WORKING,
                    OrderStatus.PARTIALLY_FILLED,
                ):
                    continue
                children = self.children_by_parent.get(parent_order.order_id, [])
                if self.scheduler.has_pending(parent_order.order_id):
                    continue
                if any(c.status not in _TERMINAL_CHILD_STATUSES for c in children):
                    continue
                parent_order.status = OrderStatus.EXPIRED
                parent_order.updated_at = now
                self._tracker_info(
                    "Parent order %s expired at %.1f%% filled (%s/%s).",
                    parent_order.order_id,
                    parent_order.fill_pct * 100,
                    parent_order.filled_quantity,
                    parent_order.total_quantity,
                )

    # ------------------------------------------------------------------
    # Fill accounting / cancellation / queries
    # ------------------------------------------------------------------

    def on_child_filled(
        self,
        child_order: ChildOrder,
        filled_qty: float,
        fill_price: float,
    ) -> ParentOrder:
        with self._lock:
            parent_order = self.orders_by_id.get(child_order.parent_order_id)
            if parent_order is None:
                raise ValueError(
                    f"Unknown parent order: {child_order.parent_order_id}"
                )
            if filled_qty <= 0:
                return parent_order
            if fill_price <= 0:
                raise ValueError(
                    f"Invalid fill_price for child order {child_order.child_id}: {fill_price}"
                )
            # Child bookkeeping: a partial child fill (filled < target) still
            # terminates the child — the executor had its shot at this slice;
            # the shortfall surfaces on the parent, which EXPIREs instead of
            # FILLs.
            child_order.filled_quantity = float(filled_qty)
            child_order.exec_price = float(fill_price)
            child_order.status = OrderStatus.FILLED
            child_order.updated_at = datetime.now(tz)

            previous_filled = parent_order.filled_quantity
            previous_value = parent_order.avg_fill_price * previous_filled
            new_value = fill_price * filled_qty
            total_filled = previous_filled + filled_qty
            parent_order.filled_quantity = total_filled
            parent_order.avg_fill_price = (
                (previous_value + new_value) / total_filled if total_filled > 0 else 0.0
            )
            if parent_order.is_complete:
                # A fill can land after cancel_order (the slice was already
                # executing when the cancel arrived). The books moved — record
                # the quantity/price above — but never resurrect a terminal
                # status.
                pass
            elif (
                parent_order.total_quantity > 0
                and total_filled >= parent_order.total_quantity
            ):
                parent_order.status = OrderStatus.FILLED
            else:
                parent_order.status = OrderStatus.PARTIALLY_FILLED
            parent_order.updated_at = datetime.now(tz)
            self._tracker_info(
                "Parent order %s fill update filled=%s avg_price=%s status=%s",
                parent_order.order_id,
                parent_order.filled_quantity,
                parent_order.avg_fill_price,
                parent_order.status.value,
            )
            return parent_order

    def cancel_order(self, order_id: str) -> ParentOrder:
        with self._lock:
            parent_order = self.orders_by_id.get(order_id)
            if parent_order is None:
                raise ValueError(f"Unknown parent order: {order_id}")
            parent_order.status = OrderStatus.CANCELLED
            parent_order.updated_at = datetime.now(tz)
            # Two-step cancel: the scheduler drops still-queued slices lazily,
            # and the registry copy of each outstanding child is marked so
            # reporting never shows live children under a cancelled parent.
            self.scheduler.cancel_parent(order_id)
            for child in self.children_by_parent.get(order_id, []):
                if child.status not in _TERMINAL_CHILD_STATUSES:
                    child.status = OrderStatus.CANCELLED
                    child.updated_at = parent_order.updated_at
            self._tracker_info("Cancelled parent order %s", order_id)
            return parent_order

    def cancel_all_open_orders(self, reason: str = "shutdown") -> int:
        """Cancel every non-terminal parent (and its queued slices).

        Used by the live engine on shutdown so the tracking log ends the
        session with every order in a terminal state instead of phantom
        WORKING entries — this matters more once DB persistence lands, since
        those rows would otherwise look like open exposure the next morning.
        Returns the number of parents cancelled.
        """
        with self._lock:
            open_ids = [o.order_id for o in self.orders if not o.is_complete]
        for order_id in open_ids:
            self.cancel_order(order_id)
        if open_ids:
            self._tracker_info(
                "Cancelled %d open parent orders (%s).", len(open_ids), reason
            )
        return len(open_ids)

    def get_order(self, order_id: str) -> Optional[ParentOrder]:
        return self.orders_by_id.get(order_id)

    def list_orders(
        self,
        portfolio_id: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> list[ParentOrder]:
        results: Iterable[ParentOrder] = self.orders
        if portfolio_id is not None:
            results = [o for o in results if o.portfolio_id == portfolio_id]
        if status is not None:
            results = [o for o in results if o.status == status]
        return list(results)
