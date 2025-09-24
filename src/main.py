# main.py
# Entry point for the MQS Trading System.
# main.py is reserved for live trading purposes.


import logging
from common.database.MQSDBConnector import MQSDBConnector
from live_trading.executor import tradeExecutor
from portfolios.portfolio_1.strategy import VolMomentum
from portfolios.portfolio_2.strategy import MomentumStrategy
from live_trading.engine import RunEngine

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def main():
    """
    Main entry point for the MQS Trading System.
    """
    db_conn = None
    try:
        db_conn = MQSDBConnector()
        logging.info("Database connector initialized.")

        live_executor = tradeExecutor()
        logging.info("Live executor initialized.")

        run_engine = RunEngine(db_connector=db_conn, executor=live_executor)
        logging.info("Run engine initialized.")

        run_engine.setup(portfolios_to_run=[VolMomentum, MomentumStrategy])
        logging.info("Run engine setup complete.")

        run_engine.run()

    except Exception as e:
        logging.critical(f"A critical error occurred in the main application loop: {e}", exc_info=True)
    finally:
        logging.info("MQS Trading System is shutting down.")

if __name__ == '__main__':
    main()