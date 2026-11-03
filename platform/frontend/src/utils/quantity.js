/**
 * K8s 资源量换算。
 *
 * CPU 和内存不能共用一个换算表：后缀 m 在 CPU 里是「毫核」（500m = 0.5 核），
 * 在内存里是另一回事。两边各一个函数，和后端
 * api/routers/sandbox_configs.py 的 to_cores / _to_quantity 一一对应。
 */

const QUANTITY_RE = /^([\d.]+)\s*([A-Za-z]*)$/

/** 内存量 → MiB。Gi→1024、Mi→1、G→1000，裸数字按 MiB。 */
export function toMiB(v) {
  const m = String(v ?? '').trim().match(QUANTITY_RE)
  if (!m) return 0
  const factor = { gi: 1024, mi: 1, g: 1000, m: 1, '': 1 }[m[2].toLowerCase()] ?? 1
  return parseFloat(m[1]) * factor
}

/** CPU 量 → 核数。500m → 0.5，裸数字按核。解析不出来返回 0。 */
export function toCores(v) {
  const m = String(v ?? '').trim().match(QUANTITY_RE)
  if (!m) return 0
  const num = parseFloat(m[1])
  return m[2].toLowerCase() === 'm' ? num / 1000 : num
}
