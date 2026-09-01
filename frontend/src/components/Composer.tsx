import { CornerDownLeft, Pause, Send, Square } from 'lucide-react'
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

/** 提交新 Run 或 Steering，并提供当前 Run 的安全控制入口。 */
export function Composer({
  runStatus,
  submitting,
  controlPending,
  onSubmit,
  onPause,
  onCancel,
}: ComposerProps) {
  const [query, setQuery] = useState('')
  const isRunning = runStatus === 'running'

  /** 发送非空 Query，成功后才清空输入框。 */
  async function submit(): Promise<void> {
    if (!query.trim() || submitting) return
    await onSubmit(query)
    setQuery('')
  }

  /** 使用 Command/Ctrl + Enter 提交，同时保留普通 Enter 换行。 */
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      void submit()
    }
  }

  return (
    <section className="composer-shell">
      <div className="composer-mode">
        <span>{isRunning ? 'STEERING' : 'NEW RUN'}</span>
        <span>{isRunning ? '将在完整 Turn 边界生效' : '复用当前 Session 上下文'}</span>
      </div>
      <div className="composer">
        <textarea
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={isRunning ? '调整当前任务方向…' : '告诉 Hexgent 你想完成什么…'}
          rows={3}
        />
        <div className="composer-actions">
          <span className="keyboard-hint"><CornerDownLeft size={12} /> ⌘ Enter</span>
          {isRunning && (
            <>
              <button className="button ghost" disabled={controlPending !== null} onClick={() => void onPause()}>
                <Pause size={15} /> {controlPending === 'pause' ? '暂停中' : '暂停'}
              </button>
              <button className="button danger-ghost" disabled={controlPending !== null} onClick={() => void onCancel()}>
                <Square size={13} /> {controlPending === 'cancel' ? '取消中' : '取消'}
              </button>
            </>
          )}
          <button className="button primary" disabled={!query.trim() || submitting} onClick={() => void submit()}>
            <Send size={15} /> {submitting ? '提交中' : isRunning ? 'Steer' : '运行'}
          </button>
        </div>
      </div>
    </section>
  )
}
