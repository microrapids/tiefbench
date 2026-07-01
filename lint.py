"""MCP config linter — static checks on a TiefWise/MCP config before it ships.

No LLM, no execution. Reads the raw config and flags agent-relevant problems
(hardcoded instance paths, unresolved base URLs, noisy names, un-curated test
endpoints, thin descriptions, auth-in-prose, missing risk/result info), returns a
scored report with concrete fixes.
"""
from __future__ import annotations
import re
import copy

SEV_WEIGHT = {"error": 15, "warn": 6, "info": 2}
TEST_HINTS = ("test", "delay", "header", "echo", "ping", "status", "/ip", "getip")


def _field(t, *keys, default=None):
    for k in keys:
        cur = t
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def _tool_view(t):
    http = t.get("_http") or {}
    return {
        "name": t.get("name", "?"),
        "description": t.get("description", "") or "",
        "method": (_field(t, "method") or http.get("method") or _field(t, "source.method") or "GET").upper(),
        "path": _field(t, "path") or http.get("path") or _field(t, "source.path") or "",
        "schema": t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}},
        "server": _field(t, "execution.serverUrl") or "",
        "risk": _field(t, "safety.risk"),
        "readonly": (t.get("annotations") or {}).get("readOnlyHint"),
        "destructive": (t.get("annotations") or {}).get("destructiveHint"),
        "security": _field(t, "source.security", "auth.scheme"),
        "examples": t.get("examples"),
        "result": t.get("result") or t.get("responseSchema"),
    }


def lint(config: dict) -> dict:
    tools = (config.get("tools") if isinstance(config, dict) else None) or []
    findings, strengths = [], []
    if not tools:
        return {"score": 0, "counts": {"error": 1, "warn": 0, "info": 0},
                "findings": [{"rule": "no-tools", "severity": "error", "tool": "-",
                              "message": "No tools array found.", "fix": "Provide a config with a 'tools' array."}]}

    def add(rule, sev, tool, message, fix):
        findings.append({"rule": rule, "severity": sev, "tool": tool, "message": message, "fix": fix})

    roots, unresolved, has_risk, has_examples, has_bindings, param_ok = set(), 0, 0, 0, 0, 0
    for raw in tools:
        t = _tool_view(raw)
        name, path, desc = t["name"], t["path"], t["description"]
        if path.startswith("/") and "/" in path[1:]:
            roots.add(path.split("/")[1])

        # 1. hardcoded instance path (a numeric literal segment, no matching param)
        segs = [s for s in path.split("/") if s]
        hard = [s for s in segs if s.isdigit()]
        if hard:
            add("hardcoded-instance-path", "error", name,
                f"path '{path}' hardcodes instance value(s) {hard} — the agent can only ever hit this one.",
                f"parameterize: replace {hard} with {{id}} and add it to inputSchema + bindings.")
        elif "{" in path:
            param_ok += 1

        # 2. unresolved / placeholder base URL
        if "{" in (t["server"] or ""):
            unresolved += 1

        # 3. path-param mismatch (write/mutate with an id-like body field but hardcoded path)
        props = (t["schema"].get("properties") or {})
        id_like = [p for p in props if p == "id" or p.endswith("_id")]
        if t["method"] in ("PUT", "PATCH", "DELETE") and id_like and "{" not in path:
            add("path-param-mismatch", "error", name,
                f"takes id field {id_like} but path '{path}' is fixed — updates/deletes will hit the wrong resource.",
                "put the id in the path: '/.../{id}' and bind it to path.")

        # 4. noisy tool name (internal ids leaking in)
        if re.search(r"__[A-Za-z0-9-]+$", name) or "__" in name:
            add("noisy-tool-name", "warn", name,
                "name carries an internal id/suffix that pollutes the model's context.",
                f"rename to '{re.sub(r'__.*$', '', name)}' (keep the raw id in source.operationId).")

        # 5. un-curated test/debug endpoint
        low = (name + " " + path).lower()
        if any(h in low for h in TEST_HINTS):
            add("uncurated-test-endpoint", "warn", name,
                "looks like a test/debug endpoint — noise as an agent tool.",
                "drop it from the pack; keep the tool set small and purposeful.")

        # 6. thin description
        if len(desc.strip()) < 30:
            add("thin-description", "warn", name,
                f"description is thin ('{desc}') — weak for tool selection.",
                "say what it does + WHEN to use / WHEN NOT (vs similar tools).")

        # 7. auth stuffed into the description
        if any(w in desc.lower() for w in ("auth", "token", "requires:")) or "⚠" in desc:
            add("auth-in-prose", "warn", name,
                "auth info is in the description (pollutes the prompt).",
                'move to a structured field: "auth": {"scheme": "...", "env": "API_TOKEN"}.')

        # 8. ambiguous risk
        if not t["risk"] and t["readonly"] is None and t["destructive"] is None:
            add("missing-risk", "warn", name,
                "no safety.risk / read-only / destructive hint — gating is a guess.",
                'add "safety": {"risk": "read|write|destructive"}.')

        # 9. no result guidance
        if not t["result"]:
            add("missing-result-projection", "info", name,
                "no result schema/projection — verbose results inflate loop cost.",
                'add "result": {"keep": ["field", ...]} to minimize what re-enters context.')

        has_risk += 1 if t["risk"] else 0
        has_examples += 1 if t["examples"] else 0
        has_bindings += 1 if (raw.get("bindings") or (raw.get("_http") or {}).get("paramBindings")) else 0

    # pack-level: possible multi-host
    if unresolved and len(roots) > 1:
        add("multi-host-one-server", "error", "(pack)",
            f"{unresolved} tools share one placeholder server but the paths span {len(roots)} roots {sorted(roots)[:5]} — one base URL can't serve multiple hosts.",
            "resolve execution.serverUrl per tool to its real host.")
    elif unresolved:
        add("unresolved-base-url", "warn", "(pack)",
            f"{unresolved} tools have a placeholder server URL (e.g. {{baseUrl}}).",
            "resolve serverUrl to a concrete host before shipping.")

    n = len(tools)
    if has_risk == n:
        strengths.append("explicit safety.risk on every tool")
    if has_examples:
        strengths.append(f"{has_examples} tool(s) ship examples")
    if has_bindings:
        strengths.append(f"{has_bindings} tool(s) carry execution bindings")
    if param_ok:
        strengths.append(f"{param_ok} tool(s) use parameterized paths")

    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warn", "info")}
    penalty = sum(SEV_WEIGHT[f["severity"]] for f in findings)
    score = max(0, 100 - penalty)
    return {"score": score, "tools": n, "counts": counts,
            "findings": sorted(findings, key=lambda f: ("error", "warn", "info").index(f["severity"])),
            "strengths": strengths}


