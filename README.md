# Agentic Workflow Engine

An implementation of a **workflow DAG combined with agent-style
decision making**: the steps are fixed and inspectable, while selected nodes
decide at runtime which path the work takes.

It ships with a customer-support triage workflow and a web debugger for
submitting requests, watching nodes execute, inspecting every input/output/log,
retrying failures and approving human gates.

| | |
|---|---|
| **Backend** | Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · SQLite |
| **Frontend** | React 18 · Vite · TypeScript (no UI framework, no graph library) |
| **Agent** | Pluggable — deterministic rule-based provider by default, real Claude API when `ANTHROPIC_API_KEY` is set |
| **Verification** | 74 backend tests · 65 browser assertions · `ruff` clean |

---

## What the assignment asked for, and where it lives

### Backend requirements

| Requirement | How it is implemented |
|---|---|
| **Persist workflow runs and node state** | Five tables (`app/models.py`): `workflow_runs`, `node_runs`, `node_logs`, `tool_calls`, `approval_requests`. Every transition is written to `node_runs`, so nothing important lives in memory. |
| **Execute nodes only after dependencies are complete** | A pure scheduler (`app/engine/scheduling.py`) resolves each **edge** to `active` / `pruned` / `blocked` / `unresolved`, then decides which nodes are ready. Independent ready nodes run concurrently. |
| **Support conditional branches** | A `branch` node evaluates declarative predicates (`app/engine/predicates.py`) and emits a label. Edges carry labels; unselected edges are pruned and their subtrees skipped. |
| **Node types: input, agent/decision, condition/branch, mock tool-call, human approval** | All five, one handler each in `app/engine/handlers/`. |
| **Validate agent/decision output before downstream execution** | Each agent node declares a Pydantic contract. Raw provider output is validated, and on failure re-prompted with the errors (bounded repair loop). Only a validated result lets the engine schedule anything downstream. |
| **Allow failed nodes to be retried or resumed** | `POST /runs/{id}/nodes/{node}/retry` resets a failed node and re-drives the run. `POST /runs/{id}/resume` also recovers nodes left `running` by a crashed process. Nodes additionally have their own automatic attempt budget. |
| **Idempotent tool calls** | Every invocation is keyed by `sha256(run · node · tool · canonical args)` and recorded in `tool_calls` under a UNIQUE constraint. A repeat with the same key replays the recorded response instead of calling the tool. |
| **Store logs/traces per node** | `node_logs` is append-only and tagged with the attempt number, so a retry never overwrites the failed attempt's evidence. `node_runs` stores resolved input, output, error + machine-readable error code, timings and attempt counts. |

### Frontend requirement

The UI is a workflow debugger: submit a request, watch node statuses on the
DAG, click any node to inspect its input / output / logs / tool calls / config,
retry failed nodes, and approve or reject human-approval steps. Branch
resolution is drawn on the edges (green = taken, dashed = pruned, red =
blocked).

### Submission

