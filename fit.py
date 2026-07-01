"""Fit Advisor — analyze a Tool Pack and predict how each option will perform.

Pure, cheap, no LLM. Reads a pack (TiefWise v0.1 pack, TiefWise MCP manifest, or
the built-in tool list), computes signals, and returns per-option verdicts
(green/amber/red) with reasons, mitigations, and an estimated cost — so the UI can
warn the user *before* they run (e.g. "MCP may underperform: too many tools").

Thresholds are heuristic seeds; they can later be recalibrated from the bake-off
DB (store.py) by sweeping pack size and measuring the accuracy/cost cliff.
"""
from __future__ import annotations
import json

PRICE_IN, PRICE_OUT = 3.0, 15.0   # ~claude-sonnet-4-6 $/1M (in, out)


def _toks(obj) -> int:
    try:
        return max(1, len(json.dumps(obj, default=str)) // 4)
    except Exception:
        return 1


# ---- normalize any pack shape into a common tool list ----
def normalize(pack: dict) -> list[dict]:
    tools = pack.get("tools", []) if isinstance(pack, dict) else []
    out = []
    for t in tools:
        schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
        http = t.get("_http") or {}
        method = (t.get("method") or http.get("method") or
                  (t.get("source") or {}).get("method") or "GET").upper()
        path = t.get("path") or http.get("path") or (t.get("source") or {}).get("path") or ""
        # risk: prefer explicit safety.risk; else MCP annotations; else method
        risk = (t.get("safety") or {}).get("risk")
        if not risk:
            ann = t.get("annotations") or {}
            if ann.get("destructiveHint"):
                risk = "destructive"
            elif ann.get("readOnlyHint"):
                risk = "read"
            else:
                risk = "read" if method == "GET" else "write"
        # bindings: normalize to {param: "path"|"query"|"body"} for the generic dispatcher
        bindings = {}
        pb = http.get("paramBindings")
        if isinstance(pb, dict):
            bindings = dict(pb)
        elif isinstance(t.get("bindings"), dict):
            for loc, names in t["bindings"].items():
                for nm in (names or []):
                    bindings[nm] = loc
        content_type = http.get("contentType") or (t.get("execution") or {}).get("contentType")
        out.append({"name": t.get("name", "?"), "description": t.get("description", ""),
                    "schema": schema, "method": method, "path": path, "risk": risk,
                    "bindings": bindings, "content_type": content_type})
    return out


def from_builtin(anthropic_tools: list, write_names: set) -> list[dict]:
    out = []
    for t in anthropic_tools:
        out.append({"name": t["name"], "description": t.get("description", ""),
                    "schema": t.get("input_schema", {}),
                    "method": "POST" if t["name"] in write_names else "GET",
                    "path": "/api/" + t["name"],
                    "risk": "write" if t["name"] in write_names else "read"})
    return out


# ---- signals ----
def signals(tools: list[dict]) -> dict:
    n = len(tools)
    schema_tokens = sum(_toks({"n": t["name"], "d": t["description"], "s": t["schema"]}) for t in tools)
    writes = sum(1 for t in tools if t["risk"] in ("write", "destructive"))
    destructive = sum(1 for t in tools if t["risk"] == "destructive")
    groups = {(t["path"].split("/")[1] if t["path"].startswith("/") and "/" in t["path"][1:]
               else t["path"] or "_") for t in tools}
    params = [len((t["schema"].get("properties") or {})) for t in tools]
    described = sum(1 for t in tools if len(t["description"] or "") >= 25)
    return {
        "n_tools": n,
        "schema_tokens": schema_tokens,
        "writes": writes, "destructive": destructive,
        "path_groups": len(groups),
        "avg_params": round(sum(params) / n, 1) if n else 0,
        "desc_quality": round(described / n, 2) if n else 0,
        "per_tool_tokens": round(schema_tokens / n) if n else 0,
    }


# ---- per-option assessment ----
def _badge(risk):  # risk score -> traffic light
    return "green" if risk < 2 else ("amber" if risk < 4 else "red")


def _est_cost(in_per_turn, turns):
    out = 500 * turns
    return round((in_per_turn * turns * PRICE_IN + out * PRICE_OUT) / 1e6, 4)


def assess(sig: dict) -> dict:
    n, st = sig["n_tools"], sig["schema_tokens"]
    multi = sig["path_groups"] >= 3
    big = st > 7000
    medium = 3000 < st <= 7000
    many = n > 25
    some = 13 <= n <= 25
    has_dx = sig["destructive"] > 0
    has_w = sig["writes"] > 0
    low_desc = sig["desc_quality"] < 0.6
    base = 800  # system + prompt overhead

    opts = []

    # B — function calling
    r, why, fix = 0, [], []
    if many: r += 2; why.append(f"All {n} tool schemas sit in context every call — selection accuracy drops.")
    elif some: r += 1; why.append(f"{n} tools is a sizable always-in-context surface.")
    if big: r += 1; why.append(f"~{st:,} schema tokens per call inflates cost.")
    if multi: r += 2; why.append("No domain routing — cross-domain mis-selection likely.")
    if not many and not multi and n <= 12: why.append("Small, controlled surface — function calling's sweet spot.")
    if has_dx: why.append("Destructive tools present — rely on the policy gate for approval.")
    fix = ["Split into smaller scoped packs", "Use Orchestrator (E) for routing", "Add tool-search if many"]
    opts.append(("b", "Function calling", r, why, fix, _est_cost(base + st, 2)))

    # C — MCP
    r, why, fix = 0, [], []
    if many or big: r += 3; why.append(f"tools/list loads all {n} schemas (~{st:,} tok) into context — this is where MCP degrades with too many tools.")
    elif some or medium: r += 1; why.append("Moderate surface; watch context growth as the pack grows.")
    if multi: r += 1; why.append("Multiple domains via one server — discovery helps but context still grows.")
    why.append("Runtime discovery + reusable across clients; governance enforced server-side.")
    fix = ["Enable tool-search / retrieval", "Split into per-domain MCP servers", "Cap exposed tools per role"]
    opts.append(("c", "MCP", r, why, fix, _est_cost(base + st, 2)))

    # D — mrapids
    r, why, fix = 0, [], []
    if low_desc: r += 2; why.append("Planner picks ops from descriptions — weak descriptions hurt selection.")
    if many: r += 1; why.append("Large catalog for the planner to choose from.")
    if has_w or has_dx: why.append("Deterministic + auditable execution — strong for governed writes.")
    why.append("Agent picks workflows, not raw endpoints.")
    fix = ["Sharpen tool descriptions", "Predefine collections for multi-step flows"]
    opts.append(("d", "mrapids", r, why, fix, _est_cost(base + st, 2)))

    # E — orchestrator
    r, why, fix = 0, [], []
    if multi: why.append("Routing + least-privilege scoping — fits multi-domain packs well.")
    if many or big: why.append("Scopes shrink the per-call tool surface — scales better than flat B/C.")
    if not multi and n <= 8: r += 1; why.append("Router overhead with little benefit for a small single-domain pack.")
    if has_dx: why.append("Destructive tools hidden unless routed to a write scope — least-privilege by construction.")
    fix = ["Best for large / multi-domain packs", "Skip for tiny single-domain packs (use B)"]
    opts.append(("e", "Orchestrator", r, why, fix, _est_cost(base + st * 0.5, 3)))

    # A — dynamic python
    r, why, fix = 0, [], []
    r += 1; why.append("High output variance; needs a hardened sandbox.")
    if has_dx: r += 3; why.append("Dynamic code + destructive tools = highest blast radius.")
    elif has_w: r += 1; why.append("Writes via generated code — gate at the gateway.")
    why.append("Maximum flexibility — best as a benchmark, not a production write path.")
    fix = ["Benchmark only", "Never on write paths without sandbox + gateway"]
    opts.append(("a", "Dynamic Python", r, why, fix, _est_cost(base + st * 0.3, 2)))

    options = [{"key": k, "label": lbl, "badge": _badge(r), "risk": r,
                "reasons": why, "mitigations": fix, "est_cost": cost}
               for (k, lbl, r, why, fix, cost) in opts]
    # recommendation: lowest risk, tie-break by preference B>E>C>D>A
    pref = {"b": 0, "e": 1, "c": 2, "d": 3, "a": 4}
    best = sorted(options, key=lambda o: (o["risk"], pref[o["key"]]))[0]
    return {"options": options, "recommended": best["key"], "recommended_label": best["label"]}


def simulate(sig: dict, n: int) -> dict:
    """Scale a signals dict to a hypothetical tool count (for the what-if slider)."""
    per = sig["per_tool_tokens"] or 120
    s = dict(sig)
    s["n_tools"] = n
    s["schema_tokens"] = per * n
    # keep write/destructive presence; scale path groups gently with size
    s["path_groups"] = max(sig["path_groups"], 1 + n // 10)
    return s


def _apply_calibration(result: dict, calib: dict, simulated: bool):
    """Fold observed bake-off stats into each option: real cost/accuracy + mismatch flags."""
    for o in result["options"]:
        obs = calib.get(o["key"])
        if not obs or not obs.get("runs"):
            continue
        o["observed"] = obs
        if obs.get("cost") is not None and not simulated:
            o["measured_cost"] = obs["cost"]        # prefer real cost over the estimate
        acc = obs.get("acc")
        if acc is not None:                          # predicted-vs-observed reconciliation
            if o["badge"] == "green" and acc < 75:
                o["reasons"].insert(0, f"⚠ Measured accuracy is only {acc} despite a good predicted fit — investigate.")
                o["badge"] = "amber"
            elif o["badge"] == "red" and acc >= 85 and not simulated:
                o["reasons"].insert(0, f"Note: measured accuracy is solid ({acc}) at the current small pack — the risk grows as it scales.")
    meta = calib.get("_meta", {})
    result["calibration"] = {"runs": meta.get("total", 0), "model": meta.get("model")}


def analyze(tools: list[dict], simulate_n: int | None = None, calib: dict | None = None) -> dict:
    sig = signals(tools)
    use = simulate(sig, simulate_n) if simulate_n else sig
    result = assess(use)
    result["signals"] = use
    result["real_n"] = sig["n_tools"]
    result["simulated"] = bool(simulate_n)
    if calib:
        _apply_calibration(result, calib, bool(simulate_n))
    return result
