"""Web chat backend for the TiefBench.

Serves a chat UI and a /api/chat endpoint that runs ANY option on a free-text
prompt, returning the answer plus live metrics (trace, tokens, cost, latency,
safety). Reuses the exact option modules from the bake-off.

Run from the tiefbench dir:
    .venv/bin/python -m uvicorn webapp.server:app --port 8800
"""
from __future__ import annotations
import os, sys, importlib, json, queue, threading
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the bake-off package importable regardless of launch cwd.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)  # option_c (mcp) / option_d (mrapids) expect this cwd

from tasks import Task                       # noqa: E402
import core                                  # noqa: E402
import grader                                # noqa: E402

OPTIONS = {
    "a": ("options.option_a_dynamic", "Dynamic Python"),
    "b": ("options.option_b_funcs", "Function calling"),
    "c": ("options.option_c_mcp", "MCP"),
    "d": ("options.option_d_mrapids", "mrapids"),
    "e": ("options.option_e_orchestrator", "Orchestrator"),
}

# Demo metadata: how to explain each option + a live-rendered workflow diagram.
OPTION_META = {
    "a": {
        "icon": "🐍", "tagline": "LLM writes & runs code (the benchmark)",
        "summary": "Claude writes a Python script that talks to the API only through a "
                   "gateway shim. We run it in a locked-down subprocess, then summarize the output.",
        "reaches": "Generated code → gateway shim → HTTP. Max flexibility, max risk.",
        "strengths": ["Any logic the model can imagine", "No predefined tools"],
        "watch": ["Needs real sandboxing", "Higher variance / harder to audit"],
        "mermaid": ("flowchart LR\n"
                    "U([User]) --> M[Claude<br/>writes Python]\n"
                    "M --> SB[/Sandbox<br/>subprocess/]\n"
                    "SB --> G{{Gateway shim}}\n"
                    "G -->|GET| API[(TiefStocks API)]\n"
                    "G -.->|POST blocked| X[/Approval required/]\n"
                    "SB --> S[Claude<br/>summarizes] --> U"),
    },
    "b": {
        "icon": "🧩", "tagline": "Curated tools + tool-use loop (baseline)",
        "summary": "A small set of hand-written tools is given to Claude. It picks tools, "
                   "we run them, results go back, repeat until it answers.",
        "reaches": "Model selects a curated tool → dispatch() → HTTP.",
        "strengths": ["Simplest & most predictable", "Small, auditable surface"],
        "watch": ["Every tool schema is always in context", "Sprawl hurts at scale"],
        "mermaid": ("flowchart LR\n"
                    "U([User]) --> M[Claude]\n"
                    "M -->|tool_use| D{{dispatch + policy}}\n"
                    "D -->|read| API[(TiefStocks API)]\n"
                    "D -.->|write| X[/Approval required/]\n"
                    "API --> D --> M --> U"),
    },
    "c": {
        "icon": "🔌", "tagline": "MCP server, tools discovered at runtime",
        "summary": "A real MCP server exposes the capabilities. The client discovers them "
                   "at runtime (tools/list) and drives Claude with them. Policy lives server-side.",
        "reaches": "Claude → MCP client → call_tool → MCP server → HTTP.",
        "strengths": ["Runtime discovery", "Reusable across many clients"],
        "watch": ["A server to run & secure", "Schema tokens grow with scale"],
        "mermaid": ("flowchart LR\n"
                    "U([User]) --> M[Claude]\n"
                    "M -->|call_tool| CL[MCP client]\n"
                    "CL <-->|tools/list| SV[MCP server]\n"
                    "SV -->|read| API[(TiefStocks API)]\n"
                    "SV -.->|write| X[/Approval required/]\n"
                    "M --> U"),
    },
    "d": {
        "icon": "⚡", "tagline": "Agent plans OpenAPI ops → mrapids runs them",
        "summary": "The agent never writes HTTP. It plans which OpenAPI operations to run; "
                   "the mrapids CLI executes them deterministically with JSON output.",
        "reaches": "Claude plans ops → mrapids run → HTTP. Deterministic & auditable.",
        "strengths": ["Deterministic execution", "Agent picks workflows, not endpoints"],
        "watch": ["Needs the CLI + spec", "Plan + summarize = 2 LLM calls"],
        "mermaid": ("flowchart LR\n"
                    "U([User]) --> P[Claude<br/>plans operations]\n"
                    "P --> R{{Runner}}\n"
                    "R -->|mrapids run| MR[mrapids CLI]\n"
                    "MR --> API[(TiefStocks API)]\n"
                    "R -.->|write method| X[/Approval required/]\n"
                    "R --> S[Claude<br/>summarizes] --> U"),
    },
    "e": {
        "icon": "🧭", "tagline": "Router → least-privilege scoped sub-agent",
        "summary": "A planner classifies intent and picks a least-privilege tool scope. A "
                   "sub-agent then runs with ONLY that scope visible. The user sees an outcome.",
        "reaches": "Router picks scope → scoped sub-agent → policy → HTTP.",
        "strengths": ["Least-privilege by construction", "Scales via routing"],
        "watch": ["Extra router hop", "Mis-route → wrong scope"],
        "mermaid": ("flowchart LR\n"
                    "U([User]) --> RT[Router LLM]\n"
                    "RT --> SC{Scope?}\n"
                    "SC --> SUB[Scoped<br/>sub-agent]\n"
                    "SUB -->|policy| API[(TiefStocks API)]\n"
                    "SUB -.->|write| X[/Approval required/]\n"
                    "SUB --> U"),
    },
}
_mods = {}
def _mod(key):
    if key not in _mods:
        _mods[key] = importlib.import_module(OPTIONS[key][0])
    return _mods[key]

