#!/usr/bin/env python3
"""Synthetic provider check for the FMP market-data path.

Invoked by the synthetic-monitoring job in .github/workflows/main.yml, which
runs every six hours:

    python scripts/api_test.py --symbol AAPL --require-fmp   # on main
    python scripts/api_test.py --symbol AAPL                 # other branches

This drives the production FMPMarketData client rather than issuing bare HTTP
calls, so a break in auth, rate limiting, or response parsing surfaces here
before it surfaces in the live ingestor.

Emits one JSON object and exits 0 on ok/warn, 1 on failed -- same contract as
NLP/monitor_daemon.py --synthetic.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.orchestrator.marketData.fmpMarketData import FMPMarketData  # noqa: E402

NY_TZ = ZoneInfo("America/New_York")

# Ten calendar days always spans at least three trading sessions, so the daily
# and intraday checks stay meaningful across weekends and holiday closures.
LOOKBACK_DAYS = 10
INTRADAY_INTERVAL_MINUTES = 5


def _window():
    """Return (from_date, to_date) as YYYY-MM-DD strings in America/New_York."""
    end = datetime.now(NY_TZ).date()
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.isoformat(), end.isoformat()


def _count(payload) -> int:
    return len(payload) if isinstance(payload, list) else 0


def run_synthetic_check(symbol: str) -> dict:
    """Exercise quote, daily, and intraday endpoints for one symbol."""
    from_date, to_date = _window()
    result: dict = {
        "symbol": symbol,
        "from": from_date,
        "to": to_date,
        "status": "ok",
        "checks": {},
        "reasons": [],
    }

    client = FMPMarketData()

    # get_current_price swallows its own errors and returns 0.0, so a zero here
    # means either a failed request or a symbol the provider no longer quotes.
    price = client.get_current_price(symbol)
    result["checks"]["quote_price"] = price
    if not price or price <= 0:
        result["status"] = "failed"
        result["reasons"].append(f"quote returned no usable price for {symbol}")

    daily = client.get_historical_data([symbol], from_date, to_date)
    result["checks"]["daily_bars"] = _count(daily)
    if not daily:
        result["status"] = "failed"
        result["reasons"].append(
            f"no daily bars for {symbol} over {from_date}..{to_date}"
        )

    intraday = client.get_intraday_data(
        [symbol], from_date, to_date, INTRADAY_INTERVAL_MINUTES
    )
    result["checks"]["intraday_bars"] = _count(intraday)
    if not intraday:
        result["status"] = "failed"
        result["reasons"].append(
            f"no {INTRADAY_INTERVAL_MINUTES}min bars for {symbol} "
            f"over {from_date}..{to_date}"
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic health check for the FMP market-data provider"
    )
    parser.add_argument(
        "--symbol",
        default="AAPL",
        help="Ticker to probe (default: AAPL)",
    )
    parser.add_argument(
        "--require-fmp",
        action="store_true",
        help="Treat a missing FMP_API_KEY as a failure instead of a warning",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Branches without access to the key still run this job; they report a warn
    # and pass, so only main enforces provider reachability.
    if not (os.getenv("FMP_API_KEY") or "").strip():
        status = "failed" if args.require_fmp else "warn"
        print(
            json.dumps(
                {
                    "symbol": args.symbol,
                    "status": status,
                    "checks": {},
                    "reasons": ["FMP_API_KEY is not set"],
                }
            )
        )
        return 1 if status == "failed" else 0

    try:
        result = run_synthetic_check(args.symbol)
    except Exception as e:  # provider construction or an unhandled client error
        print(
            json.dumps(
                {
                    "symbol": args.symbol,
                    "status": "failed",
                    "checks": {},
                    "reasons": [f"{type(e).__name__}: {e}"],
                }
            )
        )
        return 1

    print(json.dumps(result))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
