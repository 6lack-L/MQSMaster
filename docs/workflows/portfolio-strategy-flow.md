# Portfolio / Strategy Flow

How a strategy goes from "I think AAPL should be bought" to a database write, and how indicators stay warm while it does.

## Class Hierarchy

```mermaid
classDiagram
    class BasePortfolio {
        <<abstract>>
        +db, executor, tickers, lookback_days, poll_interval
        +AddIndicator(name, ticker, **kwargs)
        +RegisterIndicatorSet(definitions)
        +get_data(data_feeds)
        +generate_signals_and_trade(data, current_time)
        +OnData(context)*  // abstract
    }

    class VolMomentum
    class MomentumStrategy
    class RegimeAdaptiveStrategy
    class TrendRotateStrategy
    class RBPStrategy {
        +rbp_model: RBPModel
        +buy_threshold, sell_threshold
    }
    class CrossoverRmiStrategy

    BasePortfolio <|-- VolMomentum
    BasePortfolio <|-- MomentumStrategy
    BasePortfolio <|-- RegimeAdaptiveStrategy
    BasePortfolio <|-- TrendRotateStrategy
    BasePortfolio <|-- RBPStrategy
    BasePortfolio <|-- CrossoverRmiStrategy

    class StrategyContext {
        +Market: MarketData
        +Portfolio: PortfolioManager
        +time: datetime
        +buy(ticker, confidence)
        +sell(ticker, confidence)
    }

    class MarketData {
        +__getitem__(ticker) AssetData
        +__contains__(ticker) bool
    }

    class AssetData {
        +Open, High, Low, Close, Volume
        +Timestamp, Exists
        +History(lookback_period)
    }

    class PortfolioManager {
        +cash, total_value, positions
        +get_asset_value(ticker, price)
        +get_asset_weight(ticker, price)
    }

    StrategyContext o-- MarketData
    StrategyContext o-- PortfolioManager
    MarketData ..> AssetData
```

## Strategy Lifecycle (event mode)

```mermaid
sequenceDiagram
    participant Engine as RunEngine / BacktestRunner
    participant Portfolio as Portfolio (subclass of BasePortfolio)
    participant Indicators
    participant Context as StrategyContext
    participant Executor as tradeExecutor / BacktestExecutor
    participant DB

    Engine->>Portfolio: get_data(data_feeds)
    Portfolio->>DB: ATOMIC_STATE_QUERY (cash + positions in one round-trip)
    Portfolio->>DB: market_data + pnl_book queries
    DB-->>Portfolio: state snapshot
    Portfolio-->>Engine: data_dict

    Engine->>Portfolio: generate_signals_and_trade(data_dict, current_time)
    loop For each new bar (per-ticker, chronological)
        Portfolio->>Indicators: indicator.Update(timestamp, price, ...)
    end
    Portfolio->>Context: StrategyContext(market, portfolio, time, executor)
    Portfolio->>Portfolio: OnData(context)

    alt Buy decision
        Portfolio->>Context: context.buy(ticker, confidence)
        Context->>Executor: execute_trade(BUY, ticker, ...)
        Executor->>DB: cash + positions + trade log (atomic)
    else Sell decision
        Portfolio->>Context: context.sell(ticker, confidence)
        Context->>Executor: execute_trade(SELL, ticker, ...)
        Executor->>DB: cash + positions + trade log (atomic)
    else Hold
        Note over Portfolio: no executor call
    end
```

When a portfolio's `config.json` sets `OMS.enabled`, an `OrderManager` is threaded into `execute_trade` (via `StrategyContext(order_manager=...)`); the executor registers a parent order with it after sizing and before applying the fill. This is an opt-in tracking layer only — the fill still flows through the same `execute_trade` path shown above. See [OMS implementation status](../OMS/OMS_DESIGN.md#1a-implementation-status-as-of-2026-06-13).

`generate_signals_and_trade()` always tracks `_last_processed_timestamp`; only bars strictly newer than that are fed to indicators. This makes the path correct for both live polling (where `current_time=None` so the latest bar is used) and backtest replay (where `current_time` is the simulated clock).

## Indicator Factory

