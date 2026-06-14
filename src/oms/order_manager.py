from src.oms.order_structs import OrderStatus, AlgoType, Side, ParentOrder, ChildOrder, tz
from src.oms.scheduler import Scheduler
from datetime import datetime
from typing import Optional, Iterable

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        # Execution algorithm (TWAP/VWAP slicing) is layered in later via
        # manage_order(); sizing is owned by the executor for now.
        self.algorithm = None
        self.tracker = logging.getLogger(
            f"{portfolio_id}_tracking_logger"
        )
        self.config = config or {}
        self.orders: list[ParentOrder] = []
        self.child_orders: list[ChildOrder] = []
        self.parent_order_ids: set[str] = set()
        self.orders_by_id: dict[str, ParentOrder] = {}

    def _tracker_info(self, message: str, *args):
        if self.tracker is None:
            return
        if hasattr(self.tracker, "info"):
            self.tracker.info(message, *args)

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
    ) -> ParentOrder:
        """Register a parent order from a quantity the caller has already sized.

        The executor owns position sizing and buying-power constraints; the OMS
        takes the resulting quantity, tracks the parent order, and hands it to
        the scheduler. Returns the created ParentOrder.
        """
        if portfolio_id != self.portfolio_id:
            raise ValueError(
                f"OrderManager for portfolio {self.portfolio_id} cannot process orders for {portfolio_id}"
            )
        if algo_type is None:
            algo_type = AlgoType.MARKET
        if duration_minutes is None:
            duration_minutes = int(self.config.get("duration_minutes", 30))

        if arrival_price is None or arrival_price <= 0:
            raise ValueError(f"Invalid arrival_price for {ticker}: {arrival_price}")

        if total_quantity is None or total_quantity <= 0:
            raise ValueError(
                f"Invalid total_quantity for {ticker}: {total_quantity}"
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
        logger.info(
            "Order Created order for %s %s qty=%s timestamp=%s",
            parent_order.order_id,
            parent_order.signal_type.value,
            parent_order.total_quantity,
            parent_order.created_at.isoformat()
        )

        if parent_order.order_id in self.parent_order_ids:
            raise ValueError(f"Duplicate parent order ID: {parent_order.order_id}")

        self.parent_order_ids.add(parent_order.order_id)
        self.orders.append(parent_order)
        self.orders_by_id[parent_order.order_id] = parent_order

        logger.info(
            "Submitted order %s %s qty=%s algo=%s",
            parent_order.order_id,
            parent_order.ticker,
            parent_order.total_quantity,
            parent_order.algo_type.value,
        )
        self._tracker_info(
            "Submitted parent order %s ticker=%s qty=%s",
            parent_order.order_id,
            parent_order.ticker,
            parent_order.total_quantity,
        )

        self.scheduler.schedule_order(parent_order)
        return parent_order

    def on_child_filled(
        self,
        child_order: ChildOrder,
        filled_qty: float,
        fill_price: float,
    ) -> ParentOrder:
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
        previous_filled = parent_order.filled_quantity
        previous_value = parent_order.avg_fill_price * previous_filled
        new_value = fill_price * filled_qty
        total_filled = previous_filled + filled_qty
        parent_order.filled_quantity = total_filled
        parent_order.avg_fill_price = (
            (previous_value + new_value) / total_filled if total_filled > 0 else 0.0
        )
        if parent_order.total_quantity > 0 and total_filled >= parent_order.total_quantity:
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
        parent_order = self.orders_by_id.get(order_id)
        if parent_order is None:
            raise ValueError(f"Unknown parent order: {order_id}")
        parent_order.status = OrderStatus.CANCELLED
        parent_order.updated_at = datetime.now(tz)
        if hasattr(self.scheduler, "cancel_parent"):
            self.scheduler.cancel_parent(order_id)
        self._tracker_info("Cancelled parent order %s", order_id)
        return parent_order

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

    def manage_order(self, orders: list[ParentOrder]):
        """Drive working orders toward completion via the execution algorithm.

        Placeholder until an execution algorithm (TWAP/VWAP slicing) is wired in.
        It is intentionally a no-op rather than raising so callers polling the
        OMS lifecycle do not crash; child-order generation will be added here.
        """
        return None
