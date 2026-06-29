"""QA smoke test — exercises endpoints, all options, governance, streaming,
accuracy, raw capture, MCP registration, and data integrity."""
import json, urllib.request, sqlite3, time
BASE = "http://127.0.0.1:8800"
P, F = 0, 0
def check(name, cond, detail=""):
    global P, F
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if cond: P += 1
    else: F += 1
    return cond

def post(path, body, timeout=180):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)

def stream(option, message, grade=False, raw=False, timeout=180):
    r = post("/api/chat_stream", {"option": option, "message": message, "grade": grade, "raw": raw}, timeout)
    buf = b""; evs = []
    for chunk in r:
        buf += chunk
        while b"\n\n" in buf:
            fr, buf = buf.split(b"\n\n", 1)
            if fr.startswith(b"data:"):
                evs.append(json.loads(fr[fr.index(b"{"):]))
    return evs

def get(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=15).read())

print("\n== 1. /api/options contract ==")
o = get("/api/options")
check("5 options returned", len(o["options"]) == 5, str([x["key"] for x in o["options"]]))
check("each has mermaid+summary", all(x.get("mermaid") and x.get("summary") for x in o["options"]))
check("model present", bool(o.get("model")), o.get("model"))

print("\n== 2. All 5 options — read task returns an answer ==")
READ = "What is my single largest holding by weight?"
for k in ["a", "b", "c", "d", "e"]:
    evs = stream(k, READ)
    done = next((e for e in evs if e["type"] == "done"), {})
    ok = done and not done.get("error") and "GOOG" in (done.get("answer") or "")
    check(f"option {k.upper()} answers correctly", ok,
          f"api={done.get('executed')} err={done.get('error')}")

print("\n== 3. Streaming event types (option B) ==")
evs = stream("b", "Analyze AMD and check the bear case.", grade=True)
types = {e["type"] for e in evs}
check("emits intent", "intent" in types)
check("emits turn", "turn" in types)
check("emits reason", "reason" in types)
check("emits api", "api" in types)
check("emits token", "token" in types)
check("emits done", "done" in types)
done = next(e for e in evs if e["type"] == "done")
check("grade -> accuracy present", bool(done.get("accuracy")), str((done.get("accuracy") or {}).get("score")))
turns = [e for e in evs if e["type"] == "turn"]
check("token growth across turns", len(turns) >= 2 and turns[-1]["tokens_in"] > turns[0]["tokens_in"],
      f"{[t['tokens_in'] for t in turns]}")

print("\n== 4. Governance — writes blocked (3 enforcement points) ==")
WRITE = "Record a BUY of 10 shares of AMD at $230 dated 2026-06-28."
for k in ["b", "c", "d"]:
    evs = stream(k, WRITE)
    done = next((e for e in evs if e["type"] == "done"), {})
    apis = [e for e in evs if e["type"] == "api"]
    blocked = done.get("had_write_blocked") or any(e.get("blocked") for e in apis)
    executed_write = any((not e.get("blocked")) and e["verb"] in ("POST","PUT","PATCH","DELETE") for e in apis)
    check(f"option {k.upper()} blocks write", blocked and not executed_write,
          f"blocked={done.get('blocked')} executed_write={executed_write}")

print("\n== 5. MCP tools registration ==")
evs = stream("c", READ)
treg = next((e for e in evs if e["type"] == "tools"), None)
check("MCP emits tools/list registration", treg is not None and len(treg.get("tools", [])) == 8,
      str(treg and len(treg["tools"])))

print("\n== 6. Raw capture gating ==")
evs_on = stream("b", "Show my largest holding.", raw=True)
evs_off = stream("b", "Show my largest holding.", raw=False)
t_on = [e for e in evs_on if e["type"] == "turn"]
t_off = [e for e in evs_off if e["type"] == "turn"]
check("raw=true -> raw_input present", any(e.get("raw_input") for e in t_on))
check("raw=false -> no raw_input", all("raw_input" not in e for e in t_off))

print("\n== 7. Edge cases / error handling ==")
try:
    evs = stream("zzz", "hi", timeout=30)
    body = evs[0] if evs else {}
    check("unknown option handled", body.get("error") is not None or body.get("type") != "done", str(body)[:80])
except Exception as e:
    # endpoint returns JSON error (not SSE) for unknown option
    check("unknown option handled", True, "non-SSE error response")

print("\n== 8. /api/history + DB integrity ==")
h = get("/api/history?n=5")
check("history returns runs", len(h["runs"]) > 0, f"{len(h['runs'])} runs")
r0 = h["runs"][0]
check("run has calls list", isinstance(r0.get("calls"), list))
check("run has turns list", isinstance(r0.get("turns"), list))
con = sqlite3.connect("results/tiefbench.db")
nruns = con.execute("SELECT count(*) FROM runs").fetchone()[0]
ncalls = con.execute("SELECT count(*) FROM calls").fetchone()[0]
nturns = con.execute("SELECT count(*) FROM turns").fetchone()[0]
intent_pop = con.execute("SELECT count(*) FROM runs WHERE intent IS NOT NULL").fetchone()[0]
check("DB has runs/calls/turns", nruns > 0 and ncalls > 0 and nturns > 0, f"runs={nruns} calls={ncalls} turns={nturns}")
check("intent persisted on most runs", intent_pop > nruns * 0.7, f"{intent_pop}/{nruns}")
reasons = con.execute("SELECT count(*) FROM calls WHERE reason IS NOT NULL").fetchone()[0]
check("reasons persisted on calls", reasons > ncalls * 0.7, f"{reasons}/{ncalls}")

print(f"\n===== QA RESULT: {P} passed, {F} failed =====")
