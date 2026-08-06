import { useEffect, useState } from 'react'
import { fetchAuditActions, fetchAuditLogs } from '../api'

// 动作名的中文可读映射（describe 用）
const ACTION_LABEL = {
  login: '登录',
  login_failed: '登录失败',
  create_conversation: '创建会话',
  send_message: '发送消息',
  feedback: '提交反馈',
  kb_sync: '知识库同步',
  kb_switch: '蓝绿切换',
  execute_tool: '执行工具',
}

function ActionBadge({ action }) {
  const tone = action === 'login_failed' ? 'var(--error)'
    : action === 'kb_switch' ? 'var(--warn)'
    : 'var(--accent)'
  return (
    <span className="mono rounded px-1.5 py-px text-[10px]" style={{ background: `${tone}1a`, color: tone }}>
      {ACTION_LABEL[action] || action}
    </span>
  )
}

export default function AuditLogPanel() {
  const [items, setItems] = useState([])
  const [actions, setActions] = useState([])
  const [total, setTotal] = useState(0)
  const [actionFilter, setActionFilter] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    setBusy(true)
    setError(null)
    try {
      const data = await fetchAuditLogs({ limit: 100, action: actionFilter || undefined })
      setItems(data.items)
      setTotal(data.total)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { load() }, [actionFilter])

  useEffect(() => {
    fetchAuditActions().then((d) => setActions(d.actions)).catch(() => {})
  }, [])

  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <p className="text-sm font-semibold">审计日志</p>
          <span className="mono rounded px-1.5 py-px text-[10px]" style={{ background: 'var(--accent-dim)', color: 'var(--accent)' }}>
            {total} 条
          </span>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={actionFilter}
            onChange={(e) => setActionFilter(e.target.value)}
            className="rounded-lg border px-2 py-1 text-xs outline-none"
            style={{ background: 'var(--surface)', borderColor: 'var(--border-strong)' }}
          >
            <option value="">全部动作</option>
            {actions.map((a) => (
              <option key={a} value={a}>{ACTION_LABEL[a] || a}</option>
            ))}
          </select>
          <button
            onClick={load}
            disabled={busy}
            className="press rounded-lg border px-2.5 py-1 text-xs disabled:opacity-50"
            style={{ borderColor: 'var(--border-strong)' }}
          >
            {busy ? '加载中…' : '刷新'}
          </button>
        </div>
      </div>

      {error && (
        <p className="mb-2 rounded-lg px-3 py-2 text-xs" style={{ background: 'rgba(220,38,38,0.08)', color: 'var(--error)' }}>
          {error}
        </p>
      )}

      <div className="space-y-1.5">
        {items.length === 0 && !busy && (
          <p className="py-6 text-center text-xs text-[var(--text-secondary)]">暂无审计记录</p>
        )}
        {items.map((it) => (
          <div key={it.id} className="flex items-start gap-3 rounded-lg border px-3 py-2" style={{ borderColor: 'var(--border)' }}>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="mono text-xs font-medium">{it.user_id}</span>
                <ActionBadge action={it.action} />
                <span className="mono text-[10px] text-[var(--text-secondary)]">{it.ip || '—'}</span>
              </div>
              {it.detail && Object.keys(it.detail).length > 0 && (
                <p className="mono mt-1 break-all text-[10px] text-[var(--text-secondary)]">
                  {JSON.stringify(it.detail)}
                </p>
              )}
            </div>
            <span className="mono shrink-0 text-[10px] text-[var(--text-secondary)]">
              {it.created_at ? new Date(it.created_at).toLocaleString() : ''}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
