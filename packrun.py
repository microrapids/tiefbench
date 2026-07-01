"""Generic pack runner — the chat/execution path for an IMPORTED pack.

When the active pack isn't the built-in TiefStocks tools, the bake-off can't use
the hardcoded portfolio tools/prompt. This runs the tool-use loop over the active
pack's tools with a neutral prompt and a GENERIC HTTP dispatcher driven by each
tool's method/path/bindings + the pack's base URL/auth. Writes/destructive tools
are gated (APPROVAL_REQUIRED) from the pack's own risk classification.

If no base URL is configured, reads return a "not configured" preview (the request
that *would* be made) so tool SELECTION still works without a live endpoint.
"""
from __future__ import annotations
import json
import requests
import core
from core import RunResult, Tracer, llm_stream, timed, emit
import packs

NAME = "Pack (function calling)"
MAX_TURNS = 8


def _anthropic_tools(tools):
    out = []
    for t in tools:
        schema = t.get("schema") or {"type": "object", "properties": {}}
        if "type" not in schema:
            schema = {"type": "object", "properties": schema.get("properties", {})}
        out.append({"name": t["name"], "description": (t.get("description") or "")[:1000] or t["name"],
                    "input_schema": schema})
    return out


def _system(pack):
    return (f"You are an assistant for the \"{pack['name']}\" API. Use the available tools to help the user "
            "accurately. GOVERNANCE: any write / mutating / destructive action is HIGH RISK — call the tool to "
            "prepare it; the system will NOT execute it, it returns an APPROVAL_REQUIRED preview which you relay. "
            "Read-only actions may be performed freely.")


def _dispatch(pack, tool, args, tracer):
    method = (tool.get("method") or "GET").upper()
    path = tool.get("path") or ""
    risk = tool.get("risk", "read")
    bindings = tool.get("bindings") or {}
    args = dict(args or {})

    # policy gate — writes/destructive never execute
    if risk in ("write", "destructive") or method in ("POST", "PUT", "PATCH", "DELETE"):
        tracer.record_api(method, path, blocked=True)
        return {"status": "APPROVAL_REQUIRED",
                "message": f"'{tool['name']}' is a {risk} action — blocked by policy; human approval required.",
                "preview": args}

    # build the request from bindings
    filled = path
    query, body = {}, {}
    for k, v in args.items():
        loc = bindings.get(k, "query" if k not in path else "path")
        if loc == "path" or ("{" + k + "}") in filled:
            filled = filled.replace("{" + k + "}", str(v))
        elif loc == "body":
            body[k] = v
        else:
            query[k] = v

    base = (pack.get("env") or {}).get("base_url", "")
    if not base:
        return {"status": "NOT_CONFIGURED",
                "message": "No base URL set for this pack — set it in Packs to execute. Request preview:",
                "would_call": {"method": method, "path": filled, "query": query}}

    tracer.record_api(method, filled)
    headers = {}
    tok = (pack.get("env") or {}).get("token", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.request(method, base.rstrip("/") + filled, params=query or None,
                             headers=headers, timeout=20)
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text[:2000]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@timed
def run(task) -> RunResult:
    pack = packs.active_pack()
    tools = pack["tools"]
    tracer = Tracer()
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)
    atools = _anthropic_tools(tools)
    by_name = {t["name"]: t for t in tools}
    system = _system(pack)
    messages = [{"role": "user", "content": task.prompt}]

    for _ in range(MAX_TURNS):
        resp = llm_stream(messages, tracer, system=system, tools=atools, label="loop")
        if resp.stop_reason != "tool_use":
            res.final_text = "".join(b.text for b in resp.content if b.type == "text")
            return res
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                t = by_name.get(b.name)
                emit({"type": "reason", "tool": b.name, "reason": None})
                out = _dispatch(pack, t, b.input, tracer) if t else {"error": "unknown tool"}
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, default=str)[:6000]})
        messages.append({"role": "user", "content": results})

    res.final_text = "(stopped: max turns)"
    return res
