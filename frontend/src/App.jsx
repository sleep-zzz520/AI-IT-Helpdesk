import { useEffect, useState } from 'react'
import { createConversation, fetchConversation, fetchTraces, sendMessage } from './api'
import ChatPanel from './components/ChatPanel'
import SidePanel from './components/SidePanel'

const USER_ID = 'zhangsan'

export default function App() {
  const [conv, setConv] = useState(null)      // 当前会话（工单）
  const [messages, setMessages] = useState([])
  const [traces, setTraces] = useState([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const [tab, setTab] = useState('chat')      // 移动端：对话 / 面板切换

  async function refreshConv(id) {
    const [c, t] = await Promise.all([fetchConversation(id), fetchTraces(id)])
    setConv(c)
    setTraces(t)
  }

  async function handleNew() {
    setError(null)
    setMessages([])
    setTraces([])
    const c = await createConversation(USER_ID)
    setConv(c)
  }

  useEffect(() => { handleNew() }, [])  // 首屏自动开新会话（演示友好）

  async function handleSend(content) {
    if (!conv || sending) return
    setSending(true)
    setError(null)
    // 乐观更新：用户气泡【立即】显示，不等 Agent 跑完（几秒延迟会显得卡死）
    setMessages((prev) => [...prev, { role: 'user', content }])
    try {
      const resp = await sendMessage(conv.id, content)
      // 服务器返回的是权威完整历史（含 assistant 回复），整体替换
      setMessages(resp.messages)
      await refreshConv(conv.id)
    } catch (e) {
      // 失败：保留用户消息（确实发过），错误条提示 Agent 未响应
      setError(e.message)
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏：标题 + 会话信息 + 新会话 */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-4 lg:px-6" style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}>
        <div className="flex items-center gap-3">
          <span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent)' }} />
          <h1 className="text-sm font-semibold tracking-tight">智能IT运维服务台</h1>
          {conv && (
            <span className="mono hidden text-xs text-[var(--text-secondary)] sm:inline">
              #{conv.id} · {conv.user_id}
            </span>
          )}
        </div>
        <button
          onClick={handleNew}
          className="press rounded-lg border px-3 py-1.5 text-xs text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)]"
          style={{ borderColor: 'var(--border-strong)' }}
        >
          新会话
        </button>
      </header>

      {/* 移动端 tab 切换 */}
      <nav className="flex border-b lg:hidden" style={{ borderColor: 'var(--border)' }}>
        {[['chat', '对话'], ['panel', '工单 / Trace']].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className="flex-1 px-4 py-2 text-xs transition-colors"
            style={{
              color: tab === key ? 'var(--text-primary)' : 'var(--text-secondary)',
              borderBottom: tab === key ? '2px solid var(--accent)' : '2px solid transparent',
            }}
          >
            {label}
          </button>
        ))}
      </nav>

      {/* 主区：左对话 + 右工单面板（lg 以上双栏） */}
      <main className="min-h-0 flex-1 grid lg:grid-cols-[1fr_360px]">
        <section className={`min-h-0 flex flex-col ${tab === 'chat' ? 'flex' : 'hidden'} lg:flex`}>
          <ChatPanel
            messages={messages}
            sending={sending}
            error={error}
            onSend={handleSend}
          />
        </section>
        <aside className={`min-h-0 overflow-y-auto border-l ${tab === 'panel' ? 'block' : 'hidden'} lg:block`} style={{ borderColor: 'var(--border)' }}>
          <SidePanel conv={conv} traces={traces} />
        </aside>
      </main>
    </div>
  )
}
