import { Timer } from '@phosphor-icons/react'
import { fmtDuration } from '../format'

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

// ===== 节点语义化：把每个节点的原始 JSON 翻译成人话 =====
// tone: accent(当前/决策) / success / warn / error / neutral
function describe(node, result) {
  switch (node) {
    case 'intent': {
      const intent = result?.intent ?? result?.reused
      const label = { vpn: '识别为 VPN 故障', password: '识别为密码问题' }[intent]
      return {
        label: '意图识别', type: 'AI',
        summary: label ?? '非支持场景，直接收尾',
        tone: intent === 'vpn' ? 'accent' : 'neutral',
      }
    }
    case 'extract': {
      const parts = []
      if (result?.device) parts.push(`设备=${result.device}`)
      if (result?.error_code) parts.push(`错误码=${result.error_code}`)
      if (result?.username) parts.push(`账号=${result.username}`)
      return {
        label: '信息抽取', type: 'AI',
        summary: parts.length ? parts.join(' · ') : '未抽到新信息',
        tone: 'neutral',
      }
    }
    case 'check': {
      const missing = result?.missing
      return {
        label: '完整性检查', type: '规则',
        summary: missing?.length ? `缺少：${missing.join(', ')} → 追问` : '✓ 信息齐全，放行',
        tone: missing?.length ? 'warn' : 'success',
      }
    }
    case 'verify': {
      const cs = result?.cert_status ?? result
      if (cs?.status === 'error') {
        return { label: '查证账号状态', type: '工具', summary: `查询失败：${cs.reason}`, tone: 'error' }
      }
      const state = cs?.expired ? '已过期' : '正常'
      return {
        label: '查证账号状态', type: '工具',
        summary: `证书${state}（有效期至 ${cs?.cert_valid_until}）`,
        tone: cs?.expired ? 'warn' : 'success',
      }
    }
    case 'kb': {
      const km = result?.kb_match ?? result
      if (!km?.matched) return { label: '知识库匹配', type: '规则', summary: `未命中：${km?.reason ?? ''}`, tone: 'error' }
      return { label: '知识库匹配', type: '规则', summary: `命中方案（风险 ${km.risk}）`, tone: 'neutral' }
    }
    case 'risk': {
      return {
        label: '风险分级', type: '规则',
        summary: result?.decision === 'auto' ? '低风险 → 自动执行' : '需人工审批 → 转人工',
        tone: result?.decision === 'auto' ? 'accent' : 'warn',
      }
    }
    case 'execute': {
      const tr = result?.tool_result ?? result
      if (tr?.status === 'error') return { label: '执行 Tool', type: '工具', summary: `❌ ${tr.reason}`, tone: 'error' }
      return { label: '执行 Tool', type: '工具', summary: `✅ ${tr.message}`, tone: 'success' }
    }
    case 'close':
      return { label: '收尾关单', type: '收尾', summary: `工单已解决 · ${result?.ticket_id ?? ''}`, tone: 'success' }
    case 'handoff':
      return { label: '转人工', type: '收尾', summary: '工单已转人工处理', tone: 'warn' }
    case 'finalize':
      return { label: '收尾回复', type: '收尾', summary: result?.reply ?? '', tone: 'neutral' }
    default:
      return { label: node, type: '节点', summary: JSON.stringify(result), tone: 'neutral' }
  }
}

// 执行中占位节点的友好名称（App.jsx 预置 pending trace 用）
const PENDING_LABEL = { intent: '意图识别', extract: '信息抽取' }
// 并行节点（intent ∥ extract 同时执行，总耗时 ≠ 各阶段之和）
const PARALLEL_NODES = new Set(['intent', 'extract'])

const TONE_COLOR = {
  accent: 'var(--accent)',
  success: 'var(--success)',
  warn: 'var(--warn)',
  error: 'var(--error)',
  neutral: 'var(--text-secondary)',
}

