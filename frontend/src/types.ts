export type NodeStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'waiting_approval'
  | 'cancelled'

export type RunStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

export type EdgeState = 'active' | 'pruned' | 'blocked' | 'unresolved'

export interface WorkflowNodeDef {
  id: string
  type: string
  title: string
  description: string
  join: string
  max_attempts: number
  rank: number
  config: Record<string, unknown>
}

export interface WorkflowEdgeDef {
  source: string
  target: string
  label: string | null
}

export interface Workflow {
  id: string
  name: string
  description: string
  version: string
  output_node: string
  nodes: WorkflowNodeDef[]
  edges: WorkflowEdgeDef[]
}

export interface NodeRun {
  node_id: string
  node_type: string
  title: string
  status: NodeStatus
  attempts: number
  max_attempts: number
  error: string | null
  error_code: string | null
  selected_labels: string[] | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  has_output: boolean
}

export interface Approval {
  id: string
  node_id: string
  status: string
  prompt: string
  context: Record<string, unknown>
  note: string | null
  payload: Record<string, unknown> | null
  decided_by: string | null
  decided_at: string | null
  created_at: string
}

export interface EdgeStateOut {
  source: string
  target: string
  label: string | null
  state: EdgeState
}

export interface RunSummary {
  id: string
  workflow_id: string
  status: RunStatus
  title: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface RunDetail extends RunSummary {
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  options: Record<string, unknown>
  nodes: NodeRun[]
  edges: EdgeStateOut[]
  approvals: Approval[]
}

export interface LogEntry {
  id: number
  attempt: number
  level: string
  message: string
  payload: Record<string, unknown> | null
  created_at: string
}

export interface ToolCallEntry {
  id: string
  tool_name: string
  idempotency_key: string
  status: string
  attempt: number
  replayed_count: number
  request: Record<string, unknown>
  response: Record<string, unknown> | null
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface NodeDetail extends NodeRun {
  description: string
  config: Record<string, unknown>
  input: Record<string, unknown> | null
  output: Record<string, unknown> | null
  logs: LogEntry[]
  tool_calls: ToolCallEntry[]
  approval: Approval | null
}

export interface SystemInfo {
  version: string
  agent_provider: string
  agent_model: string | null
  tools: { name: string; description: string; side_effecting: boolean }[]
  workflows: string[]
  fault_injection_enabled: boolean
}

export interface SideEffects {
  linear_issues: Record<string, unknown>[]
  sent_emails: Record<string, unknown>[]
  counts: Record<string, number>
}
