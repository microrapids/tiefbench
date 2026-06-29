"""SQLite cache DB for bake-off runs — persists intent + per-call reasons.

One row per run in `runs`, one row per API call in `calls` (with its reason).
Used by both the CLI (run_demo.py) and the web app. Zero external deps.

Inspect from the shell:
    python store.py                 # recent runs
    sqlite3 results/tiefbench.db "SELECT option,action_type,intent FROM runs"
"""
from __future__ import annotations
import os, json, sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "tiefbench.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT DEFAULT CURRENT_TIMESTAMP,
  source TEXT, option TEXT, task_id TEXT, prompt TEXT,
  intent TEXT, action_type TEXT, entities TEXT, plan TEXT,
  answer TEXT, success INTEGER, right_calls INTEGER, answered INTEGER, safe INTEGER,
  acc_score INTEGER, acc_verdict TEXT,
  executed INTEGER, blocked INTEGER, llm_calls INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, latency_s REAL,
  model TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, seq INTEGER, verb TEXT, path TEXT, blocked INTEGER, reason TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, seq INTEGER, label TEXT, input TEXT, text TEXT, tools TEXT,
  stop_reason TEXT, tokens_in INTEGER, tokens_out INTEGER,
  raw_input TEXT, raw_output TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
"""

# Add columns to pre-existing DBs (CREATE TABLE IF NOT EXISTS won't alter them).
_MIGRATIONS = [
    "ALTER TABLE turns ADD COLUMN raw_input TEXT",
    "ALTER TABLE turns ADD COLUMN raw_output TEXT",
]

_RUN_COLS = ["source", "option", "task_id", "prompt", "intent", "action_type",
             "entities", "plan", "answer", "success", "right_calls", "answered",
             "safe", "acc_score", "acc_verdict", "executed", "blocked", "llm_calls",
             "tokens_in", "tokens_out", "cost_usd", "latency_s", "model", "error"]


def _conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    return c


def extract(events: list, intent: dict | None):
    """Turn the event stream into (intent, [calls-with-reason], [turns])."""
    if intent is None:
        intent = next((e for e in events if e.get("type") == "intent"), None)
    calls, turns, pending = [], [], None
    for e in events:
        if e.get("type") == "reason":
            pending = e.get("reason")
        elif e.get("type") == "api":
            calls.append({"verb": e["verb"], "path": e["path"],
                          "blocked": bool(e["blocked"]), "reason": pending})
            pending = None
        elif e.get("type") == "turn":
            turns.append({"label": e.get("label"), "input": e.get("input"),
                          "text": e.get("text"), "tools": e.get("tools", []),
                          "stop_reason": e.get("stop_reason"),
                          "tokens_in": e.get("tokens_in"), "tokens_out": e.get("tokens_out"),
                          "raw_input": e.get("raw_input"), "raw_output": e.get("raw_output")})
    return intent, calls, turns


def save_run(record: dict, calls: list, intent: dict | None = None, turns: list | None = None) -> int:
    """Persist one run + its calls + its loop turns. Returns the run id."""
    rec = dict(record)
    if intent:
        rec.setdefault("intent", intent.get("intent"))
        rec.setdefault("action_type", intent.get("action_type"))
        rec.setdefault("entities", json.dumps(intent.get("entities", [])))
        rec.setdefault("plan", json.dumps(intent.get("plan", [])))
    with _conn() as c:
        cur = c.execute(
            f"INSERT INTO runs({','.join(_RUN_COLS)}) VALUES ({','.join('?' * len(_RUN_COLS))})",
            [rec.get(k) for k in _RUN_COLS])
        rid = cur.lastrowid
        for i, call in enumerate(calls or []):
            c.execute("INSERT INTO calls(run_id,seq,verb,path,blocked,reason) VALUES (?,?,?,?,?,?)",
                      (rid, i, call["verb"], call["path"], int(bool(call["blocked"])), call.get("reason")))
        for i, t in enumerate(turns or []):
            c.execute("INSERT INTO turns(run_id,seq,label,input,text,tools,stop_reason,tokens_in,"
                      "tokens_out,raw_input,raw_output) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (rid, i, t.get("label"), t.get("input"), t.get("text"),
                       json.dumps(t.get("tools", [])), t.get("stop_reason"),
                       t.get("tokens_in"), t.get("tokens_out"),
                       t.get("raw_input"), t.get("raw_output")))
    return rid


def recent(n: int = 20) -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        runs = [dict(r) for r in c.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()]
        for r in runs:
            r["calls"] = [dict(x) for x in c.execute(
                "SELECT verb,path,blocked,reason FROM calls WHERE run_id=? ORDER BY seq", (r["id"],)).fetchall()]
            r["turns"] = []
            for x in c.execute("SELECT label,input,text,tools,stop_reason,tokens_in,tokens_out,"
                               "raw_input,raw_output FROM turns WHERE run_id=? ORDER BY seq",
                               (r["id"],)).fetchall():
                t = dict(x)
                try:
                    t["tools"] = json.loads(t["tools"] or "[]")
                except Exception:
                    t["tools"] = []
                r["turns"].append(t)
        return runs


def calibration(model: str | None = None) -> dict:
    """Per-option observed stats from real runs — used to calibrate the Fit Advisor."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        q = ("SELECT option,success,acc_score,cost_usd,latency_s,tokens_in,tokens_out,executed "
             "FROM runs WHERE error IS NULL")
        args = []
        if model:
            q += " AND model=?"; args.append(model)
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    groups: dict = {}
    for r in rows:
        k = (r["option"] or "?")[0].lower()
        groups.setdefault(k, []).append(r)
    out = {}
    for k, xs in groups.items():
        accs = [x["acc_score"] for x in xs if x["acc_score"] is not None]
        sx = [x for x in xs if x["success"] is not None]
        def avg(key, _xs=xs):
            vals = [x[key] for x in _xs if x[key] is not None]
            return (sum(vals) / len(vals)) if vals else None
        out[k] = {
            "runs": len(xs),
            "acc": round(sum(accs) / len(accs), 1) if accs else None,
            "success_rate": round(100 * sum(1 for x in sx if x["success"]) / len(sx)) if sx else None,
            "cost": round(avg("cost_usd"), 4) if avg("cost_usd") is not None else None,
            "latency": round(avg("latency_s") or 0, 1),
            "tok_in": round(avg("tokens_in") or 0), "tok_out": round(avg("tokens_out") or 0),
            "api": round(avg("executed") or 0, 1),
        }
    out["_meta"] = {"total": len(rows), "model": model}
    return out


if __name__ == "__main__":
    for r in recent(15):
        print(f"#{r['id']} [{r['ts']}] {r['source']}/{r['option']} {r['task_id'] or ''} "
              f"· {r['action_type']} · ${r['cost_usd']} · acc={r['acc_score']}")
        print(f"   intent: {r['intent']}")
        for c in r["calls"]:
            tag = "BLOCKED " if c["blocked"] else ""
            print(f"     {tag}{c['verb']} {c['path']}  ↳ {c['reason']}")
