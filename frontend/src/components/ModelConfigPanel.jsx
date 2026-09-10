import { useState } from 'react'
import { CheckCircle, Lightning, Trash } from '@phosphor-icons/react'
import { createLlmConfig, deleteLlmConfig, testLlmConfig } from '../api'
import { fmtDuration } from '../format'

const EMPTY_FORM = {
  name: '',
  base_url: 'https://api.openai.com/v1',
  model: '',
  api_key: '',
  json_mode: true,
}

export default function ModelConfigPanel({ configs, selectedId, onSelect, onChanged }) {
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
    <main className="model-settings-workspace min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-6xl">
        <div className="model-workspace-grid">
          <aside className="model-rail rise-in" aria-labelledby="available-models-title">
            <div className="model-rail-heading">
              <h2 id="available-models-title" className="model-section-heading">已保存模型</h2>
            </div>

            <div className="model-choice-list">
              <button
                type="button"
                onClick={() => onSelect(null)}
                className={`model-choice press ${selectedId == null ? 'is-active' : ''}`}
                aria-pressed={selectedId == null}
              >
                <span className="model-choice-copy">
                  <span className="model-choice-title">项目默认模型</span>
                  <span className="model-choice-detail mono">GLM · 速度 / 准确模式</span>
                </span>
                {selectedId == null && <CheckCircle size={18} weight="fill" className="model-choice-check" />}
              </button>

              {configs.map((config) => {
                const selected = selectedId === config.id
                return (
                  <div key={config.id} className={`model-choice model-choice-row ${selected ? 'is-active' : ''}`}>
                    <button
                      type="button"
                      onClick={() => onSelect(config.id)}
                      className="press min-w-0 flex-1 text-left"
                      aria-pressed={selected}
                    >
                      <span className="model-choice-copy">
                        <span className="model-choice-title truncate">{config.name}</span>
                        <span className="model-choice-detail mono truncate">{config.model} · {config.base_url}</span>
                      </span>
                    </button>
                    {selected && <CheckCircle size={16} weight="fill" className="model-choice-check" />}
                    <button
                      type="button"
                      onClick={() => handleDelete(config)}
                      disabled={deletingId === config.id}
                      className="model-delete-action press"
                      aria-label={`删除 ${config.name}`}
                      title={`删除 ${config.name}`}
                    >
                      <Trash size={16} />
                    </button>
                  </div>
                )
              })}
            </div>
          </aside>

          <section className="model-editor rise-in" aria-labelledby="add-model-title">
            <h2 id="add-model-title" className="model-section-heading">添加模型</h2>

            <form className="model-config-form" onSubmit={handleSave}>
              <label className="model-form-field model-form-field-wide">
                <span>配置名称</span>
                <input required maxLength="64" value={form.name} onChange={(e) => update('name', e.target.value)} placeholder="例如：我的 GPT-4.1" />
              </label>
              <label className="model-form-field">
                <span>OpenAI 兼容地址</span>
                <input required type="url" value={form.base_url} onChange={(e) => update('base_url', e.target.value)} placeholder="https://api.openai.com/v1" className="mono" />
              </label>
              <label className="model-form-field">
                <span>模型名</span>
                <input required maxLength="128" value={form.model} onChange={(e) => update('model', e.target.value)} placeholder="例如：gpt-4.1-mini" className="mono" />
              </label>
              <label className="model-form-field model-form-field-wide">
                <span>API Key</span>
                <input required type="password" autoComplete="new-password" value={form.api_key} onChange={(e) => update('api_key', e.target.value)} placeholder="只在保存或测试时提交" />
              </label>

              <label className="model-json-option model-form-field-wide">
                <input type="checkbox" checked={form.json_mode} onChange={(e) => update('json_mode', e.target.checked)} />
                <span>支持 JSON 模式</span>
              </label>

              <p className="model-security-note model-form-field-wide">
                密钥会由后端加密保存，保存后不会再次显示。测试会发送一条短请求，可能按服务商规则计费。
              </p>

              {error && <p className="form-message form-message-error model-form-field-wide" role="alert">{error}</p>}
              {notice && <p className="form-message form-message-success model-form-field-wide" role="status"><Lightning size={14} weight="fill" />{notice}</p>}

              <div className="model-form-actions model-form-field-wide">
                <button type="button" disabled={testing || saving} onClick={handleTest} className="secondary-action press">
                  {testing ? '测试中…' : '测试连接'}
                </button>
                <button type="submit" disabled={testing || saving} className="primary-action press">
                  {saving ? '保存中…' : '加密保存'}
                </button>
              </div>
            </form>
          </section>
        </div>
      </div>
    </main>
  )
}
