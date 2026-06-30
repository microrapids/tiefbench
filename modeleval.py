"""Model Eval — run the same scenarios across multiple models and compare.

Answers "I tested on model X, prod runs model Y — will it still behave?" by
probing tool selection per model, diffing each against a reference (baseline)
model, and recommending the cheapest model that still matches.

MVP: Anthropic tiers/versions, selection eval (reuses tune.probe), optional
transient BYOK key (blank = server env key). Cross-provider + full-task = later.
"""
from __future__ import annotations
import math
import core
import tune

AVAILABLE = [
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]
AGREE_BAR = 80   # % agreement with reference to be "safe to switch"


def _cost(model, tin, tout):
    p = core.PRICES.get(model)
    return round(tin / 1e6 * p[0] + tout / 1e6 * p[1], 4) if p else None


def _wilson(k, n, z=1.96):
    """95% Wilson confidence interval (as %) for k successes in n — honest about small n."""
    if n == 0:
        return (0, 100)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    halfw = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0, round(100 * (centre - halfw))), min(100, round(100 * (centre + halfw))))


def evaluate(tools, scenarios, models, reference, api_key=None, samples=1):
    texts = [(sc.get("text") if isinstance(sc, dict) else sc) for sc in scenarios]
    per_model = {}
    for mid in models:
        rows, tin, tout, err = [], 0, 0, None
        for txt in texts:
            try:
                d = tune.probe_n(tools, txt, n=samples, model=mid, api_key=api_key)
                tk = d.get("_tokens", {})
                tin += tk.get("in", 0); tout += tk.get("out", 0)
                rows.append({"scenario": txt, "chosen": d.get("chosen"), "stability": d.get("stability"),
                             "confidence": d.get("confidence"), "interchangeable": d.get("interchangeable")})
            except Exception as e:  # noqa: BLE001 - bad key/model id, surface per model
                err = str(e)[:160]
                rows.append({"scenario": txt, "chosen": None, "stability": None,
                             "confidence": None, "interchangeable": None})
        per_model[mid] = {"rows": rows, "tokens_in": tin, "tokens_out": tout, "error": err}

    ref_choice = {r["scenario"]: r["chosen"] for r in per_model.get(reference, {}).get("rows", [])}

    def _cell(mid, txt):
        r = next((r for r in per_model[mid]["rows"] if r["scenario"] == txt), None)
        return {"chosen": r["chosen"] if r else None, "stability": r["stability"] if r else None}

    # matrix: one row per scenario, each model's pick (+stability), diverges flag
    matrix = []
    for txt in texts:
        cells = {mid: _cell(mid, txt) for mid in models}
        diverges = any(mid != reference and cells[mid]["chosen"] != ref_choice.get(txt) for mid in models)
        matrix.append({"scenario": txt, "reference": ref_choice.get(txt), "cells": cells, "diverges": diverges})

    # per-model summary (with Wilson CI on agreement + behavioral stability)
    summaries = {}
    n = len(texts) or 1
    for mid, pm in per_model.items():
        agree = sum(1 for r in pm["rows"] if r["chosen"] == ref_choice.get(r["scenario"]))
        conf = [r["confidence"] for r in pm["rows"] if r["confidence"] is not None]
        stab = [r["stability"] for r in pm["rows"] if r["stability"] is not None]
        summaries[mid] = {
            "label": next((m["label"] for m in AVAILABLE if m["id"] == mid), mid),
            "is_reference": mid == reference,
            "agreement": round(100 * agree / n),
            "agreement_ci": _wilson(agree, n),
            "avg_confidence": round(sum(conf) / len(conf), 2) if conf else None,
            "avg_stability": round(sum(stab) / len(stab), 2) if stab else None,
            "ambiguous": sum(1 for r in pm["rows"] if r.get("interchangeable")),
            "est_cost": _cost(mid, pm["tokens_in"], pm["tokens_out"]),
            "tokens_in": pm["tokens_in"], "tokens_out": pm["tokens_out"],
            "samples": samples, "error": pm["error"],
        }

    # recommendation: cheapest non-reference model that still agrees >= bar
    cands = [(mid, s) for mid, s in summaries.items()
             if not s["is_reference"] and s["agreement"] >= AGREE_BAR and s["est_cost"] is not None and not s["error"]]
    rec = sorted(cands, key=lambda x: x[1]["est_cost"])[0][0] if cands else None
    ref_cost = summaries.get(reference, {}).get("est_cost")
    rec_note = None
    if rec:
        s = summaries[rec]
        save = (round(100 * (1 - s["est_cost"] / ref_cost)) if ref_cost else None)
        rec_note = (f"{s['label']} matches your reference on {s['agreement']}% of scenarios"
                    + (f" at ~{save}% lower cost" if save else "") + ".")
    elif len(models) > 1:
        rec_note = f"No cheaper model meets the {AGREE_BAR}% agreement bar — divergences are real prod risk."

    return {"reference": reference, "models": models, "matrix": matrix, "samples": samples,
            "summaries": summaries, "divergences": sum(1 for m in matrix if m["diverges"]),
            "recommended": rec, "recommendation": rec_note}
