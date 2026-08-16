"""Execution-scheduling algorithms for the OMS.

This package is the design doc's ``algorithms/`` directory (docs/OMS/
OMS_DESIGN.md §5.3–§5.5): every algorithm turns one ``ParentOrder`` into a
list of time-stamped ``ChildOrder`` slices. It does **not** do position
sizing — the executor's ``default_trade_size`` owns buying-power/margin math
and hands the OMS an already-sized ``total_quantity``.

To add a new algorithm:
  1. Add a value to ``AlgoType`` in ``src/oms/order_structs.py``.
  2. Subclass ``BaseAlgorithm`` here and implement ``generate_schedule``.
  3. Register it in ``build_algorithm`` below (map config keys from the
     portfolio's ``OMS`` block to constructor arguments).
"""

from src.oms.order_structs import AlgoType
from src.oms.sizing.base import BaseAlgorithm
from src.oms.sizing.market import MarketAlgorithm
from src.oms.sizing.twap import TWAPAlgorithm
from src.oms.sizing.vwap import VWAPAlgorithm

__all__ = [
    "AlgoType",
    "BaseAlgorithm",
    "MarketAlgorithm",
    "TWAPAlgorithm",
    "VWAPAlgorithm",
    "build_algorithm",
]


def build_algorithm(algo_type: AlgoType, config: dict) -> BaseAlgorithm:
    """Instantiate the algorithm for ``algo_type`` from an ``OMS`` config block.

    Raises ``NotImplementedError`` for enum values that exist but have no
    implementation yet (LIMIT, STOP) — the OrderManager decides whether that
    downgrades to MARKET (``fallback_to_market``) or rejects the order, so
    this factory stays policy-free.
    """
    if algo_type == AlgoType.MARKET:
        return MarketAlgorithm()
    if algo_type == AlgoType.TWAP:
        return TWAPAlgorithm(num_slices=int(config.get("twap_num_slices", 10)))
    if algo_type == AlgoType.VWAP:
        return VWAPAlgorithm(
            bucket_minutes=int(config.get("vwap_bucket_minutes", 15))
        )
    raise NotImplementedError(
        f"No execution algorithm implemented for AlgoType.{algo_type.name}"
    )
