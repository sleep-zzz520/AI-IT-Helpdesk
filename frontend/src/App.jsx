import { useEffect, useRef, useState } from 'react'
import { createConversation, sendMessageStream } from './api'
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
  // 会话代际：开新会话 +1，旧会话进行中的 SSE 请求完成后检测到代际过期就丢弃结果，
  // 避免旧回复污染新会话（异步竞态：用户回复期间开新会话）
  const genRef = useRef(0)
  const abortRef = useRef(null)  // 当前进行中 SSE 请求的 AbortController

  async function handleNew() {
    genRef.current += 1           // 旧会话的进行中请求从此失效
    abortRef.current?.abort()     // 真正断开旧 SSE 连接（后端边跑边落库，已完成节点保留）
    abortRef.current = null
    setSending(false)             // 旧加载动画立即消失
    setError(null)
    setMessages([])
    setTraces([])
    const c = await createConversation(USER_ID)
    setConv(c)
  }
  // 模型链模式：fast（速度）/ accurate（能力优先，默认），localStorage 记住选择
  const [mode, setMode] = useState(() => {
    try {
      const saved = localStorage.getItem('glm_mode')
      return saved === 'fast' || saved === 'accurate' ? saved : 'accurate'  // 脏值兜底
    } catch {
      return 'accurate'  // 隐私模式/存储被禁时兜底
    }
  })

  useEffect(() => { handleNew() }, [])  // 首屏自动开新会话（演示友好）

  function switchMode(m) {
    setMode(m)
    try {
      localStorage.setItem('glm_mode', m)
    } catch {
      /* 隐私模式/存储被禁：切换本次会话内生效即可，不持久化 */
    }
  }

  async function handleSend(content, image) {
    if (!conv || sending) return
    const myGen = genRef.current   // 记录本次请求所属的会话代际
    setSending(true)
    setError(null)
    // 乐观更新：用户气泡【立即】显示（含截图），不等 Agent 跑完
    setMessages((prev) => [...prev, { role: 'user', content, image }])
    // 预置"执行中"占位节点：首轮必经 intent∥extract，立即给用户"正在干活"的反馈
    setTraces((prev) => [...prev, { node: 'intent', pending: true }, { node: 'extract', pending: true }])
    // 可取消的 SSE 请求：开新会话时 abort（释放连接，旧数据不落地到 UI）
    const controller = new AbortController()
    abortRef.current = controller
    try {
      // SSE 流式：Agent 每完成一个节点，右侧执行链路实时点亮（可观测性核心卖点）
      const payload = await sendMessageStream(conv.id, content, image, mode, (ev) => {
        if (genRef.current !== myGen) return  // 已开新会话：丢弃旧节点的实时更新
        setTraces((prev) => {
          // 真实节点替换同名的 pending 占位，其余保留追加
          const incoming = new Set(ev.traces.map((t) => t.node))
          const rest = prev.filter((t) => !(t.pending && incoming.has(t.node)))
          return [...rest, ...ev.traces]
        })
      }, controller.signal)
      // done 后检查：期间是否开了新会话（异步竞态核心防护）
      if (genRef.current !== myGen) return
      // 追加 assistant 回复（保留本地图片气泡）+ 全量 trace 覆盖 + 工单状态
      setMessages((prev) => {
        const fresh = payload.messages.slice(prev.length)
        return fresh.length ? [...prev, ...fresh] : prev
      })
      setTraces(payload.traces)
      if (payload.conversation) setConv(payload.conversation)
    } catch (e) {
      if (genRef.current !== myGen) return  // 旧代请求（abort/过期）：错误不污染新会话
      // 失败：保留用户消息（确实发过），错误条提示 Agent 未响应，移除"执行中"占位
      setError(e.message)
      setTraces((prev) => prev.filter((t) => !t.pending))
    } finally {
      if (genRef.current === myGen) setSending(false)  // 只有当前代才能改 sending
      if (abortRef.current === controller) abortRef.current = null
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* 顶栏：标题 + 会话信息 + 新会话 */}
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-4 lg:px-6" style={{ borderColor: 'var(--border)', background: 'var(--surface)' }}>
        <div className="flex items-center gap-3">
          <span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent)' }} />
          <h1 className="text-sm font-semibold tracking-tight">智能IT运维服务台</h1>
          {/* 版本徽章：SSE 实时版（v2 = 含异步竞态修复，排查时确认浏览器加载新代码） */}
          <span
            className="mono rounded px-1.5 py-0.5 text-[10px] font-semibold"
            style={{ background: 'var(--accent)', color: '#0a0e14' }}
          >
            SSE v2
          </span>
          {conv && (
            <span className="mono hidden text-xs text-[var(--text-secondary)] sm:inline">
              #{conv.id} · {conv.user_id}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* 模型模式切换：速度（glm-4-flash 快）/ 准确（能力优先） */}
          <div className="flex rounded-[10px] border p-0.5 text-xs" style={{ borderColor: 'var(--border-strong)' }}>
            {[['fast', '⚡ 速度'], ['accurate', '🎯 准确']].map(([key, label]) => (
              <button
                key={key}
                onClick={() => switchMode(key)}
                className="press rounded-lg px-2.5 py-1 transition-colors"
                style={{
                  background: mode === key ? 'var(--accent)' : 'transparent',
                  color: mode === key ? '#0a0e14' : 'var(--text-secondary)',
                }}
                title={key === 'fast' ? '速度优先：glm-4-flash 打头，快但能力弱' : '能力优先：glm-4.7-flash 打头，最准'}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            onClick={handleNew}
            className="press rounded-lg border px-3 py-1.5 text-xs text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)]"
            style={{ borderColor: 'var(--border-strong)' }}
          >
            新会话
          </button>
        </div>
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
