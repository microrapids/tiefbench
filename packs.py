"""Pack workspace — import and manage tool sets (collections) to test.

A Pack = a curated tool set imported from a TiefWise MCP config. The user keeps a
library of packs and selects an ACTIVE one; the schema-based features (Fit Advisor,
Tune descriptions, Model Eval) all run against the active pack. Local, single-user,
file-backed — matches the desktop-first direction.

TiefStocks is the always-present default sample pack ("builtin").
"""
from __future__ import annotations
import os, json, time, hashlib
import fit
import tools as T

PACKS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
ACTIVE_FILE = os.path.join(PACKS_DIR, "_active.txt")


def _ensure():
    os.makedirs(PACKS_DIR, exist_ok=True)


def _builtin():
    return {"id": "builtin", "name": "TiefStocks (sample)", "builtin": True,
            "tools": fit.from_builtin(T.TOOLS, T.WRITE_TOOLS)}


def _path(pid):
    return os.path.join(PACKS_DIR, pid + ".json")


def _all() -> dict:
    _ensure()
    out = {"builtin": _builtin()}
    for f in os.listdir(PACKS_DIR):
        if f.endswith(".json") and not f.startswith("_"):
            try:
                p = json.load(open(os.path.join(PACKS_DIR, f)))
                out[p["id"]] = p
            except Exception:
                pass
    return out


def get_pack(pid):
    return _all().get(pid)


def get_active_id() -> str:
    _ensure()
    if os.path.exists(ACTIVE_FILE):
        a = open(ACTIVE_FILE).read().strip()
        if a and (a == "builtin" or os.path.exists(_path(a))):
            return a
    return "builtin"


def set_active(pid):
    _ensure()
    open(ACTIVE_FILE, "w").write(pid or "builtin")


def active_tools():
    return (get_pack(get_active_id()) or _builtin())["tools"]


def active_name():
    return (get_pack(get_active_id()) or _builtin())["name"]


def list_packs() -> list[dict]:
    active = get_active_id()
    packs = list(_all().values())
    packs.sort(key=lambda p: (not p.get("builtin"), p["name"].lower()))
    return [{"id": p["id"], "name": p["name"], "tools": len(p["tools"]),
             "builtin": p.get("builtin", False), "active": p["id"] == active,
             "writes": sum(1 for t in p["tools"] if t.get("risk") in ("write", "destructive")),
             "base_url": (p.get("env") or {}).get("base_url", "")}
            for p in packs]


def import_pack(name: str, config: dict) -> str:
    _ensure()
    normalized = fit.normalize(config)
    if not normalized:
        raise ValueError("no tools found — expected a TiefWise MCP config with a 'tools' array")
    pid = hashlib.sha1(f"{name}{time.time()}".encode()).hexdigest()[:10]
    json.dump({"id": pid, "name": name or "pack", "tools": normalized, "config": config,
               "env": {"base_url": "", "token": ""}},
              open(_path(pid), "w"))
    set_active(pid)
    return pid


def set_env(pid: str, base_url: str, token: str):
    if pid == "builtin":
        return
    p = get_pack(pid)
    if not p:
        return
    p["env"] = {"base_url": (base_url or "").strip(), "token": (token or "").strip()}
    json.dump(p, open(_path(pid), "w"))


def active_pack():
    return get_pack(get_active_id()) or _builtin()


def delete_pack(pid: str):
    if pid == "builtin":
        return
    p = _path(pid)
    if os.path.exists(p):
        os.remove(p)
    if get_active_id() == pid:
        set_active("builtin")
