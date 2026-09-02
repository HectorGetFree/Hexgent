import { Check, Circle, ListChecks, LoaderCircle } from 'lucide-react'

import type { AgentEvent, Plan } from '../types/api'

interface InspectorProps {
  plan: Plan | null
  events: AgentEvent[]
  controlPending: 'pause' | 'cancel' | null
}

const eventLabels: Record<string, string> = {
  text_delta: '模型文本',
  tool_started: '工具开始',
  tool_execution_completed: '工具执行完成',
  tool_finished: '工具结果提交',
  plan_created: 'Plan 创建',
  plan_updated: 'Plan 更新',
  context_compaction_started: '上下文整理开始',
  context_compaction_finished: '上下文整理完成',
  context_compaction_failed: '上下文整理失败',
  loop_limit_reached: '循环限制',
  run_finished: 'Run 结束',
  permission_requested: '等待权限',
  worker_failed: 'Worker 异常',
}

/** 以紧凑侧栏展示 Plan、工具执行和关键事件时间线。 */
export function Inspector({ plan, events, controlPending }: InspectorProps) {
  const visibleEvents = events.filter((event) => event.type !== 'text_delta').slice(-8).reverse()
  return (
    <aside className="inspector">
      <section className="inspector-section">
        <div className="inspector-title">
          <ListChecks size={15} /><span>执行计划</span>
          {controlPending ? <em>{controlPending === 'pause' ? '暂停中' : '取消中'}</em> : plan && <em>R{plan.revision}</em>}
        </div>
        {!plan ? <p className="empty-copy">复杂任务创建 Plan 后会显示在这里</p> : (
          <div className="plan-card">
            <h3>{plan.objective}</h3>
            <ol>
              {plan.steps.map((step) => (
                <li key={step.step_id} className={`plan-step ${step.status}`}>
                  <span className="step-icon">
                    {step.status === 'completed' ? <Check size={12} /> : step.status === 'in_progress' && !controlPending ? <LoaderCircle size={13} /> : <Circle size={10} />}
                  </span>
                  <span>{step.content}</span>
                </li>
              ))}
            </ol>
            {plan.blocked_reason && <p className="blocked-reason">{plan.blocked_reason}</p>}
          </div>
        )}
      </section>

      <section className="inspector-section timeline-section">
        <div className="inspector-title"><span>事件时间线</span><em>{events.length}</em></div>
        {visibleEvents.length === 0 ? <p className="empty-copy">Run 事件会按 sequence 到达</p> : (
          <ol className="event-list">
            {visibleEvents.map((event) => (
              <li key={event.sequence}>
                <span>{String(event.sequence).padStart(2, '0')}</span>
                <div><strong>{eventLabels[event.type] ?? event.type}</strong><small>{new Date(event.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</small></div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </aside>
  )
}
