# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

MQS Trading System — a quantitative trading bot (`mqs_bot`). Python, src-layout (the package lives under `src/`, declared in `pyproject.toml` as `packages.find where = ["src"]`). Runtime is Python 3.10 (CI). pandas is pinned to `2.2.2` and numpy to `<=1.26.4` — do not assume newer pandas APIs.

Rich internal docs live in `docs/` — start at `docs/README.md` (architecture diagrams + a command table + a workflow index under `docs/workflows/`). Prefer reading the relevant `docs/workflows/*.md` before large changes; this file is the quick orientation.

## Environment & commands

There is a project virtualenv at `MQS/` in the repo root. Activate it before running anything:

```bash
source MQS/bin/activate          # local venv (Docker image uses /app/MQS/bin/python)
```

| Task | Command |
|------|---------|
| Run backtest (all portfolios, parallel) | `python -m src.main_backtest` |
| Run live trading | `python -m src.main` |
| Backfill historical bars | `python -m src.orchestrator.backfill.backfill_cli concurrent --start ... --end ... --tickers ...` |
| Daily capital rebalance | `python -m src.risk_manager.daily_allocator` |
| NLP sentiment daemon | `python NLP/daemon.py start` |
| Full live stack (market-hours watchdog) | `./start.sh` |

`start.sh` is the production launcher: it sources `.env`, checks market hours via FMP, launches `src/main.py` + ingestors + NLP daemon as background processes, and kills them when the market closes.

## Tests

```bash
pytest -m smoke                                   # fast PR-gate tier
pytest -m "smoke and workflow_backtest"           # exactly what CI runs on PRs to dev
pytest tests/test_x.py::TestClass::test_name      # single test
```

- **Markers are strict** (`--strict-markers`) and defined in `pytest.ini`: `db`, `api`, `smoke`, `slow`, `e2e`, and `workflow_*` (backfill/live/backtest/indicators/nlp). Tag new tests accordingly.
- **Warnings are errors**: `filterwarnings = error::DeprecationWarning / PendingDeprecationWarning / FutureWarning`. A pandas deprecation will *fail* the test, not just warn.
- `db`-marked tests need a live PostgreSQL (via the `db_connection` fixture in `tests/conftest.py`, env vars `host/port/database/db_user/password`); `api`-marked tests need `FMP_API_KEY`. Tests that need neither construct objects with `__new__` + monkeypatched methods (see `tests/test_trade_executor_constraints.py`) — follow that pattern for fast unit coverage.
- CI lint is flake8 (`--select=E9,F63,F7,F82` is the hard gate; the rest is `--exit-zero`).

## Architecture

Two execution pipelines share the same strategies and the same user-facing trade API (`StrategyContext`), but differ in their executor and concurrency model — this difference is load-bearing.

**Strategies & config.** Each strategy is `src/portfolios/portfolio_<n>/strategy.py`, subclassing `BasePortfolio` (`portfolio_BASE/strategy.py`) and implementing `OnData(context)`. Every strategy dir has a paired `config.json`. **Configs are loaded dynamically by file location** (`inspect.getfile(portfolio_cls)` → sibling `config.json`), *not* by import — so the engine and the live engine both discover config the same way. Indicators are loaded by name via `importlib`: a new indicator just needs a file under `src/portfolios/indicators/` whose snake_case filename matches its CamelCase class (`AddIndicator` / `RegisterIndicatorSet` in the base class do the wiring + warmup).

**`StrategyContext`** (`src/portfolios/order_interface.py`) is the API handed to `OnData`: `context.buy/sell(...)`, `context.Market[ticker]`, `context.Portfolio`. Both pipelines build it in `BasePortfolio.generate_signals_and_trade` and route trades through `context._trade → executor.execute_trade(...)`. It is the single shared seam — changes here affect both backtest and live.

**Backtest pipeline.** `src/main_backtest.py` fans portfolios across a `ProcessPoolExecutor` (one batch per CPU) → `BacktestEngine.run()` → per portfolio either *event mode* (`BacktestRunner` drives a timestamp loop against a **per-portfolio** `BacktestExecutor` with a unified long/short margin model) or *fast mode* (vectorized returns + Monte Carlo via `vector_strategy_adapters`). Reports/CSVs land under `src/backtest/data/`.

**Live pipeline.** `src/main.py` builds **one shared** `tradeExecutor` (writes fills to PostgreSQL) and **one** `RunEngine`. `RunEngine.load_portfolios` reads each config and runs each portfolio in **its own thread** with a consecutive-failure circuit breaker. The live executor is shared across those threads; the backtest executor is not.

**OMS** (`src/oms/`) is gated per-portfolio behind `config.json` `OMS.enabled` (built once via `src/oms/factory.py`; absolute `src.oms...` imports only). When enabled, `StrategyContext._trade` sizes through the executor's `default_trade_size` (the RBP chokepoint), registers a `ParentOrder`, and the OMS works it as scheduled `ChildOrder` slices (TWAP/VWAP/MARKET in `src/oms/sizing/`). Fills happen when a **pump** calls `OrderManager.manage_order(now, execute_child)`: the backtest runner pumps per bar with simulated time; the live `RunEngine` runs one dedicated OMS tick thread (`oms_tick_seconds`, default 5s) across all portfolios. Load-bearing rules: (1) the per-portfolio `OrderManager` is threaded as a value (`BasePortfolio.order_manager` → `StrategyContext`) and `execute_child` is a closure — never attach OMS state to the **shared** live `tradeExecutor`; (2) `execute_child_order` does **no re-sizing** and, in live, must receive portfolio state fetched **at fill time** (never submit-time snapshots); (3) the OMS execution side comes from the **sign** of the sized notional, same rule as `execute_trade` (see "Trade direction" below). Design + status: `docs/OMS/OMS_DESIGN.md` §1a. Not built yet: DB persistence of orders, VWAP volume-profile provider, LIMIT/STOP.

**Supporting subsystems.** `src/orchestrator/` (FMP ingestion, backfill CLI, real-time ingestor, parquet cache); `src/risk_manager/` (master-portfolio capital allocation / daily rebalance); `src/common/` (DB connector `MQSDBConnector`, auth, log pipelines); `NLP/` (FinBERT sentiment daemon → `news_sentiment` table); `RBP/` (research model feeding portfolio 5).

## Conventions & gotchas

- **Dual import paths.** `live_trading/`, `portfolios/`, `orchestrator/` use a try-relative-then-absolute import idiom (`from portfolios... except ImportError: from src.portfolios...`) because code is sometimes run as a script with `src` on the path and sometimes as `src.*` modules. `oms/` uses absolute `from src.oms...` only. Consequence: the *same* class can resolve to two distinct module objects depending on entry point, which breaks `isinstance` across the two paths — match the surrounding file's import style and don't mix.
- **Timezone.** All timestamps are stored and normalized in `America/New_York`; SQL queries convert through that zone.
- **DB state is read atomically.** `BasePortfolio.ATOMIC_STATE_QUERY` fetches cash + positions in one query to avoid races; positions/cash auto-seed on first run.
- **Trade direction** in `execute_trade` is keyed off the *signed* `desired_trade_notional` (negative ⇒ SELL), not the always-non-negative sized notional — keep BUY/SELL settlement consistent across both executors.
