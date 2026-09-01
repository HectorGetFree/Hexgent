import { ArrowUp, LoaderCircle, Pause, Play, Square } from 'lucide-react'
import { useState, type KeyboardEvent } from 'react'

import type { RunStatus } from '../types/api'

interface ComposerProps {
  runStatus?: RunStatus
  submitting: boolean
  controlPending: 'pause' | 'cancel' | null
  onSubmit: (query: string) => Promise<void>
  onPause: () => Promise<void>
  onCancel: () => Promise<void>
}

/** 使用单一主按钮表达发送、运行中停止和暂停后继续三种状态。 */
export function Composer({
  runStatus,
  submitting,
  controlPending,
  onSubmit,
  onPause,
  onCancel,
}: ComposerProps) {
  const [query, setQuery] = useState('')
  const isActive = runStatus === 'pending' || runStatus === 'running'
  const hasInput = Boolean(query.trim())
  const canSteer = isActive && hasInput
  const isPaused = runStatus === 'paused'

  /** 空闲时提交 Query，暂停后允许用“继续”创建新的恢复 Run。 */
  async function submit(): Promise<void> {
    const content = query.trim() || (isPaused ? '继续' : '')
    if (!content || submitting) return
    await onSubmit(content)
    setQuery('')
  }

  /** 主按钮在运行中用于 Cancel，其他状态用于提交或继续。 */
  async function handlePrimaryAction(): Promise<void> {
    // 输入内容始终优先解释为提交；只有运行中且输入为空时，主按钮才表示 Cancel。
    if (hasInput) {
      await submit()
      return
    }
    if (isActive) {
      await onCancel()
      return
    }
    await submit()
  }

  /** 使用 Command/Ctrl + Enter 提交，同时保留普通 Enter 换行。 */
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && (!isActive || canSteer)) {
      event.preventDefault()
      void submit()
    }
  }

  const primaryDisabled = submitting
    || controlPending !== null
    || (!isActive && !isPaused && !hasInput)

  return (
    <section className="composer-shell">
      <div className="composer">
        <textarea
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            isActive
              ? '输入补充要求，可在完整 Turn 边界调整方向…'
              : isPaused
                ? '输入补充要求，或直接点击继续…'
                : '给 Hexgent 发送消息'
          }
          rows={3}
        />
        <div className="composer-actions">
          <span className="composer-hint">
            {controlPending === 'pause'
              ? '正在暂停，等待当前操作安全收束'
              : controlPending === 'cancel'
                ? '正在取消，等待当前操作安全收束'
                : isActive
                  ? '输入补充要求并发送，即可 Steering 当前 Run'
                  : isPaused
                    ? '当前上下文与 Plan 已保留'
                    : '⌘ Enter 发送'}
          </span>
          {runStatus === 'running' && (
            <button
              className="icon-button secondary-control"
              disabled={controlPending !== null}
              aria-label="暂停当前运行"
              title="暂停并保留 Context 与 Plan"
              onClick={() => void onPause()}
            >
              {controlPending === 'pause' ? <LoaderCircle className="spin" size={18} /> : <Pause size={18} />}
            </button>
          )}
          <button
            className={`icon-button primary-control ${isActive ? 'is-running' : ''}`}
            disabled={primaryDisabled}
            aria-label={canSteer ? '发送 Steering' : hasInput ? '发送消息' : isActive ? '停止当前运行' : isPaused ? '继续运行' : '发送消息'}
            title={canSteer ? '发送 Steering' : hasInput ? '发送' : isActive ? '取消当前 Run' : isPaused ? '继续' : '发送'}
            onClick={() => void handlePrimaryAction()}
          >
            {submitting ? (
              <LoaderCircle className="spin" size={20} />
            ) : hasInput ? (
              <ArrowUp size={20} />
            ) : controlPending === 'cancel' ? (
              <LoaderCircle className="spin" size={20} />
            ) : isActive ? (
              <span className="running-control"><LoaderCircle className="spin" size={23} /><Square size={9} /></span>
            ) : isPaused ? (
              <Play size={18} fill="currentColor" />
            ) : (
              <ArrowUp size={20} />
            )}
          </button>
        </div>
      </div>
    </section>
  )
}
