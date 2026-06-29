"""Gateway shim injected into the Option A sandbox.

The agent's generated code may ONLY reach the API through this module. It records
every call to a trace file and enforces the write policy (the 'API gateway' from
the design doc). Generated code runs with this as its only network surface.
"""
import os, json, urllib.request, urllib.parse

BASE = os.environ["SANDBOX_BASE_URL"]
TRACE = os.environ["SANDBOX_TRACE_FILE"]


def _log(line):
    with open(TRACE, "a") as f:
        f.write(line + "\n")


def get(path, params=None):
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    _log("GET " + path.split("?")[0])
    return json.loads(urllib.request.urlopen(BASE + path, timeout=15).read())


def post(path, body=None):
    # Policy gate: writes never execute, even from dynamic code.
    _log("BLOCKED POST " + path)
    return {"status": "APPROVAL_REQUIRED",
            "message": "Write blocked by gateway policy; human approval required.",
            "preview": body}
