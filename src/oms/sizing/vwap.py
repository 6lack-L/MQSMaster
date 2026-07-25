"""VWAP (Volume-Weighted Average Price) execution algorithm.

Sizes each child order proportionally to the expected traded volume in its
time bucket (design doc §5.5), so the order participates more heavily when
the market is naturally busy:

    Parent: BUY 1000 shares over 45 minutes, 15-minute buckets
    Profile weights (renormalized): [0.5, 0.3, 0.2]
    -> children of 500 / 300 / 200 shares at t+0 / t+15m / t+30m

The volume profile is *injected by the caller* (``volume_profile`` argument),
not queried here — the OMS layer has no DB access by design. The expected
shape is one non-negative relative weight per bucket of the order's own
window, oldest bucket first; weights are renormalized so they don't need to
sum to 1. A future data provider should build it from the ``market_data``
table, per the design doc, with a query along the lines of:

    SELECT date_trunc('hour', timestamp)
             + (EXTRACT(minute FROM timestamp)::int / 15) * INTERVAL '15 min'
             AS bucket,
           AVG(volume) AS avg_volume
    FROM market_data
    WHERE ticker = %s
      AND timestamp >= NOW() - INTERVAL '20 days'          -- vwap_lookback_days
      AND EXTRACT(dow FROM timestamp) BETWEEN 1 AND 5
    GROUP BY bucket ORDER BY bucket;

(then filter to the order's intraday window and renormalize).

When no usable profile is supplied, VWAP degrades to uniform weights — i.e.
TWAP-shaped execution — rather than refusing the order. That keeps the OMS
fail-soft: a missing analytics input should never block a trade.
"""

import logging
import math
from datetime import timedelta
from typing import List, Optional

from src.oms.order_structs import ChildOrder, ParentOrder
from src.oms.sizing.base import BaseAlgorithm, child_from_parent

logger = logging.getLogger(__name__)


class VWAPAlgorithm(BaseAlgorithm):
    """Volume-proportional slices on fixed time buckets.

    Config (portfolio ``config.json`` -> ``OMS`` block):
        vwap_bucket_minutes: width of each execution bucket (default 15).
            The number of buckets is ceil(duration_minutes / bucket_minutes).
        vwap_lookback_days: how much history the (future) profile provider
            should average over — documented here for discoverability; this
            class only consumes the resulting weights.
    """

    def __init__(self, bucket_minutes: int = 15):
        if bucket_minutes < 1:
            raise ValueError(f"vwap_bucket_minutes must be >= 1, got {bucket_minutes}")
        self.bucket_minutes = int(bucket_minutes)

    def _resolve_weights(
        self, num_buckets: int, volume_profile: Optional[List[float]]
    ) -> List[float]:
        """Validate + renormalize the caller's profile; fall back to uniform.

        Fallback (uniform) triggers when the profile is missing, has the wrong
        length, contains negatives/non-numbers, or sums to zero. Each case is
        logged so a silently-degraded VWAP is visible in the tracking logs.
        """
        uniform = [1.0 / num_buckets] * num_buckets
        if volume_profile is None:
            logger.info(
                "VWAP: no volume profile supplied; using uniform weights "
                "(TWAP-equivalent execution)."
            )
            return uniform
        try:
            weights = [float(w) for w in volume_profile]
        except (TypeError, ValueError):
            logger.warning(
                "VWAP: volume profile is not numeric (%r); using uniform weights.",
                volume_profile,
            )
            return uniform
        if len(weights) != num_buckets or any(w < 0 for w in weights):
            logger.warning(
                "VWAP: volume profile invalid (len=%d, expected %d, "
                "negatives=%s); using uniform weights.",
                len(weights),
                num_buckets,
                any(w < 0 for w in weights),
            )
            return uniform
        total_weight = sum(weights)
        if total_weight <= 0:
            logger.warning("VWAP: volume profile sums to 0; using uniform weights.")
            return uniform
        return [w / total_weight for w in weights]

    def generate_schedule(
        self,
        parent_order: ParentOrder,
        volume_profile: Optional[List[float]] = None,
    ) -> List[ChildOrder]:
        total_quantity = float(parent_order.total_quantity)
        if total_quantity <= 0:
            return []

        num_buckets = max(
            1, math.ceil(parent_order.duration_minutes / self.bucket_minutes)
        )
        weights = self._resolve_weights(num_buckets, volume_profile)

        # Whole-share allocation: floor each bucket, then put the rounding
        # remainder into the highest-weight bucket (design doc §5.5) so the
        # extra shares trade where liquidity is expected to be deepest.
        quantities = [float(math.floor(total_quantity * w)) for w in weights]
        remainder = total_quantity - sum(quantities)
        if remainder > 0:
            quantities[weights.index(max(weights))] += remainder

        bucket_interval = timedelta(minutes=self.bucket_minutes)
        children: List[ChildOrder] = []
        for bucket_index, quantity in enumerate(quantities):
            # Zero-share buckets (tiny orders spread over many buckets) are
            # skipped entirely rather than scheduled as no-op children.
            if quantity <= 0:
                continue
            children.append(
                child_from_parent(
                    parent_order,
                    target_quantity=quantity,
                    scheduled_time=parent_order.created_at
                    + bucket_index * bucket_interval,
                    # slice_index keeps the *bucket* index (not a dense
                    # renumbering) so it stays alignable with the profile.
                    slice_index=bucket_index,
                )
            )

        logger.debug(
            "VWAP schedule for parent %s: %d buckets, quantities=%s",
            parent_order.order_id,
            num_buckets,
            quantities,
        )
        return children
