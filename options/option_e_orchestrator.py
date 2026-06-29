"""Option E — Vendor-hosted planner / orchestrator.

A planner first classifies intent and picks a least-privilege tool scope; a
domain sub-agent then executes with ONLY that scope visible. The user never sees
the tool surface. Demonstrates routing + scope confinement on top of the same
policy layer.
"""
from __future__ import annotations
import json
from core import RunResult, Tracer, llm_call, llm_stream, timed, SYSTEM_PROMPT
from api.tiefstocks import TieClient
from tools import TOOLS, dispatch, to_json

NAME = "E:orchestrator"
MAX_TURNS = 8

SCOPES = {
    "portfolio_read": ["get_portfolio", "list_transactions", "get_stock", "search_stocks"],
    "research":       ["analyze_stock", "challenge_thesis", "get_thesis", "get_stock", "search_stocks"],
    "governed_write": ["add_transaction", "get_portfolio", "get_stock"],
}
ROUTER = (
    "You are an intent router. Classify the user request into exactly one scope and "
    'reply with ONLY that scope name. Options: "portfolio_read" (viewing holdings/'
    'transactions), "research" (analysis, thesis, bear case), "governed_write" '
    "(adding/changing data). Request: ")


@timed
def run(task) -> RunResult:
    tracer = Tracer()
    client = TieClient(tracer)
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)

    rresp = llm_call([{"role": "user", "content": ROUTER + task.prompt}], tracer,
                     system="You output only one scope name.", max_tokens=20, label="router")
    label = "".join(b.text for b in rresp.content if b.type == "text").strip().strip('"').lower()
    scope = next((k for k in SCOPES if k in label), "portfolio_read")
    allowed = set(SCOPES[scope])
    scoped_tools = [t for t in TOOLS if t["name"] in allowed]   # least-privilege surface

    sub_system = SYSTEM_PROMPT + f"\n\n[Routed scope: {scope}. Only scope tools are available.]"
    messages = [{"role": "user", "content": task.prompt}]
    for _ in range(MAX_TURNS):
        resp = llm_stream(messages, tracer, system=sub_system, tools=scoped_tools, label="loop")
        if resp.stop_reason != "tool_use":
            txt = "".join(b.text for b in resp.content if b.type == "text")
            res.final_text = f"[routed: {scope}] {txt}"
            return res
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = dispatch(block.name, block.input, client)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": to_json(out)})
        messages.append({"role": "user", "content": results})

    res.final_text = f"[routed: {scope}] (stopped: max turns)"
    return res
