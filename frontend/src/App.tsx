import { AlertTriangle, X } from 'lucide-react'
import { useEffect } from 'react'

import { Composer } from './components/Composer'
import { Header } from './components/Header'
import { Inspector } from './components/Inspector'
import { OutputPanel } from './components/OutputPanel'
import { PermissionDialog } from './components/PermissionDialog'
import { SessionRail } from './components/SessionRail'
import { useRunStore } from './store/runStore'

/** 组合 Session、Run 输出、控制区和运行时检查器的工作台页面。 */
export default function App() {
  const store = useRunStore()

  useEffect(() => {
    void store.initialize()
    // initialize 是稳定的 Zustand action，页面首次挂载时只恢复一次 Session。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (store.initializing) {
    return (
      <main className="boot-screen">
        <span className="boot-mark">H</span>
        <p>正在准备 Agent Workspace…</p>
      </main>
    )
  }

  return (
    <div className="app-shell">
      <Header runStatus={store.run?.status} connectionStatus={store.connectionStatus} />
      {store.error && (
        <div className="error-banner" role="alert">
          <AlertTriangle size={16} /><span>{store.error}</span>
          <button aria-label="关闭错误" onClick={store.clearError}><X size={15} /></button>
        </div>
      )}
      {store.notice && <div className="notice-banner">{store.notice}</div>}
      {store.session ? (
        <div className="workspace-grid">
          <SessionRail session={store.session} />
          <main className="conversation-pane">
            <div className="conversation-scroll">
              <OutputPanel outputText={store.outputText} isRunning={store.run?.status === 'running'} />
            </div>
            <Composer
              runStatus={store.run?.status}
              submitting={store.submitting}
              controlPending={store.controlPending}
              onSubmit={store.submitQuery}
              onPause={store.requestPause}
              onCancel={store.requestCancel}
            />
          </main>
          <Inspector plan={store.plan} tools={store.tools} events={store.events} />
        </div>
      ) : (
        <main className="fatal-state">无法创建 Session，请确认后端已经启动。</main>
      )}
      <PermissionDialog permission={store.permissions[0]} onAnswer={store.answerPermission} />
    </div>
  )
}
