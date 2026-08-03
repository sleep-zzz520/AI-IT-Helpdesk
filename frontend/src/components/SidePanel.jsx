// 工单状态 → 徽章（语义色 + 文字，仅状态处使用）
const STATUS_META = {
  open:     { label: '处理中', color: 'var(--accent)' },
  resolved: { label: '已解决', color: 'var(--success)' },
  handoff:  { label: '转人工', color: 'var(--warn)' },
}

function TicketCard({ conv }) {
  if (!conv) return null
  const st = STATUS_META[conv.status] ?? { label: conv.status, color: 'var(--text-secondary)' }
  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <p className="mono mb-3 text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">工单</p>
      <div className="space-y-2.5 text-sm">
        <Row k="会话 ID" v={<span className="mono">{conv.id}</span>} />
        <Row k="用户" v={conv.user_id} />
        <Row k="意图" v={conv.intent ? <span className="mono">{conv.intent}</span> : '待判定'} />
        <Row
          k="状态"
          v={
            <span className="inline-flex items-center gap-1.5">
              <span className="h-1.5 w-1.5 rounded-full" style={{ background: st.color }} />
              {st.label}
            </span>
          }
        />
        <Row k="工单号" v={conv.ticket_id ? <span className="mono">{conv.ticket_id}</span> : '—'} />
      </div>
    </div>
  )
}

function Row({ k, v }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-[var(--text-secondary)]">{k}</span>
      <span className="truncate text-xs text-[var(--text-primary)]">{v}</span>
    </div>
  )
}

function TraceTimeline({ traces }) {
  return (
    <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
      <p className="mono mb-3 text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">Trace 执行链路</p>
      {traces.length === 0 ? (
        <p className="text-xs text-[var(--text-secondary)]">等待 Agent 执行…</p>
      ) : (
        <ol className="relative space-y-3 border-l pl-4" style={{ borderColor: 'var(--border)' }}>
          {traces.map((t, i) => (
            <li key={i} className="rise-in relative">
              <span
                className="absolute -left-[21px] top-1.5 h-1.5 w-1.5 rounded-full"
                style={{ background: i === traces.length - 1 ? 'var(--accent)' : 'var(--border-strong)' }}
              />
              <p className="mono text-xs text-[var(--text-primary)]">{t.node}</p>
              <p className="mono mt-0.5 break-all text-[11px] leading-snug text-[var(--text-secondary)]">
                {summarize(t.result)}
              </p>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

// Trace 结果摘要：JSON 截断（面板保持克制密度）
function summarize(result) {
  const s = JSON.stringify(result)
  return s.length > 90 ? `${s.slice(0, 90)}…` : s
}

export default function SidePanel({ conv, traces }) {
  return (
    <div className="space-y-4 p-4 lg:p-5">
      <TicketCard conv={conv} />
      <TraceTimeline traces={traces} />
    </div>
  )
}
