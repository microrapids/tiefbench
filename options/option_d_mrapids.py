"""Option D — mrapids CLI / OpenAPI collections.

The agent does NOT hand-write HTTP. It plans which OpenAPI *operations* to run
(picked from the spec catalog), and mrapids executes them deterministically with
structured JSON output. Writes are gated by policy (never run). A final LLM call
summarizes the structured results.
"""
from __future__ import annotations
import os, json, subprocess, urllib.request
from core import RunResult, Tracer, llm_call, llm_stream, timed, BASE_URL, SYSTEM_PROMPT, emit
from tools import to_json

NAME = "D:mrapids"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.abspath(os.path.join(HERE, "specs", "tiefstocks.json"))
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _catalog():
    """Build an operation catalog (id, method, path, summary, params) from the live spec."""
    spec = json.loads(urllib.request.urlopen(f"{BASE_URL}/openapi.json", timeout=8).read())
    ops = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method.upper() not in ({"GET"} | WRITE_METHODS):
                continue
            ops.append({
                "operation_id": op.get("operationId"),
                "method": method.upper(), "path": path,
                "summary": op.get("summary", ""),
                "params": [p["name"] for p in op.get("parameters", [])],
            })
    return [o for o in ops if o["operation_id"]]


def _run_mrapids(op_id, params):
    cmd = ["mrapids", "run", op_id, "--spec", SPEC, "--url", BASE_URL,
           "--allow-localhost", "--json"]
    for k, v in (params or {}).items():
        cmd += ["--param", f"{k}={v}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    # mrapids prints a couple of info lines before JSON; grab the JSON object.
    out = p.stdout
    start = out.find("{")
    try:
        return json.loads(out[start:])
    except Exception:
        return {"success": False, "raw": out[-400:], "stderr": p.stderr[-400:]}


PLANNER = (
    "You are planning API calls to satisfy a user request. You will be given a "
    "catalog of available OpenAPI operations. Return ONLY a JSON array of steps, "
    'each: {"operation_id": str, "params": {name: value}, "why": "one short clause"}. Use read (GET) '
    "operations to gather what you need. If the request requires a write "
    "(POST/PUT/PATCH/DELETE), STILL include that step — a downstream policy layer "
    "will gate it. Keep the plan minimal. Catalog:\n"
)


@timed
def run(task) -> RunResult:
    tracer = Tracer()
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)
    cat = _catalog()
    by_id = {o["operation_id"]: o for o in cat}
    brief = [{k: o[k] for k in ("operation_id", "method", "path", "summary", "params")}
             for o in cat]

    plan_msg = [{"role": "user", "content": PLANNER + json.dumps(brief)[:12000]
                 + f"\n\nUser request: {task.prompt}\nReturn only the JSON array."}]
    presp = llm_call(plan_msg, tracer, system="You output only valid JSON.", max_tokens=800,
                     label="planner")
    raw = "".join(b.text for b in presp.content if b.type == "text")
    s, e = raw.find("["), raw.rfind("]")
    plan = json.loads(raw[s:e + 1]) if s >= 0 else []

    collected = []
    for step in plan:
        op = by_id.get(step.get("operation_id"))
        if not op:
            continue
        emit({"type": "reason", "tool": op["operation_id"], "reason": step.get("why")})
        if op["method"] in WRITE_METHODS:               # policy gate
            tracer.record_api(op["method"], op["path"], blocked=True)
            collected.append({"op": op["operation_id"], "status": "APPROVAL_REQUIRED",
                              "preview": step.get("params", {})})
            continue
        tracer.record_api(op["method"], op["path"])      # executed via mrapids
        collected.append({"op": op["operation_id"],
                          "result": _run_mrapids(op["operation_id"], step.get("params"))})

    summ = [{"role": "user", "content":
             f"User request: {task.prompt}\n\nResults from mrapids:\n{to_json(collected)}\n\n"
             "Write the final answer for the user. If any step required approval, say so and "
             "do NOT claim it was executed."}]
    sresp = llm_stream(summ, tracer, system=SYSTEM_PROMPT, max_tokens=700, label="summary")
    res.final_text = "".join(b.text for b in sresp.content if b.type == "text")
    return res
