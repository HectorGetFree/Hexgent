import { Clock3, FolderGit2, KeyRound, MessageSquareText } from 'lucide-react'

import type { SessionResponse } from '../types/api'

interface SessionRailProps {
  session: SessionResponse
}

/** 展示当前 Session 的稳定身份、工作区和最近 Run。 */
export function SessionRail({ session }: SessionRailProps) {
  return (
    <aside className="session-rail">
      <div className="rail-heading">
        <span className="eyebrow">CURRENT SESSION</span>
        <h2>工作会话</h2>
      </div>

      <dl className="session-facts">
        <div>
          <dt><FolderGit2 size={14} /> 工作区</dt>
          <dd title={session.workdir}>{session.workdir.split('/').at(-1)}</dd>
        </div>
        <div>
          <dt><KeyRound size={14} /> 权限模式</dt>
          <dd>{session.permission_mode}</dd>
        </div>
        <div>
          <dt><MessageSquareText size={14} /> Session</dt>
          <dd title={session.session_id}>{session.session_id.slice(-10)}</dd>
        </div>
      </dl>

      <div className="recent-runs">
        <div className="section-caption"><Clock3 size={13} /> 最近运行</div>
        {session.recent_run_ids.length === 0 ? (
          <p className="empty-copy">还没有运行记录</p>
        ) : (
          <ol>
            {session.recent_run_ids.slice(0, 6).map((runId, index) => (
              <li key={runId}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <code title={runId}>{runId.slice(-9)}</code>
              </li>
            ))}
          </ol>
        )}
      </div>

      <div className="rail-note">
        <span className="rail-note-dot" />
        Context、Plan 与 Transcript 在同一 Session 内持续复用。
      </div>
    </aside>
  )
}
