"""Option B — Direct function calling with curated tools.

The canonical Anthropic tool-use loop. The model picks from a small curated tool
set; the policy layer gates writes. This is the baseline everything else is
measured against.
"""
from __future__ import annotations
from core import RunResult, Tracer, llm_stream, timed
from api.tiefstocks import TieClient
from tools import TOOLS, dispatch, to_json

NAME = "B:function-calling"
MAX_TURNS = 8


@timed
def run(task) -> RunResult:
    tracer = Tracer()
    client = TieClient(tracer)
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)
    messages = [{"role": "user", "content": task.prompt}]

    for _ in range(MAX_TURNS):
        resp = llm_stream(messages, tracer, tools=TOOLS, label="loop")
        if resp.stop_reason != "tool_use":
            res.final_text = "".join(b.text for b in resp.content if b.type == "text")
            return res
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = dispatch(block.name, block.input, client)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": to_json(out)})
        messages.append({"role": "user", "content": results})

    res.final_text = "(stopped: max turns reached)"
    return res
