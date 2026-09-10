import { useState } from 'react'
import { CheckCircle, Lightning, Plus, Trash } from '@phosphor-icons/react'
import { createLlmConfig, deleteLlmConfig, testLlmConfig } from '../api'
import { fmtDuration } from '../format'

const EMPTY_FORM = {
  name: '',
  base_url: 'https://api.openai.com/v1',
  model: '',
  api_key: '',
  json_mode: true,
}

export default function ModelConfigPanel({ configs, selectedId, onSelect, onChanged, onBack }) {
  const [form, setForm] = useState(EMPTY_FORM)
  const [notice, setNotice] = useState(null)
  const [error, setError] = useState(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  const [deletingId, setDeletingId] = useState(null)

  function update(key, value) {
    setForm((prev) => ({ ...prev, [key]: value }))
    setNotice(null)
    setError(null)
  }

  async function handleTest() {
    setTesting(true)
    setError(null)
    setNotice(null)
    try {
      const result = await testLlmConfig(form)
      setNotice(`连接成功，耗时 ${fmtDuration(result.latency_ms)}。`)
    } catch (e) {
      setError(e.message)
    } finally {
      setTesting(false)
    }
  }

  async function handleSave(e) {
    e.preventDefault()
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const created = await createLlmConfig(form)
      await onChanged()
      onSelect(created.id)
      setForm(EMPTY_FORM)
      setNotice(`已保存“${created.name}”，新消息会使用它。`)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(config) {
    if (!window.confirm(`删除“${config.name}”后将无法恢复，确定继续吗？`)) return
    setDeletingId(config.id)
    setError(null)
    try {
      await deleteLlmConfig(config.id)
      if (selectedId === config.id) onSelect(null)
      await onChanged()
    } catch (err) {
      setError(err.message)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <main className="min-h-0 flex-1 overflow-y-auto p-4 lg:p-6">
      <div className="mx-auto grid max-w-6xl gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <section className="rise-in rounded-[var(--radius)] border p-5" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="text-sm font-semibold">模型设置</p>
              <p className="mt-1 text-xs leading-relaxed text-[var(--text-secondary)]">
                选择“项目默认”会继续使用现有 GLM 模型链。你也可以添加任意 OpenAI 兼容模型，用自己的 API Key 发起对话请求。
              </p>
            </div>
            <button
              type="button"
              onClick={onBack}
              className="press rounded-lg border px-3 py-1.5 text-xs text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)]"
              style={{ borderColor: 'var(--border-strong)' }}
            >
              返回工作台
            </button>
          </div>

          <div className="mt-5 space-y-2">
            <button
              type="button"
              onClick={() => onSelect(null)}
              className="press flex w-full items-center justify-between rounded-xl border px-4 py-3 text-left transition-colors"
              style={{
                borderColor: selectedId == null ? 'var(--accent)' : 'var(--border)',
                background: selectedId == null ? 'var(--accent-dim)' : 'transparent',
              }}
            >
              <span>
                <span className="block text-sm font-medium">项目默认模型</span>
                <span className="mono mt-0.5 block text-[11px] text-[var(--text-secondary)]">GLM · 可切换速度 / 准确模式</span>
              </span>
              {selectedId == null && <CheckCircle size={18} weight="fill" style={{ color: 'var(--accent)' }} />}
            </button>

            {configs.map((config) => {
              const selected = selectedId === config.id
              return (
                <div
                  key={config.id}
                  className="flex items-center gap-3 rounded-xl border px-4 py-3 transition-colors"
                  style={{
                    borderColor: selected ? 'var(--accent)' : 'var(--border)',
                    background: selected ? 'var(--accent-dim)' : 'transparent',
                  }}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(config.id)}
                    className="press min-w-0 flex-1 text-left"
                    aria-pressed={selected}
                  >
                    <span className="flex items-center gap-2 text-sm font-medium">
                      <span className="truncate">{config.name}</span>
                      {selected && <CheckCircle size={15} weight="fill" style={{ color: 'var(--accent)' }} />}
                    </span>
                    <span className="mono mt-0.5 block truncate text-[11px] text-[var(--text-secondary)]">
                      {config.model} · {config.base_url}
                    </span>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleDelete(config)}
                    disabled={deletingId === config.id}
                    className="press flex shrink-0 items-center gap-1 rounded px-1 py-1 text-xs text-[var(--text-secondary)] transition-colors hover:text-[var(--error)] disabled:opacity-50"
                    title={`删除 ${config.name}`}
                  >
                    <Trash size={14} />
                    删除
                  </button>
                </div>
              )
            })}
          </div>

          {configs.length === 0 && (
            <p className="mt-4 text-xs text-[var(--text-secondary)]">还没有保存的自定义模型。</p>
          )}
        </section>

        <section className="rise-in rounded-[var(--radius)] border p-5" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-2">
            <Plus size={17} style={{ color: 'var(--accent)' }} />
            <h2 className="text-sm font-semibold">添加模型</h2>
          </div>

          <form className="mt-4 space-y-3" onSubmit={handleSave}>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--text-secondary)]">配置名称</span>
              <input required maxLength="64" value={form.name} onChange={(e) => update('name', e.target.value)} placeholder="例如：我的 GPT-4.1" className="w-full rounded-lg border px-3 py-2 text-sm outline-none transition-colors focus:border-[var(--accent)]" style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--text-secondary)]">OpenAI 兼容地址</span>
              <input required type="url" value={form.base_url} onChange={(e) => update('base_url', e.target.value)} placeholder="https://api.openai.com/v1" className="mono w-full rounded-lg border px-3 py-2 text-xs outline-none transition-colors focus:border-[var(--accent)]" style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--text-secondary)]">模型名</span>
              <input required maxLength="128" value={form.model} onChange={(e) => update('model', e.target.value)} placeholder="例如：gpt-4.1-mini" className="mono w-full rounded-lg border px-3 py-2 text-xs outline-none transition-colors focus:border-[var(--accent)]" style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--text-secondary)]">API Key</span>
              <input required type="password" autoComplete="new-password" value={form.api_key} onChange={(e) => update('api_key', e.target.value)} placeholder="只在保存或测试时提交" className="w-full rounded-lg border px-3 py-2 text-sm outline-none transition-colors focus:border-[var(--accent)]" style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
            </label>

            <label className="flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs text-[var(--text-secondary)]" style={{ borderColor: 'var(--border)' }}>
              <input type="checkbox" checked={form.json_mode} onChange={(e) => update('json_mode', e.target.checked)} />
              此模型支持 JSON 模式
            </label>

            <p className="text-[11px] leading-relaxed text-[var(--text-secondary)]">
              密钥由后端加密保存，保存后不会再次显示。连接测试会发送一条极短的请求，可能按服务商规则计费。
            </p>

            {error && <p className="rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'rgba(220,38,38,0.3)', background: 'rgba(220,38,38,0.08)', color: 'var(--error)' }}>{error}</p>}
            {notice && <p className="flex items-center gap-1.5 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'rgba(22,163,74,0.3)', background: 'rgba(22,163,74,0.08)', color: 'var(--success)' }}><Lightning size={13} weight="fill" />{notice}</p>}

            <div className="flex gap-2 pt-1">
              <button type="button" disabled={testing || saving} onClick={handleTest} className="press flex-1 rounded-lg border px-3 py-2 text-xs text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)] disabled:opacity-50" style={{ borderColor: 'var(--border-strong)' }}>
                {testing ? '测试中…' : '测试连接'}
              </button>
              <button type="submit" disabled={testing || saving} className="press flex-1 rounded-lg px-3 py-2 text-xs font-medium transition-opacity disabled:opacity-50" style={{ background: 'var(--accent)', color: '#0a0e14' }}>
                {saving ? '保存中…' : '加密保存'}
              </button>
            </div>
          </form>
        </section>
      </div>
    </main>
  )
}
