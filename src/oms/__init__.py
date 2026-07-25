"""Order Management System (OMS).

Config-gated (per-portfolio ``config.json`` -> ``OMS.enabled``) layer between
a strategy's intent ("buy AAPL, confidence 0.8") and market execution. Map of
the package:

    factory.py        single construction point (both engines build the
                      per-portfolio OrderManager through here)
    order_structs.py  ParentOrder / ChildOrder dataclasses + enums
    order_manager.py  coordinator: intake, algo selection, the manage_order
                      pump, fill accounting, cancellation
    scheduler.py      poll-driven time queue of child orders
    sizing/           execution algorithms (MARKET / TWAP / VWAP)
    monitor.py        read-only execution-quality metrics
    order_types/      reserved for LIMIT/STOP trigger logic (empty)

Design + current implementation status: docs/OMS/OMS_DESIGN.md (§1a is kept
current — update it when this package changes materially).

Import convention: this package always uses absolute ``src.oms...`` imports
(no try-relative idiom) so OMS classes resolve to one module object —
see CLAUDE.md "Dual import paths" for why that matters.
"""
