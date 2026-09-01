import { create } from 'zustand'

import {
  cancelRun,
  createRun,
  createSession,
  decidePermission,
  getRun,
  getSession,
  pauseRun,
  steerRun,
} from '../api/client'
import { connectRunEvents } from '../api/eventStream'
import type {
  AgentEvent,
  ConnectionStatus,
  PermissionMode,
  PermissionRequestView,
  Plan,
  RunFinishedPayload,
  RunResponse,
  SessionResponse,
  ToolView,
} from '../types/api'

const SESSION_STORAGE_KEY = 'hexgent.session-id'
let disconnectActiveStream: (() => void) | null = null
let initializationStarted = false

interface RunWorkspaceState {
  session: SessionResponse | null
  run: RunResponse | null
  outputText: string
  plan: Plan | null
  tools: ToolView[]
  events: AgentEvent[]
  permissions: PermissionRequestView[]
  connectionStatus: ConnectionStatus
  initializing: boolean
  submitting: boolean
  controlPending: 'pause' | 'cancel' | null
  error: string | null
  notice: string | null
  initialize: (mode?: PermissionMode) => Promise<void>
  submitQuery: (query: string) => Promise<void>
  requestPause: () => Promise<void>
  requestCancel: () => Promise<void>
  answerPermission: (requestId: string, decision: 'allow' | 'deny') => Promise<void>
  applyEvent: (event: AgentEvent) => void
  clearError: () => void
}

/** 从未知异常中提取稳定、可展示的错误消息。 */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误'
}

/** 将 RunFinished 的 Agent 终态转换为 Web Run 的展示终态。 */
export function terminalStatus(payload: RunFinishedPayload): RunResponse['status'] {
  if (payload.result.status === 'interrupted') return 'paused'
  if (payload.result.status === 'cancelled') return 'cancelled'
  if (payload.result.status === 'failed') return 'failed'
  return 'completed'
}

/** 根据工具事件更新对应 call_id 的唯一展示记录。 */
export function updateTools(tools: ToolView[], event: AgentEvent): ToolView[] {
  const payload = event.payload as Record<string, string>
  const callId = payload.call_id
  if (!callId) return tools
  const existing = tools.find((tool) => tool.callId === callId)
  if (event.type === 'tool_started') {
    const next: ToolView = {
      callId,
      toolName: payload.tool_name,
      description: payload.description,
      status: 'running',
    }
    return existing
      ? tools.map((tool) => (tool.callId === callId ? next : tool))
      : [...tools, next]
  }
  return tools.map((tool) =>
    tool.callId === callId
      ? { ...tool, status: payload.status ?? tool.status }
      : tool,
  )
}

/** 在 Run 终结后重新读取后端确认状态与 Session Plan。 */
async function refreshTerminalState(runId: string, sessionId: string): Promise<void> {
  const [run, session] = await Promise.all([getRun(runId), getSession(sessionId)])
  useRunStore.setState({ run, session, plan: session.plan, controlPending: null })
}

/** 建立当前 Run 唯一的 SSE 连接，并将外部事件收敛到 Store。 */
function openRunStream(runId: string): void {
  disconnectActiveStream?.()
  disconnectActiveStream = connectRunEvents(runId, {
    onEvent: (event) => useRunStore.getState().applyEvent(event),
    onConnectionChange: (connectionStatus) => useRunStore.setState({ connectionStatus }),
    onError: (error) => useRunStore.setState({ error }),
  })
}

