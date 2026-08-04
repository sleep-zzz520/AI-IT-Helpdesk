import { useEffect, useRef, useState } from 'react'
import { Clock, Image as ImageIcon, X } from '@phosphor-icons/react'
import { fmtDuration } from '../format'

// 压缩图片：手机截图常 2-5MB，视觉接口有大小限制，必须先压缩（Canvas）
function compressImage(file, maxSize = 1280, quality = 0.8) {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      const scale = Math.min(1, maxSize / Math.max(img.width, img.height))
      const canvas = document.createElement('canvas')
      canvas.width = Math.round(img.width * scale)
      canvas.height = Math.round(img.height * scale)
      canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height)
      // 统一转 jpeg（MIME 由 data URL 自带，后端直接用）
      resolve(canvas.toDataURL('image/jpeg', quality))
    }
    img.onerror = reject
    img.src = URL.createObjectURL(file)
  })
}

export default function ChatPanel({ messages, sending, error, onSend }) {
  const [draft, setDraft] = useState('')
  // 待发送截图：{dataUrl, name}（压缩后的完整 data URL）
  const [image, setImage] = useState(null)
  const bottomRef = useRef(null)
  const fileRef = useRef(null)

  // 新消息自动滚到底
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages, sending])

  // 选择文件 → 压缩 → data URL
  async function handleFile(e) {
    const f = e.target.files?.[0]
    if (!f) return
    try {
      const dataUrl = await compressImage(f)
      setImage({ dataUrl, name: f.name })
    } catch {
      setImage(null)
    }
    e.target.value = '' // 允许重复选择同一文件
  }

  // 粘贴图片（截图后 Cmd+V 直接贴，像微信一样）
  function handlePaste(e) {
    const items = e.clipboardData?.items || []
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault() // 有图才拦截，否则文本粘贴会被误拦
        const file = item.getAsFile()
        if (file) {
          compressImage(file).then(
            (dataUrl) => setImage({ dataUrl, name: '粘贴的截图' }),
            () => setImage(null),
          )
        }
        return
      }
    }
    // 没有图片：走默认行为（正常粘贴文本）
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
                {/* Agent 本轮回复的总耗时（毫秒 → 可读格式） */}
                {m.elapsed_ms != null && (
                  <div
                    className="mono mt-1.5 flex items-center gap-1 border-t pt-1.5 text-[10px] text-[var(--text-secondary)]"
                    style={{ borderColor: 'var(--border)' }}
                    title="节点并行执行（intent ∥ extract），总耗时不等于各阶段耗时之和"
                  >
                    <Clock size={11} weight="regular" />
                    总耗时 {fmtDuration(m.elapsed_ms)}
                  </div>
                )}
              </div>
            </div>
          ) : (
            <div key={i} className="rise-in flex justify-end">
              <div
                className="max-w-[78%] rounded-[12px] border px-4 py-3 text-sm leading-relaxed"
                style={{ background: 'var(--accent-dim)', borderColor: 'rgba(45,212,167,0.3)' }}
              >
                {/* 用户上传的截图（本地 data URL，当前会话可见） */}
                {m.image && (
                  <img
                    src={m.image}
                    alt="用户上传的截图"
                    className="mb-2 max-h-52 rounded-lg object-contain"
                  />
                )}
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
            onPaste={handlePaste}
            placeholder={image ? '补充说明（可选）' : '描述你的问题，如：VPN 连不上…（截图可直接 Cmd+V 粘贴）'}
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
