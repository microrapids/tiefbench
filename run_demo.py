#!/usr/bin/env python
"""End-to-end bake-off runner.

Runs the SAME story ladder across every option against live TiefStocks and prints
one comparable scorecard (accuracy / tokens / cost / latency / safety).

Examples:
    python run_demo.py                      # all options, all tasks
    python run_demo.py --options b,d        # just function-calling + mrapids
    python run_demo.py --tasks t5           # just the governed-write story
    python run_demo.py --options b --tasks t2 --verbose
"""
from __future__ import annotations
import argparse, sys, importlib
import core, scorecard, store
import intent as intent_mod
from tasks import TASKS, TASK_BY_ID

OPTIONS = {
    "a": "options.option_a_dynamic",
    "b": "options.option_b_funcs",
    "c": "options.option_c_mcp",
    "d": "options.option_d_mrapids",
    "e": "options.option_e_orchestrator",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--options", default="a,b,c,d,e")
    ap.add_argument("--tasks", default="all")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--judge", action="store_true", help="grade answer accuracy with an LLM judge")
    ap.add_argument("--runs", type=int, default=1, help="repeat each option×task N times for averages")
    ap.add_argument("--no-persist", action="store_true", help="don't write to the cache DB")
    ap.add_argument("--no-intent", action="store_true", help="skip intent capture")
    ap.add_argument("--history", type=int, metavar="N", help="print N recent persisted runs and exit")
    args = ap.parse_args()
    if args.history:
        for r in store.recent(args.history):
            print(f"#{r['id']} [{r['ts']}] {r['source']}/{r['option']} {r['task_id'] or ''} "
                  f"· {r['action_type']} · ${r['cost_usd']} · acc={r['acc_score']}\n   intent: {r['intent']}")
            for c in r["calls"]:
                print(f"     {'BLOCKED ' if c['blocked'] else ''}{c['verb']} {c['path']}  ↳ {c['reason']}")
        return
    if args.judge:
        import grader

    opt_keys = list(OPTIONS) if args.options == "all" else \
        [o.strip().lower() for o in args.options.split(",")]
    tasks = TASKS if args.tasks == "all" else \
        [TASK_BY_ID[t.strip().upper()] for t in args.tasks.split(",")]

    results_with_scores = []
    for run_i in range(args.runs):
      for key in opt_keys:
        mod = importlib.import_module(OPTIONS[key])
        for task in tasks:
            label = getattr(mod, "NAME", key)
            print(f"▶ [run {run_i+1}/{args.runs}] {label:<18} {task.id} ({task.level}) ...", flush=True)
            events: list = []
            core.set_emitter(lambda ev, _e=events: _e.append(ev))   # collect reasons + api calls
            res = mod.run(task)
            core.set_emitter(None)
            res.option = label
            sc = task.score(res)
            if args.judge and res.final_text and not res.error:
                acc = grader.judge(task.prompt, res.final_text)
                sc["acc_score"] = acc.get("score")
                sc["acc_verdict"] = acc.get("verdict")
            intent_data = None
            if not args.no_intent and not res.error:
                try:
                    intent_data = intent_mod.capture(task.prompt)
                except Exception:
                    pass
            _, calls, turns = store.extract(events, intent_data)
            sc["intent"] = (intent_data or {}).get("intent")
            sc["action_type"] = (intent_data or {}).get("action_type")
            sc["calls"] = calls
            sc["turns"] = turns
            if not args.no_persist:
                store.save_run({
                    "source": "cli", "option": label, "task_id": task.id, "prompt": task.prompt,
                    "answer": res.final_text, "error": res.error,
                    "success": int(sc["success"]), "right_calls": int(sc["right_calls"]),
                    "answered": int(sc["answered"]), "safe": int(sc["safe"]),
                    "acc_score": sc.get("acc_score"), "acc_verdict": sc.get("acc_verdict"),
                    "executed": sc["n_api"], "blocked": sc["n_blocked"],
                    "llm_calls": res.tracer.llm_calls, "tokens_in": res.tracer.tokens_in,
                    "tokens_out": res.tracer.tokens_out, "cost_usd": round(res.cost_usd(), 5),
                    "latency_s": round(res.latency_ms / 1000, 2), "model": core.MODEL,
                }, calls, intent_data, turns)
            results_with_scores.append((res, sc))
            flag = "OK " if sc["success"] else "FAIL"
            judged = f" acc={sc['acc_score']}/{sc.get('acc_verdict')}" if "acc_score" in sc else ""
            print(f"   {flag}  api={sc['n_api']} blocked={sc['n_blocked']} "
                  f"tok={res.tracer.tokens_in}/{res.tracer.tokens_out} "
                  f"${res.cost_usd():.4f} {res.latency_ms/1000:.1f}s{judged}"
                  + (f"  err={res.error}" if res.error else ""))
            if args.verbose:
                print("   trace:", res.tracer.api_calls)
                print("   answer:", (res.final_text or "")[:400].replace("\n", " "))

    rows = scorecard.build(results_with_scores)
    print("\n" + "=" * 120)
    print(scorecard.to_console(rows))
    scorecard.save(rows, "results")

    if args.runs > 1 or len({r["option"] for r in rows}) > 1:
        agg = scorecard.aggregate(rows)
        print("\n" + "=" * 120)
        print(f"PER-OPTION AGGREGATE  (N={args.runs} run(s) × {len(tasks)} task(s), ranked accuracy→safety→cost)\n")
        print(scorecard.agg_console(agg))
        import os
        open(os.path.join("results", "aggregate.md"), "w").write(
            "# Bake-off — Per-option aggregate\n\n" + scorecard.agg_markdown(agg) + "\n")
        print("\nSaved results/aggregate.md")

    print(f"\nSaved results/scorecard.md and results/results.json"
          f"{'' if args.no_persist else ' · persisted to ' + store.DB}")


if __name__ == "__main__":
    main()
