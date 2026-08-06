import { useEffect, useState } from 'react'
import {
  ArrowClockwise, CaretDown, CaretRight, MagnifyingGlass, Swap,
} from '@phosphor-icons/react'
import {
  debugKbQuery, fetchFeedbackAnalysis, fetchKbDocuments, fetchKbStats,
  switchKb, syncKb,
} from '../api'

// 过期预警：≤0 已过期（红）/ ≤30 天（黄）/ 其余（正常）
function ExpiryBadge({ days }) {
  if (days == null) return <span className="text-xs text-[var(--text-secondary)]">—</span>
  if (days <= 0) {
    return <span className="rounded px-1.5 py-px text-[10px]" style={{ background: 'rgba(220,38,38,0.12)', color: 'var(--error)' }}>已过期 {Math.abs(days)} 天</span>
  }
  if (days <= 30) {
    return <span className="rounded px-1.5 py-px text-[10px]" style={{ background: 'rgba(234,179,8,0.12)', color: 'var(--warn)' }}>{days} 天后过期</span>
  }
  return <span className="text-xs text-[var(--text-secondary)]">{days} 天后</span>
}

const STATUS_LABEL = { active: '在库', removed: '已删除', inactive: '停用' }
const CATEGORY_LABEL = { missing: '文档缺失', stale: '文档过时', other: '其他' }

// ===== 库状态卡：蓝绿状态 + 同步 + 切换 =====
function StatsCard({ stats, busy, onSync, onSwitch }) {
  if (!stats) return null
  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
          <Stat k="在岗库" v={<span className="mono">{stats.active_collection}</span>} />
          <Stat k="候选库" v={<span className="mono">{stats.candidate_collection}</span>} />
          <Stat k="在岗 chunks" v={stats.active_chunks} />
          <Stat k="候选 chunks" v={stats.candidate_chunks} />
          <Stat k="台账文档" v={stats.doc_count} />
          <Stat k="上次同步" v={stats.last_sync ? new Date(stats.last_sync).toLocaleString() : '—'} />
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={onSync}
            disabled={busy}
            className="press flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs disabled:opacity-50"
            style={{ borderColor: 'var(--border-strong)', color: 'var(--text-primary)' }}
            title="全量写入候选库，不影响线上检索"
          >
            <ArrowClockwise size={13} className={busy ? 'animate-spin' : ''} />
            {busy ? '同步中…' : '同步到候选库'}
          </button>
          <button
            onClick={onSwitch}
            disabled={busy || !stats.candidate_chunks}
            className="press flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium disabled:opacity-40"
            style={{ background: 'var(--accent)', color: '#0a0e14' }}
            title="切换后线上检索立即用候选库；旧库保留，可再点一次回滚"
          >
            <Swap size={13} />
            切换生效
          </button>
        </div>
      </div>
      <p className="mt-3 text-[10px] leading-relaxed text-[var(--text-secondary)]">
        ℹ️ 蓝绿发布：同步只写候选库（线上零中断）→ 验证 → 点「切换生效」（秒级，回滚 = 再点一次）
      </p>
    </div>
  )
}

function Stat({ k, v }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-[var(--text-secondary)]">{k}</span>
      <span className="font-medium text-[var(--text-primary)]">{v}</span>
    </span>
  )
}

// ===== 同步结果报告 =====
function SyncResult({ report }) {
  if (!report) return null
  return (
    <div className="rounded-[12px] border p-4 text-xs leading-relaxed" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <p className="font-medium text-[var(--text-primary)]">同步报告：{report.summary}（{report.elapsed_ms}ms）</p>
      {report.failed?.length > 0 && (
        <ul className="mt-2 space-y-1 text-[var(--error)]">
          {report.failed.map(([p, e]) => <li key={p}>❌ {p}: {e}</li>)}
        </ul>
      )}
      {report.added?.length > 0 && <p className="mt-1 text-[var(--success)]">新增：{report.added.join('、')}</p>}
      {report.updated?.length > 0 && <p className="mt-1" style={{ color: 'var(--accent)' }}>更新：{report.updated.join('、')}</p>}
      {report.deleted?.length > 0 && <p className="mt-1" style={{ color: 'var(--warn)' }}>删除：{report.deleted.join('、')}</p>}
      {report.duplicates?.length > 0 && (
        <p className="mt-1 text-[var(--warn)]">疑似重复：{report.duplicates.join('、')}</p>
      )}
      <p className="mt-2 text-[var(--text-secondary)]">
        已写入候选库（{report.candidate_collection}），在岗仍为（{report.active_collection}）——
        验证后点「切换生效」
      </p>
    </div>
  )
}