- **README** — this file: what was built and how to run it.
- **[DESIGN.md](DESIGN.md)** — component-by-component design rationale.
- **Test scenarios** — branching, retry, approval, validation failure and
  idempotency, covered in both the backend suite and the browser suite (see
  [Test scenarios](#test-scenarios)).

---

## Architecture at a glance

```
                    HTTP (FastAPI)                    Browser (React + Vite)
                          │                                    │
        ┌─────────────────▼──────────────────┐                 │
        │            WorkflowEngine           │◄────────────────┘
        │  create · advance · retry · approve │   polls run + node state
        └───┬───────────────┬─────────────┬──┘
            │               │             │
   ┌────────▼──────┐ ┌──────▼──────┐ ┌────▼─────────┐
   │  scheduling   │ │  handlers   │ │  definition  │
   │ (pure, edge   │ │ input/agent │ │ frozen DAG + │
   │  resolution)  │ │ branch/tool │ │ self-validating│
   └───────────────┘ │  /approval  │ └──────────────┘
                     └──┬───────┬──┘
                        │       │
             ┌──────────▼──┐ ┌──▼────────────┐
             │ LLMProvider │ │ ToolRegistry  │
             │ mock│claude │ │ + idempotency │
             └─────────────┘ └───────────────┘
                        │
                 ┌──────▼───────┐
                 │ SQLite (async)│  runs · node_runs · logs · tool_calls · approvals
                 └───────────────┘
```

---

## Prerequisites

- **Python 3.11+** (uses `enum.StrEnum`)
- **Node 18+** (Vite 5)
- No API key, no Docker, no external services required.

---

## Running it

Two terminals. Everything works with **zero configuration**.

### 1. Backend

```bash
cd backend

python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -e ".[dev]"

uvicorn app.main:app --reload --port 8000
```

API on <http://127.0.0.1:8000> · OpenAPI docs on <http://127.0.0.1:8000/docs>.
The SQLite file is created on first start.

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

UI on <http://localhost:5173>. The dev server proxies `/api` to port 8000, so
there is nothing to configure.

### 3. Tests and linter

```bash
cd backend
pytest -q                       # 74 tests
ruff check app tests scripts    # lint config lives in pyproject.toml
```

```bash
cd frontend
npm run build                   # tsc --noEmit equivalent + production build
```

### 4. Browser verification (optional)

With **both servers running**, this drives the debugger end to end in a real
browser — selecting scenarios, clicking **Start run**, opening nodes, pressing
**Retry** and **Approve/Reject** — and asserts 65 outcomes plus a clean browser
console. It also measures layout geometry from the DOM at three viewport widths
(no clipped nodes, no edge label overlapping a node box).

```bash
cd backend
python scripts/ui_verify.py           # add --headed to watch it run
```

It drives your installed Chrome or Edge, so there is no browser to download.
Screenshots of every state land in `backend/ui-verification/` (gitignored).

### 5. Using the real Claude API (optional)

```bash
cd backend
cp .env.example .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
```

Restart the backend. Agent/decision nodes now call Claude (`claude-opus-4-8`)
with **structured outputs**; the engine still validates every response against
the node's Pydantic contract locally. The header in the UI shows which provider
is live.

### Configuration

All settings are env vars with working defaults (see `backend/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./agentic_workflow.db` | Storage |
| `AGENT_PROVIDER` | `auto` | `auto` \| `mock` \| `claude`. `auto` picks Claude when a key is present |
| `ANTHROPIC_API_KEY` | unset | Enables the Claude provider |
| `ANTHROPIC_MODEL` | `claude-opus-4-8` | Model for agent nodes |
| `ANTHROPIC_EFFORT` | `low` | Thinking depth / cost for these short extraction tasks |
| `ENABLE_FAULT_INJECTION` | `true` | When false the server strips `options.faults` and the UI hides the control |

---

## The bundled workflow

`support_triage` — the example from the brief.

```
intake ─┬─► classify ────┐
        └─► fetch_ctx ───┴─► route ─┬─(bug)─────► create_issue ──► draft_bug ──────┐
                                    ├─(billing)─► lookup_invoice ─► draft_billing ─┼─► finalize ─► send_reply
                                    └─(unclear)─► human_review ───► draft_clarify ─┘
```

| Node | Type | What it does |
|---|---|---|
| `intake` | input | Validates the request against the `SupportTicket` contract |
| `classify` | agent | Decides `bug` / `billing` / `unclear` **with a confidence score** |
| `fetch_context` | tool | CRM lookup — runs **in parallel** with `classify` |
| `route` | branch | Declarative predicates; low confidence overrides the predicted category |
| `create_issue` | tool | **Side effecting** — files a mock Linear issue |
| `draft_bug_reply` | agent | Writes the reply for the bug path |
| `lookup_invoice` | tool | Read-only invoice lookup |
| `draft_billing_reply` | agent | Writes the reply for the billing path |
| `human_review` | approval | Pauses the run for a human decision |
| `draft_clarification_reply` | agent | Writes a clarifying reply, using the reviewer's note |
| `finalize` | agent | Joins the three mutually-exclusive branches (`join=any`); its output is the run result |
| `send_reply` | tool | **Side effecting** — sends the reply |

---

## Using the debugger

1. Pick a **scenario** on the left (bug / billing / ambiguous / invalid input)
   and press **Start run**.
2. The graph updates as nodes execute. Edge colour shows branch resolution:
   green = taken, dashed grey = pruned, red = blocked by a failure.
3. Click any node to inspect **overview, input, output, per-attempt logs, tool
   calls and the node's own config**.
4. Failed nodes get a **Retry** button. Approval nodes get **Approve / Reject**
   with a note that flows downstream into the drafted reply.
5. The header shows the **real side effects** performed (issues filed, emails
   sent) — this is what makes the idempotency guarantee observable.

### Fault injection

The **Fault injection** dropdown attaches a fault spec to the run, which is how
every failure scenario is reproduced deterministically:

```jsonc
{ "faults": { "create_issue": { "kind": "tool_after_side_effect", "times": 2 } } }
```

| Kind | Effect |
|---|---|
| `tool_transient` | Node fails **before** the tool is invoked (no side effect) |
| `tool_after_side_effect` | Node fails **after** the tool succeeded — the retry must replay, not re-run |
| `agent_invalid_output` | Agent returns schema-violating JSON |
| `agent_error` | Agent provider raises a retryable error |

`times` is the number of **node attempts** the fault poisons. A node with
`max_attempts: 2` and `times: 1` recovers automatically; `times: 2` exhausts the
automatic budget and needs an operator retry.

Because this is a debug hook any client could pull, it is switchable:
`ENABLE_FAULT_INJECTION=false` makes the server drop `options.faults` and the
UI hide the control.

### The scenario worth running by hand

Pick **Bug report** + fault **"create_issue: fails AFTER the side effect"**,
then press Start.

1. `create_issue` fails — but the header already shows **issues 1**.
2. Open the node → **Tools** tab: the ledger row is `succeeded`, with a stable
   idempotency key.
3. Press **Retry**. The run completes and the counter is **still 1**. The log
   reads *"Idempotent replay: … returning the recorded response without
   re-invoking it."*

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness |
| `GET` | `/api/system` | Active agent provider, registered tools, workflows, fault-injection flag |
| `GET` | `/api/workflows` | Workflow definitions incl. layout ranks for the graph |
| `GET` | `/api/workflows/{id}` | One definition |
| `POST` | `/api/runs` | Start a run — `{workflow_id, input, options}` |
| `GET` | `/api/runs` | Recent runs |
| `GET` | `/api/runs/{id}` | Run detail: node states + **per-edge state** + approvals |
| `GET` | `/api/runs/{id}/nodes/{node_id}` | Node input / output / logs / tool calls / config |
| `GET` | `/api/runs/{id}/logs` | Whole-run trace |
| `POST` | `/api/runs/{id}/nodes/{node_id}/retry` | Retry a failed node |
| `POST` | `/api/runs/{id}/nodes/{node_id}/approve` | `{decision, note, payload, decided_by}` |
| `POST` | `/api/runs/{id}/resume` | Resume; also recovers nodes left `running` by a crash |
| `POST` | `/api/runs/{id}/cancel` | Cancel a run |
| `GET` | `/api/side-effects` | What the mock tools actually did |
| `POST` | `/api/side-effects/reset` | Clear the side-effect ledger |

---

## Test scenarios

`pytest -q` — **74 tests**. The five scenarios the brief asks for:

| Scenario | File | Key assertions |
|---|---|---|
| **Branching** | `tests/test_branching.py` | Each route runs its own path and *skips* the other two; the confidence gate overrides the predicted category; the fan-out runs in parallel |
| **Retry** | `tests/test_retry.py` | Transient failure absorbed automatically; exhausted budget fails the node and leaves downstream **pending, not skipped**; manual retry resumes to completion; crash recovery via `resume`; fault injection can be disabled |
| **Approval** | `tests/test_approval.py` | Run parks with a pending request; approval carries the reviewer's note into the drafted reply; rejection fails the run with nothing sent; retry re-opens a fresh gate |
| **Validation failure** | `tests/test_validation.py` | Bad run input fails at intake; schema-violating agent output fails the node with **no downstream side effect**; the repair loop recovers a one-off bad response inside a single attempt |
| **Idempotency** | `tests/test_idempotency.py` | Failure *after* the tool succeeded → retry replays the recorded result; exactly **one** Linear issue / one email across every retry path; keys are argument-sensitive and order-independent |

Supporting suites: `tests/test_scheduling.py` (pure scheduler units +
definition-time config validation), `tests/test_predicates.py` (branch
predicates and prompt rendering), `tests/test_api.py` (end-to-end over HTTP).

The same five scenarios are **also driven through the browser** by
`scripts/ui_verify.py`, which covers what API tests cannot: that the graph
renders the right node states, that Retry appears only on failed nodes, that
approving from the inspector carries the note into the reply, and that the
layout has no clipped nodes or overlapping labels.

---

## Project layout

```
backend/
  app/
    engine/
      definition.py    frozen DAG + self-validation
      predicates.py    declarative branch predicates (evaluate + validate)
      resolver.py      path-based node input resolution
      scheduling.py    pure edge-state resolution and readiness
      executor.py      the engine: create / advance / retry / approve / resume
      states.py        run + node + edge status vocabulary
      errors.py        error taxonomy with retryable flags
      handlers/        one handler per node type
    agents/
      contracts.py     Pydantic output contracts + provider-safe JSON Schema
      provider.py      LLMProvider protocol (the seam)
      mock.py          deterministic rule-based provider
      claude.py        Claude provider (structured outputs)
    tools/
      registry.py      ToolSpec / ToolRegistry / side-effect ledger
      mock_tools.py    mock CRM, Linear, billing, email
    workflows/         the support-triage DAG
    api/               routes, dependency wiring
    models.py          5 tables
    db.py, config.py, schemas.py, main.py
  tests/               74 tests
  scripts/ui_verify.py browser-driven verification
frontend/
  src/
    App.tsx            layout, polling, actions
    api.ts, types.ts   typed API client
    components/        Graph (hand-rolled SVG DAG), NodeInspector, NewRunForm, Json
```

See **[DESIGN.md](DESIGN.md)** for why each of these is built the way it is.
