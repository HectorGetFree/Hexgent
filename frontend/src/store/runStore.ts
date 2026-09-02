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
  ConversationMessage,
  PermissionMode,
  PermissionRequestView,
  Plan,
  RunFinishedPayload,
  RunResponse,
  SessionResponse,
  SessionEntry,
  ToolView,
} from '../types/api'

const SESSION_STORAGE_KEY = 'hexgent.session-id'
const SESSION_ENTRIES_STORAGE_KEY = 'hexgent.session-entries'
let disconnectActiveStream: (() => void) | null = null
let initializationStarted = false

interface RunWorkspaceState {
  session: SessionResponse | null
  sessionEntries: SessionEntry[]
  run: RunResponse | null
  messages: ConversationMessage[]
  plan: Plan | null
  tools: ToolView[]
  events: AgentEvent[]
  permissions: PermissionRequestView[]
  connectionStatus: ConnectionStatus
  initializing: boolean
  submitting: boolean
  controlPending: 'pause' | 'cancel' | null
  error: string | null
  initialize: (mode?: PermissionMode) => Promise<void>
  createNewSession: () => Promise<void>
  switchSession: (sessionId: string) => Promise<void>
  submitQuery: (query: string) => Promise<void>
  resumeRun: () => Promise<void>
  requestPause: () => Promise<void>
  requestCancel: () => Promise<void>
  answerPermission: (requestId: string, decision: 'allow' | 'deny') => Promise<void>
  applyEvent: (event: AgentEvent) => void
  clearError: () => void
}

/** 读取浏览器保存的会话导航；损坏数据按空列表处理。 */
function storedSessionEntries(): SessionEntry[] {
  try {
    const value = JSON.parse(localStorage.getItem(SESSION_ENTRIES_STORAGE_KEY) ?? '[]')
    return Array.isArray(value) ? value : []
  } catch {
    return []
  }
}

/** 更新会话导航并限制本地列表长度。 */
function rememberSession(session: SessionResponse, title?: string): SessionEntry[] {
  const previous = storedSessionEntries()
  const existing = previous.find((entry) => entry.sessionId === session.session_id)
  const firstQuery = session.messages.find((message) => message.role === 'user')?.content
  const entry: SessionEntry = {
    sessionId: session.session_id,
    title: (title || firstQuery || existing?.title || '新会话').slice(0, 28),
    updatedAt: new Date().toISOString(),
  }
  const entries = [entry, ...previous.filter((item) => item.sessionId !== entry.sessionId)].slice(0, 20)
  localStorage.setItem(SESSION_ENTRIES_STORAGE_KEY, JSON.stringify(entries))
  return entries
}

/** 从未知异常中提取稳定、可展示的错误消息。 */
function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误'
}

/** 为同一 Session 的对话时间线创建一条稳定消息。 */
function conversationMessage(
  role: ConversationMessage['role'],
  content: string,
  runId: string,
  streaming: boolean,
): ConversationMessage {
  return {
    id: `${role}_${crypto.randomUUID()}`,
    role,
    content,
    runId,
    createdAt: new Date().toISOString(),
    streaming,
  }
}

/** 把后端 Transcript 投影转换为前端统一对话消息。 */
export function restoreConversation(session: SessionResponse): ConversationMessage[] {
  return session.messages.map((message) => ({
    id: `history_${message.sequence}`,
    role: message.role,
    content: message.content,
    runId: null,
    createdAt: message.created_at,
    streaming: false,
  }))
}

/** 把文本增量追加到当前 Run 最近一条 Assistant 消息。 */
export function appendTextDelta(
  messages: ConversationMessage[],
  runId: string,
  delta: string,
): ConversationMessage[] {
  let targetIndex = -1
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'assistant' && messages[index].runId === runId) {
      targetIndex = index
      break
    }
  }
  if (targetIndex < 0) {
    return [...messages, conversationMessage('assistant', delta, runId, true)]
  }
  return messages.map((message, index) =>
    index === targetIndex
      ? { ...message, content: message.content + delta, streaming: true }
      : message,
  )
}