# ---- auto-fix: apply the deterministic fixes and return a corrected config ----
def _snake(name):
    s = re.sub(r"__.*$", "", name)                       # strip internal-id suffix
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()        # camelCase -> snake_case
    s = re.sub(r"[^a-z0-9_]+", "_", s).strip("_")
    return s or name


def _get_path(raw):
    return raw.get("path") or (raw.get("_http") or {}).get("path") or ""


def _set_path(raw, p):
    if "path" in raw:
        raw["path"] = p
    if raw.get("_http"):
        raw["_http"]["path"] = p


def _bind_path(raw, pname):
    b = raw.setdefault("bindings", {})
    if isinstance(b, dict) and ("path" in b or "body" in b or not b):     # v0.1 shape {path:[],body:[]}
        b.setdefault("path", [])
        if pname not in b["path"]:
            b["path"].append(pname)
        for loc in ("body", "query"):
            if loc in b and pname in b[loc]:
                b[loc].remove(pname)
    http = raw.get("_http")                                                # manifest shape {param:loc}
    if http is not None:
        http.setdefault("paramBindings", {})[pname] = "path"


def autofix(config, drop_tests=False):
    cfg = copy.deepcopy(config)
    tools = cfg.get("tools") or []
    applied, out, seen = [], [], set()
    for raw in tools:
        name = raw.get("name", "")
        path = _get_path(raw)
        low = (name + " " + path).lower()

        if drop_tests and any(h in low for h in TEST_HINTS):
            applied.append({"tool": name, "change": "removed (test/debug endpoint)"})
            continue

        # 1. clean noisy name
        if "__" in name:
            nn = _snake(name); base, i = nn, 2
            while nn in seen:
                nn = f"{base}_{i}"; i += 1
            raw.setdefault("source", {}).setdefault("operationId", name)
            raw["name"] = nn
            applied.append({"tool": name, "change": f"renamed → {nn}"})
            name = nn
        seen.add(name)

        # 2/3. parameterize hardcoded numeric path segments (+ pull id from body to path)
        props = ((raw.get("inputSchema") or raw.get("input_schema") or {}).get("properties")) or {}
        id_body = [p for p in props if p == "id" or p.endswith("_id")]
        segs, changed = path.split("/"), False
        for idx, s in enumerate(segs):
            if s.isdigit():
                prev = segs[idx - 1] if idx > 0 else "item"
                pname = id_body[0] if id_body else (re.sub(r"s$", "", prev) + "_id")
                segs[idx] = "{" + pname + "}"
                changed = True
                sch = raw.get("inputSchema") or raw.get("input_schema")
                if sch is not None:
                    sch.setdefault("properties", {}).setdefault(
                        pname, {"type": "integer", "description": f"{pname} (was hardcoded '{s}')"})
                    req = sch.setdefault("required", [])
                    if pname not in req:
                        req.append(pname)
                _bind_path(raw, pname)
        if changed:
            _set_path(raw, "/".join(segs))
            applied.append({"tool": name, "change": f"parameterized path → {'/'.join(segs)}"})

        # 4. auth in prose -> structured field
        desc = raw.get("description", "") or ""
        m = re.search(r"(?i)(oauth2\w*|bearer|api[_-]?key|basic)", desc) if ("auth" in desc.lower() or "⚠" in desc) else None
        if m:
            raw["auth"] = {"scheme": m.group(1), "env": "API_TOKEN"}
            raw["description"] = re.split(r"(?i)\n|⚠|auth", desc)[0].strip() or name
            applied.append({"tool": name, "change": "auth → structured field"})

        out.append(raw)

    cfg["tools"] = out
    rep = lint(cfg)
    remaining = [f for f in rep["findings"]
                 if f["rule"] in ("multi-host-one-server", "unresolved-base-url",
                                  "thin-description", "missing-result-projection")]
    return {"config": cfg, "applied": applied, "remaining": remaining, "new_score": rep["score"]}
