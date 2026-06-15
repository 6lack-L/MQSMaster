from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import src.backtest.runner as runner_module
from src.backtest.executor import BacktestExecutor
from src.backtest.runner import BacktestRunner
from src.backtest.utils import fetch_historical_data
from src.portfolios.portfolio_BASE.strategy import BasePortfolio

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.workflow_backtest,
]


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def execute_query(self, _sql, _params, fetch=True):
        return {"status": "success", "data": list(self._rows)}


class _DummyPortfolio(BasePortfolio):
    def OnData(self, context):
        return None


def _build_rows(start, minutes, tickers):
    rows = []
    for idx in range(minutes):
        ts = start + timedelta(minutes=idx)
        for ticker in tickers:
            base = 100.0 + (idx * 0.5)
            rows.append(
                {
                    "timestamp": ts,
                    "ticker": ticker,
                    "open_price": base - 0.2,
                    "high_price": base + 0.4,
                    "low_price": base - 0.6,
                    "close_price": base,
                    "volume": 1000 + idx,
                }
            )
    return rows


def _build_portfolio(rows, tickers):
    config = {
        "PORTFOLIO_ID": "p1",
        "TICKERS": tickers,
        "INTERVAL": 60,
        "LOOKBACK_DAYS": 1,
        "DATA_FEEDS": ["MARKET_DATA", "POSITIONS", "CASH_EQUITY", "PORT_NOTIONAL"],
    }
    db = _FakeDB(rows)
    return _DummyPortfolio(
        db_connector=db,
        executor=None,
        debug=True,
        config_dict=config,
        backtest_start_date=datetime(2025, 1, 1),
    )


def test_backtest_runner_wires_order_manager_post_construction():
    """BacktestRunner threads the OMS onto the portfolio post-construction
    (mirroring the live engine) when its executor is set up."""
    rows = _build_rows(datetime(2025, 1, 1, 9, 30), 10, ["AAPL"])
    portfolio = _build_portfolio(rows, ["AAPL"])
    assert portfolio.order_manager is None

    sentinel_om = object()
    runner = BacktestRunner(
        portfolio=portfolio,
        start_date="2025-01-01",
        end_date="2025-01-02",
        initial_capital=100000.0,
        slippage=0.0,
        order_manager=sentinel_om,
    )
    runner._setup_executor()

    assert portfolio.order_manager is sentinel_om


def test_backtest_completion(monkeypatch):
    rows = _build_rows(datetime(2025, 1, 1, 9, 30), 10, ["AAPL"])
    portfolio = _build_portfolio(rows, ["AAPL"])

    monkeypatch.setattr(runner_module, "generate_backtest_report", lambda **_: None)

    runner = BacktestRunner(
        portfolio=portfolio,
        start_date="2025-01-01",
        end_date="2025-01-02",
        initial_capital=100000.0,
        slippage=0.0,
    )

    trade_log = runner.run()
    assert isinstance(trade_log, list)


def test_backtest_returns_numeric(monkeypatch):
    rows = _build_rows(datetime(2025, 1, 1, 9, 30), 10, ["AAPL"])
    portfolio = _build_portfolio(rows, ["AAPL"])

    captured = {}

    def _capture_report(**kwargs):
        captured["perf_df"] = kwargs.get("perf_df")

    monkeypatch.setattr(runner_module, "generate_backtest_report", _capture_report)

    runner = BacktestRunner(
        portfolio=portfolio,
        start_date="2025-01-01",
        end_date="2025-01-02",
        initial_capital=100000.0,
        slippage=0.0,
    )

    runner.run()
    perf_df = captured.get("perf_df")
    assert perf_df is not None
    assert not perf_df.empty
    assert perf_df["portfolio_value"].notna().all()
    assert np.isfinite(perf_df["portfolio_value"]).all()


def test_backtest_data_quality():
    rows = _build_rows(datetime(2025, 1, 1, 9, 30), 5, ["AAPL"])
    rows.append(
        {
            "timestamp": "not-a-date",
            "ticker": "AAPL",
            "open_price": 0.0,
            "high_price": 0.0,
            "low_price": 0.0,
            "close_price": None,
            "volume": 0.0,
        }
    )

    portfolio = _build_portfolio(rows, ["AAPL"])
    df = fetch_historical_data(
        portfolio=portfolio,
        start_date=datetime(2025, 1, 1),
        end_date=datetime(2025, 1, 2),
    )

    assert not df.empty
    assert df["timestamp"].is_monotonic_increasing
    assert df["close_price"].notna().all()


def test_backtest_weights_normalized_when_missing():
    executor = BacktestExecutor(
        initial_capital=10000.0,
        tickers=["AAPL", "MSFT"],
        leverage=2.0,
        slippage=0.0,
    )

    result = executor.execute_trade(
        portfolio_id="p1",
        ticker="AAPL",
        signal_type="BUY",
        confidence=1.0,
        arrival_price=100.0,
        cash=executor.cash,
        positions=pd.DataFrame(),
        port_notional=executor.get_port_notional(),
        ticker_weight=0.0,
        timestamp=datetime(2025, 1, 1),
    )

    assert result is not None
    assert len(executor.trade_log) > 0, "Expected at least one trade to be recorded"
    trade = executor.trade_log[-1]
    trade_value = trade["shares"] * trade["fill_price"]
    # With 2 tickers and ticker_weight=0.0, expect normalization to equal weights
    # Max allocation per ticker = leveraged_notional / num_tickers = 20000 / 2 = 10000
    # Adjust expected cap based on actual normalization logic
    max_expected_trade_value = 10000.0  # or document why 5000
    assert trade_value <= max_expected_trade_value