```mermaid
flowchart TD
    REG["self.RegisterIndicatorSet({<br/>  'fast_sma': ('SimpleMovingAverage', {period: 10}),<br/>  'rmi': ('RelativeMomentumIndex', {...}),<br/>})"] --> LOOP["For each ticker × definition"]
    LOOP --> ADD["AddIndicator(name, ticker, **kwargs)"]
    ADD --> SNAKE["_camel_to_snake(name)<br/>SimpleMovingAverage → simple_moving_average"]
    SNAKE --> IMPORT["importlib.import_module('src.portfolios.indicators.{snake}')"]
    IMPORT --> INSTANTIATE["IndicatorClass(ticker=ticker, **kwargs)"]
    INSTANTIATE --> WARMUP["Query market_data for period × 1.7 days<br/>and feed each bar via indicator.Update()"]
    WARMUP --> ATTACH["Attach as self.fast_sma[ticker], self.rmi[ticker], ..."]

    classDef factory fill:#fff3e0,stroke:#e65100
    class REG,LOOP,ADD,SNAKE,IMPORT,INSTANTIATE,WARMUP,ATTACH factory
```

Adding a new indicator only requires a file under `src/portfolios/indicators/` whose snake_case name matches its CamelCase class name. Strategies can then register it through `RegisterIndicatorSet`.

### Available Indicators

| File | Class | Notes |
|------|-------|-------|
| `simple_moving_average.py` | `SimpleMovingAverage` | Plain SMA over `period` bars |
| `exponential_moving_average.py` | `ExponentialMovingAverage` | EMA with configurable smoothing |
| `displaced_moving_average.py` | `DisplacedMovingAverage` | SMA shifted forward by `displacement` |
| `relative_strength_index.py` | `RelativeStrengthIndex` | Wilder's RSI |
| `relative_momentum_index.py` | `RelativeMomentumIndex` | RSI generalized over `momentum_period` |
| `average_true_range.py` | `AverageTrueRange` | Reads `high_col` / `low_col` / `close_col` |
| `rate_of_change.py` | `RateOfChange` | Percent change over `period` |
| `vwap.py` | `VWAP` | Reads `vol_col` (typically `volume`) |

## Strategy Roster

| ID | Class | Indicators / signals | Trade rule |
|----|-------|---------------------|------------|
| 1 | `VolMomentum` | Volatility-aware momentum signal | Enter on ranked momentum, exit on regime flip |
| 2 | `MomentumStrategy` | Multi-period momentum | Long basket reweighted by momentum score |
| 3 | `RegimeAdaptiveStrategy` | Regime detector + momentum | Switch behavior based on detected regime |
| 4 | `TrendRotateStrategy` | Trend rank + rotation | Hold top-N by trend score |
| 5 | `RBPStrategy` | `RBPModel` (Relevance-Based Prediction) over engineered return / vol features | Buy on `prediction > buy_threshold`, sell on `prediction < sell_threshold` once a position exists |
| dummy | `CrossoverRmiStrategy` | SMA(20) / SMA(50) crossover + RMI(14) + winsorized vol stop | Showcase of `Portfolio` risk-off, `History`, `toolkit.winsorize`, and stop-loss logic |

## Sizing Model (live executor, in `tradeExecutor.execute_trade`)

```text
target_notional = port_notional × ticker_weight
adjustment      = target_notional - current_asset_notional
trade_notional  = adjustment × confidence
trade_qty       = floor(trade_notional / arrival_price)
```

The same shape is used in `BacktestExecutor`, with simulated cash/position bookkeeping and slippage applied to `arrival_price`.

## Fast Mode: Vector Adapters

Event-mode strategies are stateful (one bar at a time). Fast mode needs a vectorized signal across the entire window, so each strategy that opts into fast mode has a sibling adapter in `src/backtest/vector_strategy_adapters.py`:

```mermaid
flowchart LR
    EVENT["Event-mode strategy<br/>(OnData / indicator state)"]
    ADAPTER["vector_strategy_adapter<br/>(price_df → target_weights)"]
    EVENT -. mirrors .-> ADAPTER
    ADAPTER --> VEC["VectorBacktester.run_from_returns()"]
```

Adapters re-implement the relevant indicator math in pandas/numpy (`_compute_rsi`, `_compute_rmi`, etc.) so fast mode can produce a `target_weights` matrix without ever instantiating the live indicator classes. Strategies without a registered adapter simply skip fast mode with a warning — see `get_vector_adapter_for_portfolio()`.

## Indicator Registration Example

```python
# In strategy __init__:
self.RegisterIndicatorSet({
    "fast_sma": ("SimpleMovingAverage", {"period": 10}),
    "slow_sma": ("SimpleMovingAverage", {"period": 30}),
    "rsi":      ("RelativeStrengthIndex", {"period": 14}),
})

def OnData(self, context):
    for ticker in self.tickers:
        fast = self.fast_sma[ticker].Current
        slow = self.slow_sma[ticker].Current
        rsi  = self.rsi[ticker].Current
        if fast > slow and rsi < 70:
            context.buy(ticker, confidence=0.8)
```
