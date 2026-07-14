"""TWAP (Time-Weighted Average Price) execution algorithm.

Slices a parent order into equal-quantity child orders executed at fixed
intervals across ``parent_order.duration_minutes`` (design doc §5.4):

    Parent: BUY 1000 shares over 30 minutes, 10 slices
    -> 100 shares every 3 minutes, first slice at t+0

Pure clock-based execution — no market-data dependency, which makes TWAP the
safe default for illiquid tickers and the fallback when no volume profile is
available for VWAP. All quantities are whole shares (both executors floor to
whole shares), with the integer-division remainder folded into the last slice.
"""

import logging
import math
from datetime import timedelta
from typing import List, Optional

from src.oms.order_structs import ChildOrder, ParentOrder
from src.oms.sizing.base import BaseAlgorithm, child_from_parent

logger = logging.getLogger(__name__)


class TWAPAlgorithm(BaseAlgorithm):
    """Equal slices at equal intervals.

    Config (portfolio ``config.json`` -> ``OMS`` block):
        twap_num_slices: requested number of slices (default 10). The
            effective count is capped at the whole-share total so no slice
            rounds to zero shares.
    """

    def __init__(self, num_slices: int = 10):
        if num_slices < 1:
            raise ValueError(f"twap_num_slices must be >= 1, got {num_slices}")
        self.num_slices = int(num_slices)

    def generate_schedule(
        self,
        parent_order: ParentOrder,
        volume_profile: Optional[List[float]] = None,
    ) -> List[ChildOrder]:
        # volume_profile is accepted for interface parity (BaseAlgorithm) and
        # deliberately ignored: TWAP is defined as volume-agnostic.
        total_quantity = float(parent_order.total_quantity)
        if total_quantity <= 0:
            return []

        # Never create zero-share slices: a 3-share order with 10 requested
        # slices becomes 3 slices of 1 share.
        num_slices = max(1, min(self.num_slices, int(total_quantity)))
        interval = timedelta(minutes=parent_order.duration_minutes) / num_slices

        base_quantity = float(math.floor(total_quantity / num_slices))
        children: List[ChildOrder] = []
        allocated = 0.0
        for slice_index in range(num_slices):
            if slice_index < num_slices - 1:
                quantity = base_quantity
            else:
                # Remainder shares go into the last slice (design doc §5.4).
                quantity = total_quantity - allocated
            allocated += quantity
            children.append(
                child_from_parent(
                    parent_order,
                    target_quantity=quantity,
                    # First slice fires immediately at the parent's creation
                    # time; the schedule spans [t+0, t+duration).
                    scheduled_time=parent_order.created_at + slice_index * interval,
                    slice_index=slice_index,
                )
            )

        logger.debug(
            "TWAP schedule for parent %s: %d slices of ~%s shares every %s",
            parent_order.order_id,
            num_slices,
            base_quantity,
            interval,
        )
        return children
