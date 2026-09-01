import { MessageSquare, Plus } from 'lucide-react'

import type { SessionEntry, SessionResponse } from '../types/api'

interface SessionRailProps {
  session: SessionResponse
  entries: SessionEntry[]
  onCreate: () => Promise<void>
  onSwitch: (sessionId: string) => Promise<void>
}

/** 展示浏览器访问过的真实 Web Session，并提供新建与切换入口。 */
export function SessionRail({ session, entries, onCreate, onSwitch }: SessionRailProps) {
  return (
    <aside className="session-rail">
      <div className="rail-heading"><span className="eyebrow">SESSIONS</span><h2>会话</h2></div>
      <button className="new-session-button" onClick={() => void onCreate()}>
        <Plus size={16} /><span>新建会话</span>
      </button>
      <nav className="session-list" aria-label="会话列表">
        {entries.map((entry) => (
          <button
            key={entry.sessionId}
            className={entry.sessionId === session.session_id ? 'active' : ''}
            onClick={() => void onSwitch(entry.sessionId)}
          >
            <MessageSquare size={15} />
            <span>{entry.title}</span>
          </button>
        ))}
      </nav>
    </aside>
  )
}
