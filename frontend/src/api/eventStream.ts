import { AGENT_EVENT_TYPES, type AgentEvent, type ConnectionStatus } from '../types/api'
import { API_BASE_URL } from './client'

export interface EventStreamHandlers {
  onEvent: (event: AgentEvent) => void
  onConnectionChange: (status: ConnectionStatus) => void
  onError: (message: string) => void
}

/**
 * 建立 Run 的 SSE 连接并订阅全部命名事件。
 * EventSource 断线重连时会自动携带浏览器记录的 Last-Event-ID。
 */
export function connectRunEvents(
  runId: string,
  handlers: EventStreamHandlers,
): () => void {
  // 1. 每个 Run 创建独立 EventSource，避免旧 Run 的事件串入新 Run。
  handlers.onConnectionChange('connecting')
  const source = new EventSource(`${API_BASE_URL}/api/runs/${runId}/events`)
  let hasConnected = false

  source.onopen = () => {
    hasConnected = true
    handlers.onConnectionChange('connected')
  }

  // 2. 后端使用命名 SSE event，不能只依赖默认 onmessage。
  for (const type of AGENT_EVENT_TYPES) {
    source.addEventListener(type, (message) => {
      try {
        const event = JSON.parse((message as MessageEvent<string>).data) as AgentEvent
        handlers.onEvent(event)
      } catch {
        handlers.onError(`无法解析 ${type} 事件`)
      }
    })
  }

  source.onerror = () => {
    // 3. 浏览器会自动重连；只有显式关闭时状态才应为 closed。
    if (source.readyState === EventSource.CLOSED) {
      handlers.onConnectionChange('closed')
      handlers.onError('事件连接已关闭，请重新加载 Run')
    } else {
      handlers.onConnectionChange(hasConnected ? 'reconnecting' : 'connecting')
    }
  }

  // 4. 返回清理函数，组件切换 Run 或卸载时主动释放长连接。
  return () => {
    source.close()
    handlers.onConnectionChange('closed')
  }
}
