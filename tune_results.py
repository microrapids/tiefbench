"""Tune v2 — result-projection tuning.

Verbose tool results are re-fed into context every loop turn, so they dominate
multi-step cost. This tunes what tools RETURN (a config-layer field projection —
no API change): run a scenario through the real loop, AI-draft a per-tool
projection, approve it, re-run, and prove the loop-token cut while the answer
holds (LLM-judge on both).

Executes against the built-in TiefStocks tools (real loop). For a customer this
would point at their live MCP/API.
"""
from __future__ import annotations
import json
import core, grader
from tools import TOOLS, dispatch, to_json
from api.tiefstocks import TieClient


def _get(d, path):
    cur = d
    for p in path.split("."):
        cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def apply_projection(result, keep):
    """Keep only the given (possibly dotted) field paths — the config transform."""
    if not isinstance(result, dict) or not keep:
        return result
    out = {}
    for path in keep:
        val = _get(result, path)
        if val is None:
            continue
        parts = path.split(".")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = val
    return out or result


def run_loop(scenario, projections=None, model=None, api_key=None, max_turns=6):
    projections = projections or {}
    tracer = core.Tracer()
    client = TieClient(tracer)
    cli = core.client_for(api_key)
    mdl = model or core.current_model()
    messages = [{"role": "user", "content": scenario}]
    tin = tout = turns = 0
    tools, sizes, final = [], {}, ""
    for _ in range(max_turns):
        resp = cli.messages.create(model=mdl, max_tokens=900, system=core.SYSTEM_PROMPT,
                                   messages=messages, tools=TOOLS)
        tin += resp.usage.input_tokens; tout += resp.usage.output_tokens; turns += 1
        tu = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not tu:
            final = txt; break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in tu:
            tools.append(b.name)
            out = dispatch(b.name, b.input, client)
            full_str = to_json(out)
            projected = b.name in projections
            if projected:
                out = apply_projection(out, projections[b.name])
            proj_str = to_json(out)
            sizes.setdefault(b.name, {"full": len(full_str), "proj": len(proj_str),
                                      "sample": None if projected else out})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": proj_str})
        messages.append({"role": "user", "content": results})
    return {"input_tokens": tin, "output_tokens": tout, "turns": turns,
            "tools": tools, "sizes": sizes, "answer": final}


SUGGEST_SYS = ("You reduce MCP tool result payloads to only the fields an agent needs, "
               "to cut agent-loop cost. Output ONLY a JSON array.")


def suggest_projections(scenario, baseline):
    samples = {n: i.get("sample") for n, i in baseline["sizes"].items() if i.get("sample")}
    if not samples:
        return []
    msg = [{"role": "user", "content":
            f"User scenario: {scenario}\n\nTool results (full JSON) the agent received:\n" +
            "\n".join(f"### {n}\n{to_json(r)[:2500]}" for n, r in samples.items()) +
            "\n\nFor each tool, list the MINIMAL set of fields (use dotted paths for nested, "
            "e.g. health.overall) the agent actually needs to answer this scenario. Drop large "
            "arrays/blobs/nested detail that don't change the answer. Return JSON array: "
            '[{"tool":"<name>","keep":["field","nested.field"],"rationale":"<why the rest is safe to drop>"}]'}]
    r = core._anthropic().messages.create(model=core.MODEL, max_tokens=1000, system=SUGGEST_SYS, messages=msg)
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("["), txt.rfind("]")
    try:
        return json.loads(txt[s:e + 1])
    except Exception:
        return []


def _judge(scenario, answer):
    if not answer:
        return None
    try:
        return grader.judge(scenario, answer).get("score")
    except Exception:
        return None


def analyze(scenario, model=None, api_key=None):
    base = run_loop(scenario, projections=None, model=model, api_key=api_key)
    sugs = suggest_projections(scenario, base)
    sizes = {n: {"full": i["full"]} for n, i in base["sizes"].items()}
    return {"baseline": {"input_tokens": base["input_tokens"], "output_tokens": base["output_tokens"],
                         "turns": base["turns"], "tools": base["tools"], "answer": base["answer"],
                         "acc": _judge(scenario, base["answer"]), "sizes": sizes},
            "suggestions": sugs}


def retest(scenario, projections, model=None, api_key=None):
    r = run_loop(scenario, projections=projections or {}, model=model, api_key=api_key)
    sizes = {n: {"full": i["full"], "proj": i["proj"]} for n, i in r["sizes"].items()}
    return {"after": {"input_tokens": r["input_tokens"], "turns": r["turns"], "tools": r["tools"],
                      "answer": r["answer"], "acc": _judge(scenario, r["answer"]), "sizes": sizes}}
