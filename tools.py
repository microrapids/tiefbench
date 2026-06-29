"""Curated tool surface + dispatch + the deterministic policy/approval layer.

Shared by Option B (function calling), Option E (orchestrator), and mirrored by
the MCP server (Option C). The policy layer lives OUTSIDE the model: write tools
are intercepted and never executed — the agent only ever gets an
APPROVAL_REQUIRED preview back. This is the governance demo.
"""
from __future__ import annotations
import json
from api.tiefstocks import TieClient
from core import emit

# Names the policy layer treats as mutating / high-risk.
WRITE_TOOLS = {"add_transaction", "record_decision"}

# Anthropic tool schemas (also used to derive the MCP server + mrapids picker).
TOOLS = [
    {"name": "get_portfolio", "description": "Get current portfolio holdings with tickers, shares, cost basis, market value, and P&L.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "search_stocks", "description": "Search for stocks/tickers by free-text query.",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}},
    {"name": "get_stock", "description": "Get detail for one ticker.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "get_thesis", "description": "Get the investment thesis (status + latest version) for a ticker.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "analyze_stock", "description": "Run intelligence analysis on a ticker (price move, severity, classification).",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "challenge_thesis", "description": "Get the adversarial / bear-case challenge view for a ticker.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}},
    {"name": "list_transactions", "description": "List portfolio transactions, optionally filtered by ticker.",
     "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}}},
    {"name": "add_transaction", "description": "WRITE: record a buy/sell transaction (ticker, action, shares, price, date YYYY-MM-DD).",
     "input_schema": {"type": "object", "properties": {
         "ticker": {"type": "string"}, "action": {"type": "string", "enum": ["BUY", "SELL"]},
         "shares": {"type": "number"}, "price": {"type": "number"}, "date": {"type": "string"}},
         "required": ["ticker", "action", "shares", "price", "date"]}},
]

# Every tool also accepts a `reason`: the model must say WHY it's calling it.
# Captured for explainability; stripped before the API call.
for _t in TOOLS:
    _t["input_schema"]["properties"]["reason"] = {
        "type": "string",
        "description": "One short clause: why this call is needed for the user's request."}


def dispatch(name: str, args: dict, client: TieClient, *, enforce_policy: bool = True):
    """Execute a tool call. Returns a JSON-serializable result dict."""
    args = dict(args or {})
    reason = args.pop("reason", None)          # capture the model's justification
    emit({"type": "reason", "tool": name, "reason": reason})
    # ---- policy gate: writes never execute ----
    if name in WRITE_TOOLS and enforce_policy:
        client.tracer.record_api("POST", "/api/portfolio/transactions", blocked=True)
        return {
            "status": "APPROVAL_REQUIRED",
            "message": "Write blocked by policy. Preview generated; human approval required before execution.",
            "preview": args,
        }

    if name == "get_portfolio":   return client.portfolio()
    if name == "search_stocks":   return client.search(args["q"])
    if name == "get_stock":       return client.stock(args["ticker"])
    if name == "get_thesis":      return client.thesis(args["ticker"])
    if name == "analyze_stock":   return client.analyze(args["ticker"])
    if name == "challenge_thesis":return client.challenge(args["ticker"])
    if name == "list_transactions": return client.transactions(args.get("ticker"))
    if name == "add_transaction":  # only reached if policy disabled
        return client.post("/api/portfolio/transactions", args)
    return {"error": f"unknown tool {name}"}


def to_json(obj) -> str:
    try:
        return json.dumps(obj, default=str)[:6000]
    except Exception:
        return str(obj)[:6000]
