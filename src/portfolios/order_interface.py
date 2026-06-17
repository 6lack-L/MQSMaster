# src/portfolios/order_interface.py
import logging
from datetime import datetime

import pandas as pd
import pytz as pytz

try:
    # This import registers the .toolkit accessor globally
    from portfolios import toolkit  
    from portfolios.market_data_api import MarketData
    from portfolios.portfolio_interface import PortfolioManager
    
except ImportError:
    logging.warning("toolkit relative import failed; using absolute import.")
    try:
        from src.portfolios import toolkit
        from src.portfolios.market_data_api import MarketData
        from src.portfolios.portfolio_interface import PortfolioManager
    except ImportError:
        logging.error("Failed to import toolkit from both relative and absolute paths.")
        raise


class StrategyContext:
    """
    The master context object passed to the strategy's OnData method on each time step.
    It encapsulates MarketData, PortfolioManager, and provides trade execution methods
    """

    def __init__(
        self,
        market_data_df,
        cash_df,
        positions_df,
        port_notional_df,
        current_time,
        executor,
        portfolio_config,
        order_manager=None,
    ):
        self._executor = executor
        self._order_manager = order_manager
        self._portfolio_config = portfolio_config
        self._positions_df = positions_df
        effective_time = current_time
        timezone = pytz.timezone("America/New_York")
        if effective_time is None:
            if (
                market_data_df is not None
                and not getattr(market_data_df, "empty", True)
                and "timestamp" in market_data_df.columns
            ):
                try:
                    effective_time = pd.to_datetime(market_data_df["timestamp"]).max()
                except Exception:
                    effective_time = datetime.now(timezone)
            else:
                effective_time = datetime.now(timezone)
        self.time = effective_time

        # Initialize the high-level helper classes
        self.Market = MarketData(market_data_df, effective_time)

        cash_val = (
            cash_df.iloc[0]["notional"]
            if cash_df is not None and not cash_df.empty
            else 0.0
        )

        port_val = (
            port_notional_df.iloc[0]["notional"]
            if port_notional_df is not None and not port_notional_df.empty
            else 0.0
        )

        self.Portfolio = PortfolioManager(
            cash=cash_val, total_value=port_val, positions_df=positions_df
        )

    def buy(self, ticker: str, confidence: float = 1.0):
        self._trade(ticker, "BUY", confidence)

    def sell(self, ticker: str, confidence: float = 1.0):
        self._trade(ticker, "SELL", confidence)

    def _trade(self, ticker: str, signal_type: str, confidence: float):
        asset_data = self.Market[ticker]
        if not asset_data.Exists or asset_data.Close is None or asset_data.Close <= 0:
            logging.warning(
                "Skip trade: no valid market data for %s at %s (Exists=%s, Close=%s)",
                ticker,
                self.time,
                asset_data.Exists,
                asset_data.Close,
            )
            return

        if self._order_manager is not None:
            # OMS path: size with the executor's default model, then register the
            # order with the OMS (which owns execution from here).
            sizing = self._executor.default_trade_size(
                signal_type=signal_type,
                ticker=ticker,
                arrival_price=asset_data.Close,
                confidence=confidence,
                cash=self.Portfolio.cash,
                positions=self._positions_df,
                port_notional=self.Portfolio.total_value,
                ticker_weight=self.Portfolio.get_asset_weight(ticker, asset_data.Close),
            )
            if sizing.quantity > 0:
                try:
                    self._order_manager.process_order(
                        portfolio_id=self._portfolio_config["id"],
                        ticker=ticker,
                        side=signal_type,
                        confidence=confidence,
                        arrival_price=asset_data.Close,
                        total_quantity=sizing.quantity,
                        timestamp=self.time,
                    )
                except Exception as e:
                    logging.error(
                        "OrderManager.process_order failed for %s: %s", ticker, e
                    )
        else:
            # Direct execution: the executor sizes and fills in one call.
            self._executor.execute_trade(
                portfolio_id=self._portfolio_config["id"],
                ticker=ticker,
                signal_type=signal_type,
                confidence=confidence,
                arrival_price=asset_data.Close,
                cash=self.Portfolio.cash,
                positions=self._positions_df,
                port_notional=self.Portfolio.total_value,
                ticker_weight=self.Portfolio.get_asset_weight(ticker, asset_data.Close),
                timestamp=self.time,
            )
