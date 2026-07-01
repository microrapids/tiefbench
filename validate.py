"""Probe-vs-execution validation harness.

Measures whether the cheap selection PROBE (used by Tune & Model Eval) predicts
what the REAL multi-turn agent actually does — per model, over a tagged scenario
set, matching against the FULL execution tool sequence (not just the first call).

Outputs a "transfer score" per model:
  - first_match: probe's pick == the tool the agent calls first   (strict)
  - set_match:   probe's pick is among the tools the agent used    (lenient — fair for multi-step)

CLI:  python validate.py
API:  validate(models=[...], probe_samples=3, exec_samples=1)
"""
from __future__ import annotations
import math
from collections import Counter
import core, tune, fit
import tools as T

NORM = fit.from_builtin(T.TOOLS, T.WRITE_TOOLS)

# Tagged scenarios: "single" = one clear tool, "multi" = exploratory / multi-step.
SCENARIOS = [
    ("What does my portfolio look like?", "single"),
    ("Show me my largest holding", "single"),
    ("List my transactions", "single"),
    ("Search for Nvidia", "single"),
    ("Do I have a thesis on AMD?", "single"),
    ("What's the bear case on AMD?", "single"),
    ("Get the details for AMD", "single"),
    ("Run the intelligence analysis on AMD", "single"),
    ("Is AMD a buy right now?", "multi"),
    ("Give me a full read on AMD", "multi"),
    ("Is my AMD thesis still valid given the bear case?", "multi"),
    ("Why did my portfolio change today?", "multi"),
    ("Should I trim my biggest position?", "multi"),
    ("How is AMD doing and does it match my thesis?", "multi"),
]


def wilson(k, n, z=1.96):
    if n == 0:
        return (0, 100)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, round(100 * (c - h))), min(100, round(100 * (c + h))))


def exec_trace(scenario, model=None, api_key=None, max_turns=6):
    """Run the REAL tool-use loop (production system prompt, native tools, writes
    gated) and capture the ordered tool sequence the agent actually calls."""
    from tools import TOOLS, dispatch, to_json
    from api.tiefstocks import TieClient
    tracer = core.Tracer()
    client = TieClient(tracer)
    cli = core.client_for(api_key)
    mdl = model or core.current_model()
    messages = [{"role": "user", "content": scenario}]
    seq = []
    for _ in range(max_turns):
        resp = cli.messages.create(model=mdl, max_tokens=800, system=core.SYSTEM_PROMPT,
                                   messages=messages, tools=TOOLS)
        tu = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not tu:
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in tu:
            seq.append(b.name)
            out = dispatch(b.name, b.input, client)     # gates writes
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": to_json(out)})
        messages.append({"role": "user", "content": results})
    return {"first": seq[0] if seq else "(no tool)", "tools": seq}


def validate(scenarios=None, models=None, probe_samples=3, exec_samples=1, api_key=None):
    scns = scenarios or [{"text": t, "tag": g} for t, g in SCENARIOS]
    models = models or [core.MODEL]
    out_models = {}
    for mid in models:
        rows, first_hits, set_hits, err = [], 0, 0, None
        for sc in scns:
            text = sc["text"] if isinstance(sc, dict) else sc
            tag = sc.get("tag", "single") if isinstance(sc, dict) else "single"
            try:
                probe_modal = tune.probe_n(NORM, text, n=probe_samples, model=mid, api_key=api_key)["chosen"]
                firsts, union = Counter(), set()
                for _ in range(max(1, exec_samples)):
                    tr = exec_trace(text, model=mid, api_key=api_key)
                    firsts[tr["first"]] += 1
                    union |= set(tr["tools"])
                exec_first = firsts.most_common(1)[0][0]
                first_match = probe_modal == exec_first
                set_match = probe_modal in union
            except Exception as e:  # noqa: BLE001
                err = str(e)[:160]
                probe_modal, exec_first, union, first_match, set_match = None, None, set(), False, False
            first_hits += first_match
            set_hits += set_match
            rows.append({"scenario": text, "tag": tag, "probe": probe_modal,
                         "exec_first": exec_first, "exec_tools": sorted(union),
                         "first_match": first_match, "set_match": set_match})
        n = len(scns) or 1
        out_models[mid] = {
            "label": next((m["label"] for m in __import__("modeleval").AVAILABLE if m["id"] == mid), mid),
            "rows": rows,
            "transfer_score": round(100 * first_hits / n),
            "transfer_ci": wilson(first_hits, n),
            "set_coverage": round(100 * set_hits / n),
            "n": n, "error": err,
            "by_tag": {tag: _tag_score(rows, tag) for tag in ("single", "multi")},
        }
    return {"models": models, "scenarios": len(scns),
            "probe_samples": probe_samples, "exec_samples": exec_samples,
            "results": out_models}


def _tag_score(rows, tag):
    sub = [r for r in rows if r["tag"] == tag]
    if not sub:
        return None
    return round(100 * sum(1 for r in sub if r["first_match"]) / len(sub))


def main():
    d = validate()
    for mid, r in d["results"].items():
        print(f"\n=== {r['label']} — transfer score {r['transfer_score']}% (CI {r['transfer_ci'][0]}–{r['transfer_ci'][1]}) "
              f"· set-coverage {r['set_coverage']}% · single={r['by_tag']['single']}% multi={r['by_tag']['multi']}% ===")
        for row in r["rows"]:
            print(f"  [{row['tag']:6}] {row['scenario'][:38]:38} probe={str(row['probe']):20} "
                  f"first={str(row['exec_first']):20} {'✓' if row['first_match'] else ('~' if row['set_match'] else '✗')}")


if __name__ == "__main__":
    main()
