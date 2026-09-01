import { describe, expect, it } from 'vitest'

import type { AgentEvent, ConversationMessage, RunFinishedPayload, SessionResponse, ToolView } from '../types/api'
import { appendTextDelta, finishAssistantMessages, restoreConversation, terminalStatus, updateTools } from './runStore'

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
  it('把多个 Run 的文本保存在同一 Session 对话时间线中', () => {
    const firstRun = appendTextDelta([], 'run_1', '第一轮回答')
    const secondRun = appendTextDelta(firstRun, 'run_2', '第二轮回答')
    expect(secondRun.map((message) => message.content)).toEqual([
      '第一轮回答',
      '第二轮回答',
    ])
  })

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

  it('只在 run_finished 后结束对应 Assistant 消息的加载态', () => {
    const messages: ConversationMessage[] = [{
      id: 'assistant_1',
      role: 'assistant',
      content: '',
      runId: 'run_1',
      createdAt: '2026-09-01T15:00:00+08:00',
      streaming: true,
    }]
    expect(finishAssistantMessages(messages, 'run_1', 'paused', 'user_pause')).toEqual([{
      ...messages[0],
      streaming: false,
      terminalStatus: 'paused',
      stopReason: 'user_pause',
    }])
  })

  it('把 Session Transcript 历史恢复为稳定的前端消息', () => {
    const session = {
      session_id: 'session_1',
      workdir: '/workspace',
      permission_mode: 'default',
      active_run_id: null,
      recent_run_ids: [],
      plan: null,
      messages: [{
        sequence: 7,
        role: 'assistant',
        content: '历史回答',
        created_at: '2026-09-01T10:00:00+08:00',
      }],
    } satisfies SessionResponse
    expect(restoreConversation(session)).toEqual([{
      id: 'history_7',
      role: 'assistant',
      content: '历史回答',
      runId: null,
      createdAt: '2026-09-01T10:00:00+08:00',
      streaming: false,
    }])
  })
})
