"""Intent capture — a fast pre-step that records what the agent understood.

Runs before the option executes, so every option shows the same structured
reading of the user's request: goal, entities, action type, and a planned
sequence. Cheap model, independent of the option under test.
"""
from __future__ import annotations
import os, json
import core

INTENT_MODEL = os.environ.get("TIEFBENCH_INTENT_MODEL", "claude-haiku-4-5-20251001")

PROMPT = (
    "You read a user message for a portfolio/stocks assistant and extract intent. "
    "Output ONLY JSON: "
    '{"intent": "one-sentence goal", '
    '"entities": ["tickers/amounts/dates mentioned"], '
    '"action_type": "read" | "analysis" | "write", '
    '"plan": ["short steps the agent should take, in order"]}'
)


def capture(message: str) -> dict:
    r = core._anthropic().messages.create(
        model=INTENT_MODEL, max_tokens=400,
        system="You output only valid JSON.",
        messages=[{"role": "user", "content": f"User message: {message}\n\n{PROMPT}"}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    s, e = txt.find("{"), txt.rfind("}")
    try:
        data = json.loads(txt[s:e + 1])
    except Exception:
        data = {"intent": message, "entities": [], "action_type": "read", "plan": []}
    data["model"] = INTENT_MODEL
    return data
