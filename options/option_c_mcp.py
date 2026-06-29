"""Option C — MCP client: discover tools at runtime, then drive Claude with them.

Connects to mcp_server.py over stdio, calls tools/list (runtime discovery), maps
the discovered schemas into Anthropic tool format, and runs the tool-use loop —
dispatching every tool call back through the MCP session. Writes are gated
server-side. The API trace is reconstructed from tool calls for scoring parity.
"""
from __future__ import annotations
import os, sys, json, asyncio
from core import RunResult, Tracer, timed, SYSTEM_PROMPT, MODEL, MAX_TOKENS, emit, capture_turn
from tools import to_json

NAME = "C:mcp"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_TURNS = 8

# Map MCP tool name -> endpoint path template, so scoring matches other options.
ENDPOINT = {
    "get_portfolio": lambda a: "GET /api/portfolio",
    "search_stocks": lambda a: "GET /api/search",
    "get_stock": lambda a: f"GET /api/stocks/{a.get('ticker')}",
    "get_thesis": lambda a: f"GET /api/thesis/{a.get('ticker')}",
    "analyze_stock": lambda a: f"GET /api/intelligence/analyze/{a.get('ticker')}",
    "challenge_thesis": lambda a: f"GET /api/intelligence/challenge/{a.get('ticker')}",
    "list_transactions": lambda a: "GET /api/portfolio/transactions",
    "add_transaction": lambda a: "POST /api/portfolio/transactions",
}


def _record(tracer: Tracer, name: str, args: dict):
    desc = ENDPOINT.get(name, lambda a: f"CALL {name}")(args)
    method, path = desc.split(" ", 1)
    tracer.record_api(method, path, blocked=(name == "add_transaction"))


async def _run_async(task) -> RunResult:
    import anthropic
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    tracer = Tracer()
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)
    aclient = anthropic.Anthropic()

    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"], cwd=HERE,
                                   env={**os.environ, "PYTHONPATH": HERE})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()                      # runtime discovery
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": t.inputSchema} for t in listed.tools]
            emit({"type": "tools", "source": "mcp",
                  "tools": [t["name"] for t in tools]})              # registration event

            messages = [{"role": "user", "content": task.prompt}]
            for _ in range(MAX_TURNS):
                # NOTE: aclient is the SYNC Anthropic client, so its stream() is a
                # sync context manager — use plain `with`/`for` inside the coroutine.
                with aclient.messages.stream(model=MODEL, max_tokens=MAX_TOKENS,
                                             system=SYSTEM_PROMPT, messages=messages,
                                             tools=tools) as stream:
                    for delta in stream.text_stream:
                        emit({"type": "token", "text": delta})
                    resp = stream.get_final_message()
                tracer.record_llm(resp.usage)
                capture_turn(messages, resp, "loop", SYSTEM_PROMPT)
                if resp.stop_reason != "tool_use":
                    res.final_text = "".join(b.text for b in resp.content if b.type == "text")
                    return res
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        bargs = block.input or {}
                        emit({"type": "reason", "tool": block.name, "reason": bargs.get("reason")})
                        _record(tracer, block.name, bargs)
                        out = await session.call_tool(block.name, bargs)
                        text = "".join(getattr(c, "text", "") for c in out.content) or "{}"
                        results.append({"type": "tool_result", "tool_use_id": block.id,
                                        "content": text[:6000]})
                messages.append({"role": "user", "content": results})
            res.final_text = "(stopped: max turns)"
            return res


@timed
def run(task) -> RunResult:
    return asyncio.run(_run_async(task))
