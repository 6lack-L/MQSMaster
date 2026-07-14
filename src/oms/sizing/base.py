from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from src.oms.order_structs import ChildOrder, ParentOrder


def child_from_parent(
    parent_order: ParentOrder,
    target_quantity: float,
    scheduled_time: datetime,
    slice_index: int,
) -> ChildOrder:
    """Build one slice, copying the parent's identity fields.

    Single construction point for every algorithm so a child is always
    self-contained for the executor seam (portfolio_id / arrival_price /
    confidence travel with it — see the ChildOrder docstring for why state
    snapshots must never be added here).
    """
    return ChildOrder(
        parent_order_id=parent_order.order_id,
        portfolio_id=parent_order.portfolio_id,
        ticker=parent_order.ticker,
        signal_type=parent_order.signal_type,
        target_quantity=float(target_quantity),
        scheduled_time=scheduled_time,
        arrival_price=float(parent_order.arrival_price),
        confidence=float(parent_order.confidence),
        slice_index=slice_index,
    )


class BaseAlgorithm(ABC):
    """All execution algorithms implement this interface."""

    @abstractmethod
    def generate_schedule(
        self,
        parent_order: ParentOrder,
        volume_profile: Optional[List[float]] = None,
    ) -> List[ChildOrder]:
        """
        Given a parent order, produce a list of child orders
        with target quantities and scheduled execution times.
        """
        ...
