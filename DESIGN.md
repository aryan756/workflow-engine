# Design

A component-by-component account of what each piece does, **why it is built
that way, and what was rejected**. For what the project is and how to run it,
see [README.md](README.md).

---

## 0. The core bet

The brief asks for a workflow with *fixed, inspectable steps* where *selected
nodes make dynamic decisions*. Those two halves pull in opposite directions: an
agent is by nature unpredictable, and a workflow engine is only trustworthy if
it is predictable.

The resolution that drove every decision below:

> **The structure is static data. The only thing the model decides is which
> declared branch label is taken — and only after its output has been validated
> against a schema the workflow itself declares.**

So a workflow definition contains no callbacks, no lambdas and no `eval`. It is
a frozen dataclass of nodes, edges, path expressions and predicates. That single
constraint is what makes the engine renderable by a UI that knows nothing about
node semantics, diffable in code review, validatable at startup, and safe to
execute even when the agent misbehaves.

The layering follows from it:

```
definition ──► scheduling ──► executor ──► handlers ──► providers / tools
  (data)        (pure fn)      (I/O)      (per type)      (pluggable)
```

Each layer only knows about the one below. `scheduling` has no I/O, `handlers`
never touch a vendor SDK, and `definition` never imports the engine.

---

## 1. `models.py` — the persistence model

Five tables. Everything the UI shows and everything a retry needs comes from
here; there is no in-memory run registry to lose.

**`workflow_runs`** — one row per run. Holds the input payload, the resolved
output, run status, an error summary, and `options_json` (which carries the
fault spec). It also records `workflow_version`, so a run is traceable to the
definition it started under.

**`node_runs`** — *the* durable state machine, one row per node per run. This is
the design centre of gravity. Because the node's status, attempt count,
resolved input, output, error code and selected branch labels are all columns,
the engine can be stopped at any point and resumed by re-reading this table.
`(run_id, node_id)` is unique, which makes the row a natural lock target and
makes "claim this node" a status check rather than a coordination problem.

*Why not derive node state from an event log?* Event sourcing would give a
better audit story, but every scheduling decision would then require a replay.
The scheduler runs on every pass, so a directly-queryable current-state table is
the right trade. The event log exists separately, for humans, as `node_logs`.

**`node_logs`** — append-only trace, tagged with `attempt`. Never updated,
never deleted. This is deliberate: a retry must not erase the evidence of the
attempt that failed, or the debugger becomes useless exactly when you need it.
The failed attempt and the successful one sit in the same timeline.

**`tool_calls`** — the idempotency ledger. `idempotency_key` carries a UNIQUE
constraint, so the guarantee is enforced by the database rather than by
application logic that could race. `replayed_count` makes replays visible
instead of silent.

**`approval_requests`** — human gates as rows. Because the gate is durable, a
run can sit parked across a process restart, and the reviewer's note and payload
survive to flow downstream.

Two small conventions: `new_id(prefix)` produces readable, greppable ids
(`run_…`, `nr_…`, `tc_…`, `apr_…`), and JSON columns are used for payloads
because they are genuinely schemaless (arbitrary node IO) — the *structured*
facts we query on are real columns.

**Why `lazy="raise"` on `WorkflowRun.nodes`.** The relationship exists for
cascade semantics, but every read path queries `NodeRun` explicitly (often in a
different session). `lazy="selectin"` silently issued a second query on every
run fetch; `lazy="raise"` turns an accidental implicit load into an error.

---

## 2. `db.py` — engine and sessions

Async SQLAlchemy 2.0 over `aiosqlite`. Async throughout because agent nodes make
network calls and independent nodes run concurrently — a sync session would
serialise the interesting part.

SQLite is configured with four pragmas that matter for this workload:

| Pragma | Why |
|---|---|
| `journal_mode=WAL` | Concurrent node executions each hold their own session; WAL lets readers proceed during a write |
| `busy_timeout=10000` | Rather than failing instantly on the single-writer lock, wait |
| `foreign_keys=ON` | Makes the `ON DELETE CASCADE` on child tables real |
| `synchronous=NORMAL` | WAL-appropriate durability/throughput balance for a dev database |

*Why SQLite?* A reviewer must be able to clone and run with no services. The
engine treats the database as a plain relational store — no SQLite-specific
SQL — so moving to Postgres is a URL change plus the concurrency work described
in §17.

---

## 3. `engine/definition.py` — the DAG as data

`WorkflowDefinition` is a frozen dataclass of `NodeDef` and `EdgeDef` tuples
plus an `output_node`. `NodeDef` carries `type`, `join`, `max_attempts`,
`retry_backoff_seconds` and an opaque `config` mapping interpreted by that
node type's handler.

**Why frozen dataclasses over a class hierarchy.** A `BugNode(Node)` subclass
with an `execute()` method would be the obvious OO shape, but it fuses
*structure* with *behaviour*. Keeping them apart means the definition can be
serialised to the frontend wholesale (`/api/workflows` returns node configs
verbatim), and behaviour lives in a handler registry keyed by type.

**Why edges are first-class with an optional `label`.** Conditional routing
could have been a property of nodes ("this node's next step depends on…"), but
then the graph can't be drawn or reasoned about without executing it. Making
the label an edge property means branch resolution is a pure function of
(edge, source state) — which is exactly what §7 exploits.

**`join: ALL | ANY`.** A node after a fan-out needs every parent; a node joining
mutually-exclusive branches needs exactly one. Without this distinction
`finalize` could never become ready, because two of its three parents are always
skipped.

### Self-validation at registration

`validate()` rejects, in this order:

1. duplicate node ids, edges referencing unknown nodes, no entry node, an
   `output_node` that isn't a node;
2. **per-node config** — unknown contract names, agent nodes missing `task`,
   tool nodes missing `tool`, malformed branch cases, unknown predicate
   operators, binary operators with no `value`;
3. labelled edges from non-branch nodes, labels a branch can never emit,
   labels with nowhere to go;
4. conditional joins declared `join=all` (which could never become ready);
5. cycles, via topological sort.

Config checks run **before** the wiring checks because the wiring checks *read*
branch cases — a malformed case would otherwise surface as a confusing "unwired
label" error. (That ordering was a bug, caught by a test.)

`WorkflowEngine` additionally cross-checks `definition.required_tools()` against
the registered `ToolRegistry` at construction. This matters more than it looks:
without it a typo like `crm.fetch_custmer` surfaces only when a run *reaches*
that node — possibly after an upstream side effect has already fired. Pushing
the check to startup converts a half-executed run into a failed boot.

`ranks()` does longest-path layering and is served to the frontend, so graph
layout is computed from the definition rather than hand-positioned in the UI.

---

## 4. `engine/predicates.py` — routing rules as data

```python
{"var": "confidence", "op": "lt", "value": 0.6}
{"all": [{"var": "category", "op": "eq", "value": "bug"}, …]}
```

**Why not `eval` or a lambda.** A string expression is a code-injection surface
and unrenderable; a lambda can't be serialised to the UI or checked at startup.
A tiny structured predicate language costs ~100 lines and buys three things: the
rule is visible in the node inspector, it is validated at registration, and the
branch node can record *every* predicate's outcome so a routing decision is
auditable after the fact rather than reconstructed from logs.

Two deliberate behaviours:

- **Type mismatches mean "did not match", not a crash.** `None > 0.6` raises in
  Python; here it returns `False`. A missing upstream field should route to the
  default, not fail the run.
- **Evaluation and validation are separate functions.** `validate()` is what
  lets `definition.py` reject `{"op": "approximately"}` at import time. Keeping
  them in one module is what makes that check possible without a circular
  import between the definition and the handler.

---

## 5. `engine/resolver.py` — declarative node inputs

Node inputs are path expressions over a document of `{run, nodes}`:

```python
"inputs": {
    "customer": "$.nodes.fetch_context.output.result",
    "reply":    {"first_of": ["$.nodes.draft_bug_reply.output",
                              "$.nodes.draft_billing_reply.output"]},
    "channel":  {"const": "email"},
}
```

**Why not just let handlers reach into the run.** If a handler fetched its own
upstream data, the data flow would be invisible — you could not answer "what
feeds this node?" without reading Python. As paths, the wiring is inspectable in
the UI, and the resolved values are persisted to `node_runs.input_json`, so the
debugger shows exactly what the node saw.

**`first_of` is what makes the join node branch-agnostic.** `finalize` reads
"whichever draft actually ran" without knowing branches exist.

**Missing paths resolve to `None` rather than raising.** A path into a skipped
node is the *normal* case on a branching workflow, not an error.

The document is built once per resolution context and cached — a node with four
inputs would otherwise rebuild the whole node map four times.

---

## 6. `engine/states.py` — the vocabulary

`NodeStatus`, `RunStatus`, `EdgeState` as `StrEnum`, so they compare and
serialise as plain strings while still being typed at the call site.

The set of node statuses is small but each one earns its place — in particular
`SKIPPED` (branch not taken) and `WAITING_APPROVAL` (parked, not running and not
finished) are distinct from `FAILED`, and the whole scheduler depends on that
distinction.

---

## 7. `engine/scheduling.py` — readiness is a property of edges

The hardest logic in the system is "which nodes may run, which are dead
branches, and is the run finished?" It is therefore **pure functions over
`{node_id: NodeState}`** with no I/O, which makes it directly unit-testable —
`tests/test_scheduling.py` exercises it with hand-built states and no database.

Each incoming edge resolves to:

| Edge state | When |
|---|---|
| `active` | source succeeded, and the edge is unconditional **or** the branch chose this label |
| `pruned` | source was skipped, or the branch chose a different label |
| `blocked` | source **failed** |
| `unresolved` | source hasn't finished |

Then: `join=all` is ready when every edge is active, skipped if any is pruned;
`join=any` is ready on one active edge, skipped only when all are pruned.

### The one decision that makes retry work

**A failed node leaves its successors `pending`, not `skipped`.** That is the
`blocked` state, and it is the reason there is no compensating logic anywhere in
the engine: after a successful retry, the DAG is already in a state where the
rest of the run simply becomes ready. If failure propagated as "skip", a retry
would have to walk the subtree and un-skip it, which is exactly the kind of
bookkeeping that goes wrong.

Asserted directly in `test_failed_node_blocks_rather_than_skips_downstream`.

`determine_run_status` maps the same plan onto a run-level status, with an
explicit "nothing runnable, nothing failed, nodes remain" case that fails the
run rather than hanging silently.

---

## 8. `engine/errors.py` — a taxonomy, not just messages

Every engine error carries a machine-readable `code` and a `retryable` flag.

`retryable` is the interesting field: it separates *the engine should try again
on its own* from *a human must look at this*. It drives the automatic attempt
budget, and it is surfaced in the trace so the UI can tell an operator whether
retrying is likely to help rather than offering a button that will just fail
again.

The specific classes encode real distinctions: `InputValidationError` is not
retryable (the same payload will fail identically), `AgentOutputValidationError`
is (a fresh sample may satisfy the schema), `ToolTransientError` is, and
`ToolPermanentError` is not.

---

## 9. `engine/executor.py` — the engine

Responsibilities: materialise a run, repeatedly ask the scheduler what may run,
execute ready nodes, persist everything, and expose retry/approve/resume.

**The advance loop.** Load state → ask for a plan → mark newly-skipped nodes →
if nothing is ready, finalise the run status → otherwise execute all ready nodes
and loop. Skips are applied in their own pass so cascading skips settle before
anything executes. A bounded guard counter prevents a pathological definition
from spinning forever.

**Concurrency.** Ready nodes are independent by construction, so they run under
a single `asyncio.gather`. `classify` and `fetch_context` genuinely execute in
parallel. Each node gets **its own DB session** — sharing one across concurrent
tasks would be a correctness bug in SQLAlchemy.

**Locking.** All mutations of one run are serialised by a per-run lock. It is
ref-counted (`_LockEntry`) and dropped only when nobody holds *or waits on* it —
the ref count is what prevents two callers ever landing on different lock
objects for the same run, which a naive "delete if unlocked" would allow.

**Attempts.** `max_attempts` is the automatic budget consumed inside one
execution, with exponential backoff; the manual `retry_node` is a separate,
operator-driven path. Both increment the same `attempts` counter so the trace
tells the whole story.

**Failure persistence uses a fresh session.** If a node fails, the working
session may hold partial state; `_persist_failure` opens a new session to write
the terminal status, so a poisoned session cannot lose the fact that the node
failed.

**Retrying an approval node supersedes the old request** rather than re-reading
the decision that failed it — otherwise a retry would instantly re-fail with the
same rejection.

**`resume_run`** additionally resets nodes left `RUNNING` by a crashed process,
which is the crash-recovery story: the in-memory pieces (locks, tasks) are pure
optimisation, and everything needed to continue is in the database.

**`_sanitize_options`** strips `faults` when fault injection is disabled — the
debug hook is gated at the point of entry rather than trusted at the point of
use.

---

## 10. `engine/handlers/` — one handler per node type

A handler is anything with `async execute(ctx) -> NodeResult`. The registry maps
`NodeType → handler`, so adding a node type is a new file plus a registry entry;
the executor never grows a branch.

`ExecutionContext` bundles what a handler may touch (resolved inputs, session,
provider, tool registry, logger, attempt number, fault spec). `NodeResult`
carries the output plus an optional status, which is how the approval node parks
itself without the executor knowing what approval means.

`NodeLogger` buffers trace rows into the node's session, tagged with the attempt.

### 10.1 `input_node.py`

Validates the run payload against a declared contract. Small, but it is the
first gate: an unvalidated payload never reaches an agent. Failure is
non-retryable by design.

### 10.2 `agent_node.py` — the validation gate

1. render a prompt from resolved inputs,
2. ask the provider for JSON,
3. validate against the node's Pydantic contract,
4. on failure, **re-prompt with the validation errors attached** (bounded
   repair loop),
5. only a validated result returns.

Step 3 is the hard guarantee the brief asks for: **a malformed decision can
never reach a tool call.** `test_agent_output_violating_its_contract_fails_the_node`
asserts the side-effect ledger is empty when classification fails.

The repair loop also makes a merely *flaky* provider a non-event — a model that
is wrong once and right on the retry never fails the node.

**Prompt rendering does not use `str.format`.** Templates are author-written
data and routinely contain literal braces (a JSON example in a system prompt).
`format_map` parses those as field specs and either raises or silently corrupts
them. Rendering substitutes only exact `{identifier}` placeholders and leaves
every other brace alone.

### 10.3 `branch_node.py`

Evaluates cases top to bottom, first match wins, falling back to `default`. It
records **every** case and whether it matched into the node output, so the UI
shows why a path was taken, not just which.

In the bundled workflow the confidence gate is listed *before* the category
cases, so an unsure model escalates to a human by construction — see §18.

### 10.4 `tool_node.py` — idempotency

Easy to hand-wave, so it is built around one specific failure: **a node that
dies after its tool already succeeded.**

Every invocation is keyed by `sha256(run_id | node_id | tool | canonical_json(args))`
and recorded in `tool_calls` under a UNIQUE constraint. Arguments are part of the
key, so a retry whose upstream inputs changed correctly re-invokes instead of
replaying a stale result; canonical JSON means key order can't produce a false
miss.

**The commit ordering is the load-bearing part:**

```
insert  status=in_progress  → COMMIT     durable "about to run" marker
call the tool                            the side effect
update  status=succeeded    → COMMIT     durable record of the effect
… anything that fails after this point cannot un-record it
```

Committing the effect *before* the attempt can fail is what makes the guarantee
hold under a later failure, a rollback, or a crash. The `tool_after_side_effect`
fault exists purely to exercise this path.

Three prior states are handled distinctly, because they genuinely differ:

| Prior ledger state | Behaviour |
|---|---|
| `succeeded` | Replay the recorded response, increment `replayed_count`, never re-invoke |
| `failed` | Genuine retry — re-invoke |
| `in_progress` | A crash between "started" and "finished". We *cannot* know whether the effect landed, so we re-invoke and **say so loudly in the trace**. This case is at-least-once, and pretending otherwise would be worse than admitting it |

### 10.5 `approval_node.py`

