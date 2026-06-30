"""Shared core: config, tracing, LLM wrapper, result types, pricing.

Every option imports from here so the scorecard is apples-to-apples.
"""
from __future__ import annotations
import os, time, json, contextvars
from dataclasses import dataclass, field

# ---- Event bus (for live streaming to the web UI) ---------------------------
# An option run can emit progress events (api calls, text tokens). The web SSE
# endpoint installs an emitter for the duration of a run; everywhere else it's a
# no-op, so the CLI is unaffected.
_emitter = contextvars.ContextVar("emitter", default=None)

def set_emitter(fn):
    _emitter.set(fn)

# When on, capture_turn attaches the full raw prompt + response to each turn.
_capture_raw = contextvars.ContextVar("capture_raw", default=False)

def set_capture_raw(v):
    _capture_raw.set(bool(v))

# Per-request model override (e.g. the chat's model picker); blank -> env default.
_model = contextvars.ContextVar("model", default=None)

def set_model(m):
    _model.set(m or None)

def current_model():
    return _model.get() or MODEL

def emit(event: dict):
    fn = _emitter.get()
    if fn:
        try:
            fn(event)
        except Exception:
            pass

# ---- Config -----------------------------------------------------------------
BASE_URL = os.environ.get("TIEFSTOCKS_URL", "http://127.0.0.1:8001")
MODEL = os.environ.get("TIEFBENCH_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = 1500

# Approximate list prices, USD per 1M tokens (input, output). Override via env.
PRICES = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

SYSTEM_PROMPT = (
    "You are a portfolio operations assistant for the TiefStocks API. "
    "Use the available capabilities to answer the user's request accurately. "
    "GOVERNANCE: writes/mutations (adding a transaction, recording a decision, "
    "changing a thesis) are HIGH RISK. To propose a write, CALL the corresponding "
    "write tool with the proposed values — the system will NOT execute it; it "
    "returns an APPROVAL_REQUIRED preview which you must relay to the user. Never "
    "claim a write succeeded. Read-only actions may be performed freely."
)

# ---- Tracing ----------------------------------------------------------------
@dataclass
class Tracer:
    """Accumulates LLM + API usage for a single task run."""
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    api_calls: list[str] = field(default_factory=list)   # e.g. "GET /api/portfolio"
    model: str | None = None                             # model actually used (for pricing)

    def record_llm(self, usage) -> None:
        self.llm_calls += 1
        self.tokens_in += getattr(usage, "input_tokens", 0) or 0
        self.tokens_out += getattr(usage, "output_tokens", 0) or 0
        if self.model is None:
            self.model = current_model()

    def record_api(self, method: str, path: str, *, blocked: bool = False) -> None:
        prefix = "BLOCKED " if blocked else ""
        self.api_calls.append(f"{prefix}{method} {path}")
        emit({"type": "api", "verb": method, "path": path, "blocked": blocked})


@dataclass
class RunResult:
    option: str
    task_id: str
    final_text: str = ""
    tracer: Tracer = field(default_factory=Tracer)
    latency_ms: float = 0.0
    error: str | None = None

    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.tracer.model or MODEL, (3.0, 15.0))
        return self.tracer.tokens_in / 1e6 * pin + self.tracer.tokens_out / 1e6 * pout


# ---- LLM wrapper ------------------------------------------------------------
_client = None
def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def client_for(api_key: str | None = None):
    """Client for the given key. A user-supplied key is used transiently (fresh
    client, never cached/stored/logged); blank falls back to the env key."""
    if api_key:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    return _anthropic()


def _jsonable(o):
    """Serialize Anthropic content blocks (pydantic) and anything else for raw capture."""
    try:
        return o.model_dump()
    except Exception:
        return str(o)


def capture_turn(messages, resp, label=None, system=None):
    """Emit a 'turn' event capturing this loop iteration: what triggered it, the
    model's text, and which tools it decided to call. Used to reconstruct the
    full agent loop (prompts -> decisions -> final output). When raw capture is
    on, also attach the full prompt (system + messages) and raw response."""
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    tools = [{"name": b.name, "args": {k: v for k, v in (b.input or {}).items() if k != "reason"}}
             for b in resp.content if getattr(b, "type", "") == "tool_use"]
    last = messages[-1] if messages else None
    if last and last.get("role") == "user" and isinstance(last.get("content"), str):
        inp = last["content"]
    elif last and last.get("role") == "user" and isinstance(last.get("content"), list):
        inp = "(tool results from previous step)"
    else:
        inp = ""
    ev = {"type": "turn", "label": label, "input": inp[:600], "text": text[:600],
          "tools": tools, "stop_reason": getattr(resp, "stop_reason", None),
          "tokens_in": resp.usage.input_tokens, "tokens_out": resp.usage.output_tokens}
    if _capture_raw.get():
        try:
            ev["raw_input"] = json.dumps({"system": system, "messages": messages},
                                         default=_jsonable, indent=2)[:40000]
        except Exception as e:  # noqa: BLE001
            ev["raw_input"] = f"(serialize error: {e})"
        try:
            ev["raw_output"] = json.dumps(resp.model_dump(), default=_jsonable, indent=2)[:40000]
        except Exception as e:  # noqa: BLE001
            ev["raw_output"] = f"(serialize error: {e})"
    emit(ev)


def llm_call(messages, tracer: Tracer, *, system=SYSTEM_PROMPT, tools=None,
             max_tokens=MAX_TOKENS, label=None):
    """Single Claude call. Records usage. Returns the raw message response."""
    kwargs = dict(model=current_model(), max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    resp = _anthropic().messages.create(**kwargs)
    tracer.record_llm(resp.usage)
    capture_turn(messages, resp, label, system)
    return resp


def llm_stream(messages, tracer: Tracer, *, system=SYSTEM_PROMPT, tools=None,
               max_tokens=MAX_TOKENS, label=None):
    """Streaming Claude call. Emits {'type':'token'} deltas as text arrives, then
    records usage and returns the final message (with any tool_use blocks)."""
    kwargs = dict(model=current_model(), max_tokens=max_tokens, system=system, messages=messages)
    if tools:
        kwargs["tools"] = tools
    with _anthropic().messages.stream(**kwargs) as stream:
        for delta in stream.text_stream:
            emit({"type": "token", "text": delta})
        final = stream.get_final_message()
    tracer.record_llm(final.usage)
    capture_turn(messages, final, label, system)
    return final


def timed(fn):
    """Wrap an option's run(task) -> RunResult, filling latency + catching errors."""
    def wrapper(task):
        t0 = time.time()
        try:
            res = fn(task)
        except Exception as e:  # noqa: BLE001 - surface any failure into the scorecard
            res = RunResult(option=fn.__module__, task_id=task.id, error=repr(e))
        res.latency_ms = (time.time() - t0) * 1000
        return res
    return wrapper
