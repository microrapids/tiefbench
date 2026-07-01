"""Validate result-formatting tuning (thread #3).

Question: can a verbose tool result be reshaped as a CONFIG-LAYER transform (field
projection — no API change), and how much does it actually cut multi-step loop
tokens while preserving the answer?

Method: run the real tool-use loop twice on a multi-step scenario — once with full
tool results, once with a projection transform applied at the result layer — and
compare total input tokens across the loop.
"""
from __future__ import annotations
import core
from tools import TOOLS, dispatch, to_json
from api.tiefstocks import TieClient

# A projection = "keep these fields" applied to a tool's result. This is the kind
# of transform an MCP proxy / gateway could apply from config (no code change).
def project(name, r):
    if not isinstance(r, dict):
        return r
    if name == "analyze_stock":
        return {k: r.get(k) for k in ("ticker", "price", "change_pct", "direction",
                                      "severity", "classification", "classification_reason")} | {
            "health": (r.get("health") or {}).get("overall"),
            "reaction": (r.get("reaction") or {}).get("verdict"),
            "explanation": (r.get("explanation") or "")[:500]}
    if name == "challenge_thesis":
        return {k: r.get(k) for k in ("ticker", "summary", "adjusted_confidence",
                                      "original_confidence", "confidence_adjustment")}
    if name == "get_thesis":
        lat = r.get("latest") or {}
        return {"ticker": r.get("ticker"), "status": r.get("status"),
                "buy_reason": lat.get("buy_reason"), "bull_case": lat.get("bull_case"),
                "bear_case": lat.get("bear_case")}
    return r


def run_loop(scenario, projected, max_turns=6):
    tracer = core.Tracer()
    client = TieClient(tracer)
    messages = [{"role": "user", "content": scenario}]
    tin = tout = turns = 0
    tools_used, final = [], ""
    for _ in range(max_turns):
        resp = core._anthropic().messages.create(
            model=core.MODEL, max_tokens=900, system=core.SYSTEM_PROMPT,
            messages=messages, tools=TOOLS)
        tin += resp.usage.input_tokens; tout += resp.usage.output_tokens; turns += 1
        tu = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        txt = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if not tu:
            final = txt; break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in tu:
            tools_used.append(b.name)
            out = dispatch(b.name, b.input, client)
            if projected:
                out = project(b.name, out)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": to_json(out)})
        messages.append({"role": "user", "content": results})
    return {"in": tin, "out": tout, "turns": turns, "tools": tools_used, "final": final}


def main():
    import json
    from api.tiefstocks import TieClient
    from core import Tracer
    # 1) Direct per-tool result-size reduction (independent of loop noise)
    c = TieClient(Tracer())
    print("=== per-tool result size (chars) full -> projected ===")
    for name, call in [("analyze_stock", lambda: c.analyze("AMD")),
                       ("challenge_thesis", lambda: c.challenge("AMD")),
                       ("get_thesis", lambda: c.thesis("AMD"))]:
        full = call()
        f, p = len(to_json(full)), len(to_json(project(name, full)))
        print(f"  {name:16} {f:6} -> {p:5}  ({round(100*(1-p/f))}% smaller)")

    scenario = "Give me a full read on AMD: the intelligence analysis, my thesis, and the bear case."
    print(f"\n=== multi-step loop: '{scenario[:50]}...' ===")
    base = run_loop(scenario, projected=False)
    proj = run_loop(scenario, projected=True)
    price_in = core.PRICES.get(core.MODEL, (3, 15))[0]
    for tag, r in [("FULL results ", base), ("PROJECTED    ", proj)]:
        cost = r["in"] / 1e6 * price_in
        print(f"  {tag} turns={r['turns']} tools={r['tools']} input_tokens={r['in']} (${round(cost,4)})")
    if base["in"]:
        cut = round(100 * (1 - proj["in"] / base["in"]))
        print(f"\n  >>> input-token reduction across the loop: {cut}%  ({base['in']} -> {proj['in']})")
    print(f"\n  FULL answer:      {base['final'][:220]}")
    print(f"  PROJECTED answer: {proj['final'][:220]}")


if __name__ == "__main__":
    main()
