import type {
  EdgeState,
  EdgeStateOut,
  NodeRun,
  NodeStatus,
  Workflow,
} from '../types'

const NODE_W = 160
const NODE_H = 62
// Wide enough for a branch label to sit in the gap without touching either box.
const GAP_X = 62
const GAP_Y = 22
const PAD = 20
const TITLE_CHARS = 21
const LABEL_INSET = 7

const TYPE_GLYPH: Record<string, string> = {
  input: '⇥',
  agent: '✦',
  branch: '⑂',
  tool: '⚙',
  approval: '☑',
}

const ELLIPSIS = '…'
const DOT = '·'
const ARROW = '→'

interface Props {
  workflow: Workflow
  /** Empty when no run is selected - the DAG still renders, all pending. */
  nodes: NodeRun[]
  edges: EdgeStateOut[]
  selected: string | null
  onSelect: (nodeId: string) => void
}

/** Edges are identified by all three fields: a branch can have several edges
 *  to the same target under different labels. */
const edgeKey = (source: string, target: string, label: string | null) =>
  `${source} ${target} ${label ?? ''}`

interface Placed {
  id: string
  x: number
  y: number
}

function layout(workflow: Workflow): { placed: Map<string, Placed>; w: number; h: number } {
  const byRank = new Map<number, string[]>()
  for (const node of workflow.nodes) {
    const list = byRank.get(node.rank) ?? []
    list.push(node.id)
    byRank.set(node.rank, list)
  }

  const ranks = [...byRank.keys()].sort((a, b) => a - b)
  const tallest = Math.max(...ranks.map((r) => byRank.get(r)!.length))
  const columnH = tallest * NODE_H + (tallest - 1) * GAP_Y

  const placed = new Map<string, Placed>()
  for (const rank of ranks) {
    const ids = byRank.get(rank)!
    const height = ids.length * NODE_H + (ids.length - 1) * GAP_Y
    const offset = (columnH - height) / 2
    ids.forEach((id, i) => {
      placed.set(id, {
        id,
        x: PAD + rank * (NODE_W + GAP_X),
        y: PAD + offset + i * (NODE_H + GAP_Y),
      })
    })
  }

  return {
    placed,
    w: PAD * 2 + ranks.length * NODE_W + (ranks.length - 1) * GAP_X,
    h: PAD * 2 + columnH,
  }
}

function edgePath(a: Placed, b: Placed): string {
  const x1 = a.x + NODE_W
  const y1 = a.y + NODE_H / 2
  const x2 = b.x
  const y2 = b.y + NODE_H / 2
  const dx = Math.max(30, (x2 - x1) / 2)
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`
}

const EDGE_CLASS: Record<EdgeState, string> = {
  active: 'edge-active',
  pruned: 'edge-pruned',
  blocked: 'edge-blocked',
  unresolved: 'edge-idle',
}

export function Graph({ workflow, nodes, edges, selected, onSelect }: Props) {
  const { placed, w, h } = layout(workflow)
  const runState = new Map<string, NodeRun>(nodes.map((n) => [n.node_id, n]))
  const edgeState = new Map<string, EdgeState>(
    edges.map((e) => [edgeKey(e.source, e.target, e.label), e.state]),
  )

  return (
    <div className="graph-scroll">
      {/* viewBox + width:100% so the whole DAG always fits the panel, capped at
          its natural size so it never scales *up* on a wide screen. */}
      <svg
        viewBox={`0 0 ${w} ${h}`}
        width="100%"
        preserveAspectRatio="xMidYMin meet"
        style={{ maxWidth: w, height: 'auto', display: 'block' }}
        className="graph"
        role="img"
        aria-label="workflow graph"
      >
        <defs>
          {(['active', 'pruned', 'blocked', 'idle'] as const).map((kind) => (
            <marker
              key={kind}
              id={`arrow-${kind}`}
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" className={`arrow arrow-${kind}`} />
            </marker>
          ))}
        </defs>

        {workflow.edges.map((edge) => {
          const a = placed.get(edge.source)
          const b = placed.get(edge.target)
          if (!a || !b) return null
          const key = edgeKey(edge.source, edge.target, edge.label)
          const state = edgeState.get(key) ?? 'unresolved'
          const kind = state === 'unresolved' ? 'idle' : state
          // Anchor the label to the START of the gap past the source node.
          // Centring it in the gap (or worse, at the midpoint between node
          // centres) makes half the text land back on top of the source box -
          // measured, not guessed. See geometry assertions in ui_verify.py.
          const gapStart = a.x + NODE_W
          const t = 0.35
          const label = {
            x: gapStart + LABEL_INSET,
            y: a.y + NODE_H / 2 + (b.y - a.y) * t - 6,
          }
          return (
            <g key={key}>
              <path
                d={edgePath(a, b)}
                className={`edge ${EDGE_CLASS[state]}`}
                markerEnd={`url(#arrow-${kind})`}
                fill="none"
              />
              {edge.label && (
                <text
                  x={label.x}
                  y={label.y}
                  textAnchor="start"
                  className={`edge-label ${EDGE_CLASS[state]}`}
                >
                  {edge.label}
                </text>
              )}
            </g>
          )
        })}

        {workflow.nodes.map((def) => {
          const pos = placed.get(def.id)!
          const state = runState.get(def.id)
          const status: NodeStatus = state?.status ?? 'pending'
          const isSelected = selected === def.id
          return (
            <g
              key={def.id}
              transform={`translate(${pos.x}, ${pos.y})`}
              className={`node node-${status} ${isSelected ? 'node-selected' : ''}`}
              data-node-id={def.id}
              data-status={status}
              onClick={() => onSelect(def.id)}
              role="button"
              aria-label={`${def.title} (${status})`}
              tabIndex={0}
              onKeyDown={(e) => e.key === 'Enter' && onSelect(def.id)}
            >
              <rect width={NODE_W} height={NODE_H} rx={9} className="node-box" />
              <rect width={4} height={NODE_H} rx={2} className="node-accent" />
              <title>{`${def.title} (${status})`}</title>
              <text x={14} y={23} className="node-title">
                {def.title.length > TITLE_CHARS
                  ? `${def.title.slice(0, TITLE_CHARS - 1)}${ELLIPSIS}`
                  : def.title}
              </text>
              <text x={14} y={42} className="node-meta">
                {TYPE_GLYPH[def.type] ?? '*'} {def.type}
                {state && state.attempts > 1 ? ` ${DOT} ${state.attempts} attempts` : ''}
              </text>
              <text x={14} y={55} className="node-status">
                {status.replace('_', ' ')}
                {state?.duration_ms ? ` ${DOT} ${Math.round(state.duration_ms)}ms` : ''}
              </text>
              {state?.selected_labels?.length ? (
                <text x={NODE_W - 10} y={23} className="node-badge" textAnchor="end">
                  {ARROW} {state.selected_labels.join(',')}
                </text>
              ) : null}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
