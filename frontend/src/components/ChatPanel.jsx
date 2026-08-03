import { useEffect, useRef, useState } from 'react'

export default function ChatPanel({ messages, sending, error, onSend }) {
  const [draft, setDraft] = useState('')
  const bottomRef = useRef(null)
  const inputRef = useRef(null)

  // 新消息自动滚到底
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, sending])

  function handleSubmit(e) {
    e.preventDefault()
    const content = draft.trim()
    if (!content || sending) return
    onSend(content)
    setDraft('')
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 消息流 */}
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-6 lg:px-6">
        {messages.length === 0 && !sending && (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center">
            <p className="text-sm text-[var(--text-primary)]">向 Agent 描述你的 IT 问题</p>
            <p className="mono max-w-[38ch] text-xs leading-relaxed text-[var(--text-secondary)]">
              试试："VPN连不上，报错Error 800，设备是Windows 11"
            </p>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === 'assistant' ? (
            <div key={i} className="rise-in flex justify-start">
              <div
                className="max-w-[78%] rounded-[12px] border px-4 py-3 text-sm leading-relaxed"
                style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}
              >
                {m.content}
              </div>
            </div>
          ) : (
            <div key={i} className="rise-in flex justify-end">
              <div
                className="max-w-[78%] rounded-[12px] border px-4 py-3 text-sm leading-relaxed"
                style={{ background: 'var(--accent-dim)', borderColor: 'rgba(45,212,167,0.3)' }}
              >
                {m.content}
              </div>
            </div>
          ),
        )}

        {/* 发送中：三点脉冲（非转圈） */}
        {sending && (
          <div className="flex justify-start">
            <div className="flex items-center gap-1 rounded-[12px] border px-4 py-3" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
              <span className="typing-dot" /><span className="typing-dot" /><span className="typing-dot" />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* 错误提示（内联，不弹窗） */}
      {error && (
        <div className="mx-4 mb-2 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'rgba(248,113,113,0.3)', background: 'rgba(248,113,113,0.08)', color: 'var(--error)' }}>
          {error}
        </div>
      )}

      {/* 输入区 */}
      <form onSubmit={handleSubmit} className="flex shrink-0 items-end gap-2 border-t p-3 lg:p-4" style={{ borderColor: 'var(--border)' }}>
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="描述你的问题，如：VPN 连不上…"
          disabled={sending}
          className="flex-1 rounded-[12px] border px-4 py-2.5 text-sm outline-none transition-colors placeholder:text-[var(--text-secondary)] disabled:opacity-50"
          style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
        />
        <button
          type="submit"
          disabled={sending || !draft.trim()}
          className="press rounded-[12px] px-5 py-2.5 text-sm font-medium transition-opacity disabled:opacity-40"
          style={{ background: 'var(--accent)', color: '#0a0e14' }}
        >
          发送
        </button>
      </form>
    </div>
  )
}
