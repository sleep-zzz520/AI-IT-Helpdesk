import { useEffect, useRef, useState } from 'react'
import { Cpu, Database, ShieldCheck, SignOut } from '@phosphor-icons/react'
import { createConversation, sendMessageStream, clearToken, fetchLlmConfigs, fetchMe, getToken } from './api'
import ChatPanel from './components/ChatPanel'
import KbManager from './components/KbManager'
import SidePanel from './components/SidePanel'
import Login from './components/Login'
import AuditLogPanel from './components/AuditLogPanel'
import ModelConfigPanel from './components/ModelConfigPanel'

export default function App() {
  const [user, setUser] = useState(null)       // 当前登录用户（null = 未登录）
  const [conv, setConv] = useState(null)      // 当前会话（工单）
  const [messages, setMessages] = useState([])
  const [traces, setTraces] = useState([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState(null)
  const [lastElapsed, setLastElapsed] = useState(null)  // 本轮 Agent 总耗时（链路区顶部展示）
  const [tab, setTab] = useState('chat')      // 移动端：对话 / 面板切换
  const [view, setView] = useState('chat')    // 视图：chat 工作台 / kb 知识库管理 / audit 审计
  const [llmConfigs, setLlmConfigs] = useState([])
  const [selectedLlmConfigId, setSelectedLlmConfigId] = useState(null)
  // 会话代际：开新会话 +1，旧会话进行中的 SSE 请求完成后检测到代际过期就丢弃结果，
  // 避免旧回复污染新会话（异步竞态：用户回复期间开新会话）
  const genRef = useRef(0)
  const abortRef = useRef(null)  // 当前进行中 SSE 请求的 AbortController
  const isAdmin = user?.role === 'admin'

  // 启动时恢复登录态：有 token 就拉一次 /me 验证有效性（过期则回到登录页）
  useEffect(() => {
    if (!getToken()) return
    fetchMe().then(setUser).catch(() => clearToken())
  }, [])

  useEffect(() => { if (user) handleNew() }, [user])  // 登录成功后自动开新会话

  // 模型配置属于当前用户，登录后再读；退出时清空，避免切账号后沿用旧选择。
  useEffect(() => {
    if (!user) {
      setLlmConfigs([])
      setSelectedLlmConfigId(null)
      return
    }
    refreshLlmConfigs()
  }, [user])

  async function refreshLlmConfigs() {
    try {
      const configs = await fetchLlmConfigs()
      setLlmConfigs(configs)
      setSelectedLlmConfigId((current) => configs.some((item) => item.id === current) ? current : null)
      return configs
    } catch {
      // 配置读取失败不妨碍默认 GLM 模式继续工作；设置页会在用户操作时显示具体错误。
      setLlmConfigs([])
      return []
    }
  }

  async function handleNew() {
    genRef.current += 1           // 旧会话的进行中请求从此失效
    abortRef.current?.abort()     // 真正断开旧 SSE 连接（后端边跑边落库，已完成节点保留）
    abortRef.current = null
    setSending(false)             // 旧加载动画立即消失
    setError(null)
    setLastElapsed(null)
    setMessages([])
    setTraces([])
    const c = await createConversation()
    setConv(c)
  }

  function handleLogout() {
    genRef.current += 1
    abortRef.current?.abort()
    clearToken()
    setUser(null)
    setConv(null)
    setMessages([])
    setTraces([])
    setView('chat')
    setLlmConfigs([])
    setSelectedLlmConfigId(null)
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
      const payload = await sendMessageStream(conv.id, content, image, mode, selectedLlmConfigId, (ev) => {
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
      if (payload.elapsed_ms) setLastElapsed(payload.elapsed_ms)
      if (payload.conversation) setConv(payload.conversation)
    } catch (e) {
      if (e.unauthorized) { handleLogout(); return }  // 登录过期 → 回登录页
      if (genRef.current !== myGen) return  // 旧代请求（abort/过期）：错误不污染新会话
      // 失败：保留用户消息（确实发过），错误条提示 Agent 未响应，移除"执行中"占位
      setError(e.message)
      setTraces((prev) => prev.filter((t) => !t.pending))
    } finally {
      if (genRef.current === myGen) setSending(false)  // 只有当前代才能改 sending
      if (abortRef.current === controller) abortRef.current = null
    }
  }

  // 未登录 → 登录页
  if (!user) {
    return <Login onLogin={setUser} />
  }

  const selectedLlmConfig = llmConfigs.find((item) => item.id === selectedLlmConfigId) || null

  return (
    <div className="flex h-full flex-col">
      <header className="app-topbar">
        <div className="app-topbar-brand">
          <span className="app-brand-mark" aria-hidden="true" />
          <h1 className="truncate text-sm font-semibold tracking-tight">智能IT运维服务台</h1>
          <span className="app-runtime-badge mono">SSE v2</span>
        </div>

        <nav className="app-primary-nav" aria-label="主导航">
          <button
            onClick={() => setView('chat')}
            className={`app-nav-item press ${view === 'chat' ? 'is-active' : ''}`}
            aria-current={view === 'chat' ? 'page' : undefined}
            title="返回对话工作台"
          >
            工作台
          </button>
          <button
            onClick={() => setView('models')}
            className={`app-nav-item press ${view === 'models' ? 'is-active' : ''}`}
            aria-current={view === 'models' ? 'page' : undefined}
            title="添加、测试或切换自己的 OpenAI 兼容模型"
          >
            <Cpu size={14} />
            模型
          </button>
          {isAdmin && (
            <button
              onClick={() => setView('kb')}
              className={`app-nav-item press ${view === 'kb' ? 'is-active' : ''}`}
              aria-current={view === 'kb' ? 'page' : undefined}
              title="知识库台账、蓝绿切换、检索调试和反馈分析"
            >
              <Database size={13} />
              知识库
            </button>
          )}
          {isAdmin && (
            <button
              onClick={() => setView('audit')}
              className={`app-nav-item press ${view === 'audit' ? 'is-active' : ''}`}
              aria-current={view === 'audit' ? 'page' : undefined}
              title="查看安全可追溯的操作记录"
            >
              <ShieldCheck size={13} />
              审计
            </button>
          )}
        </nav>

        <div className="app-topbar-actions">
          {selectedLlmConfig ? (
            <span
              className="model-context mono max-w-36 truncate"
              title={`${selectedLlmConfig.model} · ${selectedLlmConfig.base_url}`}
            >
              {selectedLlmConfig.name}
            </span>
          ) : (
            <div className="mode-control" aria-label="项目默认模型模式">
              {[['fast', '速度'], ['accurate', '准确']].map(([key, label]) => (
                <button
                  key={key}
                  onClick={() => switchMode(key)}
                  className={`press ${mode === key ? 'is-active' : ''}`}
                  title={key === 'fast' ? '速度优先：glm-4-flash 打头，快但能力弱' : '能力优先：glm-4.7-flash 打头，最准'}
                >
                  {label}
                </button>
              ))}
            </div>
          )}

          <span className={`user-context mono ${isAdmin ? 'is-admin' : ''}`} title={`租户 ${user.tenant_id}`}>
            {user.display_name || user.username}
            <span>{isAdmin ? '管理员' : '用户'}</span>
          </span>

          <button onClick={() => { handleNew(); setView('chat') }} className="secondary-action press new-session-action">
            新会话
          </button>
          <button
            onClick={handleLogout}
            className="app-icon-action press"
            aria-label="退出登录"
            title="退出登录"
          >
            <SignOut size={17} />
          </button>
        </div>
      </header>

      {/* 模型设置与管理员页面都使用独立工作区，避免挤压对话布局。 */}
      {view === 'models' ? (
        <ModelConfigPanel
          configs={llmConfigs}
          selectedId={selectedLlmConfigId}
          onSelect={setSelectedLlmConfigId}
          onChanged={refreshLlmConfigs}
        />
      ) : view === 'audit' ? (
        <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
          <AuditLogPanel />
        </main>
      ) : view === 'kb' ? (
        <main className="min-h-0 flex-1">
          <KbManager />
        </main>
      ) : (
      // 移动端 tab 切换 + 主工作台（包 div：三元分支内只能有一个根元素；
      // 注意：括号内不能用 {/* */} 注释——会被解析成空对象字面量，踩过的坑）
      <div className="flex min-h-0 flex-1 flex-col">
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
          <SidePanel conv={conv} traces={traces} elapsedMs={lastElapsed} />
        </aside>
      </main>
      </div>
      )}
    </div>
  )
}
