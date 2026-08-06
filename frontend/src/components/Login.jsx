import { useState } from 'react'
import { login, setToken } from '../api'

// 演示账号提示（多租户隔离演示用）
const DEMO_ACCOUNTS = [
  { u: 'admin', p: 'Admin@2025', label: '管理员 · Acme 集团', hint: '可看审计 / 管理知识库' },
  { u: 'zhangsan', p: 'Zhangsan@2025', label: '张三 · Acme 集团', hint: '普通用户' },
  { u: 'lisi', p: 'Lisi@2025', label: '李四 · Globex 科技', hint: '普通用户（另一租户）' },
]

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const { token, user } = await login(username.trim(), password)
      setToken(token)
      onLogin(user)  // 通知 App 进入工作台
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4" style={{ background: 'var(--bg)' }}>
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full" style={{ background: 'var(--accent)' }} />
          <h1 className="text-lg font-semibold tracking-tight">智能IT运维服务台</h1>
        </div>
        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-[14px] border p-6"
          style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}
        >
          <div>
            <label className="mb-1 block text-xs text-[var(--text-secondary)]">用户名</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="w-full rounded-[10px] border px-3 py-2 text-sm outline-none transition-colors focus:border-[var(--accent)]"
              style={{ background: 'var(--surface-2)', borderColor: 'var(--border-strong)' }}
              placeholder="admin / zhangsan / lisi"
              autoComplete="username"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-[var(--text-secondary)]">密码</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-[10px] border px-3 py-2 text-sm outline-none transition-colors focus:border-[var(--accent)]"
              style={{ background: 'var(--surface-2)', borderColor: 'var(--border-strong)' }}
              placeholder="••••••••"
              autoComplete="current-password"
            />
          </div>
          {error && (
            <p className="rounded-lg px-3 py-2 text-xs" style={{ background: 'rgba(220,38,38,0.08)', color: 'var(--error)' }}>
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || !username || !password}
            className="press w-full rounded-[10px] py-2.5 text-sm font-medium transition-opacity disabled:opacity-40"
            style={{ background: 'var(--accent)', color: '#0a0e14' }}
          >
            {busy ? '登录中…' : '登录'}
          </button>
        </form>

        {/* 演示账号提示：一键填充（多租户隔离演示 = 换账号登录看数据隔离） */}
        <div className="mt-4 rounded-[12px] border p-3" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
          <p className="mb-2 text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--text-secondary)]">
            演示账号（点击填充）
          </p>
          <div className="space-y-1.5">
            {DEMO_ACCOUNTS.map((a) => (
              <button
                key={a.u}
                type="button"
                onClick={() => { setUsername(a.u); setPassword(a.p); setError(null) }}
                className="press flex w-full items-center justify-between rounded-lg border px-3 py-1.5 text-xs transition-colors hover:border-[var(--accent)]"
                style={{ borderColor: 'var(--border-strong)' }}
              >
                <span className="mono">{a.u}</span>
                <span className="text-[var(--text-secondary)]">{a.label} · {a.hint}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
