import logging
import math
from collections import namedtuple
from typing import Dict, List

import pandas as pd

# Result of the default sizing model: the share quantity to trade, the signed
# desired notional (its sign drives BUY vs SELL settlement), and the execution
# price used. Shared by the OMS path (reads .quantity) and execute_trade.
Sizing = namedtuple("Sizing", ["quantity", "desired_notional", "exec_price"])


class BacktestExecutor:
    """
    A backtest executor that manages a single, unified portfolio,
    supporting long/short positions with a realistic margin model that mirrors live trading constraints.
    """

    def __init__(
        self,
        initial_capital: float,
        tickers: List[str],
        leverage: float = 2.0,
        slippage: float = 0.0,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.tickers = tickers
        self.leverage = leverage
        self.slippage = slippage
        # --- Unified Portfolio State ---
        self.cash = initial_capital
        self.positions: Dict[str, float] = {ticker: 0.0 for ticker in tickers}
        self.latest_prices: Dict[str, float] = {ticker: 0.0 for ticker in tickers}
        self.trade_log: List[Dict] = []

        self.logger.info(
            f"BacktestExecutor initialized with {initial_capital:.2f} capital, "
            f"leverage={leverage}, slippage={slippage}, for tickers: {tickers}"
        )

    def _apply_slippage(self, price: float, signal_type: str) -> float:
        """
        Applies slippage to the execution price based on the trade direction.
        - For BUY orders, the price is increased.
        - For SELL orders, the price is decreased.
        """
        if signal_type == "BUY":
            return price * (1 + self.slippage)
        elif signal_type == "SELL":
            return price * (1 - self.slippage)
        return price

    def update_price(self, ticker: str, price: float):
        """Updates the latest known price for a ticker."""
        if ticker in self.latest_prices:
            self.latest_prices[ticker] = price

    def get_port_notional(self) -> float:
        """Calculates the total current equity of the portfolio."""
        positions_value = sum(
            self.positions[ticker] * self.latest_prices.get(ticker, 0.0)
            for ticker in self.tickers
        )
        return self.cash + positions_value

    def get_position_value(self, ticker: str) -> float:
        """Calculates the notional value of a single ticker's position."""
        return self.positions.get(ticker, 0.0) * self.latest_prices.get(ticker, 0.0)

    def get_data_feeds(self) -> Dict[str, pd.DataFrame]:
        """Generates the portfolio state dataframes required by the strategy."""
        cash_df = pd.DataFrame([{"notional": self.cash}])
        positions_list = [
            {"ticker": ticker, "quantity": quantity}
            for ticker, quantity in self.positions.items()
        ]
        positions_df = pd.DataFrame(positions_list)
        port_notional_df = pd.DataFrame([{"notional": self.get_port_notional()}])

        return {
            "CASH_EQUITY": cash_df,
            "POSITIONS": positions_df,
            "PORT_NOTIONAL": port_notional_df,
        }

    def _calculate_buying_power(self, portfolio_equity: float) -> float:
        """Calculates the available buying power based on a margin model."""
        gross_position_value = sum(
            abs(self.positions[ticker] * self.latest_prices.get(ticker, 0.0))
            for ticker in self.tickers
        )
        buying_power = (portfolio_equity * self.leverage) - gross_position_value
        return max(0, buying_power)

    def default_trade_size(
        self,
        signal_type,
        ticker,
        arrival_price,
        confidence,
        cash,
        positions,
        port_notional,
        ticker_weight,
    ):
        """Size a trade with the default target-weight model, without filling.

        Single sizing entry point for both the OMS path (which reads
        ``.quantity`` to register a parent order) and ``execute_trade`` (which
        also needs ``.desired_notional`` for BUY/SELL direction and
        ``.exec_price`` for the fill). Returns a ``Sizing`` with ``quantity == 0``
        on any no-trade. ``cash``/``positions`` are accepted for signature parity
        with the live executor; backtest sizes against its own ``self`` state.
        """
        try:
            port_notional = float(port_notional)
            arrival_price = float(arrival_price)
            confidence = max(0.0, min(1.0, float(confidence)))
            ticker_weight = float(ticker_weight)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Numeric conversion failed in default_trade_size: {e}")
            return Sizing(0, 0.0, 0.0)

        signal_type = signal_type.upper()
        if signal_type not in ("BUY", "SELL", "HOLD"):
            self.logger.warning(
                f"Invalid signal type '{signal_type}' for {ticker}. Must be BUY, SELL, or HOLD."
            )
            return Sizing(0, 0.0, 0.0)
        if signal_type == "HOLD" or confidence == 0.0:
            self.logger.debug(
                "Skip trade: signal=%s confidence=%.2f", signal_type, confidence
            )
            return Sizing(0, 0.0, 0.0)

        exec_price = self._apply_slippage(arrival_price, signal_type)
        if exec_price <= 0:
            self.logger.warning(
                f"Cannot size trade for {ticker}: invalid exec price {exec_price} after slippage."
            )
            return Sizing(0, 0.0, exec_price)

        # If ticker_weight is 0 (no current position), default to equal-weight.
        if ticker_weight == 0.0:
            if not self.tickers:
                self.logger.error("No tickers list available for fallback allocation.")
                return Sizing(0, 0.0, exec_price)
            ticker_weight = 1.0 / len(self.tickers)

        buying_power = self._calculate_buying_power(port_notional)

        current_quantity = self.positions.get(ticker, 0.0)
        current_notional_value = current_quantity * exec_price

        target_notional = port_notional * ticker_weight
        # A SELL signal targets a negative (short) position
        if signal_type == "SELL":
            target_notional *= -1

        desired_trade_notional = (target_notional - current_notional_value) * confidence

        # Ignore trades smaller than $1.00 notional.
        if abs(desired_trade_notional) < 1.0:
            self.logger.debug(
                "Skip trade: desired_notional too small (%.2f) for %s",
                desired_trade_notional,
                ticker,
            )
            return Sizing(0, desired_trade_notional, exec_price)

        # For buys, we are also constrained by the actual cash available.
        if desired_trade_notional > 0:  # This is a BUY operation
            tradable_notional = min(
                abs(desired_trade_notional), buying_power, self.cash
            )
        else:  # This is a SELL/SHORT operation
            tradable_notional = min(abs(desired_trade_notional), buying_power)

        if tradable_notional < 1.0:
            return Sizing(0, desired_trade_notional, exec_price)

        quantity_to_trade = math.floor(tradable_notional / exec_price)
        if quantity_to_trade <= 0:
            return Sizing(0, desired_trade_notional, exec_price)

        return Sizing(quantity_to_trade, desired_trade_notional, exec_price)

    def execute_trade(
        self,
        portfolio_id,
        ticker,
        signal_type,
        confidence,
        arrival_price,
        cash,
        positions,
        port_notional,
        ticker_weight,
        timestamp,
    ):
        # Size with the shared default model (handles coercion, signal/price
        # validation, equal-weight fallback, and buying-power/cash constraints).
        sizing = self.default_trade_size(
            signal_type=signal_type,
            ticker=ticker,
            arrival_price=arrival_price,
            confidence=confidence,
            cash=cash,
            positions=positions,
            port_notional=port_notional,
            ticker_weight=ticker_weight,
        )
        if sizing.quantity <= 0:
            return

        quantity_to_trade = sizing.quantity
        exec_price = sizing.exec_price
        signal_type = signal_type.upper()

        # --- Execute the Trade (settle the fill into the executor's own
        # unified portfolio state; the ``cash``/``positions`` params are kept
        # only for signature parity with the live executor). ---
        trade_value = quantity_to_trade * exec_price
        current_quantity = self.positions.get(ticker, 0.0)
        if sizing.desired_notional > 0:  # Finalizing a BUY
            self.cash -= trade_value
            self.positions[ticker] = current_quantity + quantity_to_trade
        else:  # Finalizing a SELL
            self.cash += trade_value
            self.positions[ticker] = current_quantity - quantity_to_trade

        self.trade_log.append(
            {
                "timestamp": timestamp,
                "portfolio_id": portfolio_id,
                "ticker": ticker,
                "signal_type": signal_type,
                "confidence": max(0.0, min(1.0, float(confidence))),
                "shares": quantity_to_trade,
                "fill_price": exec_price,
                "cash_after": self.cash,
                "position_size": self.positions.get(ticker, 0.0),
            }
        )

        return {
            "status": "success",
            "quantity": quantity_to_trade,
            "updated_cash": self.cash,
            "updated_quantity": self.positions.get(ticker, 0.0),
        }

    def dump_trade_log(self) -> list[str]:
        """
        Generate formatted trade log entries and return them as a list of strings.
        """
        trade_logs: list[str] = []
        for entry in self.trade_log:
            ts = entry["timestamp"]
            ts_str = (
                ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(ts, "strftime") else str(ts)
            )
            msg = (
                f"[{entry.get('portfolio_id', 'unknown')}] "
                f"{ts_str} - "
                f"{entry['ticker']} | {entry['signal_type']} "
                f"{entry['shares']} @ {entry['fill_price']:.2f}$ "
                f"cash={entry['cash_after']:.2f}$"
                f" qty={entry['position_size']} "
            )
            trade_logs.append(msg)
        return trade_logs
