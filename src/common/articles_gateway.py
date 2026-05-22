"""API gateway for ``fetch_articles`` and ``fetch_alt_articles``.

Wraps the FMP and Alpha Vantage news endpoints with the shared
:class:`FMPRateLimiter` (NLP cap = 2000 req/min) so NLP scraping stays
within the FMP account budget shared with the realtime ingestor.

Alpha Vantage uses a separate ``ALPHA_KEY`` budget; rate-limit handling
for AV lives in the scraper itself.
"""

from __future__ import annotations

import os

import requests

from src.common.auth.apiAuth import APIAuth
from src.common.fmp_rate_limiter import FMPRateLimiter

REQUEST_TIMEOUT_SECONDS = 15


class ArticlesGateway:
    """HTTP wrapper around FMP / Alpha Vantage news endpoints."""

    def __init__(self):
        api = APIAuth()
        self.fmp_key = api.get_fmp_api_key()
        self.alpha_key = os.getenv("ALPHA_KEY")
        self._fmp_limiter = FMPRateLimiter.for_nlp()

    def fetch_fmp_news(self, ticker, page=0):
        """Single-ticker FMP news page.

        FMP's multi-ticker support returns the top-N newest articles
        across the request (not per ticker), so we don't expose
        comma-separated input here: callers want per-ticker freshness.
        """
        self._fmp_limiter.acquire()
        url = (
            f"https://financialmodelingprep.com/api/v3/stock_news"
            f"?tickers={ticker}&page={page}&apikey={self.fmp_key}"
        )
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()

    def fetch_alpha_news(self, ticker_list, time_from, time_to):
        """Alpha Vantage NEWS_SENTIMENT batch endpoint."""
        tickers = ",".join(ticker_list)
        url = (
            f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
            f"&tickers={tickers}&time_from={time_from}&time_to={time_to}"
            f"&apikey={self.alpha_key}"
        )
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json()