app = FastAPI(title="TiefBench Chat")


class ChatReq(BaseModel):
    option: str = "b"
    message: str
    grade: bool = False
    raw: bool = False
    model: str | None = None      # per-request model override; blank = env default


@app.get("/api/options")
def options():
    return {"options": [{"key": k, "label": v[1], **OPTION_META.get(k, {})}
                        for k, v in OPTIONS.items()],
            "model": core.MODEL, "api": core.BASE_URL}


def _payload(req: ChatReq, res, mod, key) -> dict:
    blocked = [c for c in res.tracer.api_calls if c.startswith("BLOCKED")]
    executed = [c for c in res.tracer.api_calls if not c.startswith("BLOCKED")]
    accuracy = None
    if req.grade and res.final_text and not res.error:
        try:
            accuracy = grader.judge(req.message, res.final_text)
        except Exception as e:  # noqa: BLE001
            accuracy = {"score": None, "verdict": "ungraded", "issues": [str(e)[:120]]}
    return {
        "type": "done",
        "option": getattr(mod, "NAME", key),
        "answer": res.final_text or "(no answer)",
        "error": res.error,
        "trace": res.tracer.api_calls,
        "executed": len(executed),
        "blocked": len(blocked),
        "had_write_blocked": bool(blocked),
        "llm_calls": res.tracer.llm_calls,
        "tokens_in": res.tracer.tokens_in,
        "tokens_out": res.tracer.tokens_out,
        "cost_usd": round(res.cost_usd(), 5),
        "latency_s": round(res.latency_ms / 1000, 2),
        "model": res.tracer.model or req.model or core.MODEL,
        "accuracy": accuracy,
    }


class FitReq(BaseModel):
    pack: dict | None = None        # a TiefWise pack/manifest; null = built-in pack
    simulate_n: int | None = None   # what-if tool count


@app.post("/api/fit")
def api_fit(req: FitReq):
    import fit
    import tools as T
    norm = fit.normalize(req.pack) if req.pack else fit.from_builtin(T.TOOLS, T.WRITE_TOOLS)
    if not norm:
        return {"error": "no tools found in pack"}
    # calibrate from real runs only for the built-in pack (that's what was measured)
    calib = None
    if not req.pack:
        try:
            import store
            calib = store.calibration(core.MODEL)
        except Exception:
            calib = None
    return fit.analyze(norm, simulate_n=req.simulate_n, calib=calib)


@app.get("/advisor")
def advisor_page():
    return FileResponse(os.path.join(STATIC, "advisor.html"))


class TuneReq(BaseModel):
    pack: dict | None = None
    scenarios: list = []          # list of strings or {text, expected}
    edits: dict | None = None     # {tool_name: new_description} for retest
    samples: int = 1              # N-sample each scenario for stability


def _tune_tools(req):
    import fit
    import tools as T
    return fit.normalize(req.pack) if req.pack else fit.from_builtin(T.TOOLS, T.WRITE_TOOLS)


@app.post("/api/tune/analyze")
def tune_analyze(req: TuneReq):
    import tune
    norm = _tune_tools(req)
    if not norm:
        return {"error": "no tools in pack"}
    if not req.scenarios:
        return {"error": "no scenarios provided"}
    return tune.analyze(norm, req.scenarios, samples=max(1,min(req.samples,9)))


@app.post("/api/tune/retest")
def tune_retest(req: TuneReq):
    import tune
    norm = _tune_tools(req)
    return tune.retest(norm, req.edits or {}, req.scenarios, samples=max(1,min(req.samples,9)))


@app.get("/tune")
def tune_page():
    return FileResponse(os.path.join(STATIC, "tune.html"))


class ResultsTuneReq(BaseModel):
    scenario: str = ""
    projections: dict | None = None    # {tool: [keep field paths]}
    model: str | None = None
    api_key: str | None = None


