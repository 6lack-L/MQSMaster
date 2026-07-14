"""Execution-quality monitoring for the OMS (in-memory).

Read-only views over an ``OrderManager``'s order book: slippage vs arrival
price, fill rates, and per-parent summaries for logs/dashboards. This is the
observability half of the design doc's ``order_tracker.py`` (§5.7); the
persistence half (writing lifecycle events to ``oms_parent_orders`` /
``oms_child_orders``, §6) is not built yet — when it is, it should consume
these same snapshots so the DB rows and the logged metrics can never
disagree.

Everything here must stay side-effect free (no mutation of orders, no I/O
beyond the caller's logging) so it is always safe to call from any thread.
"""

from typing import List

from src.oms.order_structs import ParentOrder, Side


class OrderMonitor:
    """Stateless metric helpers — all methods are static by design."""

    @staticmethod
    def slippage_bps(parent_order: ParentOrder) -> float:
        """Signed implementation shortfall vs the arrival price, in basis
        points. Positive means the execution was worse than arrival for the
        order's side (paid up on a BUY, sold down on a SELL); negative means
        price improvement. 0.0 when nothing has filled yet."""
        if parent_order.filled_quantity <= 0 or parent_order.arrival_price <= 0:
            return 0.0
        raw = (
            (parent_order.avg_fill_price / parent_order.arrival_price) - 1.0
        ) * 10000.0
        # For a SELL, a fill *below* arrival is the adverse direction.
        return raw if parent_order.signal_type == Side.BUY else -raw

    @staticmethod
    def summarize(parent_order: ParentOrder) -> dict:
        """One flat dict per parent — stable keys, log/JSON friendly. This is
        the shape a future DB writer or dashboard row should serialize."""
        return {
            "order_id": parent_order.order_id,
            "portfolio_id": parent_order.portfolio_id,
            "ticker": parent_order.ticker,
            "side": parent_order.signal_type.value,
            "algo": parent_order.algo_type.value,
            "status": parent_order.status.value,
            "total_quantity": parent_order.total_quantity,
            "filled_quantity": parent_order.filled_quantity,
            "fill_pct": parent_order.fill_pct,
            "arrival_price": parent_order.arrival_price,
            "avg_fill_price": parent_order.avg_fill_price,
            "slippage_bps": OrderMonitor.slippage_bps(parent_order),
            "created_at": parent_order.created_at,
            "updated_at": parent_order.updated_at,
        }

    @staticmethod
    def snapshot(order_manager) -> List[dict]:
        """Summaries for every parent the manager has ever registered.
        Typed loosely (any object with ``.orders``) to avoid importing
        OrderManager and creating an import cycle."""
        return [OrderMonitor.summarize(order) for order in order_manager.orders]
