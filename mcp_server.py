"""Option C — MCP server exposing TiefStocks capabilities as discoverable tools.

Run standalone over stdio. The policy gate lives here on the server (write tools
return APPROVAL_REQUIRED, never execute) — governance enforced server-side, not
in the model. The Option C client discovers these via tools/list at runtime.
"""
from __future__ import annotations
from mcp.server.fastmcp import FastMCP
from core import Tracer
from api.tiefstocks import TieClient

mcp = FastMCP("tiefstocks")
_client = TieClient(Tracer())  # server-side client; its tracer is unused downstream


@mcp.tool()
def get_portfolio(reason: str = "") -> dict:
    """Get current portfolio holdings (ticker, shares, cost basis, market value, P&L)."""
    return _client.portfolio()


@mcp.tool()
def search_stocks(q: str, reason: str = "") -> dict:
    """Search for stocks/tickers by free-text query."""
    return _client.search(q)


@mcp.tool()
def get_stock(ticker: str, reason: str = "") -> dict:
    """Get detail for one ticker."""
    return _client.stock(ticker)


@mcp.tool()
def get_thesis(ticker: str, reason: str = "") -> dict:
    """Get the investment thesis (status + latest version) for a ticker."""
    return _client.thesis(ticker)


@mcp.tool()
def analyze_stock(ticker: str, reason: str = "") -> dict:
    """Run intelligence analysis on a ticker (price move, severity, classification)."""
    return _client.analyze(ticker)


@mcp.tool()
def challenge_thesis(ticker: str, reason: str = "") -> dict:
    """Get the adversarial / bear-case challenge view for a ticker."""
    return _client.challenge(ticker)


@mcp.tool()
def list_transactions(ticker: str = "", reason: str = "") -> dict:
    """List portfolio transactions, optionally filtered by ticker."""
    return _client.transactions(ticker or None)


@mcp.tool()
def add_transaction(ticker: str, action: str, shares: float, price: float, date: str, reason: str = "") -> dict:
    """WRITE: record a buy/sell transaction. Gated by policy — returns a preview for approval."""
    return {"status": "APPROVAL_REQUIRED",
            "message": "Write blocked by MCP server policy; human approval required.",
            "preview": {"ticker": ticker, "action": action, "shares": shares,
                        "price": price, "date": date}}


if __name__ == "__main__":
    mcp.run()
