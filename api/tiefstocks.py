"""Thin instrumented HTTP client for the TiefStocks API.

Every call is recorded on the tracer so we can score correct API usage.
"""
from __future__ import annotations
import requests
from core import BASE_URL, Tracer


class TieClient:
    def __init__(self, tracer: Tracer, base_url: str = BASE_URL, timeout: int = 15):
        self.tracer = tracer
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict | None = None):
        self.tracer.record_api("GET", path)
        r = requests.get(self.base + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json_body: dict):
        # Real execution path (only reached if policy allows it).
        self.tracer.record_api("POST", path)
        r = requests.post(self.base + path, json=json_body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---- convenience read methods ----
    def portfolio(self):                 return self.get("/api/portfolio")
    def search(self, q):                 return self.get("/api/search", {"q": q})
    def stock(self, ticker):             return self.get(f"/api/stocks/{ticker}")
    def thesis(self, ticker):            return self.get(f"/api/thesis/{ticker}")
    def analyze(self, ticker):           return self.get(f"/api/intelligence/analyze/{ticker}")
    def challenge(self, ticker):         return self.get(f"/api/intelligence/challenge/{ticker}")
    def transactions(self, ticker=None):
        return self.get("/api/portfolio/transactions", {"ticker": ticker} if ticker else None)
