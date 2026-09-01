import { Activity, CircleDot, Hexagon } from 'lucide-react'

import type { ConnectionStatus, RunStatus } from '../types/api'

interface HeaderProps {
  runStatus?: RunStatus
  connectionStatus: ConnectionStatus
}

const statusLabels: Record<RunStatus, string> = {
  pending: '等待执行',
  running: '运行中',
  paused: '已暂停',
  cancelled: '已取消',
  completed: '已完成',
  failed: '失败',
}

/** 展示产品身份、Run 终态和 SSE 连接状态。 */
export function Header({ runStatus, connectionStatus }: HeaderProps) {
  const connected = connectionStatus === 'connected'
  return (
    <header className="topbar">
      <div className="brand-lockup">
        <span className="brand-mark" aria-hidden="true"><Hexagon size={20} /></span>
        <span className="brand-name">HEXGENT</span>
        <span className="brand-divider" />
        <span className="brand-section">Workspace</span>
      </div>
      <div className="topbar-status">
        {runStatus && (
          <span className={`status-chip status-${runStatus}`}>
            <CircleDot size={12} />
            {statusLabels[runStatus]}
          </span>
        )}
        <span className={`connection-state ${connected ? 'is-connected' : ''}`}>
          <Activity size={13} />
          {connectionStatus === 'reconnecting' ? '正在重连' : connected ? '事件流在线' : '事件流空闲'}
        </span>
      </div>
    </header>
  )
}
