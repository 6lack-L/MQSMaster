# src/live_trading/engine.py

import inspect
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import List

import pytz

try:
    from portfolios.portfolio_BASE.strategy import BasePortfolio
except ImportError:
    logging.warning(
        "BasePortfolio relative import failed; using absolute import."
    )
    try:
        from src.portfolios.portfolio_BASE.strategy import BasePortfolio
    except ImportError as abs_err:
        logging.error(
            "Failed to import BasePortfolio from both relative and absolute paths. Details: %s",
            abs_err,
        )
        raise

class RunEngine:
    """
    Manages and runs multiple trading portfolios concurrently for live trading.
    Updated to dynamically load configurations for each portfolio.
    """

    def __init__(
        self,
        db_connector,
        executor,
        debug=False,
        max_consecutive_failures=5,
        oms_tick_seconds: float = 5.0,
    ):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.db_connector = db_connector
        self.executor = executor
        self.debug = debug
        self.portfolios: List[BasePortfolio] = []
        self.running = True

        self.max_consecutive_failures = max_consecutive_failures
        self.failure_counts = {}

        # OMS pump cadence. Slice timing must be finer than the portfolio
        # poll interval (a 3-minute TWAP slice cannot wait for an hourly
        # poll), so a single dedicated thread pumps every OMS-enabled
        # portfolio's OrderManager on this tick. The Event gives run() a
        # prompt shutdown handle (vs sleeping out a full tick).
        self.oms_tick_seconds = max(0.5, float(oms_tick_seconds))
        self._oms_stop_event = threading.Event()

    def load_portfolios(self, portfolio_classes: List[type[BasePortfolio]]):
        """
        Initializes portfolio objects from the provided classes and loads them.
        """
        for portfolio_cls in portfolio_classes:
            try:
                # --- NEW: Dynamically load the config for the portfolio ---
                class_file_path = inspect.getfile(portfolio_cls)
                portfolio_dir = os.path.dirname(class_file_path)
                config_path = os.path.join(portfolio_dir, "config.json")

                if not os.path.exists(config_path):
                    self.logger.error(
                        f"Configuration file not found for {portfolio_cls.__name__} at {config_path}"
                    )
                    continue

                with open(config_path, "r") as f:
                    config_data = json.load(f)

                # Gate the OMS behind config (per-portfolio, fail-soft to the proven direct-execution path). Construction is centralized in src.oms.factory so the live and backtest engines cannot drift on how they read OMS config or resolve the portfolio id.
                from src.oms.factory import build_order_manager, resolve_portfolio_id
                portfolio_id = resolve_portfolio_id(config_data)
                order_manager = build_order_manager(
                    config_data, portfolio_id, logger=self.logger
                )

                # --- UPDATED: Instantiate with the loaded config_dict ---
                portfolio_instance = portfolio_cls(
                    db_connector=self.db_connector,
                    executor=self.executor,
                    debug=self.debug,
                    config_dict=config_data,
                )
                # Thread the OMS through the portfolio post-construction, matching
                # BacktestRunner (`self.portfolio.order_manager = ...`), so the two
                # engines wire the OMS the same way. None keeps the direct path.
                portfolio_instance.order_manager = order_manager
                self.portfolios.append(portfolio_instance)
                self.failure_counts[portfolio_instance.portfolio_id] = 0
                self.logger.info(
                    f"Successfully loaded portfolio: {portfolio_cls.__name__}"
                )

            except Exception as e:
                self.logger.exception(
                    f"Failed to load portfolio {portfolio_cls.__name__}: {e}"
                )

    def _run_portfolio(self, portfolio: BasePortfolio):
        """
        The target function for each portfolio's thread. Contains the polling loop
        and circuit breaker logic.
        """
        portfolio_id = portfolio.portfolio_id
        self.logger.info(
            f"Starting run loop for portfolio {portfolio_id} ({portfolio.__class__.__name__})."
        )

        while self.running:
            try:
                start_time = time.time()

                # The get_data and generate_signals_and_trade calls remain the same,
                # as the new API is handled within these methods in the base class.
                data = portfolio.get_data(portfolio.data_feeds)
                portfolio.generate_signals_and_trade(data, current_time=None)

                if self.failure_counts[portfolio_id] > 0:
                    self.logger.info(
                        f"Portfolio {portfolio_id} recovered after {self.failure_counts[portfolio_id]} failures."
                    )
                    self.failure_counts[portfolio_id] = 0

                if portfolio.debug:
                    self.logger.info(
                        f"Debug mode: stopping portfolio {portfolio_id} after one run."
                    )
                    break

                elapsed_time = time.time() - start_time
                sleep_time = max(0, portfolio.poll_interval - elapsed_time)
                time.sleep(sleep_time)

            except Exception as e:
                self.failure_counts[portfolio_id] += 1
                self.logger.exception(
                    f"Exception in portfolio {portfolio_id} loop. Consecutive failure "
                    f"count: {self.failure_counts[portfolio_id]}/{self.max_consecutive_failures}. Error: {e}"
                )

                if self.failure_counts[portfolio_id] >= self.max_consecutive_failures:
                    self.logger.critical(
                        f"CIRCUIT BREAKER TRIPPED: Portfolio {portfolio_id} has failed "
                        f"{self.max_consecutive_failures} consecutive times. Stopping this portfolio thread."
                    )
                    break

                time.sleep(portfolio.poll_interval)

        self.logger.info(f"Stopped run loop for portfolio {portfolio_id}.")

    # ------------------------------------------------------------------
    # OMS pump (live counterpart of the backtest runner's per-bar pump)
    # ------------------------------------------------------------------

    def _oms_portfolios(self) -> List[BasePortfolio]:
        return [
            p for p in self.portfolios if getattr(p, "order_manager", None) is not None
        ]

    def _make_child_executor(self, portfolio: BasePortfolio):
        """Build the execute_child callable for one portfolio.

        Load-bearing detail: portfolio state (cash/positions/notional) is
        fetched FRESH inside every call — via the portfolio's atomic state
        query — never captured when the order was submitted. A slice can
        execute minutes after submission, and other fills move the books in
        between; settling against stale state writes wrong absolute values
        to cash_equity_book/positions_book.

        The per-portfolio OrderManager stays a call-scoped value throughout
        (closure -> manage_order parameter); nothing is ever attached to the
        shared tradeExecutor (see CLAUDE.md "OMS" on why that would race
        across portfolio threads).
        """

        def _execute(child_order):
            data = portfolio.get_data(["POSITIONS", "CASH_EQUITY", "PORT_NOTIONAL"])
            cash_df = data.get("CASH_EQUITY")
            positions_df = data.get("POSITIONS")
            port_df = data.get("PORT_NOTIONAL")
            if (
                cash_df is None
                or cash_df.empty
                or positions_df is None
                or positions_df.empty
                or port_df is None
                or port_df.empty
            ):
                # Books unreadable right now: report failure so the OMS
                # retries rather than settling against unknown state.
                return {
                    "status": "error",
                    "message": "could not fetch fresh portfolio state",
                }
            return self.executor.execute_child_order(
                child_order,
                cash=cash_df.iloc[0]["notional"],
                positions=positions_df,
                port_notional=port_df.iloc[0]["notional"],
            )

        return _execute

    def _run_oms_pump(self):
        """Dedicated OMS thread: tick every ``oms_tick_seconds``, pump each
        OMS-enabled portfolio's OrderManager, and on shutdown cancel whatever
        is still queued so the session ends with every order terminal."""
        ny_tz = pytz.timezone("America/New_York")
        self.logger.info(
            "OMS pump started (tick=%.1fs, portfolios=%d).",
            self.oms_tick_seconds,
            len(self._oms_portfolios()),
        )
        while self.running and not self._oms_stop_event.is_set():
            for portfolio in self._oms_portfolios():
                try:
                    portfolio.order_manager.manage_order(
                        now=datetime.now(ny_tz),
                        execute_child=self._make_child_executor(portfolio),
                    )
                except Exception as e:
                    # One portfolio's OMS failure must not starve the others;
                    # the next tick retries naturally.
                    self.logger.exception(
                        f"OMS pump failed for portfolio "
                        f"{portfolio.portfolio_id}: {e}"
                    )
            self._oms_stop_event.wait(timeout=self.oms_tick_seconds)

        # Drain on shutdown: cancel open parents (and their queued slices)
        # so tracking logs end the session with terminal states only.
        for portfolio in self._oms_portfolios():
            try:
                cancelled = portfolio.order_manager.cancel_all_open_orders(
                    reason="engine shutdown"
                )
                if cancelled:
                    self.logger.info(
                        "Cancelled %d open OMS orders for portfolio %s on shutdown.",
                        cancelled,
                        portfolio.portfolio_id,
                    )
            except Exception as e:
                self.logger.exception(
                    f"OMS shutdown drain failed for portfolio "
                    f"{portfolio.portfolio_id}: {e}"
                )
        self.logger.info("OMS pump stopped.")

    def run(self):
        """
        Starts the trading engine, running all loaded portfolios in separate threads.
        """
        if not self.portfolios:
            self.logger.warning("No portfolios loaded. Exiting.")
            return

        self.logger.info(f"Starting RunEngine with {len(self.portfolios)} portfolios.")
        threads = []
        for portfolio in self.portfolios:
            thread = threading.Thread(target=self._run_portfolio, args=(portfolio,))
            threads.append(thread)
            thread.start()

        # One pump thread serves all OMS-enabled portfolios (daemon so a
        # hung DB call cannot block process exit; the drain is best-effort).
        oms_thread = None
        if self._oms_portfolios():
            oms_thread = threading.Thread(
                target=self._run_oms_pump, name="oms-pump", daemon=True
            )
            oms_thread.start()

        try:
            while self.running:
                if not any(t.is_alive() for t in threads):
                    self.logger.warning(
                        "All portfolio threads have stopped. Shutting down RunEngine."
                    )
                    self.running = False
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.warning(
                "Keyboard interrupt received. Shutting down all portfolios."
            )
            self.running = False

        for thread in threads:
            thread.join()

        if oms_thread is not None:
            # Wake the pump immediately (skip the remaining tick wait), let
            # it run its shutdown drain, then bound the wait.
            self._oms_stop_event.set()
            oms_thread.join(timeout=30)

        self.logger.info(
            "All portfolio threads have been joined. RunEngine shutdown complete."
        )
