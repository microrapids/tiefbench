"""AI description enrichment for MCP tools (static — from the config alone).

Drafts richer "what it does + when to use / when NOT to use" descriptions for
tools with thin descriptions, grounded in name/method/path/params. It's a DRAFT to
review (the exported config is inspected before import); behavioral proof is Tune's
job. One LLM call for the whole batch.
"""
from __future__ import annotations
import json
import core

SYS = ("You write clear, disambiguating MCP tool descriptions for an AI agent. "
       "Output ONLY a JSON object mapping tool name -> improved description.")


def descriptions(config: dict, only_thin: bool = True, thin_len: int = 40) -> dict:
    tools = (config.get("tools") if isinstance(config, dict) else None) or []
    items = []
    for t in tools:
        desc = t.get("description", "") or ""
        if only_thin and len(desc.strip()) >= thin_len:
            continue
        http = t.get("_http") or {}
        items.append({
            "name": t.get("name"),
            "current": desc,
            "method": t.get("method") or http.get("method"),
            "path": t.get("path") or http.get("path"),
            "params": list(((t.get("inputSchema") or t.get("input_schema") or {}).get("properties") or {}).keys()),
        })
    if not items:
        return {}
    msg = [{"role": "user", "content":
            "Improve these tool descriptions for an AI agent. Each must say WHAT it does AND WHEN to use / "
            "WHEN NOT to use (to disambiguate from similar tools). 1–2 sentences, concrete, no fluff. "
            "Base it only on the signature below (don't invent behavior beyond name/path/params).\n\n"
            + json.dumps(items)[:7000]
            + '\n\nReturn ONLY JSON: {"tool_name": "improved description", ...}'}]
    r = core._anthropic().messages.create(model=core.MODEL, max_tokens=1400, system=SYS, messages=msg)
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("{"), txt.rfind("}")
    try:
        return json.loads(txt[s:e + 1])
    except Exception:
        return {}