/** 将 RunFinished 的 Agent 终态转换为 Web Run 的展示终态。 */
export function terminalStatus(payload: RunFinishedPayload): RunResponse['status'] {
  if (payload.result.status === 'interrupted') return 'paused'
  const knownStatuses: RunResponse['status'][] = [
    'cancelled',
    'completed',
    'blocked',
    'incomplete',
    'limit_reached',
    'failed',
  ]
  return knownStatuses.includes(payload.result.status as RunResponse['status'])
    ? payload.result.status as RunResponse['status']
    : 'failed'
}

/**
 * 把 Run 终态写回对应的 Assistant 消息。
 *
 * SSE 的 run_finished 才是 Pause/Cancel 已安全收束的事实来源；HTTP 202 只代表
 * 控制请求已被接受，因此不能在点击按钮时提前结束加载态。
 */
export function finishAssistantMessages(
  messages: ConversationMessage[],
  runId: string,
  status: RunResponse['status'],
  stopReason: string | null,
): ConversationMessage[] {
  return messages.map((message) =>
    message.role === 'assistant' && message.runId === runId
      ? { ...message, streaming: false, terminalStatus: status, stopReason }
      : message,
  )
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
  sessionEntries: [],
  run: null,
  messages: [],
  plan: null,
  tools: [],
  events: [],
  permissions: [],
  connectionStatus: 'idle',
  initializing: true,
  submitting: false,
  controlPending: null,
  error: null,

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
      const sessionEntries = rememberSession(session)

      // 2. 若刷新时 Run 仍活跃，恢复状态并重新连接支持回放的 SSE。
      let run: RunResponse | null = null
      if (session.active_run_id) {
        run = await getRun(session.active_run_id)
      }
      set({
        session,
        sessionEntries,
        run,
        plan: session.plan,
        messages: restoreConversation(session),
        initializing: false,
      })
      if (run && ['pending', 'running'].includes(run.status)) {
        openRunStream(run.run_id)
      }
    } catch (error) {
      initializationStarted = false
      set({ initializing: false, error: messageOf(error) })
    }
  },

  /** 创建独立 Web Session，并把它加入左侧会话导航。 */
  createNewSession: async () => {
    const mode = get().session?.permission_mode ?? 'default'
    set({ initializing: true, error: null })
    try {
      disconnectActiveStream?.()
      disconnectActiveStream = null
      const session = await createSession(mode)
      localStorage.setItem(SESSION_STORAGE_KEY, session.session_id)
      set({
        session,
        sessionEntries: rememberSession(session),
        run: null,
        messages: [],
        plan: null,
        tools: [],
        events: [],
        permissions: [],
        connectionStatus: 'idle',
        controlPending: null,
        initializing: false,
      })
    } catch (error) {
      set({ initializing: false, error: messageOf(error) })
    }
  },

  /** 切换到浏览器记录的 Web Session；失效条目从导航中移除。 */
  switchSession: async (sessionId) => {
    if (sessionId === get().session?.session_id) return
    set({ initializing: true, error: null })
    try {
      const session = await getSession(sessionId)
      let run: RunResponse | null = null
      if (session.active_run_id) run = await getRun(session.active_run_id)
      disconnectActiveStream?.()
      disconnectActiveStream = null
      localStorage.setItem(SESSION_STORAGE_KEY, session.session_id)
      set({
        session,
        sessionEntries: rememberSession(session),
        run,
        messages: restoreConversation(session),
        plan: session.plan,
        tools: [],
        events: [],
        permissions: [],
        controlPending: null,
        connectionStatus: 'idle',
        initializing: false,
      })
      if (run && ['pending', 'running'].includes(run.status)) openRunStream(run.run_id)
    } catch (error) {
      const entries = storedSessionEntries().filter((entry) => entry.sessionId !== sessionId)
      localStorage.setItem(SESSION_ENTRIES_STORAGE_KEY, JSON.stringify(entries))
      set({ sessionEntries: entries, initializing: false, error: messageOf(error) })
    }
  },

  /** 无活跃 Run 时创建新 Run；运行中则把 Query 作为 Steering 提交。 */
  submitQuery: async (query) => {
    const { session, run } = get()
    if (!session || !query.trim()) return
    set({ submitting: true, error: null })
    try {
      if (run && ['pending', 'running'].includes(run.status)) {
        await steerRun(run.run_id, query)
        set((state) => ({
          messages: [
            ...finishAssistantMessages(state.messages, run.run_id, 'incomplete', 'steered'),
            conversationMessage('user', query, run.run_id, false),
            conversationMessage('assistant', '', run.run_id, true),
          ],
        }))
      } else {
        // 暂停后输入新目标表示放弃旧 Plan，再以原始 Query 开始新 Run。
        if (run?.status === 'paused') {
          await cancelRun(run.run_id)
          set({ plan: null })
        }
        // 新 Run 清理 Run 级工具和事件，但保留同一 Session 的完整对话时间线。
        const nextRun = await createRun(session.session_id, query)
        set((state) => ({
          run: nextRun,
          messages: [
            ...state.messages,
            conversationMessage('user', query, nextRun.run_id, false),
            conversationMessage('assistant', '', nextRun.run_id, true),
          ],
          tools: [],
          events: [],
          permissions: [],
          connectionStatus: 'connecting',
        }))
        openRunStream(nextRun.run_id)
      }
      set((state) => ({
        sessionEntries: state.session
          ? rememberSession(
              state.session,
              state.messages.filter((message) => message.role === 'user').length <= 1
                ? query
                : undefined,
            )
          : state.sessionEntries,
      }))
    } catch (error) {
      set({ error: messageOf(error) })
    } finally {
      set({ submitting: false })
    }
  },

  /** 暂停后不改变目标，以同一 Session、Context 和 Plan 创建恢复 Run。 */
  resumeRun: async () => {
    const session = get().session
    const run = get().run
    if (!session || run?.status !== 'paused') return
    set({ submitting: true, error: null })
    try {
      const nextRun = await createRun(session.session_id, '继续', false)
      set(() => ({
        run: nextRun,
        tools: [],
        events: [],
        permissions: [],
        connectionStatus: 'connecting',
      }))
      openRunStream(nextRun.run_id)
    } catch (error) {
      set({ error: messageOf(error) })
    } finally {
      set({ submitting: false })
    }
  },

  /** 点击后立即进入页面暂停态，Core 继续在后台收束到安全边界。 */
  requestPause: async () => {
    const run = get().run
    if (!run || !['pending', 'running'].includes(run.status)) return
    set({
      run: { ...run, status: 'paused' },
      controlPending: 'pause',
      error: null,
      messages: finishAssistantMessages(get().messages, run.run_id, 'paused', 'user_pause'),
    })
    try {
      await pauseRun(run.run_id)
    } catch (error) {
      set({ run, controlPending: null, error: messageOf(error) })
    }
  },

  /** 请求 Cancel；最终 cancelled 状态仍等待 RunFinishedEvent。 */
  requestCancel: async () => {
    const run = get().run
    if (!run || !['pending', 'running'].includes(run.status)) return
    set({
      controlPending: 'cancel',
      error: null,
    })
    try {
      await cancelRun(run.run_id)
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
      if (!(state.controlPending === 'pause' && state.run?.status === 'paused')) {
        patch.messages = appendTextDelta(state.messages, event.run_id, payload.delta)
      }
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
      const status = terminalStatus(payload)
      patch.run = { ...state.run, status }
      patch.controlPending = null
      patch.connectionStatus = 'closed'
      patch.messages = finishAssistantMessages(
        state.messages,
        event.run_id,
        status,
        payload.result.stop_reason,
      )
      // Cancel 会在 Agent 安全边界清除活动 Plan；先按终态事件清理本地投影，
      // 随后的 Session 刷新再与后端最终状态核对，避免侧栏继续展示旧 pending 步骤。
      if (status === 'cancelled') patch.plan = null
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
