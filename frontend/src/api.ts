import type {
  NodeDetail,
  RunDetail,
  RunSummary,
  SideEffects,
  SystemInfo,
  Workflow,
} from './types'

const BASE = '/api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = body.detail ?? detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  system: () => request<SystemInfo>('/system'),
  workflows: () => request<Workflow[]>('/workflows'),
  runs: () => request<RunSummary[]>('/runs'),
  run: (runId: string) => request<RunDetail>(`/runs/${runId}`),
  node: (runId: string, nodeId: string) =>
    request<NodeDetail>(`/runs/${runId}/nodes/${nodeId}`),
  createRun: (body: {
    workflow_id: string
    input: Record<string, unknown>
    options?: Record<string, unknown>
  }) =>
    request<RunSummary>('/runs', { method: 'POST', body: JSON.stringify(body) }),
  retry: (runId: string, nodeId: string) =>
    request<unknown>(`/runs/${runId}/nodes/${nodeId}/retry`, { method: 'POST' }),
  approve: (
    runId: string,
    nodeId: string,
    body: { decision: 'approve' | 'reject'; note?: string; decided_by?: string },
  ) =>
    request<unknown>(`/runs/${runId}/nodes/${nodeId}/approve`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  resume: (runId: string) =>
    request<unknown>(`/runs/${runId}/resume`, { method: 'POST' }),
  sideEffects: () => request<SideEffects>('/side-effects'),
  resetSideEffects: () =>
    request<unknown>('/side-effects/reset', { method: 'POST' }),
}
