// 耗时格式化：毫秒 → 人类可读（<1ms 显示 <1 ms，<1s 显示 ms，否则显示 x.x s）
export function fmtDuration(ms) {
  if (ms == null || Number.isNaN(ms)) return ''
  if (ms < 1) return '<1 ms'
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}
