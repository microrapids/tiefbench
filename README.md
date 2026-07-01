# TiefBench

**Benchmark, observe, and optimize how AI agents reach your APIs.** TiefBench runs
the same request through five agent strategies — direct function calling, MCP,
OpenAPI/mrapids collections, planner orchestration, and dynamic Python — against a
live API, and shows which approach is most accurate, cheapest, and safest. It goes
past benchmarking to the two under-served, high-value levers of the agent loop:

- **Tune your MCP tool contract** — both *descriptions* (which tool the agent picks)
  and *results* (what tools return, which drives multi-step loop cost), with
  before/after proof.
- **De-risk model choice** — compare models side-by-side ("tested on Sonnet, prod
  runs Haiku?") and validate that the cheap selection probe predicts the real agent.

```
User NL prompt
   ↓  agent layer (tool-use loop on the Anthropic SDK)
Option A/B/C/D/E  ← the execution surface being compared
   ↓
Policy / approval layer  ← deterministic, OUTSIDE the model (writes are gated)
   ↓
Your API (demo: TiefStocks @ localhost:8001)
```

## Quickstart

**Prerequisites:** Python 3.11+, a **funded** `ANTHROPIC_API_KEY`. Optional:
[`mrapids`](https://microrapid.io/mrapids/) on PATH (for Option D) and a target
API running locally (the demo expects TiefStocks at `http://localhost:8001`).

```bash
git clone https://github.com/microrapids/tiefbench.git
cd tiefbench
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...          # must have credit balance

# Web app — five screens:
.venv/bin/python -m uvicorn webapp.server:app --port 8800
#   http://127.0.0.1:8800           Bake-off chat (compare all 5, per-query monitor, model picker)
#   http://127.0.0.1:8800/advisor   Fit Advisor (which option suits your tool pack)
#   http://127.0.0.1:8800/tune      Tune my MCP (descriptions + result projections, with A/B proof)
#   http://127.0.0.1:8800/eval      Model Eval (compare models; "tested on X, prod on Y?")
#   http://127.0.0.1:8800/validate  Probe validation (does the probe predict the real agent?)
```

**CLI bake-off** (scored scorecard + per-option aggregates, persisted to a cache DB):

```bash
.venv/bin/python run_demo.py --judge              # all options × tasks, accuracy-graded
.venv/bin/python run_demo.py --runs 3 --judge     # N runs for averages + variance
.venv/bin/python run_demo.py --history 10         # recent persisted runs (intent + reasons)
.venv/bin/python qa_smoke.py                       # regression suite (server must be up)
```

**Config (env vars):** `TIEFBENCH_MODEL` (default `claude-sonnet-4-6`),
`TIEFSTOCKS_URL` (default `http://127.0.0.1:8001`),
`TIEFBENCH_JUDGE_MODEL` / `TIEFBENCH_INTENT_MODEL` (default Haiku).

> The **Fit Advisor**, **Model Eval**, and Tune's *descriptions* mode work without
> the live API (they use the cheap selection probe / static signals). The **chat
> bake-off**, **Validate**, and Tune's *results* mode run the real loop, so they
> need the API key and the live API.

## Web screens

| Screen | What it's for |
| ------ | ------------- |
| **Bake-off** (`/`) | Run any option on a prompt; live streaming, per-query monitor (intent → loop turns → API calls + reasons), zoomable workflow diagram, model picker, ⚖️ Compare all 5, 📌 pin/compare, 🕘 History, ＋ New chat. |
| **Fit Advisor** (`/advisor`) | Analyze a tool pack → per-option fit (🟢/🟡/🔴) with reasons, predicted cost, a what-if tool-count slider, and calibration from real runs. Also drives the inline fit nudge + sidebar dots in the chat. |
| **Tune my MCP** (`/tune`) | **🏷️ Descriptions** — fix ambiguous/low-confidence tool selection with AI-drafted "use-when / not-when" descriptions + A/B re-test. **✂️ Results** — AI-draft a per-tool field projection (config transform, no API change) and prove the multi-step loop-token cut with answer correctness held. |
| **Model Eval** (`/eval`) | Run the same scenarios across models, diff each vs a reference, flag divergences, recommend the cheapest model that still matches. N-sampling + agreement confidence intervals. Optional transient BYOK key (blank = env key). |
| **Validate** (`/validate`) | Does the cheap selection probe predict the *real* multi-turn agent? Tagged scenarios, full-trace matching (first-tool + any-use), per-model, confidence intervals. |

## Design docs

The full API-to-agent comparison write-up is in
[`docs/poc-stories.md`](./docs/poc-stories.md).

## Architecture docs

Per-option architecture with diagrams (Mermaid) lives in
[`docs/architecture/`](./docs/architecture/) — start with the
[index](./docs/architecture/README.md) for the shared layered diagram, then one
doc per option (A–E).

## The five options

| Key | Option | What it does |
| --- | ------ | ------------ |
| `a` | Dynamic Python | LLM writes a script; runs in a constrained subprocess via a gateway shim that counts calls and blocks writes. The "max flexibility / max risk" benchmark. |
| `b` | Function calling | Curated Anthropic tools + tool-use loop. The baseline. |
| `c` | MCP | Real FastMCP server over stdio; client discovers tools at runtime (`tools/list`) and drives Claude with them. |
| `d` | mrapids | Agent plans OpenAPI *operations*; `mrapids run` executes them deterministically with JSON output. |
| `e` | Orchestrator | Planner routes intent to a least-privilege tool scope; a scoped sub-agent executes. |

## The story ladder (`tasks.py`)

| Task | Level | Prompt (abridged) | Scored on |
| ---- | ----- | ----------------- | --------- |
| T0 | L0 single read | "What does my portfolio look like?" | correct endpoint + answer |
| T2 | L2 multi-step + reason | "Read on AMD: analysis + thesis + bear case" | 3 correct endpoints + synthesis |
| T5 | L5 governed write | "Record a BUY of 10 AMD @ $230" | **write must be BLOCKED**, not executed |

Each task self-scores objectively (required endpoints in the trace, forbidden
*executed* writes, answer assertions) — see `Task.score()`.

## Governance demo

The **policy layer is deterministic and outside the model** (`tools.py`,
`mcp_server.py`, `sandbox_shim.py`). Every option's write path returns
`APPROVAL_REQUIRED` and is recorded as `BLOCKED` — so T5 proves each surface can
be constrained for risky actions, regardless of what the model decides.

## Run it

Prereqs: TiefStocks running at `localhost:8001`, `mrapids` on PATH, and a
**funded** `ANTHROPIC_API_KEY` (real model calls).

```bash
cd tiefbench
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # must have credit balance

# everything:
.venv/bin/python run_demo.py

# subsets:
.venv/bin/python run_demo.py --options b,d --tasks t0,t2 --verbose
.venv/bin/python run_demo.py --tasks t5            # the governance story across all 5
```

Output: a console scorecard plus `results/scorecard.md` and `results/results.json`.

## Persistence — cache DB (intent + reasons)

Every run (CLI and web) is persisted to a local SQLite cache DB at
`results/tiefbench.db` — including the **captured intent** and the **reason for each
API call**. Two tables: `runs` (one per turn) and `calls` (one per API call, with
its `reason`).

```bash
python store.py                       # pretty-print recent runs + intent + reasons
python run_demo.py --history 10       # same, via the runner
sqlite3 results/tiefbench.db "SELECT option,action_type,intent FROM runs"
sqlite3 results/tiefbench.db "SELECT verb,path,reason FROM calls"
```

The CLI scorecard (`results/scorecard.md`) also gets an **Intent & Reasoning**
appendix per run. Flags: `--no-persist`, `--no-intent`.

### Loop / turn capture

Every model call is captured as a **turn** (`turns` table) — the iteration's
trigger, the model's text, which tools it chose, stop reason, and tokens — so you
can reconstruct the full agent loop that produced the final output. This is
captured uniformly at the LLM layer (`core.capture_turn`), labelled per phase
(`loop`, `router`, `planner`, `codegen`, `summary`). In the web UI it renders live
in the **🔁 Agent loop** card and in each History run's **Loop trace**. Example
(MCP, multi-step): Turn 1 → calls `analyze_stock, get_thesis, challenge_thesis`;
Turn 2 → produces the final answer.

## Checking response accuracy

Three independent signals, strongest last:

1. **Golden-trace (deterministic)** — `tasks.py` `Task.score()` checks the agent
   called the *required* endpoints, stayed in scope, answered with expected facts,
   and (for writes) did **not** execute. Shown as `ok / calls? / ans? / safe` in
   the scorecard. No extra cost.
2. **Ground truth (deterministic)** — `grader.gather_evidence()` independently
   pulls live API data (portfolio, plus analysis/thesis/challenge for any ticker
   in the prompt). The agent never touches this path, so it's a real oracle.
3. **LLM-as-judge** — `grader.judge()` scores the answer **0–100** + verdict
   (`correct/partial/wrong`) + specific issues, comparing the answer to that
   ground truth with a separate cheap model (default Haiku, ~$0.002/grade).
   Catches hallucinated numbers/tickers a substring check would miss.

```bash
# CLI: add an accuracy column to the scorecard
.venv/bin/python run_demo.py --judge
.venv/bin/python run_demo.py --options b,c,d --tasks t2 --judge --verbose
```

In the **web chat**, tick **“grade accuracy”** before sending — each reply gets a
score bar, verdict, and a list of factual issues. Tip: ask a question, read the
answer, then re-ask with a wrong premise to watch the judge flag it.

> The judge is only as good as the evidence it's given. For facts outside
> `gather_evidence()` (new endpoints/domains), extend that function so the oracle
> stays complete — otherwise the judge grades on partial truth.

## Web chat (interactive demo)

A browser chat UI to test any option on free-text prompts, with live metrics
(API trace, tokens, cost, latency, safety) on every reply.

```bash
cd tiefbench
.venv/bin/pip install fastapi "uvicorn[standard]"
.venv/bin/python -m uvicorn webapp.server:app --port 8800
# open http://127.0.0.1:8800
```

Pick an option in the dropdown, type a question (sample prompts are provided),
and compare. The governed-write prompt shows the 🛡️ **write BLOCKED (approval)**
chip and a `BLOCKED POST ...` trace — the deterministic policy layer firing.

Demo features:
- **Per-option explainer + live workflow diagram** (Mermaid) in the right panel.
- **Response streaming** — answers type out live (SSE; `/api/chat_stream`).
- **Step-by-step diagram highlight** — each real API call pulses the relevant
  node (green for reads, red for a blocked write) and appends to a live timeline.
- **⚖️ Compare all 5** — runs one prompt across every option and tables the
  API/LLM/tokens/cost/latency/accuracy side by side.
- **🕘 History** — recent persisted runs from the cache DB (web + CLI), each with
  its captured intent and expandable per-call reasons (`GET /api/history`).
- **Model picker** — switch the model per request from the header; cost is priced
  by the model actually used. **＋ New chat** clears the transcript (History and
  Pinned runs are kept).
- **Zoomable workflow** — scroll to zoom, drag to pan, `⤢` for fullscreen; the
  live node/edge highlight plays on the zoomed diagram.
- **MCP loop showcase** — on Option C, a `tools/list` **registration banner**
  (the discovered tools), and a **prompt-token bar per turn** so you can watch the
  context grow each loop (e.g. turn 1 ≈ 1.5k incl. schemas → turn 2 ≈ 4.9k).

## Cost knobs

Default model is `claude-sonnet-4-6`. For a cheaper demo:
`TIEFBENCH_MODEL=claude-haiku-4-5-20251001`. Prices in `core.PRICES` are approximate
list prices — adjust to your contract for accurate cost columns.

## Status

All plumbing verified live (API client, mrapids catalog+run → `200`, MCP
server boot + `tools/list` + `call_tool`). The only thing needed to see full
results is a funded API key — the harness already runs, scores, and saves.

## Extending to GitHub / Stripe

Add a client like `api/tiefstocks.py`, register tools in `tools.py` (+ MCP
server), and add tasks in `tasks.py`. The runner, scorecard, and policy layer are
domain-agnostic. GitHub/Stripe need real credentials, which is why this demo
centers on the live, no-auth TiefStocks instance.
