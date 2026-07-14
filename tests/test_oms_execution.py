"""Coverage for the OMS execution seam (the wiring ported from sim_scheduler).

Verifies the two ``execute_child_order`` implementations and the pump wiring
contracts:

  * BacktestExecutor.execute_child_order — settles slices into the unified
    in-memory portfolio at current simulated prices, no re-sizing.
  * tradeExecutor.execute_child_order — settles against caller-supplied FRESH
    state and returns the OMS fill contract; DB errors route to retry.
  * OrderManager + BacktestExecutor end-to-end — a sliced parent works to
    completion through manage_order.
  * RunEngine OMS pump — fresh-state adapter behavior and shutdown drain.
  * StrategyContext direction rule — OMS side follows the SIGN of the sized
    notional (CLAUDE.md "Trade direction"), not the raw signal.

Everything is in-memory / mocked (no DB, no API), following the
``__new__`` + monkeypatched-methods pattern from
tests/test_trade_executor_constraints.py.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import Mock

import pandas as pd
import pytest

from src.backtest.executor import BacktestExecutor, Sizing
from src.live_trading.executor import tradeExecutor
from src.oms.order_manager import OrderManager
from src.oms.order_structs import OrderStatus, Side, tz
from src.oms.sizing.base import child_from_parent

pytestmark = [pytest.mark.smoke]

T0 = tz.localize(datetime(2026, 1, 5, 10, 0, 0))


def _child(ticker="AAPL", side=Side.BUY, quantity=10.0, arrival=100.0,
           portfolio_id="1", scheduled_time=None):
    """Build a self-contained child order without going through an algorithm."""
    from src.oms.order_structs import ChildOrder

    return ChildOrder(
        parent_order_id="parent-1",
        portfolio_id=portfolio_id,
        ticker=ticker,
        signal_type=side,
        target_quantity=quantity,
        scheduled_time=scheduled_time or T0,
        arrival_price=arrival,
        confidence=0.8,
    )


@pytest.mark.workflow_backtest
class TestBacktestExecuteChildOrder:
    def _executor(self, cash=100000.0, price=100.0):
        executor = BacktestExecutor(
            initial_capital=cash, tickers=["AAPL"], slippage=0.0
        )
        executor.update_price("AAPL", price)
        return executor

    def test_buy_settles_into_unified_state(self):
        executor = self._executor()
        result = executor.execute_child_order(_child(quantity=10.0), timestamp=T0)
        assert result["status"] == "success"
        assert result["filled_quantity"] == 10.0
        assert result["fill_price"] == 100.0
        assert executor.cash == 100000.0 - 1000.0
        assert executor.positions["AAPL"] == 10.0
        (entry,) = executor.trade_log
        assert entry["signal_type"] == "BUY"
        assert entry["portfolio_id"] == "1"
        assert entry["confidence"] == 0.8
        assert entry["timestamp"] == T0

    def test_sell_settles_opposite_direction(self):
        executor = self._executor()
        executor.positions["AAPL"] = 50.0
        result = executor.execute_child_order(
            _child(side=Side.SELL, quantity=10.0), timestamp=T0
        )
        assert result["status"] == "success"
        assert executor.cash == 100000.0 + 1000.0
        assert executor.positions["AAPL"] == 40.0

    def test_no_price_reports_error_for_retry(self):
        executor = BacktestExecutor(
            initial_capital=100000.0, tickers=["AAPL"], slippage=0.0
        )  # no update_price -> price 0.0
        result = executor.execute_child_order(_child(), timestamp=T0)
        assert result["status"] == "error"
        assert executor.cash == 100000.0
        assert executor.trade_log == []

    def test_slippage_applied_per_slice(self):
        executor = BacktestExecutor(
            initial_capital=100000.0, tickers=["AAPL"], slippage=0.01
        )
        executor.update_price("AAPL", 100.0)
        result = executor.execute_child_order(_child(quantity=10.0), timestamp=T0)
        assert result["fill_price"] == pytest.approx(101.0)


@pytest.mark.workflow_backtest
class TestOmsBacktestEndToEnd:
    """A sliced parent works to completion against a real BacktestExecutor,
    pumped the same way BacktestRunner pumps (per-bar, sim time)."""

    def test_twap_parent_fills_through_pump(self):
        executor = BacktestExecutor(
            initial_capital=100000.0, tickers=["AAPL"], slippage=0.0
        )
        executor.update_price("AAPL", 100.0)
        manager = OrderManager(
            portfolio_id="1",
            config={"default_algo": "TWAP", "twap_num_slices": 4,
                    "duration_minutes": 20},
        )
        parent = manager.process_order(
            portfolio_id="1",
            ticker="AAPL",
            side="BUY",
            confidence=0.8,
            arrival_price=100.0,
            total_quantity=100.0,
            timestamp=T0,
        )

        # Pump like the runner does: per bar, prices already updated.
        sim_time = T0
        for _ in range(5):
            manager.manage_order(
                now=sim_time,
                execute_child=lambda child, _ts=sim_time: (
                    executor.execute_child_order(child, timestamp=_ts)
                ),
            )
            sim_time += timedelta(minutes=5)

        assert parent.status == OrderStatus.FILLED
        assert parent.filled_quantity == 100.0
        assert executor.positions["AAPL"] == 100.0
        assert executor.cash == pytest.approx(100000.0 - 100.0 * 100.0)
        # Four distinct fills, one per slice, at four distinct sim times.
        assert len(executor.trade_log) == 4
        assert len({e["timestamp"] for e in executor.trade_log}) == 4


@pytest.mark.workflow_live
class TestLiveExecuteChildOrder:
    def _executor(self, price_map, db_results=None):
        executor = tradeExecutor.__new__(tradeExecutor)
        executor.dbconn = object()
        executor.leverage = 2.0
        executor.rbp_overlay = None
        executor.logger = logging.getLogger("test_oms_live_executor")
        executor.get_current_price = lambda ticker: float(
            price_map.get(ticker, 0.0)
        )
        executor.db_calls = []

        def fake_update_database(*args):
            executor.db_calls.append(args)
            return (db_results or {"status": "success"})

        executor.update_database = fake_update_database
        return executor

    def _positions(self):
        return pd.DataFrame({"ticker": ["AAPL"], "quantity": [50.0]})

    def test_buy_settles_against_fresh_state(self):
        executor = self._executor({"AAPL": 102.0})
        result = executor.execute_child_order(
            _child(quantity=10.0, arrival=100.0),
            cash=50000.0,
            positions=self._positions(),
            port_notional=150000.0,
            timestamp=T0,
        )
        assert result == {
            "status": "success",
            "filled_quantity": 10.0,
            "fill_price": 102.0,
        }
        (call,) = executor.db_calls
        (pid, ticker, side, qty, updated_cash, updated_qty,
         arrival, exec_price, slippage_bps, ts, port_notional) = call
        assert (pid, ticker, side, qty) == ("1", "AAPL", "BUY", 10.0)
        assert updated_cash == 50000.0 - 10.0 * 102.0
        assert updated_qty == 60.0  # fresh 50 + 10, not a stale snapshot
        assert arrival == 100.0
        assert slippage_bps == pytest.approx(200.0)  # 102 vs 100 arrival
        assert ts == T0
        assert port_notional == 150000.0 - 10.0 * 102.0

    def test_sell_settles_opposite_direction(self):
        executor = self._executor({"AAPL": 102.0})
        executor.execute_child_order(
            _child(side=Side.SELL, quantity=10.0, arrival=100.0),
            cash=50000.0,
            positions=self._positions(),
            port_notional=150000.0,
            timestamp=T0,
        )
        (call,) = executor.db_calls
        assert call[2] == "SELL"
        assert call[4] == 50000.0 + 10.0 * 102.0  # cash increases
        assert call[5] == 40.0  # position decreases

    def test_no_price_reports_error_without_db_write(self):
        executor = self._executor({})
        result = executor.execute_child_order(
            _child(),
            cash=50000.0,
            positions=self._positions(),
            port_notional=150000.0,
            timestamp=T0,
        )
        assert result["status"] == "error"
        assert executor.db_calls == []

    def test_db_rollback_routes_to_retry(self):
        executor = self._executor(
            {"AAPL": 102.0}, db_results={"status": "error", "message": "boom"}
        )
        result = executor.execute_child_order(
            _child(),
            cash=50000.0,
            positions=self._positions(),
            port_notional=150000.0,
            timestamp=T0,
        )
        # The error dict passes through so OrderManager._parse_fill treats
        # the attempt as failed and re-queues the slice.
        assert result["status"] == "error"


@pytest.mark.workflow_live
class TestEnginePumpWiring:
    def _engine(self):
        from src.live_trading.engine import RunEngine

        return RunEngine(db_connector=object(), executor=Mock())

    def _portfolio_with_state(self, state):
        portfolio = Mock()
        portfolio.portfolio_id = "1"
        portfolio.get_data = Mock(return_value=state)
        return portfolio

    def test_child_executor_binds_fresh_state_per_call(self):
        engine = self._engine()
        state = {
            "CASH_EQUITY": pd.DataFrame({"notional": [50000.0]}),
            "POSITIONS": pd.DataFrame({"ticker": ["AAPL"], "quantity": [50.0]}),
            "PORT_NOTIONAL": pd.DataFrame({"notional": [150000.0]}),
        }
        portfolio = self._portfolio_with_state(state)
        engine.executor.execute_child_order = Mock(
            return_value={"status": "success", "filled_quantity": 1.0,
                          "fill_price": 100.0}
        )

        execute = engine._make_child_executor(portfolio)
        child = _child()
        execute(child)
        execute(child)

        # State is re-fetched on EVERY execution (fresh, never cached).
        assert portfolio.get_data.call_count == 2
        _, kwargs = engine.executor.execute_child_order.call_args
        assert kwargs["cash"] == 50000.0
        assert kwargs["port_notional"] == 150000.0

    def test_child_executor_fails_soft_when_state_unreadable(self):
        engine = self._engine()
        portfolio = self._portfolio_with_state(
            {
                "CASH_EQUITY": pd.DataFrame(),
                "POSITIONS": pd.DataFrame(),
                "PORT_NOTIONAL": pd.DataFrame(),
            }
        )
        engine.executor.execute_child_order = Mock()

        result = engine._make_child_executor(portfolio)(_child())

        assert result["status"] == "error"
        engine.executor.execute_child_order.assert_not_called()

    def test_pump_drains_open_orders_on_shutdown(self):
        engine = self._engine()
        order_manager = Mock()
        order_manager.cancel_all_open_orders = Mock(return_value=2)
        portfolio = Mock()
        portfolio.portfolio_id = "1"
        portfolio.order_manager = order_manager
        engine.portfolios = [portfolio]

        engine.running = False  # loop exits immediately, drain still runs
        engine._run_oms_pump()

        order_manager.cancel_all_open_orders.assert_called_once_with(
            reason="engine shutdown"
        )


class TestStrategyContextDirectionRule:
    """OMS execution side must follow the sign of the sized notional, exactly
    like execute_trade's settlement (CLAUDE.md "Trade direction")."""

    def _context(self, order_manager, sizing):
        from src.portfolios.order_interface import StrategyContext

        dates = pd.date_range(
            "2026-01-05", periods=2, freq="D", tz="America/New_York"
        )
        market_data = pd.DataFrame(
            [
                {
                    "timestamp": date,
                    "ticker": "AAPL",
                    "open_price": 100.0,
                    "high_price": 101.0,
                    "low_price": 99.0,
                    "close_price": 100.0,
                    "volume": 1_000_000,
                }
                for date in dates
            ]
        )
        executor = Mock()
        executor.default_trade_size.return_value = sizing
        return StrategyContext(
            market_data_df=market_data,
            cash_df=pd.DataFrame({"notional": [50000.0]}),
            positions_df=pd.DataFrame({"ticker": ["AAPL"], "quantity": [100.0]}),
            port_notional_df=pd.DataFrame({"notional": [150000.0]}),
            current_time=pd.Timestamp("2026-01-06", tz="America/New_York"),
            executor=executor,
            portfolio_config={"id": "1"},
            order_manager=order_manager,
        )

    def test_buy_signal_with_negative_notional_registers_sell(self):
        # Over-weighted position: BUY signal sizes to a NEGATIVE notional,
        # i.e. the trade that moves toward target is a trim (SELL).
        order_manager = OrderManager(portfolio_id="1", config={})
        context = self._context(
            order_manager, Sizing(quantity=10, desired_notional=-1000.0,
                                  exec_price=100.0)
        )
        context.buy("AAPL", confidence=0.8)
        (parent,) = order_manager.orders_by_id.values()
        assert parent.signal_type == Side.SELL

    def test_buy_signal_with_positive_notional_registers_buy(self):
        order_manager = OrderManager(portfolio_id="1", config={})
        context = self._context(
            order_manager, Sizing(quantity=10, desired_notional=1000.0,
                                  exec_price=100.0)
        )
        context.buy("AAPL", confidence=0.8)
        (parent,) = order_manager.orders_by_id.values()
        assert parent.signal_type == Side.BUY
