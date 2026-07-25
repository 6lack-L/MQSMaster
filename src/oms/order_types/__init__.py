"""Reserved for order-*type* semantics (LIMIT / STOP trigger handling).

Intentionally empty for now. Execution *scheduling* algorithms (TWAP / VWAP /
MARKET slicing) live in ``src/oms/sizing/`` — do not add a second copy here.
When LIMIT/STOP support lands (see ``AlgoType.LIMIT`` / ``AlgoType.STOP`` in
``order_structs.py``), this package should own the price-trigger logic that
decides *whether* a child order may execute, while ``sizing/`` keeps owning
*when* and *how much*.
"""