function FlowTimeline({ traces }) {
  if (!traces.length) {
    return <p className="text-xs text-[var(--text-secondary)]">等待 Agent 执行…</p>
  }
  return (
    <ol className="relative">
      {traces.map((t, i) => {
        // pending = 已预置但还没执行完的节点（App.jsx 发送时立即塞入，SSE 完成后替换）
        const meta = t.pending
          ? { label: PENDING_LABEL[t.node] || t.node, type: 'AI', summary: null, tone: 'accent' }
          : describe(t.node, t.result)
        const isLatest = i === traces.length - 1
        return (
          <li key={i} className="relative flex gap-3 pb-4 last:pb-0">
            {/* 节点间连接线 */}
            {i < traces.length - 1 && (
              <span className="absolute left-[5px] top-4 bottom-0 w-px" style={{ background: 'var(--border-strong)' }} />
            )}
            {/* 节点圆点（状态色；pending 用呼吸动画表示执行中） */}
            <span
              className={`relative z-10 mt-1.5 h-[11px] w-[11px] shrink-0 rounded-full ${t.pending ? 'animate-pulse' : ''}`}
              style={{
                background: isLatest ? TONE_COLOR[meta.tone] : 'var(--surface)',
                border: `2px solid ${TONE_COLOR[meta.tone]}`,
              }}
            />
            {/* 节点内容 */}
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span
                  className="mono text-xs"
                  style={{ color: isLatest ? 'var(--text-primary)' : 'var(--text-secondary)' }}
                >
                  {meta.label}
                </span>
                <span className="flex shrink-0 items-center gap-1.5">
                  {/* 并行标记：intent ∥ extract 同时执行，解释"总耗时 ≠ 各阶段之和" */}
                  {PARALLEL_NODES.has(t.node) && (
                    <span
                      className="mono shrink-0 rounded px-1 py-px text-[9px]"
                      style={{ background: 'var(--accent-dim)', color: 'var(--accent)' }}
                      title="此节点与相邻 LLM 节点并行执行"
                    >
                      ∥ 并行
                    </span>
                  )}
                  {/* 节点耗时（阶段时间）；执行中不显示 */}
                  {!t.pending && t.elapsed_ms != null && (
                    <span className="mono flex items-center gap-0.5 text-[10px] text-[var(--text-secondary)]">
                      <Timer size={11} weight="regular" />
                      {fmtDuration(t.elapsed_ms)}
                    </span>
                  )}
                  <span
                    className="mono shrink-0 rounded border px-1.5 py-px text-[10px]"
                    style={{ borderColor: 'var(--border)', color: 'var(--text-secondary)' }}
                  >
                    {meta.type}
                  </span>
                </span>
              </div>
              {t.pending ? (
                <p className="mt-0.5 animate-pulse text-[11px]" style={{ color: 'var(--accent)' }}>
                  执行中…
                </p>
              ) : (
                <p className="mt-0.5 text-[11px] leading-snug" style={{ color: TONE_COLOR[meta.tone] }}>
                  {meta.summary}
                </p>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}

export default function SidePanel({ conv, traces, elapsedMs }) {
  return (
    <div className="space-y-4 p-4 lg:p-5">
      <TicketCard conv={conv} />
      <div className="rounded-[12px] border p-4" style={{ background: 'var(--surface)', borderColor: 'var(--border)' }}>
        <div className="mb-4 flex items-center justify-between gap-2">
          <p className="mono text-[11px] uppercase tracking-[0.16em] text-[var(--text-secondary)]">Agent 执行链路</p>
          {/* 本轮总耗时：与气泡一致；与各节点耗时不同（节点并行执行） */}
          {elapsedMs != null && (
            <span className="mono flex items-center gap-1 text-[11px] text-[var(--text-primary)]">
              <Timer size={13} weight="regular" />
              本轮总耗时 {fmtDuration(elapsedMs)}
            </span>
          )}
        </div>
        <FlowTimeline traces={traces} />
        {traces.length > 0 && (
          <p className="mt-3 text-[10px] leading-relaxed text-[var(--text-secondary)]">
            ℹ️ intent ∥ extract 并行执行，总耗时 = 最长节点耗时，≠ 各阶段耗时之和
          </p>
        )}
      </div>
    </div>
  )
}
