# This is where backtests are setup.
# Simply add you portfolio class to the list of portfolio classes in the `setup` method.

# High level overview of how to set up a backtest:

# 1. Load your portfolio through imports such as `from portfolios.portfolio_1.strategy import VolMomentum`
# 2. Set up the backtest engine with the database connector and backtest executor.
# 3. Call the `setup` method with the portfolio class, start date, end date, and initial capital.
# 4. Call the `run` method to execute the backtest.
# src/main_backtest.py

import logging
from common.database.MQSDBConnector import MQSDBConnector
from portfolios.portfolio_2.strategy import MomentumStrategy
from backtest.backtest_engine import BacktestEngine

logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def main():
    """
    Main entry point for the MQS Trading System backtests.
    """
    try:
        dbconn = MQSDBConnector()
        
        backtest_engine = BacktestEngine(db_connector=dbconn, backtest_executor=None)

        backtest_engine.setup(
            portfolio_classes=[MomentumStrategy],
            start_date="2024-01-01",
            end_date="2025-01-01",
            initial_capital=1000000.0,
            slippage=0.00001
        )
        
        backtest_engine.run()

    finally:
        logging.info("===== DONE =====")

if __name__ == '__main__':
    main()