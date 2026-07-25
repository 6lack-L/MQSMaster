from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid
import pytz

tz = pytz.timezone("America/New_York")


class OrderStatus(Enum):
    PENDING = "PENDING"  # Created, not yet started
    WORKING = "WORKING"  # Algorithm is actively slicing
    PARTIALLY_FILLED = "PARTIAL"  # Some child orders filled
    FILLED = "FILLED"  # All child orders filled
    CANCELLED = "CANCELLED"  # User or system cancelled
    # Terminal state for a parent whose schedule ran out (all child orders
    # reached a terminal state) without reaching total_quantity. Distinct from
    # CANCELLED so execution-quality reporting can separate "we chose to stop"
    # from "the algo could not complete within duration_minutes".
    EXPIRED = "EXPIRED"


class AlgoType(Enum):
    MARKET = "MARKET"  # Immediate execution (current behavior)
    LIMIT = "LIMIT"  # Place limit orders at specified price levels
    STOP = "STOP"  # Trigger orders when price crosses a threshold
    VWAP = "VWAP"
    TWAP = "TWAP"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

@dataclass
class ParentOrder:
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    portfolio_id: str = ""
    ticker: str = ""
    signal_type: Side = Side.BUY
    total_quantity: float = 0.0
    filled_quantity: float = 0.0
    algo_type: AlgoType = AlgoType.MARKET
    status: OrderStatus = OrderStatus.PENDING
    arrival_price: float = 0.0
    avg_fill_price: float = 0.0
    confidence: float = 1.0
    duration_minutes: int = 30  # How long the algo has to work the order
    created_at: datetime = field(default_factory=lambda: datetime.now(tz))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz))

    @property
    def remaining_quantity(self) -> float:
        return self.total_quantity - self.filled_quantity

    @property
    def is_complete(self) -> bool:
        # Terminal statuses: the OMS will not generate or execute any more
        # child orders for this parent. PARTIALLY_FILLED is *not* terminal —
        # it means child orders are still outstanding.
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        )

    @property
    def fill_pct(self) -> float:
        return (
            self.filled_quantity / self.total_quantity
            if self.total_quantity > 0
            else 0.0
        )


@dataclass
class ChildOrder:
    """One executable slice of a ParentOrder.

    Deliberately self-contained for execution: the executor seam
    (``execute_child_order``) receives only the child, so identity fields it
    needs (``portfolio_id``, ``arrival_price``, ``confidence``) are copied
    from the parent at schedule-generation time. These are *identity* copies
    only — a child must NEVER carry portfolio *state* snapshots
    (cash/positions at submit time): slices execute minutes after submission
    and must settle against fresh state fetched at fill time.
    """

    child_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_order_id: str = ""
    portfolio_id: str = ""
    ticker: str = ""
    signal_type: Side = Side.BUY
    target_quantity: float = 0.0
    filled_quantity: float = 0.0
    scheduled_time: Optional[datetime] = None
    # Parent's decision price, for per-slice slippage in trade logs.
    arrival_price: float = 0.0
    # Parent's registered confidence (the strategy's raw value — any RBP
    # blend was already applied inside default_trade_size when the parent
    # was sized). Informational for trade logs only: child quantities are
    # fixed at schedule generation and never re-sized at fill time.
    confidence: float = 1.0
    exec_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    slice_index: int = 0  # Which slice (0, 1, 2, ...)
    # Number of failed execution attempts so far. OrderManager.manage_order
    # retries a failed child once (design doc §5.6) before cancelling it.
    attempts: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(tz))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz))