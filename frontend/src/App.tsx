import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { Graph } from './components/Graph'
import { Json } from './components/Json'
import { NewRunForm } from './components/NewRunForm'
import { NodeInspector } from './components/NodeInspector'
import type {
  NodeDetail,
  RunDetail,
  RunSummary,
  SideEffects,
  SystemInfo,
  Workflow,
} from './types'

const LIVE_STATUSES = new Set(['pending', 'running'])

export default function App() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runId, setRunId] = useState<string | null>(null)
  const [run, setRun] = useState<RunDetail | null>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [node, setNode] = useState<NodeDetail | null>(null)
  const [effects, setEffects] = useState<SideEffects | null>(null)
  const [busy, setBusy] = useState(false)
  const [nodeLoading, setNodeLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  // --- bootstrap --------------------------------------------------------
  useEffect(() => {
    Promise.all([api.system(), api.workflows(), api.runs()])
      .then(([info, workflows, runList]) => {
        setSystem(info)
        setWorkflow(workflows[0] ?? null)
        setRuns(runList)
        if (runList.length) setRunId(runList[0].id)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  // Switching runs must drop the previous run's detail immediately. Otherwise
  // the graph and status pill keep rendering the *old* run until the first
  // fetch for the new one lands - which reads as "my new run already finished".
  useEffect(() => {
    setRun(null)
    setNode(null)
    setSelectedNode(null)
  }, [runId])

  const refreshEffects = useCallback(() => {
    api.sideEffects().then(setEffects).catch(() => undefined)
  }, [])

  useEffect(refreshEffects, [refreshEffects])

  // --- run + node polling ----------------------------------------------
  const loadRun = useCallback(async (id: string) => {
    const detail = await api.run(id)
    setRun(detail)
    // Keep the sidebar in step. Without this the list keeps whatever status the
    // run had when it was created, so a finished run still reads "running".
    setRuns((prev) =>
      prev.map((r) =>
        r.id === detail.id
          ? { ...r, status: detail.status, error: detail.error, finished_at: detail.finished_at }
          : r,
      ),
    )
    return detail
  }, [])

  const loadNode = useCallback(
    async (id: string, nodeId: string, showSpinner = false) => {
      if (showSpinner) setNodeLoading(true)
      try {
        setNode(await api.node(id, nodeId))
      } finally {
        setNodeLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    if (!runId) return
    let cancelled = false

    const tick = async () => {
      try {
        const detail = await loadRun(runId)
        if (cancelled) return
        if (selectedNode) await loadNode(runId, selectedNode)
        refreshEffects()
        if (LIVE_STATUSES.has(detail.status)) {
          pollRef.current = window.setTimeout(tick, 600)
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (pollRef.current) window.clearTimeout(pollRef.current)
    }
  }, [runId, selectedNode, loadRun, loadNode, refreshEffects])

  // --- actions ----------------------------------------------------------
  const startRun = async (
    input: Record<string, unknown>,
    options: Record<string, unknown>,
  ) => {
    if (!workflow) return
    setBusy(true)
    setError(null)
    try {
      const summary = await api.createRun({
        workflow_id: workflow.id,
        input,
        options,
      })
      setRuns(await api.runs())
      setRunId(summary.id)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const selectNode = (nodeId: string) => {
    setSelectedNode(nodeId)
    if (runId) void loadNode(runId, nodeId, true)
  }

  const retryNode = async (nodeId: string) => {
    if (!runId) return
    setBusy(true)
    setError(null)
    try {
      await api.retry(runId, nodeId)
      await loadRun(runId)
      await loadNode(runId, nodeId, true)
      refreshEffects()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const decide = async (
    nodeId: string,
    decision: 'approve' | 'reject',
    note: string,
  ) => {
    if (!runId) return
    setBusy(true)
    setError(null)
    try {
      await api.approve(runId, nodeId, {
        decision,
        note: note || undefined,
        decided_by: 'debugger-ui',
      })
      await loadRun(runId)
      await loadNode(runId, nodeId, true)
      refreshEffects()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app" data-busy={busy}>
      <header className="topbar">
        <div className="brand">
          <span className="logo">◆</span>
          <div>
            <h1>Agentic Workflow Debugger</h1>
            <div className="hint">
              {workflow?.name ?? 'loading…'} · v{system?.version ?? '—'}
            </div>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`pill pill-provider`}>
            agent: {system?.agent_provider ?? '…'}
            {system?.agent_model ? ` (${system.agent_model})` : ''}
          </span>
          {effects && (
            <span className="pill pill-effects" title="Real side effects performed">
              issues {effects.counts.linear_issues} · emails{' '}
              {effects.counts.sent_emails}
            </span>
          )}
          <button
            className="ghost"
            onClick={async () => {
              await api.resetSideEffects()
              refreshEffects()
            }}
          >
            reset effects
          </button>
        </div>
      </header>

      {error && (
        <div className="alert alert-error global">
          {error}
          <button className="ghost" onClick={() => setError(null)}>
            dismiss
          </button>
        </div>
      )}

      <main className="layout">
        <div className="col-left">
          <NewRunForm
            busy={busy}
            faultInjectionEnabled={system?.fault_injection_enabled ?? true}
            onSubmit={startRun}
          />

          <section className="panel runs">
            <h2>Runs</h2>
            <ul>
              {runs.map((r) => (
                <li
                  key={r.id}
                  className={r.id === runId ? 'run active' : 'run'}
                  onClick={() => setRunId(r.id)}
                >
                  <div className="run-title">{r.title ?? r.id}</div>
                  <div className="run-meta">
                    <span className={`pill pill-${r.status}`}>
                      {r.status.replace('_', ' ')}
                    </span>
                    <span className="mono hint">{r.id.slice(4, 12)}</span>
                  </div>
                </li>
              ))}
              {!runs.length && <li className="hint">No runs yet.</li>}
            </ul>
          </section>
        </div>

        <div className="col-center">
          <section className="panel run-header">
            {run ? (
              <>
                <div>
                  <h2>{run.title ?? run.id}</h2>
                  <div className="hint mono">{run.id}</div>
                </div>
                <div className="run-header-right">
                  <span
                    className={`pill pill-${run.status}`}
                    data-testid="run-status"
                    data-status={run.status}
                    data-run-id={run.id}
                  >
                    {run.status.replace('_', ' ')}
                  </span>
                  {run.status === 'waiting_approval' && (
                    <span className="hint">select the approval node to decide →</span>
                  )}
                </div>
              </>
            ) : (
              <h2>No run selected</h2>
            )}
          </section>

          <section className="panel graph-panel">
            {workflow ? (
              // The DAG renders from the definition alone, so the shape of the
              // workflow is visible before the first run exists.
              <Graph
                workflow={workflow}
                nodes={run?.nodes ?? []}
                edges={run?.edges ?? []}
                selected={selectedNode}
                onSelect={selectNode}
              />
            ) : (
              <div className="hint pad">Loading workflow…</div>
            )}
            <div className="legend">
              {[
                'succeeded',
                'running',
                'failed',
                'waiting_approval',
                'skipped',
                'pending',
              ].map((s) => (
                <span key={s} className="legend-item">
                  <i className={`swatch swatch-${s}`} />
                  {s.replace('_', ' ')}
                </span>
              ))}
            </div>
          </section>

          {run && (
            <section className="panel run-io">
              <div className="run-io-col">
                <h3>Run input</h3>
                <Json value={run.input} />
              </div>
              <div className="run-io-col">
                <h3>Run output</h3>
                {run.error ? (
                  <div className="alert alert-error">{run.error}</div>
                ) : (
                  <Json value={run.output} />
                )}
              </div>
            </section>
          )}
        </div>

        <div className="col-right">
          <NodeInspector
            node={node}
            loading={nodeLoading}
            busy={busy}
            onRetry={retryNode}
            onApprove={decide}
          />
        </div>
      </main>
    </div>
  )
}
