import logging
import math
from collections import namedtuple
from datetime import datetime
from typing import Callable, Optional

import pandas as pd
import pytz

# Result of the default sizing model: share quantity, the signed desired notional
# (its sign drives BUY vs SELL settlement), and the execution price fetched.
# Shared by the OMS path (reads .quantity) and execute_trade. Mirrors the
# BacktestExecutor's Sizing so the StrategyContext seam is uniform.
Sizing = namedtuple("Sizing", ["quantity", "desired_notional", "exec_price"])

try:
    from common.auth.apiAuth import APIAuth
    from common.database.schemaDefinitions import MQSDBConnector
    from orchestrator.marketData.fmpMarketData import FMPMarketData
except ImportError:
    logging.warning(
        "APIAuth, MQSDBConnector, FMPMarketData relative import failed; using absolute import."
    )
    try:
        from src.common.auth.apiAuth import APIAuth
        from src.common.database.schemaDefinitions import MQSDBConnector
        from src.orchestrator.marketData.fmpMarketData import FMPMarketData
    except ImportError:
        logging.error(
            "Failed to import necessary modules from both relative and absolute paths."
        )
        raise
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)


class tradeExecutor:
    def __init__(self, db_connector: MQSDBConnector, leverage: float = 2.0, rbp_overlay: Optional[Callable[[str, str, str, float], float]] = None):
        """Initializes the tradeExecutor and its components."""
        self.dbconn = db_connector
        self.api_auth = APIAuth()
        self.fmp_api_key = self.api_auth.get_fmp_api_key()
        self.marketData = FMPMarketData()
        self.logger = logging.getLogger(__name__)
        self.leverage = leverage
        self.rbp_overlay = rbp_overlay
        self.logger.info(f"tradeExecutor initialized with leverage={self.leverage}.")
        self.logger.info("RBP overlay: %s", "enabled" if rbp_overlay else "disabled")

    def _calculate_buying_power(
        self,
        portfolio_equity: float,
        positions_df: pd.DataFrame,
        current_ticker: str,
        current_ticker_price: float,
    ) -> float:
        if positions_df.empty:
            return portfolio_equity * self.leverage

        gross_position_value = 0.0
        for _, row in positions_df.iterrows():
            ticker = row["ticker"]
            quantity = row["quantity"]
            price = 0.0

            if ticker == current_ticker:
                price = current_ticker_price
            else:
                price = self.get_current_price(ticker)

            # --- ROBUSTNESS CHECK ---
            if price <= 0:
                self.logger.critical(
                    f"Could not fetch valid price for position {ticker} during buying power calculation. "
                    f"Temporarily halting new trades by returning zero buying power."
                )
                return float(
                    0.0  # Return 0 to prevent trading with an uncertain portfolio state
                )

            gross_position_value += abs(float(quantity) * price)

        buying_power = (portfolio_equity * self.leverage) - gross_position_value
        return max(0, buying_power)

    def default_trade_size(
        self,
        portfolio_id,
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
        ``.exec_price`` for the fill). Fetches the live price once so
        ``execute_trade`` reuses it rather than re-fetching. Returns a ``Sizing``
        with ``quantity == 0`` on any no-trade. This is also the single
        chokepoint for the RBP conviction overlay, so every sizing path (direct
        fill and OMS) blends confidence identically.
        """
        try:
            cash_val = float(cash)
            port_notional_val = float(port_notional)
            arrival_price_val = float(arrival_price)
            confidence_val = max(0.0, min(1.0, float(confidence)))
            ticker_weight_val = float(ticker_weight)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Numeric conversion failed in default_trade_size: {e}")
            return Sizing(0, 0.0, 0.0)

        signal_type = signal_type.upper()
        if signal_type not in ("BUY", "SELL", "HOLD"):
            self.logger.debug("Skip trade: unsupported signal_type=%s", signal_type)
            return Sizing(0, 0.0, 0.0)

        # RBP conviction overlay (single chokepoint for all portfolios and both
        # the direct-fill and OMS paths). Never propagates errors — a failing
        # overlay falls back to the raw confidence.
        if self.rbp_overlay is not None:
            try:
                confidence_val = max(
                    0.0,
                    min(
                        1.0,
                        float(
                            self.rbp_overlay(
                                portfolio_id, ticker, signal_type, confidence_val
                            )
                        ),
                    ),
                )
            except Exception as exc:
                self.logger.warning(
                    "RBP overlay raised unexpectedly for %s/%s: %s",
                    portfolio_id,
                    ticker,
                    exc,
                )

        if signal_type == "HOLD" or confidence_val == 0.0:
            self.logger.debug(
                "Skip trade: signal=%s confidence=%.2f", signal_type, confidence_val
            )
            return Sizing(0, 0.0, 0.0)

        # Buying power constrains both new buys and new shorts.
        buying_power = self._calculate_buying_power(
            port_notional_val, positions, ticker, arrival_price_val
        )

        exec_price = self.get_current_price(ticker)
        if exec_price <= 0:
            self.logger.error(
                f"Could not fetch valid execution price for {ticker}. Aborting."
            )
            return Sizing(0, 0.0, exec_price)

        current_pos_row = positions[positions["ticker"] == ticker]
        current_quantity = (
            float(current_pos_row["quantity"].iloc[0])
            if not current_pos_row.empty
            else 0.0
        )
        current_notional_value = current_quantity * exec_price

        target_notional = port_notional_val * ticker_weight_val
        if signal_type == "SELL":
            target_notional *= -1

        desired_trade_notional = (
            target_notional - current_notional_value
        ) * confidence_val

        # Ignore trades smaller than $1.00 notional.
        if abs(desired_trade_notional) < 1.0:
            self.logger.debug(
                "Skip trade: desired_notional too small (%.2f) for %s",
                desired_trade_notional,
                ticker,
            )
            return Sizing(0, desired_trade_notional, exec_price)

        if desired_trade_notional > 0:  # BUY: constrain by cash AND buying power
            final_trade_notional = min(desired_trade_notional, cash_val, buying_power)
        else:  # SELL/SHORT: constrain by buying power
            final_trade_notional = min(abs(desired_trade_notional), buying_power)

        if final_trade_notional < 1.0:
            self.logger.debug(
                "Skip trade: final_notional too small (%.2f) for %s",
                final_trade_notional,
                ticker,
            )
            return Sizing(0, desired_trade_notional, exec_price)

        quantity_to_trade = math.floor(final_trade_notional / exec_price)
        if quantity_to_trade == 0:
            self.logger.debug(
                "Skip trade: quantity_to_trade=0 (notional=%.2f price=%.2f) for %s",
                final_trade_notional,
                exec_price,
                ticker,
            )
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
        positions,  # This is a DataFrame
        port_notional,
        ticker_weight,
        timestamp,
    ):
        """
        Sizes the trade with the shared default model and, on a non-zero
        quantity, settles it through the database. Sizing (coercion, signal/price
        validation, buying power, live price fetch) lives in default_trade_size;
        this method owns only the fill and DB bookkeeping. Confidence blending
        (RBP overlay) happens inside default_trade_size, the shared chokepoint.
        """
        sizing = self.default_trade_size(
            portfolio_id=portfolio_id,
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
        signal_type = signal_type.upper()

        # Current position is needed for settlement; recompute locally (no I/O).
        current_pos_row = positions[positions["ticker"] == ticker]
        current_quantity = (
            float(current_pos_row["quantity"].iloc[0])
            if not current_pos_row.empty
            else 0.0
        )

        try:
            cash_val = float(cash)
            port_notional_val = float(port_notional)
            arrival_price_val = float(arrival_price)
            exec_price = sizing.exec_price
            slippage_bps = float(
                ((exec_price / arrival_price_val) - 1) * 10000
                if arrival_price_val > 0
                else 0
            )
        except (ValueError, TypeError) as e:
            self.logger.error(f"Numeric conversion failed: {e}")
            return

        # --- Execute and Update Database ---
        updated_cash = cash_val
        updated_quantity = current_quantity
        trade_value = quantity_to_trade * exec_price

        if sizing.desired_notional > 0:  # Finalizing a BUY
            updated_cash = cash_val - trade_value
            updated_quantity = current_quantity + quantity_to_trade
            port_notional_val = port_notional_val - trade_value
        elif sizing.desired_notional < 0:  # Finalizing a SELL
            updated_cash = cash_val + trade_value
            updated_quantity = current_quantity - quantity_to_trade
            port_notional_val = port_notional_val + trade_value

        return self.update_database(
            portfolio_id,
            ticker,
            signal_type,
            quantity_to_trade,
            updated_cash,
            updated_quantity,
            arrival_price_val,
            exec_price,
            slippage_bps,
            timestamp,
            port_notional_val,
        )

    def execute_child_order(
        self,
        child_order,
        cash,
        positions,  # DataFrame, same shape as execute_trade's
        port_notional,
        timestamp=None,
    ):
        """Settle one pre-sized OMS child order against FRESH portfolio state.

        The OMS execution seam (``OrderManager.manage_order`` calls this via
        its ``execute_child`` callable). Two rules are load-bearing:

        * **No re-sizing.** The parent was sized through
          ``default_trade_size`` (buying power, cash, RBP overlay) when it
          was created; this method fills the slice's fixed
          ``target_quantity``. Re-sizing here would double-apply confidence
          and make the worked schedule unpredictable.
        * **State must be fetched at fill time by the caller** (cash /
          positions / port_notional arguments) — typically via the
          portfolio's ``ATOMIC_STATE_QUERY`` right before this call. Never
          pass state captured when the order was submitted: slices execute
          minutes later, other fills move the books in between, and a stale
          snapshot writes wrong absolute cash/position values to the DB.

        Returns the OMS fill contract
        ``{"status": "success", "filled_quantity": ..., "fill_price": ...}``
        on success, or an ``{"status": "error", ...}`` dict / ``None``-ish
        result that ``manage_order`` routes to retry-then-cancel.
        """
        ticker = child_order.ticker
        signal_type = child_order.signal_type.value
        quantity_to_trade = float(child_order.target_quantity)
        if quantity_to_trade <= 0:
            return {
                "status": "error",
                "message": f"invalid child quantity {quantity_to_trade}",
            }

        try:
            cash_val = float(cash)
            port_notional_val = float(port_notional)
        except (ValueError, TypeError) as e:
            self.logger.error(f"Numeric conversion failed in execute_child_order: {e}")
            return {"status": "error", "message": str(e)}

        exec_price = self.get_current_price(ticker)
        if exec_price <= 0:
            self.logger.error(
                f"Could not fetch valid execution price for {ticker}; "
                f"child {child_order.child_id} not filled."
            )
            return {"status": "error", "message": f"no price for {ticker}"}

        # Slippage is measured against the parent's decision price so the
        # trade log shows per-slice implementation shortfall.
        arrival_price = float(child_order.arrival_price) or exec_price
        slippage_bps = (
            ((exec_price / arrival_price) - 1) * 10000 if arrival_price > 0 else 0.0
        )

        current_pos_row = positions[positions["ticker"] == ticker]
        current_quantity = (
            float(current_pos_row["quantity"].iloc[0])
            if not current_pos_row.empty
            else 0.0
        )

        # Settlement mirrors execute_trade; direction comes from the child's
        # explicit side, which the OMS derived from the SIGN of the sized
        # notional at parent creation (the same rule execute_trade applies —
        # see CLAUDE.md "Trade direction").
        trade_value = quantity_to_trade * exec_price
        if signal_type == "BUY":
            updated_cash = cash_val - trade_value
            updated_quantity = current_quantity + quantity_to_trade
            port_notional_val = port_notional_val - trade_value
        else:  # SELL
            updated_cash = cash_val + trade_value
            updated_quantity = current_quantity - quantity_to_trade
            port_notional_val = port_notional_val + trade_value

        if timestamp is None:
            # Repo convention: all timestamps in America/New_York (a UTC
            # fill written near midnight ET would land on the wrong
            # trading date in cash_equity_book).
            timestamp = datetime.now(pytz.timezone("America/New_York"))

        result = self.update_database(
            child_order.portfolio_id,
            ticker,
            signal_type,
            quantity_to_trade,
            updated_cash,
            updated_quantity,
            arrival_price,
            exec_price,
            slippage_bps,
            timestamp,
            port_notional_val,
        )
        if not isinstance(result, dict) or result.get("status") != "success":
            # DB transaction rolled back: nothing settled, let the OMS retry.
            return result if isinstance(result, dict) else {"status": "error"}

        return {
            "status": "success",
            "filled_quantity": quantity_to_trade,
            "fill_price": exec_price,
        }

    def update_database(
        self,
        portfolio_id,
        ticker,
        signal_type,
        quantity_to_trade,
        updated_cash,
        updated_quantity,
        arrival_price,
        exec_price,
        slippage_bps,
        timestamp,
        port_notional,
    ):
        """
        Update database tables after trade execution within a single transaction.
        If any operation fails, all changes are rolled back.
        """
        date_part = timestamp.date()
        trade_notional = abs(quantity_to_trade * exec_price)
        if signal_type == "SELL":
            trade_notional = -trade_notional

        conn = None
        try:
            conn = self.dbconn.get_connection()
            if not conn:
                self.logger.error("Failed to get a database connection from the pool.")
                return

            with conn.cursor() as cursor:
                # Update cash_equity_book
                cash_query = """
                    INSERT INTO cash_equity_book (timestamp, date, portfolio_id, currency, notional)
                    VALUES (%s, %s, %s, %s, %s)
                """
                cash_values = (
                    timestamp,
                    date_part,
                    portfolio_id,
                    "USD",
                    float(round(updated_cash, 2)),
                )
                cursor.execute(cash_query, cash_values)

                # Update positions_book (upsert: insert or update if exists)
                position_query = """
                    INSERT INTO positions_book (portfolio_id, ticker, quantity, updated_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (portfolio_id, ticker)
                    DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = EXCLUDED.updated_at
                """
                position_values = (
                    portfolio_id,
                    ticker,
                    float(updated_quantity),
                    timestamp,
                )
                cursor.execute(position_query, position_values)

                # Insert trade log with new fields
                trade_log_query = """
                    INSERT INTO trade_execution_logs (
                        portfolio_id, ticker, exec_timestamp, side, quantity,
                        arrival_price, exec_price, slippage_bps,
                        notional, notional_local, currency, fx_rate
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                trade_log_values = (
                    portfolio_id,
                    ticker,
                    timestamp,
                    signal_type,
                    float(quantity_to_trade),
                    float(round(arrival_price, 2)),
                    float(round(exec_price, 2)),
                    float(round(slippage_bps, 2)),
                    float(round(port_notional, 2)),
                    float(round(trade_notional, 2)),
                    "USD",
                    1.0,
                )
                cursor.execute(trade_log_query, trade_log_values)

            conn.commit()
            self.logger.info(
                f"Database successfully updated\n [Portfolio {portfolio_id} | Cash: ${updated_cash:,.2f} | Time: {timestamp}]: {signal_type} {quantity_to_trade} {ticker} @ ${exec_price:,.2f}.\n"
            )
            return {
                "status": "success",
                "quantity": quantity_to_trade,
                "updated_cash": updated_cash,
            }

        except Exception as e:
            self.logger.exception(
                "Database update transaction failed. Rolling back all changes."
            )
            if conn:
                try:
                    conn.rollback()
                except Exception as rollback_error:
                    self.logger.error(
                        f"Failed to rollback transaction: {rollback_error}"
                    )
            return {"status": "error", "message": str(e)}

        finally:
            if conn:
                self.dbconn.release_connection(conn)

    def get_current_price(self, ticker):
        """
        Fetch real-time stock price for a single ticker using FMP API.
        Returns float: Current price of the ticker, or 0.0 if not found or on error.
        """
        url = f"https://financialmodelingprep.com/api/v3/quote/{ticker}"
        params = {"apikey": self.fmp_api_key}

        try:
            data = self.marketData._make_request(url, params)
            if (
                isinstance(data, list)
                and data
                and "price" in data[0]
                and data[0]["price"] is not None
            ):
                return float(data[0]["price"])

            self.logger.warning(
                f"No valid price found for ticker {ticker} in API response. Response: {data}"
            )
            return 0.0

        except Exception as e:
            self.logger.error(f"Price fetch failed for {ticker}: {e}")
            return 0.0

    def liquidate(self, portfolio_id):
        """
        Liquidates all long and short positions for a given portfolio.
        """
        self.logger.warning(
            f"Attempting to liquidate all positions for portfolio {portfolio_id}."
        )

        # This logic would need access to the full portfolio state (cash, positions, etc.)
        # A complete implementation is complex as it requires fetching the current state.
        # This is a conceptual fix for the logic.

        # 1. Fetch current positions from the database
        # 2. Fetch current cash and calculate port_notional

        # For each position:
        #   If quantity > 0 (long), create a SELL signal for the full quantity.
        #   If quantity < 0 (short), create a BUY signal for the absolute quantity.
        #   Call self.execute_trade() for each signal.

        # NOTE: The original `liquidate` function is flawed and needs a significant rewrite
        # to fetch the full portfolio state before it can generate the correct closing trades.
        # The provided code has a bug calling a non-existent `self.sell`.
        self.logger.error(
            "The liquidate function is not fully implemented and contains logical errors."
        )
