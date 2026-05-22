from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid
import pytz


class OrderStatus(Enum):
    PENDING = "PENDING"  # Created, not yet started
    WORKING = "WORKING"  # Algorithm is actively slicing
    PARTIALLY_FILLED = "PARTIAL"  # Some child orders filled
    FILLED = "FILLED"  # All child orders filled
    CANCELLED = "CANCELLED"  # User or system cancelled


class AlgoType(Enum):
    MARKET = "MARKET"  # Immediate execution (current behavior)
    LIMIT = "LIMIT"  # Place limit orders at specified price levels
    STOP = "STOP"  # Trigger orders when price crosses a threshold
    VWAP = "VWAP"
    TWAP = "TWAP"


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderType(Enum):
    Market = "MARKET"
    Limit = "LIMIT"
    Stop = "STOP"

@dataclass
class ParentOrder:
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    portfolio_id: str = ""
    ticker: str = ""
    side: Side = Side.BUY
    total_quantity: float = 0.0
    filled_quantity: float = 0.0
    algo_type: AlgoType = AlgoType.MARKET
    status: OrderStatus = OrderStatus.PENDING
    arrival_price: float = 0.0
    avg_fill_price: float = 0.0
    confidence: float = 1.0
    duration_minutes: int = 30  # How long the algo has to work the order
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @property
    def remaining_quantity(self) -> float:
        return self.total_quantity - self.filled_quantity

    @property
    def is_complete(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED)

    @property
    def fill_pct(self) -> float:
        return (
            self.filled_quantity / self.total_quantity
            if self.total_quantity > 0
            else 0.0
        )

tz = pytz.timezone("America/New_York")
@dataclass
class ChildOrder:
    child_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_order_id: str = ""
    ticker: str = ""
    side: Side = Side.BUY
    target_quantity: float = 0.0
    filled_quantity: float = 0.0
    scheduled_time: Optional[datetime] = None
    exec_price: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    slice_index: int = 0  # Which slice (0, 1, 2, ...)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)