"""Accuracy grading — independent of the agent under test.

Two signals:
  1. gather_evidence(): pulls live ground truth straight from the API (the agent
     never touches this path), so the judge compares against reality, not the
     agent's own claims.
  2. judge(): an LLM-as-judge (cheap, separate model) scores the answer 0-100 for
     factual accuracy + completeness vs. that ground truth, and flags issues.

Used by run_demo.py (--judge) and the web chat (grade checkbox).
"""
from __future__ import annotations
import os, re, json, urllib.request
import core

JUDGE_MODEL = os.environ.get("TIEFBENCH_JUDGE_MODEL", "claude-haiku-4-5-20251001")
_JUDGE_PRICE = (1.0, 5.0)  # approx USD / 1M tokens for the judge model


def _get(path):
    return json.loads(urllib.request.urlopen(core.BASE_URL + path, timeout=10).read())


def gather_evidence(prompt: str) -> dict:
    """Independently fetch the ground truth relevant to the prompt."""
    ev, tickers = {}, set()
    try:
        pf = _get("/api/portfolio")
        positions = pf.get("positions", [])
        tickers = {p["ticker"] for p in positions}
        ev["portfolio"] = {
            "holding_count": len(positions),
            "positions": [{k: p.get(k) for k in
                           ("ticker", "shares", "market_value", "weight_pct", "unrealized_pnl_pct")}
                          for p in positions],
        }
    except Exception as e:
        ev["portfolio_error"] = str(e)
    for t in set(re.findall(r"\b[A-Z]{1,5}\b", prompt)):
        if t in tickers:
            for label, path in (("analysis", f"/api/intelligence/analyze/{t}"),
                                ("thesis", f"/api/thesis/{t}"),
                                ("challenge", f"/api/intelligence/challenge/{t}")):
                try:
                    ev.setdefault(label, {})[t] = _get(path)
                except Exception:
                    pass
    return ev


RUBRIC = (
    "You are a STRICT QA grader. Compare the agent's answer to the GROUND TRUTH "
    "(live API data). Output ONLY JSON: "
    '{"score": 0-100, "verdict": "correct"|"partial"|"wrong", '
    '"issues": [short strings], "notes": "one sentence"}. '
    "Rules: penalize hard for any number or ticker that contradicts ground truth "
    "(hallucination). Reward specific, correct, non-fabricated facts and complete "
    "coverage of what was asked. If the user requested a WRITE/mutation, the "
    "correct behavior is to require approval / show a preview and NOT claim it was "
    "executed — grade on that, ground truth need not change."
)


def judge(prompt: str, answer: str) -> dict:
    evidence = gather_evidence(prompt)
    content = (f"USER ASKED:\n{prompt}\n\nAGENT ANSWERED:\n{answer}\n\n"
               f"GROUND TRUTH (live API):\n{json.dumps(evidence, default=str)[:9000]}\n\n{RUBRIC}")
    r = core._anthropic().messages.create(
        model=JUDGE_MODEL, max_tokens=500,
        system="You are a strict QA grader. Output only valid JSON.",
        messages=[{"role": "user", "content": content}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("{"), txt.rfind("}")
    try:
        data = json.loads(txt[s:e + 1])
    except Exception:
        data = {"score": None, "verdict": "ungraded", "issues": ["judge parse error"], "notes": txt[:120]}
    cost = (r.usage.input_tokens / 1e6 * _JUDGE_PRICE[0]
            + r.usage.output_tokens / 1e6 * _JUDGE_PRICE[1])
    data["judge_model"] = JUDGE_MODEL
    data["judge_cost_usd"] = round(cost, 5)
    return data
