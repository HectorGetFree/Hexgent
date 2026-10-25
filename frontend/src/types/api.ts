/** Hexgent Web API 在浏览器侧使用的稳定协议类型。 */

export type PermissionMode = 'default' | 'accept_edits' | 'plan' | 'dont_ask'
export type RunStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'cancelled'
  | 'completed'
  | 'blocked'
  | 'incomplete'
  | 'limit_reached'
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
  messages: SessionHistoryMessage[]
}

export interface SessionHistoryMessage {
  sequence: number
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

export interface SessionEntry {
  sessionId: string
  title: string
  updatedAt: string
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

export interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  runId: string | null
  createdAt: string
  streaming: boolean
  responseId?: string
  /** Assistant 消息对应 Run 的最终状态；运行中保持为空。 */
  terminalStatus?: RunStatus | null
  /** Agent 主动停止时提供的机器可读原因，例如 user_pause。 */
  stopReason?: string | null
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
  'steering_received',
  'steering_applied',
] as const

export type AgentEventType = (typeof AGENT_EVENT_TYPES)[number]

export type ConnectionStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'closed'
