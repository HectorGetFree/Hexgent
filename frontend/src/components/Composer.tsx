import { ArrowUp, LoaderCircle, Pause, Play } from 'lucide-react'
import type { KeyboardEvent } from 'react'

import type { RunStatus } from '../types/api'

interface ComposerProps {
  runStatus?: RunStatus
  submitting: boolean
  controlPending: 'pause' | 'cancel' | null
  query: string
  onQueryChange: (query: string) => void
  onSubmit: (query: string) => Promise<void>
  onPause: () => Promise<void>
  onResume: () => Promise<void>
}

/** 使用单一主按钮表达发送、运行中停止和暂停后继续三种状态。 */
export function Composer({
  runStatus,
  submitting,
  controlPending,
  query,
  onQueryChange,
  onSubmit,
  onPause,
  onResume,
}: ComposerProps) {
  const isActive = runStatus === 'pending' || runStatus === 'running'
  const hasInput = Boolean(query.trim())
  const canSteer = isActive && hasInput
  const isPaused = runStatus === 'paused'

  /** 提交输入框中的原始 Query；空输入由主按钮状态单独处理。 */
  async function submit(): Promise<void> {
    const content = query.trim()
    if (!content || submitting) return
    await onSubmit(content)
    onQueryChange('')
  }

  /** 主按钮在运行中用于 Pause，暂停后用于继续，输入内容始终优先提交。 */
  async function handlePrimaryAction(): Promise<void> {
    // 输入内容始终优先解释为提交；只有运行中且输入为空时，主按钮才表示 Cancel。
    if (hasInput) {
      await submit()
      return
    }
    if (isActive) {
      await onPause()
      return
    }
    if (isPaused) {
      await onResume()
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
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            isActive
              ? '输入新要求，让当前生成让位；在途工具收束后继续…'
              : isPaused
                ? '输入补充要求，或直接点击继续…'
                : '给 Hexgent 发送消息'
          }
          rows={3}
        />
        <div className="composer-actions">
          <span className="composer-hint">
            {isActive
              ? '可随时输入新要求调整方向'
              : isPaused
                ? '已暂停'
                : '⌘ Enter 发送'}
          </span>
          <button
            className={`icon-button primary-control ${isActive ? 'is-running' : ''}`}
            disabled={primaryDisabled}
            aria-label={canSteer ? '发送 Steering' : hasInput ? '发送消息' : isActive ? '暂停当前运行' : isPaused ? '继续运行' : '发送消息'}
            title={canSteer ? '发送 Steering' : hasInput ? '发送' : isActive ? '暂停并保留 Context 与 Plan' : isPaused ? '继续' : '发送'}
            onClick={() => void handlePrimaryAction()}
          >
            {submitting ? (
              <LoaderCircle className="spin" size={20} />
            ) : hasInput ? (
              <ArrowUp size={20} />
            ) : controlPending !== null ? (
              <LoaderCircle className="spin" size={20} />
            ) : isActive ? (
              <Pause size={18} fill="currentColor" />
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
