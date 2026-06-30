"""Tune my MCP — improve tool descriptions from real scenarios (AI-draft + approve).

The thing being tuned is *tool selection given descriptions*, so we use a cheap
"selection probe": given the pack + a scenario, which ONE tool would the agent
pick, how confident, and are the top choices interchangeable? No live API/auth
needed — it isolates description quality and makes A/B re-testing fast.

Flow: analyze(scenarios) -> per-scenario selection + issues + AI-drafted
description fixes; retest(edits) -> re-probe with approved edits for before/after.
"""
from __future__ import annotations
import json
import core


def _tools_block(tools: list[dict]) -> str:
    lines = []
    for t in tools:
        params = list((t.get("schema") or {}).get("properties", {}).keys())
        line = f"- {t['name']}: {t.get('description', '') or '(no description)'}"
        if params:
            line += f"  [params: {', '.join(params)}]"
        lines.append(line)
    return "\n".join(lines)


PROBE_SYS = ("You are an AI agent choosing exactly ONE tool to fulfill a request. "
             "Choose only from the listed tools. Output ONLY valid JSON.")


def probe(tools: list[dict], scenario: str, model: str | None = None, api_key: str | None = None) -> dict:
    msg = [{"role": "user", "content":
            "TOOLS:\n" + _tools_block(tools) +
            f"\n\nUSER REQUEST: {scenario}\n\n"
            'Return JSON: {"chosen": "<tool name or null>", '
            '"ranked": ["up to 3 tool names, best first"], "confidence": <0..1>, '
            '"why": "<one short clause>", "why_not": "<why not the runner-up>", '
            '"interchangeable": <true if the top choices are basically equivalent for this request>}'}]
    r = core.client_for(api_key).messages.create(
        model=model or core.MODEL, max_tokens=400, system=PROBE_SYS, messages=msg)
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("{"), txt.rfind("}")
    try:
        d = json.loads(txt[s:e + 1])
    except Exception:
        d = {"chosen": None, "ranked": [], "confidence": 0, "why": "", "why_not": "", "interchangeable": False}
    d["_tokens"] = {"in": getattr(r.usage, "input_tokens", 0) or 0,
                    "out": getattr(r.usage, "output_tokens", 0) or 0}
    return d


def probe_n(tools, scenario, n=1, model=None, api_key=None) -> dict:
    """Run the selection probe N times → modal tool + behavioral STABILITY
    (how often it picks the same tool) + distribution. Stability is the honest
    ambiguity signal: self-reported confidence can rationalize, repeated sampling
    cannot."""
    from collections import Counter
    picks, confs, inter, tin, tout, last = Counter(), [], 0, 0, 0, {}
    for _ in range(max(1, n)):
        d = probe(tools, scenario, model=model, api_key=api_key)
        last = d
        picks[d.get("chosen")] += 1
        if d.get("confidence") is not None:
            confs.append(d["confidence"])
        if d.get("interchangeable"):
            inter += 1
        tk = d.get("_tokens", {})
        tin += tk.get("in", 0); tout += tk.get("out", 0)
    total = sum(picks.values()) or 1
    modal, modal_ct = picks.most_common(1)[0]
    return {"chosen": modal, "stability": round(modal_ct / total, 2),
            "distribution": dict(picks), "samples": total,
            "confidence": round(sum(confs) / len(confs), 2) if confs else None,
            "interchangeable": inter > total / 2,
            "ranked": last.get("ranked", []), "why": last.get("why"), "why_not": last.get("why_not"),
            "_tokens": {"in": tin, "out": tout}}


def diagnose(d: dict, expected: str | None = None) -> list[str]:
    issues = []
    if not d.get("chosen"):
        issues.append("no_tool")
    if (d.get("confidence") or 0) < 0.6:
        issues.append("low_confidence")
    # behavioral ambiguity: flips tools across samples, or self-reports interchangeable
    if d.get("interchangeable") or (d.get("stability") is not None and d["stability"] < 0.7):
        issues.append("ambiguous")
    if expected and d.get("chosen") and d["chosen"] != expected:
        issues.append("wrong_tool")
    return issues


def _probe_all(tools, scenarios, samples=1):
    out = []
    for sc in scenarios:
        text = sc.get("text") if isinstance(sc, dict) else sc
        expected = sc.get("expected") if isinstance(sc, dict) else None
        d = probe_n(tools, text, n=samples)
        d["scenario"] = text
        d["expected"] = expected
        d["issues"] = diagnose(d, expected)
        out.append(d)
    return out


def summarize(results):
    n = len(results) or 1
    stab = [r["stability"] for r in results if r.get("stability") is not None]
    return {
        "scenarios": len(results),
        "samples": (results[0].get("samples") if results else 1),
        "ambiguous": sum(1 for r in results if "ambiguous" in r["issues"]),
        "low_confidence": sum(1 for r in results if "low_confidence" in r["issues"]),
        "no_tool": sum(1 for r in results if "no_tool" in r["issues"]),
        "wrong_tool": sum(1 for r in results if "wrong_tool" in r["issues"]),
        "clean": sum(1 for r in results if not r["issues"]),
        "avg_confidence": round(sum((r.get("confidence") or 0) for r in results) / n, 2),
        "avg_stability": round(sum(stab) / len(stab), 2) if stab else None,
    }


SUGGEST_SYS = ("You improve MCP tool descriptions so an agent reliably selects the right tool. "
               "Output ONLY a JSON array.")


def suggest(tools, results):
    problems = [r for r in results if r["issues"]]
    if not problems:
        return []
    names = set()
    for r in problems:
        for nm in (r.get("ranked") or [])[:2]:
            names.add(nm)
        if r.get("chosen"):
            names.add(r["chosen"])
    cur = {t["name"]: t.get("description", "") for t in tools if t["name"] in names}
    if not cur:
        return []
    probtxt = "\n".join(
        f'- "{r["scenario"]}" -> chose {r.get("chosen")} (conf {r.get("confidence")}, issues: {", ".join(r["issues"])})'
        for r in problems)
    msg = [{"role": "user", "content":
            "Current tool descriptions:\n" + "\n".join(f"- {k}: {v}" for k, v in cur.items()) +
            "\n\nProblem scenarios (selection was ambiguous / low-confidence / wrong):\n" + probtxt +
            "\n\nRewrite ONLY the descriptions that need disambiguation. Each new description must state "
            "WHEN to use and WHEN NOT to use it (vs the confusable tool), be specific and concise. "
            'Return JSON array: [{"name":"<tool>","new_description":"<improved>","rationale":"<why this fixes it>"}]'}]
    r = core._anthropic().messages.create(model=core.MODEL, max_tokens=1000, system=SUGGEST_SYS, messages=msg)
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("["), txt.rfind("]")
    try:
        return json.loads(txt[s:e + 1])
    except Exception:
        return []


def analyze(tools, scenarios, samples=1):
    results = _probe_all(tools, scenarios, samples=samples)
    return {"results": results, "summary": summarize(results),
            "suggestions": suggest(tools, results),
            "tools": [{"name": t["name"], "description": t.get("description", "")} for t in tools]}


def apply_edits(tools, edits: dict):
    out = []
    for t in tools:
        t2 = dict(t)
        if edits.get(t["name"]):
            t2["description"] = edits[t["name"]]
        out.append(t2)
    return out


def retest(tools, edits, scenarios, samples=1):
    new_tools = apply_edits(tools, edits or {})
    results = _probe_all(new_tools, scenarios, samples=samples)
    return {"results": results, "summary": summarize(results)}
