import { useState } from 'react'
import type { NodeDetail } from '../types'
import { Json } from './Json'

interface Props {
  node: NodeDetail | null
  loading: boolean
  busy: boolean
  onRetry: (nodeId: string) => void
  onApprove: (nodeId: string, decision: 'approve' | 'reject', note: string) => void
}

type Tab = 'overview' | 'input' | 'output' | 'logs' | 'tools' | 'config'

export function NodeInspector({ node, loading, busy, onRetry, onApprove }: Props) {
  const [tab, setTab] = useState<Tab>('overview')
  const [note, setNote] = useState('')

  if (!node) {
    return (
      <aside className="panel inspector empty-state">
        <h2>Node inspector</h2>
        <p className="hint">Select a node in the graph to inspect its state, IO and trace.</p>
      </aside>
    )
  }

  const tabs: Tab[] = ['overview', 'input', 'output', 'logs', 'tools', 'config']
  const awaitingApproval = node.status === 'waiting_approval'

  return (
    <aside className="panel inspector">
      <header className="inspector-head">
        <div>
          <h2>{node.title}</h2>
          <div className="hint mono">
            {node.node_id} · {node.node_type}
          </div>
        </div>
        <span className={`pill pill-${node.status}`}>{node.status.replace('_', ' ')}</span>
      </header>

      {node.status === 'failed' && (
        <div className="alert alert-error">
          <strong>{node.error_code}</strong>
          <div>{node.error}</div>
          <button
            className="primary"
            disabled={busy}
            onClick={() => onRetry(node.node_id)}
          >
            Retry this node
          </button>
        </div>
      )}

      {awaitingApproval && node.approval && (
        <div className="alert alert-approval">
          <strong>Human approval required</strong>
          <div>{node.approval.prompt}</div>
          <textarea
            rows={2}
            placeholder="Optional note (passed to the downstream draft)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="row">
            <button
              className="primary"
              disabled={busy}
              onClick={() => onApprove(node.node_id, 'approve', note)}
            >
              Approve
            </button>
            <button
              className="danger"
              disabled={busy}
              onClick={() => onApprove(node.node_id, 'reject', note)}
            >
              Reject
            </button>
          </div>
        </div>
      )}

      <nav className="tabs">
        {tabs.map((t) => (
          <button
            key={t}
            className={t === tab ? 'tab active' : 'tab'}
            onClick={() => setTab(t)}
          >
            {t}
            {t === 'logs' && node.logs.length ? ` (${node.logs.length})` : ''}
            {t === 'tools' && node.tool_calls.length ? ` (${node.tool_calls.length})` : ''}
          </button>
        ))}
      </nav>

      <div className="inspector-body">
        {loading && <div className="hint">Refreshing…</div>}

        {tab === 'overview' && (
          <dl className="facts">
            <div>
              <dt>Attempts</dt>
              <dd>
                {node.attempts} / {node.max_attempts} automatic
              </dd>
            </div>
            <div>
              <dt>Duration</dt>
              <dd>{node.duration_ms ? `${Math.round(node.duration_ms)} ms` : '—'}</dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd className="mono">{fmt(node.started_at)}</dd>
            </div>
            <div>
              <dt>Finished</dt>
              <dd className="mono">{fmt(node.finished_at)}</dd>
            </div>
            {node.selected_labels?.length ? (
              <div>
                <dt>Branch taken</dt>
                <dd>{node.selected_labels.join(', ')}</dd>
              </div>
            ) : null}
            {node.description ? (
              <div className="wide">
                <dt>Purpose</dt>
                <dd>{node.description}</dd>
              </div>
            ) : null}
            {node.approval ? (
              <div className="wide">
                <dt>Approval</dt>
                <dd>
                  {node.approval.status}
                  {node.approval.decided_by ? ` by ${node.approval.decided_by}` : ''}
                  {node.approval.note ? ` — “${node.approval.note}”` : ''}
                </dd>
              </div>
            ) : null}
          </dl>
        )}

        {tab === 'input' && <Json value={node.input} />}
        {tab === 'output' && <Json value={node.output} />}

        {tab === 'logs' && (
          <ol className="logs">
            {node.logs.map((log) => (
              <li key={log.id} className={`log log-${log.level}`}>
                <div className="log-head">
                  <span className={`level level-${log.level}`}>{log.level}</span>
                  <span className="mono hint">attempt {log.attempt}</span>
                  <span className="mono hint">{fmt(log.created_at)}</span>
                </div>
                <div className="log-msg">{log.message}</div>
                {log.payload && (
                  <details>
                    <summary>payload</summary>
                    <pre className="json">{JSON.stringify(log.payload, null, 2)}</pre>
                  </details>
                )}
              </li>
            ))}
            {!node.logs.length && <li className="hint">No trace yet.</li>}
          </ol>
        )}

        {tab === 'tools' && (
          <div>
            {node.tool_calls.map((call) => (
              <div key={call.id} className="toolcall">
                <div className="toolcall-head">
                  <strong>{call.tool_name}</strong>
                  <span className={`pill pill-${call.status}`}>{call.status}</span>
                </div>
                <div className="hint mono">key {call.idempotency_key.slice(0, 20)}…</div>
                <div className="hint">
                  invoked once · replayed {call.replayed_count}×
                </div>
                <Json value={call.request} label="request" />
                <Json value={call.response} label="response" />
                {call.error && <div className="alert alert-error">{call.error}</div>}
              </div>
            ))}
            {!node.tool_calls.length && (
              <div className="hint">This node makes no tool calls.</div>
            )}
          </div>
        )}

        {tab === 'config' && <Json value={node.config} />}
      </div>
    </aside>
  )
}

function fmt(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleTimeString([], {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}
