"""MARKET pseudo-algorithm: immediate single-slice pass-through.

Reproduces the pre-OMS behavior (one signal -> one fill) inside the OMS
lifecycle so MARKET orders get the same tracking, retry, and reporting as
sliced orders. Also the downgrade target for orders below
``min_order_notional`` and (via ``fallback_to_market``) for algo types that
are not implemented yet (LIMIT / STOP).
"""

from typing import List, Optional

from src.oms.order_structs import ChildOrder, ParentOrder
from src.oms.sizing.base import BaseAlgorithm, child_from_parent


class MarketAlgorithm(BaseAlgorithm):
    """One child order for the full quantity, scheduled immediately."""

    def generate_schedule(
        self,
        parent_order: ParentOrder,
        volume_profile: Optional[List[float]] = None,
    ) -> List[ChildOrder]:
        if parent_order.total_quantity <= 0:
            return []
        return [
            child_from_parent(
                parent_order,
                target_quantity=float(parent_order.total_quantity),
                # Due as soon as the parent exists: the next manage_order()
                # pump releases it.
                scheduled_time=parent_order.created_at,
                slice_index=0,
            )
        ]
