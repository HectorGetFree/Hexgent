/** Hexgent Web API 在浏览器侧使用的稳定协议类型。 */

export type PermissionMode = 'default' | 'accept_edits' | 'plan' | 'dont_ask'
export type RunStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'cancelled'
  | 'completed'
  | 'failed'

export type PlanStepStatus = 'pending' | 'in_progress' | 'completed'
export type PlanStatus = PlanStepStatus | 'blocked'

export interface PlanStep {
  content: string
  process_message: string
  status: PlanStepStatus
  step_id: string
}

export interface Plan {
  objective: string
  status: PlanStatus
  steps: PlanStep[]
  revision: number
  last_replan_reason: string | null
  blocked_reason: string | null
}

export interface SessionResponse {
  session_id: string
  workdir: string
  permission_mode: PermissionMode
  active_run_id: string | null
  recent_run_ids: string[]
  plan: Plan | null
}

export interface RunResponse {
  run_id: string
  session_id: string
  status: RunStatus
  created_at: string
  finished_at: string | null
  error: string | null
}

export interface WebErrorResponse {
  error?: {
    code?: string
    message?: string
    details?: unknown
  }
  detail?: Array<{ msg?: string }> | string
}

export interface ToolView {
  callId: string
  toolName: string
  description: string
  status: string
}

export interface PermissionRequestView {
  requestId: string
  toolName: string
  reason: string
  permissionMode: PermissionMode
}

export interface RunFinishedPayload {
  result: {
    output_text: string
    elapsed_seconds: number
    stop_reason: string | null
    status: string
  }
}

export interface AgentEvent<TPayload = Record<string, unknown>> {
  sequence: number
  type: AgentEventType
  created_at: string
  run_id: string
  payload: TPayload
}

export const AGENT_EVENT_TYPES = [
  'text_delta',
  'tool_started',
  'tool_execution_completed',
  'tool_finished',
  'tool_batch_started',
  'tool_batch_finished',
  'plan_created',
  'plan_updated',
  'context_compaction_started',
  'context_compaction_finished',
  'context_compaction_failed',
  'loop_limit_reached',
  'loop_transition',
  'run_finished',
  'permission_requested',
  'worker_failed',
] as const

export type AgentEventType = (typeof AGENT_EVENT_TYPES)[number]

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'
