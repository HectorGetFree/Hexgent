import type {
  PermissionMode,
  RunResponse,
  SessionResponse,
  WebErrorResponse,
} from '../types/api'

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
).replace(/\/$/, '')

/** 将后端 JSON 错误结构转换成适合页面展示的消息。 */
function errorMessage(body: WebErrorResponse | null, status: number): string {
  if (body?.error?.message) {
    return body.error.message
  }
  if (typeof body?.detail === 'string') {
    return body.detail
  }
  if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
    return body.detail[0].msg
  }
  return `请求失败（HTTP ${status}）`
}

/** 发送 JSON 请求，并统一处理空响应、协议错误和业务错误。 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as WebErrorResponse | null
    throw new Error(errorMessage(body, response.status))
  }
  return (await response.json()) as T
}

/** 创建新的 Web Session；工作区由后端白名单决定。 */
export function createSession(permissionMode: PermissionMode): Promise<SessionResponse> {
  return request('/api/sessions', {
    method: 'POST',
    body: JSON.stringify({ permission_mode: permissionMode }),
  })
}

/** 查询 Session 的活跃 Run、历史 Run 和当前 Plan。 */
export function getSession(sessionId: string): Promise<SessionResponse> {
  return request(`/api/sessions/${sessionId}`)
}

/** 创建后台 Agent Run，接口仅等待 Run 登记完成。 */
export function createRun(
  sessionId: string,
  query: string,
  visibleQuery = true,
): Promise<RunResponse> {
  return request(`/api/sessions/${sessionId}/runs`, {
    method: 'POST',
    body: JSON.stringify({ query, visible_query: visibleQuery }),
  })
}

/** 查询后端确认的 Run 状态。 */
export function getRun(runId: string): Promise<RunResponse> {
  return request(`/api/runs/${runId}`)
}

/** 请求活跃 Run 在安全边界暂停。 */
export function pauseRun(runId: string): Promise<unknown> {
  return request(`/api/runs/${runId}/pause`, { method: 'POST' })
}

/** 请求活跃 Run 安全取消，并放弃旧 Plan 的执行义务。 */
export function cancelRun(runId: string): Promise<unknown> {
  return request(`/api/runs/${runId}/cancel`, { method: 'POST' })
}

/** 向运行中 Run 的 Steering 邮箱提交保持原文的 Query。 */
export function steerRun(runId: string, query: string): Promise<unknown> {
  return request(`/api/runs/${runId}/steer`, {
    method: 'POST',
    body: JSON.stringify({ query, mode: 'interrupt', command_id: crypto.randomUUID() }),
  })
}

/** 回答一条工具权限审批请求。 */
export function decidePermission(
  runId: string,
  requestId: string,
  decision: 'allow' | 'deny',
): Promise<unknown> {
  return request(`/api/runs/${runId}/permissions/${requestId}`, {
    method: 'POST',
    body: JSON.stringify({ decision }),
  })
}
