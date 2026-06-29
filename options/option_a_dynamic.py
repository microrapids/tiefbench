"""Option A — Agent writes Python / calls APIs dynamically (the benchmark).

Max flexibility, max risk. The model writes a script that talks to the API only
through the gateway shim (which counts calls and blocks writes). We run it in a
constrained subprocess with a timeout, then summarize its output.
"""
from __future__ import annotations
import os, re, sys, tempfile, subprocess
from core import RunResult, Tracer, llm_call, llm_stream, timed, BASE_URL, SYSTEM_PROMPT

NAME = "A:dynamic-python"
HERE = os.path.dirname(os.path.abspath(__file__))

CODER = (
    "You write a short Python 3 script to satisfy the user's request against the "
    "TiefStocks API. You MUST use the provided gateway module — no other network "
    "access:\n"
    "    from sandbox_shim import get, post\n"
    "    get(path, params=None)   # e.g. get('/api/portfolio')\n"
    "    post(path, body)         # writes are gated by the gateway\n"
    "Useful endpoints: /api/portfolio, /api/search?q=, /api/stocks/{t}, "
    "/api/thesis/{t}, /api/intelligence/analyze/{t}, /api/intelligence/challenge/{t}, "
    "/api/portfolio/transactions (POST to add). "
    "print() the data you gather. Do NOT attempt to bypass the gateway. "
    "Return ONLY the code in a ```python fenced block."
)


@timed
def run(task) -> RunResult:
    tracer = Tracer()
    res = RunResult(option=NAME, task_id=task.id, tracer=tracer)

    cresp = llm_call([{"role": "user", "content": f"{CODER}\n\nUser request: {task.prompt}"}],
                     tracer, system="You are a careful Python developer.", max_tokens=900,
                     label="codegen")
    raw = "".join(b.text for b in cresp.content if b.type == "text")
    m = re.search(r"```(?:python)?\s*(.*?)```", raw, re.S)
    code = (m.group(1) if m else raw).strip()

    with tempfile.TemporaryDirectory() as d:
        trace_file = os.path.join(d, "trace.log")
        open(trace_file, "w").close()
        script = os.path.join(d, "agent_code.py")
        with open(script, "w") as f:
            f.write(code)
        env = {"PATH": os.environ.get("PATH", ""), "SANDBOX_BASE_URL": BASE_URL,
               "SANDBOX_TRACE_FILE": trace_file, "PYTHONPATH": HERE}
        try:
            proc = subprocess.run([sys.executable, script], capture_output=True, text=True,
                                  timeout=40, env=env, cwd=d)
            output = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else ""))[:6000]
        except subprocess.TimeoutExpired:
            output = "(sandbox timed out)"
        for line in open(trace_file):
            line = line.strip()
            if line.startswith("BLOCKED"):
                method, path = line.replace("BLOCKED ", "").split(" ", 1)
                tracer.record_api(method, path, blocked=True)
            elif line:
                method, path = line.split(" ", 1)
                tracer.record_api(method, path)

    summ = [{"role": "user", "content":
             f"User request: {task.prompt}\n\nScript output:\n{output}\n\n"
             "Write the final answer for the user. If a write was gated, say it needs approval; "
             "do not claim it executed."}]
    sresp = llm_stream(summ, tracer, system=SYSTEM_PROMPT, max_tokens=700, label="summary")
    res.final_text = "".join(b.text for b in sresp.content if b.type == "text")
    return res