// ===== 检索调试：双路召回 + 融合明细 =====
function HitList({ title, hits, color }) {
  if (!hits?.length) return null
  return (
    <div className="mb-3">
      <p className="mono mb-1 text-[10px] uppercase tracking-[0.14em]" style={{ color }}>
        {title}（{hits.length}）
      </p>
      <ol className="space-y-1">
        {hits.map((h, i) => (
          <li key={h.id + i} className="rounded-lg border px-2.5 py-1.5" style={{ borderColor: 'var(--border)', background: 'var(--surface-2)' }}>
            <div className="flex items-center justify-between gap-2 text-[10px]">
              <span className="truncate mono" style={{ color: 'var(--text-secondary)' }}>
                #{i + 1} {h.source_url}
              </span>
              <span className="mono shrink-0" style={{ color }}>score {h.score}</span>
            </div>
            <p className="mt-0.5 text-[11px] text-[var(--text-primary)]">{h.text}</p>
          </li>
        ))}
      </ol>
    </div>
  )
}

function KbDebug() {
  const [query, setQuery] = useState('')
  const [scenario, setScenario] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)

  async function run() {
    if (!query.trim() || loading) return
    setLoading(true); setErr(null)
    try {
      setResult(await debugKbQuery({ query: query.trim(), scenario: scenario || null, top_k: 5 }))
    } catch (e) {
      setErr(e.message); setResult(null)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <p className="mono mb-3 text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">检索调试</p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && run()}
          placeholder="输入问题，如：VPN 证书过期 Error 800 怎么续期"
          className="min-w-0 flex-1 rounded-lg border px-3 py-2 text-sm outline-none placeholder:text-[var(--text-secondary)]"
          style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
        />
        <select
          value={scenario}
          onChange={(e) => setScenario(e.target.value)}
          className="rounded-lg border px-2 py-2 text-xs"
          style={{ background: 'var(--surface-2)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
        >
          <option value="">全部场景</option>
          {['vpn', 'password', 'email', 'software'].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <button
          onClick={run}
          disabled={loading}
          className="press flex items-center gap-1.5 rounded-lg px-3 py-2 text-xs font-medium disabled:opacity-50"
          style={{ background: 'var(--accent)', color: '#0a0e14' }}
        >
          <MagnifyingGlass size={13} />
          检索
        </button>
      </div>

      {err && <p className="mt-2 text-xs text-[var(--error)]">{err}</p>}
      {result && (
        <div className="mt-3">
          <p className="mb-2 text-[11px] text-[var(--text-secondary)]">
            耗时 {result.elapsed_ms}ms · 向量召回 {result.stats.vector_recall} · BM25 召回 {result.stats.bm25_recall}
            {result.stats.reranked ? ' · Rerank 已精排' : ' · Rerank 关闭'}
          </p>
          <HitList title="向量路召回" hits={result.vector_hits} color="var(--accent)" />
          <HitList title="BM25 路召回" hits={result.bm25_hits} color="var(--warn)" />
          <HitList title="RRF 融合最终命中" hits={result.final} color="var(--success)" />
        </div>
      )}
    </div>
  )
}

// ===== 文档台账（体检报告）=====
function DocTable({ docs, onRefresh }) {
  if (!docs) return null
  return (
    <div className="rounded-[12px] border" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <div className="flex items-center justify-between px-4 pt-4">
        <p className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">
          文档台账（{docs.length}）— 源文档与向量库的同步状态
        </p>
        <button onClick={onRefresh} className="press rounded p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]" title="刷新">
          <ArrowClockwise size={12} />
        </button>
      </div>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-[10px] uppercase tracking-wide text-[var(--text-secondary)]" style={{ borderBottom: '1px solid var(--border)' }}>
              <th className="px-4 py-2 font-medium">文档</th>
              <th className="px-2 py-2 font-medium">状态</th>
              <th className="px-2 py-2 font-medium">块数</th>
              <th className="px-2 py-2 font-medium">有效期</th>
              <th className="px-2 py-2 font-medium">最近变更</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id} className="align-top" style={{ borderBottom: '1px solid var(--border)' }}>
                <td className="max-w-[220px] px-4 py-2">
                  <p className="truncate mono text-[11px] text-[var(--text-primary)]" title={d.path}>{d.path}</p>
                  <p className="mt-0.5 text-[10px] text-[var(--text-secondary)]" title={d.changelog}>{d.changelog}</p>
                </td>
                <td className="px-2 py-2">
                  <span className="rounded px-1.5 py-px text-[10px]"
                    style={{ background: d.status === 'active' ? 'var(--accent-dim)' : 'var(--surface-2)', color: d.status === 'active' ? 'var(--accent)' : 'var(--text-secondary)' }}>
                    {STATUS_LABEL[d.status] || d.status}
                  </span>
                </td>
                <td className="mono px-2 py-2 text-[var(--text-secondary)]">{d.chunk_count}</td>
                <td className="px-2 py-2"><ExpiryBadge days={d.expiring_days} /></td>
                <td className="mono px-2 py-2 text-[10px] text-[var(--text-secondary)]">
                  {d.updated_at ? new Date(d.updated_at).toLocaleString() : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ===== 负反馈分析 =====
function FeedbackAnalysis() {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  async function load() {
    setLoading(true)
    try {
      setData(await fetchFeedbackAnalysis())
      setOpen(true)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <button onClick={load} className="flex w-full items-center justify-between">
        <span className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">
          👍/👎 反馈分析{data && ` · 差评率 ${(data.summary.down_rate * 100).toFixed(1)}%`}
        </span>
        {loading ? <ArrowClockwise size={12} className="animate-spin" /> : (open ? <CaretDown size={12} /> : <CaretRight size={12} />)}
      </button>

      {data && (
        <div className="mt-3 space-y-3 text-xs">
          <div className="flex flex-wrap gap-2 text-[11px]">
            <Chip label={`👍 ${data.summary.up}`} color="var(--success)" />
            <Chip label={`👎 ${data.summary.down}`} color="var(--warn)" />
            <Chip label={`未反馈 ${data.summary.unrated}`} color="var(--text-secondary)" />
            {Object.entries(data.by_category).map(([k, n]) => (
              <Chip key={k} label={`${CATEGORY_LABEL[k] || k} ${n}`} color="var(--accent)" />
            ))}
          </div>
          {data.items.length === 0 && <p className="text-[var(--text-secondary)]">暂无 👎 反馈。用户点过 👎 的消息会在这里归类（文档缺失 / 文档过时）。</p>}
          {data.items.map((it) => (
            <div key={it.message_id} className="rounded-lg border px-3 py-2" style={{ borderColor: 'var(--border)', background: 'var(--surface-2)' }}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded px-1.5 py-px text-[10px] font-medium"
                  style={{ background: it.category === 'missing' ? 'rgba(220,38,38,0.12)' : 'rgba(234,179,8,0.12)', color: it.category === 'missing' ? 'var(--error)' : 'var(--warn)' }}>
                  {CATEGORY_LABEL[it.category] || it.category}
                </span>
                <span className="mono text-[10px] text-[var(--text-secondary)]">会话 #{it.conversation_id}</span>
                <span className="truncate text-[10px] text-[var(--text-secondary)]">问题：{it.query}</span>
              </div>
              <p className="mt-1 text-[var(--text-primary)]">{it.content}</p>
              {it.sources?.length > 0 && (
                <p className="mt-1 text-[10px] text-[var(--text-secondary)]">
                  命中依据：{it.sources.join('、')}
                </p>
              )}
              <p className="mt-0.5 text-[10px]" style={{ color: it.category === 'missing' ? 'var(--error)' : 'var(--warn)' }}>{it.reason}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Chip({ label, color }) {
  return <span className="rounded px-1.5 py-0.5" style={{ background: 'var(--surface-2)', color }}>{label}</span>
}

// ===== 管理页主组件 =====
export default function KbManager() {
  const [stats, setStats] = useState(null)
  const [docs, setDocs] = useState(null)
  const [busy, setBusy] = useState(false)        // 同步/切换进行中
  const [syncReport, setSyncReport] = useState(null)
  const [notice, setNotice] = useState(null)     // 切换/错误提示

  async function load() {
    try {
      const [s, d] = await Promise.all([fetchKbStats(), fetchKbDocuments()])
      setStats(s)
      // 后端返回 {docs, total}：只取 docs 数组（踩过的坑：整个对象传给
      // DocTable 后 docs.map 崩溃 → React 整树卸载 → 黑屏）
      setDocs(d.docs)
    } catch (e) {
      setNotice(e.message)
    }
  }
  useEffect(() => { load() }, [])

  async function handleSync() {
    setBusy(true); setSyncReport(null); setNotice(null)
    try {
      setSyncReport(await syncKb())
      await load() // 刷新统计（candidate chunks 变化）
    } catch (e) {
      setNotice(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function handleSwitch() {
    if (!window.confirm('切换后线上检索立即使用候选库；旧库保留，可再次点击回滚。确认切换？')) return
    setBusy(true); setNotice(null)
    try {
      const r = await switchKb()
      setNotice(`已切换到 ${r.switched_to}（在岗 ${r.active_chunks} chunks）。回滚：再次点击切换`)
      await load()
    } catch (e) {
      setNotice(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 lg:p-6">
      {notice && (
        <div className="rounded-lg border px-3 py-2 text-xs"
          style={{ borderColor: 'rgba(13,148,136,0.35)', background: 'rgba(13,148,136,0.08)', color: 'var(--accent)' }}>
          {notice}
        </div>
      )}
      <StatsCard stats={stats} busy={busy} onSync={handleSync} onSwitch={handleSwitch} />
      <SyncResult report={syncReport} />
      <KbDebug />
      <DocTable docs={docs} onRefresh={load} />
      <FeedbackAnalysis />
    </div>
  )
}
