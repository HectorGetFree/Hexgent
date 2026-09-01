import { ShieldAlert } from 'lucide-react'

import type { PermissionRequestView } from '../types/api'

interface PermissionDialogProps {
  permission?: PermissionRequestView
  onAnswer: (requestId: string, decision: 'allow' | 'deny') => Promise<void>
}

/** 以模态层展示当前最早到达的工具权限审批请求。 */
export function PermissionDialog({ permission, onAnswer }: PermissionDialogProps) {
  if (!permission) return null
  return (
    <div className="modal-backdrop" role="presentation">
      <section className="permission-dialog" role="dialog" aria-modal="true" aria-labelledby="permission-title">
        <span className="permission-icon"><ShieldAlert size={22} /></span>
        <span className="eyebrow">PERMISSION REQUIRED</span>
        <h2 id="permission-title">允许执行 {permission.toolName}？</h2>
        <p>{permission.reason}</p>
        <dl>
          <div><dt>权限模式</dt><dd>{permission.permissionMode}</dd></div>
          <div><dt>请求编号</dt><dd>{permission.requestId.slice(-10)}</dd></div>
        </dl>
        <div className="dialog-actions">
          <button className="button ghost" onClick={() => void onAnswer(permission.requestId, 'deny')}>拒绝</button>
          <button className="button primary" onClick={() => void onAnswer(permission.requestId, 'allow')}>允许本次操作</button>
        </div>
      </section>
    </div>
  )
}
