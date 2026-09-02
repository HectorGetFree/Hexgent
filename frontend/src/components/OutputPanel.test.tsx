import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import type { ConversationMessage } from '../types/api'
import { OutputPanel } from './OutputPanel'

/** 构造指定运行状态下的空 Assistant 消息。 */
function assistantMessage(streaming: boolean): ConversationMessage {
  return {
    id: 'assistant_1',
    role: 'assistant',
    content: '',
    runId: 'run_1',
    createdAt: '2026-09-01T15:00:00+08:00',
    streaming,
    terminalStatus: streaming ? null : 'paused',
    stopReason: streaming ? null : 'user_pause',
  }
}

describe('OutputPanel Assistant 运行状态', () => {
  it('运行中显示正在处理', () => {
    const html = renderToStaticMarkup(<OutputPanel messages={[assistantMessage(true)]} />)
    expect(html).toContain('Hexgent 正在处理')
  })

  it('暂停终态不再显示正在处理', () => {
    const html = renderToStaticMarkup(<OutputPanel messages={[assistantMessage(false)]} />)
    expect(html).toContain('本次运行已暂停')
    expect(html).not.toContain('Hexgent 正在处理')
  })

  it('不向用户展示取消协议的内部收束提示', () => {
    const html = renderToStaticMarkup(
      <OutputPanel messages={[assistantMessage(true)]} />,
    )
    expect(html).not.toContain('正在取消，等待当前操作安全收束')
    expect(html).toContain('Hexgent 正在处理')
  })

  it('在黑色回答前展示灰色运行过程', () => {
    const message = { ...assistantMessage(false), content: '最终结论' }
    const html = renderToStaticMarkup(
      <OutputPanel
        messages={[message]}
        events={[{
          sequence: 1,
          type: 'tool_started',
          created_at: '2026-09-01T16:00:00+08:00',
          run_id: 'run_1',
          payload: { tool_name: 'read_file', description: '读取配置文件' },
        }]}
      />,
    )
    expect(html).toContain('运行过程')
    expect(html).toContain('开始处理工具：读取配置文件')
    expect(html).toContain('最终结论')
  })

  it('用户 Query 支持复制和重新编辑，Agent 回答支持复制', () => {
    const userMessage: ConversationMessage = {
      id: 'user_1',
      role: 'user',
      content: '检查项目',
      runId: 'run_1',
      createdAt: '2026-09-01T15:00:00+08:00',
      streaming: false,
    }
    const html = renderToStaticMarkup(
      <OutputPanel
        messages={[userMessage, { ...assistantMessage(false), content: '检查完成' }]}
        onEditQuery={() => undefined}
      />,
    )
    expect(html.match(/aria-label="复制内容"/g)).toHaveLength(2)
    expect(html).toContain('aria-label="编辑这条 Query"')
  })
})
