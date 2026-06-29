"""Render run results into a console scorecard + markdown + JSON."""
from __future__ import annotations
import json, os


def _row(r, sc):
    return {
        "option": r.option, "task": r.task_id,
        "success": sc["success"], "right_calls": sc["right_calls"],
        "answered": sc["answered"], "safe": sc["safe"],
        "api_calls": sc["n_api"], "blocked": sc["n_blocked"],
        "llm_calls": r.tracer.llm_calls,
        "tok_in": r.tracer.tokens_in, "tok_out": r.tracer.tokens_out,
        "cost_usd": round(r.cost_usd(), 5),
        "latency_s": round(r.latency_ms / 1000, 2),
        "acc_score": sc.get("acc_score"),
        "acc_verdict": sc.get("acc_verdict"),
        "intent": sc.get("intent"),
        "action_type": sc.get("action_type"),
        "calls": sc.get("calls", []),
        "error": r.error,
    }


def build(results_with_scores):
    return [_row(r, sc) for r, sc in results_with_scores]


def to_console(rows):
    hdr = ["option", "task", "ok", "calls?", "ans?", "safe", "acc", "api", "blk",
           "llm", "tok_in", "tok_out", "$", "sec"]
    def cell(b): return "✓" if b else "✗"
    lines = []
    lines.append("  ".join(f"{h:<16}" if h == "option" else f"{h:>7}" for h in hdr))
    lines.append("-" * 128)
    for x in rows:
        acc = "-" if x.get("acc_score") is None else str(x["acc_score"])
        lines.append("  ".join([
            f"{x['option']:<16}", f"{x['task']:>7}", f"{cell(x['success']):>7}",
            f"{cell(x['right_calls']):>7}", f"{cell(x['answered']):>7}",
            f"{cell(x['safe']):>7}", f"{acc:>7}", f"{x['api_calls']:>7}", f"{x['blocked']:>7}",
            f"{x['llm_calls']:>7}", f"{x['tok_in']:>7}", f"{x['tok_out']:>7}",
            f"{x['cost_usd']:>7}", f"{x['latency_s']:>7}"]))
    return "\n".join(lines)


def to_markdown(rows):
    cols = ["option", "task", "action_type", "success", "right_calls", "answered", "safe",
            "acc_score", "acc_verdict", "api_calls", "blocked", "llm_calls",
            "tok_in", "tok_out", "cost_usd", "latency_s"]
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for x in rows:
        out.append("| " + " | ".join(str(x[c]) for c in cols) + " |")
    return "\n".join(out)


def to_reasoning(rows):
    """Per-run appendix: captured intent + each API call's reason."""
    out = ["\n## Intent & Reasoning\n"]
    for x in rows:
        out.append(f"### {x['option']} · {x['task']}")
        if x.get("intent"):
            out.append(f"- **Intent** ({x.get('action_type') or '?'}): {x['intent']}")
        calls = x.get("calls") or []
        if calls:
            out.append("- **API calls & why:**")
            for c in calls:
                tag = "BLOCKED " if c.get("blocked") else ""
                out.append(f"  - `{tag}{c['verb']} {c['path']}` — {c.get('reason') or '(no reason given)'}")
        else:
            out.append("- _(no API calls)_")
        out.append("")
    return "\n".join(out)


def aggregate(rows):
    """Per-option averages + variance across all tasks/runs — for picking a winner."""
    from statistics import mean, pstdev
    groups = {}
    for x in rows:
        groups.setdefault(x["option"], []).append(x)
    out = []
    for opt, xs in groups.items():
        accs = [x["acc_score"] for x in xs if x.get("acc_score") is not None]
        costs = [x["cost_usd"] for x in xs]
        lats = [x["latency_s"] for x in xs]
        out.append({
            "option": opt, "runs": len(xs),
            "success_%": round(100 * mean([1 if x["success"] else 0 for x in xs])),
            "safe_%": round(100 * mean([1 if x["safe"] else 0 for x in xs])),
            "avg_acc": round(mean(accs), 1) if accs else None,
            "acc_sd": round(pstdev(accs), 1) if len(accs) > 1 else 0.0,
            "avg_cost": round(mean(costs), 4),
            "cost_sd": round(pstdev(costs), 4) if len(costs) > 1 else 0.0,
            "avg_lat": round(mean(lats), 1),
            "avg_api": round(mean([x["api_calls"] for x in xs]), 1),
            "avg_tok_in": round(mean([x["tok_in"] for x in xs])),
            "avg_tok_out": round(mean([x["tok_out"] for x in xs])),
        })
    # rank: accuracy first, then safety, then cost (the decision rule)
    return sorted(out, key=lambda r: (-(r["avg_acc"] or 0), -r["safe_%"], r["avg_cost"]))


def agg_console(agg):
    hdr = ["option", "runs", "succ%", "safe%", "acc", "acc±", "cost$", "cost±", "lat_s", "api", "tok_in", "tok_out"]
    lines = ["  ".join(f"{h:<16}" if h == "option" else f"{h:>8}" for h in hdr), "-" * 130]
    for a in agg:
        lines.append("  ".join([
            f"{a['option']:<16}", f"{a['runs']:>8}", f"{a['success_%']:>8}", f"{a['safe_%']:>8}",
            f"{a['avg_acc']:>8}", f"{a['acc_sd']:>8}", f"{a['avg_cost']:>8}", f"{a['cost_sd']:>8}",
            f"{a['avg_lat']:>8}", f"{a['avg_api']:>8}", f"{a['avg_tok_in']:>8}", f"{a['avg_tok_out']:>8}"]))
    return "\n".join(lines)


def agg_markdown(agg):
    cols = ["option", "runs", "success_%", "safe_%", "avg_acc", "acc_sd", "avg_cost",
            "cost_sd", "avg_lat", "avg_api", "avg_tok_in", "avg_tok_out"]
    out = ["## Per-option aggregate (ranked: accuracy → safety → cost)\n",
           "| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for a in agg:
        out.append("| " + " | ".join(str(a[c]) for c in cols) + " |")
    return "\n".join(out)


def save(rows, outdir):
    os.makedirs(outdir, exist_ok=True)
    json.dump(rows, open(os.path.join(outdir, "results.json"), "w"), indent=2)
    open(os.path.join(outdir, "scorecard.md"), "w").write(
        "# Bake-off Scorecard\n\n" + to_markdown(rows) + "\n" + to_reasoning(rows) + "\n")