@app.post("/api/tune/results/analyze")
def tune_results_analyze(req: ResultsTuneReq):
    import tune_results
    if not req.scenario.strip():
        return {"error": "no scenario provided"}
    core.set_model(req.model)
    return tune_results.analyze(req.scenario, model=req.model, api_key=req.api_key or None)


@app.post("/api/tune/results/retest")
def tune_results_retest(req: ResultsTuneReq):
    import tune_results
    core.set_model(req.model)
    return tune_results.retest(req.scenario, req.projections or {}, model=req.model, api_key=req.api_key or None)


class EvalReq(BaseModel):
    pack: dict | None = None
    scenarios: list = []
    models: list = []                # model ids to compare
    reference: str | None = None     # baseline model id
    api_key: str | None = None       # optional; blank = server env key (transient, never stored)
    samples: int = 1                 # N-sample each (model, scenario) for stability


@app.get("/api/eval/models")
def eval_models():
    import modeleval
    return {"models": modeleval.AVAILABLE, "default_model": core.MODEL}


@app.post("/api/eval")
def api_eval(req: EvalReq):
    import modeleval
    norm = _tune_tools(req)
    if not norm:
        return {"error": "no tools in pack"}
    if not req.scenarios:
        return {"error": "no scenarios provided"}
    models = req.models or [core.MODEL]
    reference = req.reference or models[0]
    return modeleval.evaluate(norm, req.scenarios, models, reference, api_key=req.api_key or None, samples=max(1,min(req.samples,9)))


@app.get("/eval")
def eval_page():
    return FileResponse(os.path.join(STATIC, "model-eval.html"))


class ValidateReq(BaseModel):
    models: list = []
    probe_samples: int = 3
    exec_samples: int = 1
    api_key: str | None = None
    scenarios: list | None = None     # optional override; default = built-in tagged set


@app.post("/api/validate")
def api_validate(req: ValidateReq):
    import validate as V
    models = req.models or [core.MODEL]
    return V.validate(scenarios=req.scenarios, models=models,
                      probe_samples=max(1, min(req.probe_samples, 7)),
                      exec_samples=max(1, min(req.exec_samples, 3)),
                      api_key=req.api_key or None)


@app.get("/api/validate/scenarios")
def validate_scenarios():
    import validate as V
    return {"scenarios": [{"text": t, "tag": g} for t, g in V.SCENARIOS]}


@app.get("/validate")
def validate_page():
    return FileResponse(os.path.join(STATIC, "validate.html"))


@app.get("/api/history")
def history(n: int = 25):
    try:
        import store
        return {"runs": store.recent(n)}
    except Exception as e:  # noqa: BLE001
        return {"runs": [], "error": str(e)}


@app.post("/api/chat")
def chat(req: ChatReq):
    key = req.option.lower()
    if key not in OPTIONS:
        return {"error": f"unknown option '{key}'"}
    mod = _mod(key)
    core.set_model(req.model)
    res = mod.run(Task(id="chat", level="chat", prompt=req.message))
    return _payload(req, res, mod, key)


@app.post("/api/chat_stream")
def chat_stream(req: ChatReq):
    """Server-Sent Events: live token + api-call events, then a final 'done'."""
    key = req.option.lower()
    if key not in OPTIONS:
        return {"error": f"unknown option '{key}'"}
    mod = _mod(key)
    q: queue.Queue = queue.Queue()
    DONE = object()
    holder: dict = {}

    events: list = []

    def worker():
        core.set_emitter(lambda ev: (events.append(ev), q.put(ev)))   # tee: stream + collect
        core.set_capture_raw(req.raw)
        core.set_model(req.model)
        try:
            try:
                import intent
                idata = {"type": "intent", **intent.capture(req.message)}
                events.append(idata)          # also collect for persistence
                q.put(idata)
            except Exception:
                pass
            holder["res"] = mod.run(Task(id="chat", level="chat", prompt=req.message))
        finally:
            q.put(DONE)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is DONE:
                break
            yield f"data: {json.dumps(ev)}\n\n"
        payload = _payload(req, holder["res"], mod, key)
        _persist(req, payload, events)
        yield f"data: {json.dumps(payload)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _persist(req, payload, events):
    try:
        import store
        intent, calls, turns = store.extract(events, None)
        a = payload.get("accuracy") or {}
        store.save_run({
            "source": "web", "option": payload["option"], "task_id": None,
            "prompt": req.message, "answer": payload["answer"], "error": payload["error"],
            "acc_score": a.get("score"), "acc_verdict": a.get("verdict"),
            "executed": payload["executed"], "blocked": payload["blocked"],
            "llm_calls": payload["llm_calls"], "tokens_in": payload["tokens_in"],
            "tokens_out": payload["tokens_out"], "cost_usd": payload["cost_usd"],
            "latency_s": payload["latency_s"], "model": payload["model"],
        }, calls, intent, turns)
    except Exception:
        pass


STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
