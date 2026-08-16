"""Single construction site for the per-portfolio OrderManager.

Both execution pipelines gate the OMS behind ``config.json`` ``OMS.enabled`` and
build a *per-portfolio* ``OrderManager``. Previously each engine
(``RunEngine.load_portfolios`` and ``BacktestEngine.run``) parsed the ``OMS``
config slice and re-derived the portfolio id on its own — the kind of duplicated
config reading that already produced a ``PORTFOLIO_ID`` key bug. This module
owns that logic once so the two pipelines cannot drift.

Construction stays in the engine layer (not ``BasePortfolio``) on purpose: the
engine owns the OMS lifecycle, the portfolio stays OMS-agnostic and receives one
by dependency injection, and the absolute ``from src.oms...`` import keeps the
manager on a single canonical module path (avoiding the dual-import-path
``isinstance`` hazard).
"""

import logging
from typing import Optional

from src.oms.order_manager import OrderManager

_module_logger = logging.getLogger(__name__)


def resolve_portfolio_id(config_data: dict) -> str:
    """Resolve a portfolio id from its config the same way ``BasePortfolio`` does.
    """
    portfolio_id = config_data.get("PORTFOLIO_ID", "0")
    if portfolio_id is None:
        _module_logger.warning(f"PORTFOLIO_ID not found in config; defaulting to '0'. Full config: {config_data}")
        return "0"
    return str(portfolio_id)


def build_order_manager(
    config_data: dict,
    portfolio_id: str,
    logger: Optional[logging.Logger] = None,
) -> Optional[OrderManager]:
    """Build a per-portfolio ``OrderManager`` when ``OMS.enabled``; else ``None``.

    Returns ``None`` when the OMS is disabled *or* when construction fails — in
    both cases the caller runs the proven direct-execution path. Failing soft
    (rather than skipping the portfolio) matches the OMS's tracking-only role and
    the executor's own "OMS error → fall through to direct fill" philosophy.

    ``portfolio_id`` is passed in (not re-derived) so callers that already hold a
    parsed id — e.g. ``portfolio_instance.portfolio_id`` in backtest — reuse it;
    pre-instantiation callers use :func:`resolve_portfolio_id`.
    """
    log = logger or _module_logger

    oms_config = config_data.get("OMS", {})
    if not oms_config.get("enabled", False):
        return None

    try:
        order_manager = OrderManager(portfolio_id=portfolio_id, config=oms_config)
        log.info(
            f"OrderManager initialized for portfolio {portfolio_id} with OMS config."
        )
        return order_manager
    except Exception as e:
        log.error(
            f"Failed to initialize OrderManager for portfolio {portfolio_id}: {e}"
        )
        return None
