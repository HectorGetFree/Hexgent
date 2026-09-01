import { describe, expect, it } from 'vitest'

import type { AgentEvent, RunFinishedPayload, ToolView } from '../types/api'
import { terminalStatus, updateTools } from './runStore'

/** 构造只关注类型和 payload 的最小 AgentEvent。 */
function event(type: AgentEvent['type'], payload: Record<string, unknown>): AgentEvent {
  return {
    sequence: 1,
    type,
    created_at: '2026-08-31T22:00:00+08:00',
    run_id: 'run_test',
    payload,
  }
}

describe('Run Store 事件投影', () => {
  it('根据 call_id 把工具开始和完成收敛到同一条记录', () => {
    const started = updateTools([], event('tool_started', {
      call_id: 'call_1',
      tool_name: 'read_file',
      description: '读取配置文件',
    }))
    const finished = updateTools(started, event('tool_finished', {
      call_id: 'call_1',
      tool_name: 'read_file',
      status: 'success',
    }))
    expect(finished).toEqual<ToolView[]>([{
      callId: 'call_1',
      toolName: 'read_file',
      description: '读取配置文件',
      status: 'success',
    }])
  })

  it.each([
    ['interrupted', 'paused'],
    ['cancelled', 'cancelled'],
    ['failed', 'failed'],
    ['completed', 'completed'],
    ['blocked', 'completed'],
  ])('把 Agent 终态 %s 映射为 Web 状态 %s', (agentStatus, webStatus) => {
    const payload: RunFinishedPayload = {
      result: {
        output_text: '',
        elapsed_seconds: 1,
        stop_reason: null,
        status: agentStatus,
      },
    }
    expect(terminalStatus(payload)).toBe(webStatus)
  })
})
