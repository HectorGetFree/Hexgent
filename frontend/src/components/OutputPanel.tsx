import { Bot, Sparkles } from 'lucide-react'
import { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import type { AgentEvent, ConversationMessage } from '../types/api'

interface OutputPanelProps {
  messages: ConversationMessage[]
  events?: AgentEvent[]
  controlPending?: 'pause' | 'cancel' | null
}

/** 把公开运行事件转换成终端灰色信息在 Web 中的等价表达。 */
function activityLabel(event: AgentEvent): string | null {
  const payload = event.payload as Record<string, unknown>
  if (event.type === 'tool_started') return `开始处理工具：${String(payload.description ?? payload.tool_name ?? '')}`
  if (event.type === 'tool_finished') return `工具结束：${String(payload.tool_name ?? '')}，状态：${String(payload.status ?? '')}`
  if (event.type === 'tool_batch_started') return `开始并行处理 ${String(payload.tool_count ?? '')} 个工具`
  if (event.type === 'tool_batch_finished') return `并行工具批次完成：成功 ${String(payload.succeeded_count ?? 0)}，失败 ${String(payload.failed_count ?? 0)}`
  if (event.type === 'plan_created') return '已创建执行计划'
  if (event.type === 'plan_updated') {
    const plan = payload.plan as { steps?: Array<{ status: string; process_message: string }> } | undefined
    return plan?.steps?.find((step) => step.status === 'in_progress')?.process_message ?? '已更新计划进度'
  }
  if (event.type === 'context_compaction_started') return '正在整理较早的对话上下文'
  if (event.type === 'context_compaction_finished') return `上下文整理完成：约 ${String(payload.before_tokens ?? 0)} → ${String(payload.after_tokens ?? 0)} Token`
  if (event.type === 'context_compaction_failed') return String(payload.message ?? '上下文整理失败')
  if (event.type === 'loop_limit_reached') return `运行已达到限制：${String(payload.reason ?? '')}`
  return null
}

/** 为没有文本输出的终态 Run 提供明确反馈，避免继续伪装成加载中。 */
function emptyAssistantLabel(message: ConversationMessage): string {
  if (message.terminalStatus === 'paused') return '本次运行已暂停，Context 与 Plan 已保留。'
  if (message.terminalStatus === 'cancelled') return '本次运行已取消。'
  if (message.terminalStatus === 'blocked') return '本次运行已阻塞，需要补充信息后继续。'
  if (message.terminalStatus === 'incomplete') return '本次运行尚未完成，可以继续提交要求。'
  if (message.terminalStatus === 'limit_reached') return '本次运行达到资源限制后已安全收束。'
  if (message.terminalStatus === 'failed') return '本次运行失败，未产生文本输出。'
  return '本次运行已结束，未产生文本输出。'
}

/** 按同一 Session 的时间顺序连续展示用户 Query 和 Agent 回答。 */
export function OutputPanel({ messages, events = [], controlPending = null }: OutputPanelProps) {
  const endRef = useRef<HTMLDivElement>(null)
  const activities = events.map(activityLabel).filter((label): label is string => Boolean(label)).slice(-16)
  const latestAssistantId = [...messages].reverse().find((message) => message.role === 'assistant')?.id

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  if (messages.length === 0) {
    return (
      <section className="empty-state">
        <div className="empty-orbit"><Sparkles size={26} /></div>
        <h1>有什么可以帮忙的？</h1>
        <p>输入目标后，回答、工具执行和 Plan 进度会实时显示在同一个会话中。</p>
      </section>
    )
  }

  return (
    <section className="conversation-thread" aria-label="当前会话消息">
      {messages.map((message) => (
        <article key={message.id} className={`message-row ${message.role}`}>
          {message.role === 'assistant' && (
            <span className="message-avatar" aria-hidden="true"><Bot size={18} /></span>
          )}
          <div className="message-content">
            {message.id === latestAssistantId && activities.length > 0 && (
              <div className="activity-stream" aria-label="运行过程">
                {activities.map((activity, index) => <p key={`${index}_${activity}`}>{activity}</p>)}
              </div>
            )}
            {message.role === 'user' ? (
              <p>{message.content}</p>
            ) : message.content ? (
              <div className="markdown-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            ) : message.streaming ? (
              <div className="thinking-state">
                <span className="spinner" />
                <span>
                  {controlPending === 'pause'
                    ? '正在暂停，等待当前操作安全收束'
                    : controlPending === 'cancel'
                      ? '正在取消，等待当前操作安全收束'
                      : 'Hexgent 正在处理'}
                </span>
              </div>
            ) : (
              <div className={`run-stop-state ${message.terminalStatus ?? 'completed'}`}>
                {emptyAssistantLabel(message)}
              </div>
            )}
          </div>
        </article>
      ))}
      <div ref={endRef} aria-hidden="true" />
    </section>
  )
}