export const useRunStore = create<RunWorkspaceState>((set, get) => ({
  session: null,
  run: null,
  outputText: '',
  plan: null,
  tools: [],
  events: [],
  permissions: [],
  connectionStatus: 'idle',
  initializing: true,
  submitting: false,
  controlPending: null,
  error: null,
  notice: null,

  /** 恢复浏览器记住的 Session；不存在或失效时创建新 Session。 */
  initialize: async (mode = 'default') => {
    // React StrictMode 在开发环境会重复执行 Effect；避免并发创建两个 Web Session。
    if (initializationStarted) return
    initializationStarted = true
    set({ initializing: true, error: null })
    try {
      // 1. 优先恢复同一浏览器最近 Session，避免刷新页面后丢失 Plan 摘要。
      const storedSessionId = localStorage.getItem(SESSION_STORAGE_KEY)
      let session: SessionResponse
      if (storedSessionId) {
        try {
          session = await getSession(storedSessionId)
        } catch {
          localStorage.removeItem(SESSION_STORAGE_KEY)
          session = await createSession(mode)
        }
      } else {
        session = await createSession(mode)
      }
      localStorage.setItem(SESSION_STORAGE_KEY, session.session_id)

      // 2. 若刷新时 Run 仍活跃，恢复状态并重新连接支持回放的 SSE。
      let run: RunResponse | null = null
      if (session.active_run_id) {
        run = await getRun(session.active_run_id)
      }
      set({ session, run, plan: session.plan, initializing: false })
      if (run && ['pending', 'running'].includes(run.status)) {
        openRunStream(run.run_id)
      }
    } catch (error) {
      initializationStarted = false
      set({ initializing: false, error: messageOf(error) })
    }
  },

  /** 无活跃 Run 时创建新 Run；运行中则把 Query 作为 Steering 提交。 */
  submitQuery: async (query) => {
    const { session, run } = get()
    if (!session || !query.trim()) return
    set({ submitting: true, error: null, notice: null })
    try {
      if (run?.status === 'running') {
        await steerRun(run.run_id, query)
        set({ notice: 'Steering 已提交，将在完整 Turn 边界生效。' })
      } else {
        // 新 Run 使用全新展示缓冲，但后端继续复用当前 Session 的 Context 和 Plan。
        const nextRun = await createRun(session.session_id, query)
        set({
          run: nextRun,
          outputText: '',
          tools: [],
          events: [],
          permissions: [],
          connectionStatus: 'connecting',
        })
        openRunStream(nextRun.run_id)
      }
    } catch (error) {
      set({ error: messageOf(error) })
    } finally {
      set({ submitting: false })
    }
  },

  /** 请求 Pause，但不提前把本地状态伪装成 paused。 */
  requestPause: async () => {
    const run = get().run
    if (!run || run.status !== 'running') return
    set({ controlPending: 'pause', error: null })
    try {
      await pauseRun(run.run_id)
      set({ notice: '暂停请求已接受，正在等待 Agent 安全收束。' })
    } catch (error) {
      set({ controlPending: null, error: messageOf(error) })
    }
  },

  /** 请求 Cancel；最终 cancelled 状态仍等待 RunFinishedEvent。 */
  requestCancel: async () => {
    const run = get().run
    if (!run || !['pending', 'running'].includes(run.status)) return
    set({ controlPending: 'cancel', error: null })
    try {
      await cancelRun(run.run_id)
      set({ notice: '取消请求已接受，已完成的副作用不会回滚。' })
    } catch (error) {
      set({ controlPending: null, error: messageOf(error) })
    }
  },

  /** 提交权限决定；后端确认后才从待审批队列移除。 */
  answerPermission: async (requestId, decision) => {
    const run = get().run
    if (!run) return
    try {
      await decidePermission(run.run_id, requestId, decision)
      set((state) => ({
        permissions: state.permissions.filter(
          (permission) => permission.requestId !== requestId,
        ),
      }))
    } catch (error) {
      set({ error: messageOf(error) })
    }
  },

  /** 按 sequence 幂等应用一条 AgentEvent，并更新相应展示投影。 */
  applyEvent: (event) => {
    const state = get()
    if (state.events.some((current) => current.sequence === event.sequence)) return

    // 1. 所有事件先进入时间线；各专用面板只维护自己的最小投影。
    const patch: Partial<RunWorkspaceState> = {
      events: [...state.events, event],
    }
    if (event.type === 'text_delta') {
      const payload = event.payload as { delta: string }
      patch.outputText = state.outputText + payload.delta
    } else if (event.type === 'tool_started' || event.type === 'tool_execution_completed' || event.type === 'tool_finished') {
      patch.tools = updateTools(state.tools, event)
    } else if (event.type === 'plan_created' || event.type === 'plan_updated') {
      patch.plan = (event.payload as { plan: Plan }).plan
    } else if (event.type === 'permission_requested') {
      const payload = event.payload as {
        request_id: string
        tool_name: string
        reason: string
        permission_mode: PermissionMode
      }
      patch.permissions = [
        ...state.permissions,
        {
          requestId: payload.request_id,
          toolName: payload.tool_name,
          reason: payload.reason,
          permissionMode: payload.permission_mode,
        },
      ]
    } else if (event.type === 'worker_failed') {
      patch.error = (event.payload as { message: string }).message
    } else if (event.type === 'run_finished' && state.run && state.session) {
      const payload = event.payload as unknown as RunFinishedPayload
      patch.run = { ...state.run, status: terminalStatus(payload) }
      patch.controlPending = null
      patch.connectionStatus = 'closed'
      // 2. 最终事件发出后主动关闭 EventSource，再读取后端最终投影。
      disconnectActiveStream?.()
      disconnectActiveStream = null
      void refreshTerminalState(state.run.run_id, state.session.session_id).catch(
        (error: unknown) => set({ error: messageOf(error) }),
      )
    }
    set(patch)
  },

  /** 清除当前页面错误提示。 */
  clearError: () => set({ error: null }),
}))
