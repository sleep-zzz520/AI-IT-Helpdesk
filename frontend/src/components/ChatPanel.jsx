import { useEffect, useRef, useState } from 'react'
import { Image as ImageIcon, X } from '@phosphor-icons/react'

export default function ChatPanel({ messages, sending, error, onSend }) {
  const [draft, setDraft] = useState('')
  // 待发送截图：{dataUrl, name}（dataUrl 是完整 data URL，自带 MIME 类型）
  const [image, setImage] = useState(null)
  const bottomRef = useRef(null)
  const fileRef = useRef(null)

  // 新消息自动滚到底
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, sending])

  // 选择文件 → 读成 data URL
  function handleFile(e) {
    const f = e.target.files?.[0]
    if (!f) return
    const reader = new FileReader()
    reader.onload = () => setImage({ dataUrl: reader.result, name: f.name })
    reader.readAsDataURL(f)
    e.target.value = '' // 允许重复选择同一文件
  }

  function handleSubmit(e) {
    e.preventDefault()
    const content = draft.trim()
    if ((!content && !image) || sending) return
    onSend(content, image?.dataUrl)
    setDraft('')
    setImage(null)
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
      <form onSubmit={handleSubmit} className="flex shrink-0 flex-col gap-2 border-t p-3 lg:p-4" style={{ borderColor: 'var(--border)' }}>
        {/* 待发送截图预览 */}
        {image && (
          <div className="flex items-center gap-2">
            <img
              src={image.dataUrl}
              alt="待发送截图"
              className="h-14 rounded-lg border object-cover"
              style={{ borderColor: 'var(--border-strong)' }}
            />
            <span className="truncate text-xs text-[var(--text-secondary)]">{image.name}</span>
            <button
              type="button"
              onClick={() => setImage(null)}
              className="press ml-auto rounded-full p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              aria-label="移除截图"
            >
              <X size={14} />
            </button>
          </div>
        )}

        <div className="flex items-end gap-2">
          {/* 隐藏的文件选择 + 图片按钮 */}
          <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={handleFile} />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={sending}
            className="press rounded-[12px] border p-2.5 text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)] disabled:opacity-50"
            style={{ borderColor: 'var(--border)' }}
            title="上传报错截图"
            aria-label="上传截图"
          >
            <ImageIcon size={18} />
          </button>
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={image ? '补充说明（可选）' : '描述你的问题，如：VPN 连不上…'}
            disabled={sending}
            className="flex-1 rounded-[12px] border px-4 py-2.5 text-sm outline-none transition-colors placeholder:text-[var(--text-secondary)] disabled:opacity-50"
            style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
          />
          <button
            type="submit"
            disabled={sending || (!draft.trim() && !image)}
            className="press rounded-[12px] px-5 py-2.5 text-sm font-medium transition-opacity disabled:opacity-40"
            style={{ background: 'var(--accent)', color: '#0a0e14' }}
          >
            发送
          </button>
        </div>
      </form>
    </div>
  )
}