First execution opens an `ApprovalRequest` and returns `WAITING_APPROVAL`, which
takes the node out of the schedulable set. A decision via the API puts the node
back to `PENDING`; the handler re-runs, finds the decision, and either succeeds
(carrying the reviewer's note and payload downstream) or fails with
`approval_rejected`.

**Why re-run the handler instead of completing the node from the API.** The API
records a decision; the *engine* decides what that means. Keeping the transition
inside the handler means approval follows the same attempt/logging/validation
path as everything else, with no second code path that can drift.

---

## 11. `agents/` — the provider seam

**`provider.py`** defines `LLMProvider` as a `Protocol` with one method. An
agent node never imports a vendor SDK; it builds an `AgentRequest` and hands it
over. That seam is what allows a deterministic provider in tests, a rule-based
one by default, and Claude in production **without the workflow definition
changing**.

**`contracts.py`** holds the Pydantic output contracts and a
`json_schema_for()` that strips keywords the structured-output layer doesn't
accept (`minimum`, `maxLength`, …). Those constraints are still enforced locally
by Pydantic — so the contract stays exactly as strict as it reads, and the
provider merely gets the subset it can honour.

**`mock.py`** is the default: keyword scoring for classification, templates for
drafting, dispatching on the same `task` identifier the Claude provider ignores.

*Why a mock is the default rather than an afterthought.* A reviewer can clone
and run everything with no API key, and every test is reproducible
byte-for-byte. A test suite whose outcome depends on a model's sampling is not a
test suite.

**`claude.py`** uses the Messages API with structured outputs, so the model is
constrained to the same JSON Schema the contract describes. It maps SDK
exceptions onto the engine's taxonomy with correct `retryable` flags, and
handles `stop_reason` values (`refusal`, `max_tokens`) explicitly rather than
letting them surface as confusing parse errors. The engine still re-validates
locally: **a provider is never trusted**, however good its guarantees.

---

## 12. `tools/` — mock externals with observable effects

`ToolSpec` carries a name, description, handler, `required_args` and a
`side_effecting` flag. That flag is not decoration: it marks the tools whose
replay must be prevented, and it is surfaced through `/api/system`.

`SideEffectLedger` is module-level state recording what the mock tools *actually
did*. This is what turns the idempotency claim from an assertion into an
observation — the UI header shows "issues 1 · emails 1", and after a retry it
still shows 1. Tests assert against the same ledger.

The mocks are deliberately dumb but *stateful*, and they raise
`ToolPermanentError` on bad arguments so the taxonomy is exercised end to end.

---

## 13. `config.py` — settings

`pydantic-settings`, every value env-overridable with a working default. Two
choices worth noting:

- **`agent_provider: auto`** resolves to Claude when a key is present and the
  mock otherwise. Zero-config for a reviewer; one env var for real usage; and
  `mock`/`claude` remain available to pin behaviour explicitly.
- **`enable_fault_injection`** exists because `options.faults` lets any API
  client force node failures. That is the entire point of a debugger and a
  liability anywhere else, so it is a switch rather than an assumption.

---

## 14. HTTP layer

**`schemas.py`** — separate response models from ORM models. The wire format is
a deliberate contract, not an accident of the database schema; it also lets the
run-detail endpoint expose derived data that has no table.

**`api/routes.py`** — the endpoints in the README. Two design points:

- **`/api/runs/{id}` returns per-edge state.** The frontend could re-derive
  which branches were pruned, but then the scheduler's logic would exist twice
  in two languages and drift. The server owns the semantics; the UI draws them.
- Engine exceptions map to HTTP: unknown run/node → 404, invalid transition
  (retrying a node that didn't fail) → **409**, not 500. A conflict is a normal
  operator mistake, not a server error.

**`api/deps.py`** — the composition root. Provider selection, tool registry and
workflow registry are assembled in exactly one place and injected into the
engine. Nothing else in the codebase constructs a provider, which is why the
seam in §11 stays honest.

**`main.py`** — lifespan creates the schema and builds the engine once; CORS is
configured for the Vite dev origin.

---

## 15. Frontend

**Why React + Vite and nothing else.** The brief says frontend polish is not the
focus, so the goal was maximum debugger utility with minimum surface: no
component library, no state manager, no graph library. Total dependencies:
`react`, `react-dom`.

**Why a hand-rolled SVG DAG instead of React Flow.** The graph needs to render
*engine semantics* — pruned branches, blocked edges, per-node attempt counts,
selected labels — and layering that onto a generic library's node/edge model is
more work than the ~200-line component it replaces. Layout is a layered
algorithm fed by the server's `rank`, so the frontend never guesses graph
structure. It also keeps the whole bundle at ~51 KB gzipped.

The SVG uses a `viewBox` with `width: 100%` capped at natural size, so the whole
DAG always fits the panel and never scales up on a wide screen.

**Component split:**

| File | Responsibility |
|---|---|
| `App.tsx` | Layout, run/node polling, actions (start, retry, approve) |
| `api.ts` | Thin typed fetch wrapper; surfaces API `detail` messages as errors |
| `types.ts` | Mirrors the response schemas |
| `Graph.tsx` | Layered SVG DAG, node/edge state colouring |
| `NodeInspector.tsx` | Tabbed inspector, retry + approve controls |
| `NewRunForm.tsx` | Scenario presets and the fault dropdown |
| `Json.tsx` | Consistent JSON rendering |

**Why polling rather than SSE/WebSocket.** The run-detail endpoint is already a
complete snapshot, so a 600 ms poll while a run is live is a few lines and no
reconnect logic. SSE is the obvious next step and needs no API change.

**Scenario presets exist for a reason.** A reviewer should be able to reproduce
all five required scenarios from the dropdowns without reading the test suite.

---

## 16. Testing strategy

Four layers, deliberately different in kind:

1. **Pure units** (`test_scheduling.py`, `test_predicates.py`) — the scheduler
   and predicate logic with no database, plus definition-time validation.
2. **Engine scenarios** (`test_branching`, `test_retry`, `test_approval`,
   `test_validation`, `test_idempotency`) — the five required scenarios driven
   through the real engine against a temp SQLite file.
3. **HTTP** (`test_api.py`) — the endpoints the UI actually calls, including
   409 on an invalid transition.
4. **Browser** (`scripts/ui_verify.py`) — real Chrome, real clicks, plus
   DOM-measured layout geometry.

Layer 4 exists because layers 1–3 cannot see whether the graph renders the right
colours, whether Retry appears only on failed nodes, or whether a label is drawn
on top of a node. It found five defects that no type-check or API test could
(§18).

Fault injection is a first-class, run-scoped input rather than test-only
plumbing, which is why the same five scenarios are reproducible from the UI
dropdown *and* from the suite.

---

## 17. Trade-offs and what I'd change for production

**SQLite + a single process.** Runs are advanced by an in-process asyncio task
under a per-run lock. Correct for one process; it does not coordinate across
replicas. For production: move node execution onto a real queue and swap the
lock for `SELECT … FOR UPDATE` on the run row. The engine is already shaped for
this — all state is in the database, `resume_run` recovers nodes left `running`
by a dead worker, and the tool ledger already makes re-delivery safe.

**The UI polls** every 600 ms while a run is live. SSE is the next step.

**Retries are immediate-ish.** `retry_backoff_seconds` applies an exponential
factor but there is no jitter and no dead-letter queue.

**Approval is unauthenticated.** `decided_by` is accepted from the client. Real
deployments need an identity that came from a session, not a request body.

**Nothing is compensating.** A run that fails after a side effect leaves the
effect in place — deliberately, since the ledger makes the retry safe. Genuine
rollback (unsend, delete the issue) needs per-tool compensating actions, a
larger design than this brief calls for.

**One workflow version.** Definitions carry a `version` and runs record it, but
there is no migration path for a run whose definition changed mid-flight. I'd
snapshot the definition into the run row at creation.

**No schema migrations.** Tables are created with `create_all`. A real
deployment needs Alembic before the first schema change.

**The mock tools are in-process.** The side-effect ledger is module-level state,
which is right for a demo and wrong for anything multi-worker.

---

## 18. Notes from the review passes

### Why the confidence gate is a workflow rule, not a prompt instruction

`route` checks `confidence < 0.6` **before** it looks at the predicted category.
An unsure model escalates to a human by construction. The alternative — telling
the model in its system prompt to "say unclear if unsure" — puts a safety
property inside the thing being guarded. The DAG enforces it whatever the model
returns.

### Bugs found by self-review

- **`str.format` in prompt templates** — literal braces in an author-written
  template raise or corrupt. Replaced with explicit placeholder substitution
  (`test_literal_braces_survive_rendering`).
- **Edge identity in the UI** keyed by `source→target`, which collides if a
  branch ever has two labelled edges to the same node. Now keyed on all three.
- **`lazy="selectin"`** issuing a silent extra query on every run fetch.
- **Unbounded lock map** — one `asyncio.Lock` per run retained forever, now
  ref-counted.

### Bugs found by driving the UI in a real browser

- **Stale run on switch.** Starting a new run kept rendering the *previous*
  run's graph and status until the first fetch landed, so a new run briefly
  appeared to have already finished.
- **The DAG didn't fit.** Half the branch nodes were clipped off the panel, and
  clicking a node scrolled the entry node out of view.
- **Edge labels drawn on top of nodes.** Moving the label into the gap wasn't
  enough — a centre-anchored label still spilled back over the box, which only
  showed up once I *measured* the bounding boxes instead of judging a
  scaled-down screenshot. Labels are now start-anchored in a wider gap, and
  `ui_verify.py` asserts zero label/node intersections at three viewport widths
  so it cannot regress.
- **Stale run list** — finished runs still showed `running` in the sidebar.
- **A 404 on every page load** (missing favicon), caught by the console-error
  listener. Cosmetic, but noise in exactly the place you look when something
  real breaks.

Nodes also gained `data-node-id` / `data-status` attributes and `<title>`
tooltips — added so the browser checks could address them, kept for
accessibility.

Tooling: `ruff` (config in `pyproject.toml`) runs clean across `app`, `tests`
and `scripts`; the frontend type-checks under `tsc` with `noUnusedLocals`;
`scripts/ui_verify.py` asserts 65 UI outcomes in a real browser with a clean
console.
