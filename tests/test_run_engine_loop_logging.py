import json
import logging

from src.live_trading.engine import RunEngine


class _BehaviorPortfolio:
    def __init__(
        self,
        engine,
        *,
        portfolio_id="p-test",
        debug=False,
        fail_first_n=0,
        stop_after_successes=1,
    ):
        self.engine = engine
        self.portfolio_id = portfolio_id
        self.debug = debug
        self.poll_interval = 0.01
        self.data_feeds = []
        self._call_count = 0
        self._fail_first_n = fail_first_n
        self._success_count = 0
        self._stop_after_successes = stop_after_successes

    def get_data(self, _feeds):
        return {"ok": True}

    def generate_signals_and_trade(self, _data, current_time=None):
        self._call_count += 1
        if self._call_count <= self._fail_first_n:
            raise RuntimeError("synthetic failure")

        self._success_count += 1
        if self._success_count >= self._stop_after_successes:
            self.engine.running = False


def test_failure_counter_increments_on_exception(monkeypatch):
    engine = RunEngine(
        db_connector=object(),
        executor=object(),
        max_consecutive_failures=2,
    )
    portfolio = _BehaviorPortfolio(engine, fail_first_n=99, stop_after_successes=99)
    engine.failure_counts[portfolio.portfolio_id] = 0

    monkeypatch.setattr("src.live_trading.engine.time.sleep", lambda *_: None)

    engine._run_portfolio(portfolio)

    assert engine.failure_counts[portfolio.portfolio_id] == 2


def test_failure_counter_resets_after_recovery(monkeypatch):
    engine = RunEngine(db_connector=object(), executor=object())
    portfolio = _BehaviorPortfolio(engine, fail_first_n=1, stop_after_successes=1)
    engine.failure_counts[portfolio.portfolio_id] = 0

    monkeypatch.setattr("src.live_trading.engine.time.sleep", lambda *_: None)

    engine._run_portfolio(portfolio)

    assert portfolio._call_count == 2
    assert engine.failure_counts[portfolio.portfolio_id] == 0


def test_circuit_breaker_stops_after_threshold(monkeypatch, caplog):
    engine = RunEngine(
        db_connector=object(),
        executor=object(),
        max_consecutive_failures=3,
    )
    portfolio = _BehaviorPortfolio(engine, fail_first_n=99, stop_after_successes=99)
    engine.failure_counts[portfolio.portfolio_id] = 0

    monkeypatch.setattr("src.live_trading.engine.time.sleep", lambda *_: None)

    with caplog.at_level(logging.CRITICAL, logger="RunEngine"):
        engine._run_portfolio(portfolio)

    assert engine.failure_counts[portfolio.portfolio_id] == 3
    assert any("CIRCUIT BREAKER TRIPPED" in rec.getMessage() for rec in caplog.records)


def test_load_portfolios_wires_order_manager_post_construction(monkeypatch, tmp_path):
    """RunEngine.load_portfolios assigns the OMS onto the portfolio
    post-construction (mirroring BacktestRunner), not via the constructor.

    The fake portfolio intentionally does NOT accept ``order_manager`` in its
    ``__init__`` so that a revert to constructor-passing would fail loudly with
    a TypeError instead of silently regressing.
    """
    from src.live_trading import engine as engine_mod

    sentinel_om = object()

    class _InitOnlyPortfolio:
        def __init__(self, db_connector, executor, debug=False, config_dict=None):
            self.portfolio_id = config_dict["PORTFOLIO_ID"]
            self.order_manager = None

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"PORTFOLIO_ID": "live-test"}))

    monkeypatch.setattr(
        engine_mod.inspect, "getfile", lambda _cls: str(tmp_path / "strategy.py")
    )
    monkeypatch.setattr(
        "src.oms.factory.resolve_portfolio_id", lambda cfg: cfg["PORTFOLIO_ID"]
    )
    monkeypatch.setattr(
        "src.oms.factory.build_order_manager", lambda *a, **k: sentinel_om
    )

    engine = RunEngine(db_connector=object(), executor=object())
    engine.load_portfolios([_InitOnlyPortfolio])

    assert len(engine.portfolios) == 1
    assert engine.portfolios[0].order_manager is sentinel_om


def test_debug_mode_runs_single_cycle(monkeypatch):
    engine = RunEngine(db_connector=object(), executor=object())
    portfolio = _BehaviorPortfolio(
        engine,
        debug=True,
        fail_first_n=0,
        stop_after_successes=99,
    )
    engine.failure_counts[portfolio.portfolio_id] = 0

    monkeypatch.setattr("src.live_trading.engine.time.sleep", lambda *_: None)

    engine._run_portfolio(portfolio)

    assert portfolio._call_count == 1
    assert engine.failure_counts[portfolio.portfolio_id] == 0
