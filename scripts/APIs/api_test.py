#!/usr/bin/env python3
"""Lightweight synthetic checks for market data providers.

This script is intentionally not a pytest module and is designed for scheduled
monitoring jobs in CI/CD.
"""

import argparse
import json
import os

import requests
import yfinance as yf


def check_yfinance(symbol: str) -> dict:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="5d", interval="1d")
    if history.empty:
        raise RuntimeError(f"yfinance returned empty history for {symbol}")

    close_series = history["Close"].dropna()
    if close_series.empty:
        raise RuntimeError(f"yfinance returned no close values for {symbol}")

    latest_close = float(close_series.iloc[-1])
    if latest_close <= 0:
        raise RuntimeError(
            f"yfinance returned non-positive close for {symbol}: {latest_close}"
        )

    return {
        "provider": "yfinance",
        "symbol": symbol,
        "rows": int(len(history)),
        "latest_close": latest_close,
    }


def check_fmp(symbol: str, timeout_seconds: int = 10) -> dict:
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        return {
            "provider": "fmp",
            "symbol": symbol,
            "status": "skipped",
            "reason": "missing FMP_API_KEY",
        }

    response = requests.get(
        "https://financialmodelingprep.com/stable/quote",
        params={"symbol": symbol, "apikey": api_key},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()

    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"FMP returned empty payload for {symbol}")

    row = payload[0]
    price = float(row.get("price") or 0.0)
    if price <= 0:
        raise RuntimeError(f"FMP returned non-positive price for {symbol}: {price}")

    return {
        "provider": "fmp",
        "symbol": symbol,
        "status": "ok",
        "price": price,
    }


def run_synthetic_checks(symbol: str, require_fmp: bool) -> int:
    results = []

    try:
        results.append(check_yfinance(symbol))
    except Exception as exc:
        print(
            json.dumps({"status": "failed", "provider": "yfinance", "error": str(exc)})
        )
        return 1

    try:
        fmp_result = check_fmp(symbol)
        results.append(fmp_result)
        if require_fmp and fmp_result.get("status") == "skipped":
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "provider": "fmp",
                        "error": "FMP check required but FMP_API_KEY is not set",
                    }
                )
            )
            return 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "provider": "fmp", "error": str(exc)}))
        return 1

    print(json.dumps({"status": "ok", "checks": results}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run synthetic market data checks")
    parser.add_argument("--symbol", default="AAPL", help="Ticker symbol to validate")
    parser.add_argument(
        "--require-fmp",
        action="store_true",
        help="Fail when FMP_API_KEY is missing",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run_synthetic_checks(symbol=args.symbol, require_fmp=args.require_fmp)


if __name__ == "__main__":
    raise SystemExit(main())
