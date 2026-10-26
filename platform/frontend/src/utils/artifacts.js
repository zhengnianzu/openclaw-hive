// 沙箱产物浏览的纯前端工具：扩展名分类、条目排序、大小格式化。
// 只放无副作用的小函数，便于在组件外单测。

// 扩展名 → 分组。`.md` 归「文本」而不是「文档」，因为它是纯文本、直接预览即可；
// 真正的「文档」是那些必须转换才能看的格式。
// 与后端 api/services/artifact_store.py 的扩展名分类保持一致：
// doc = 需要 LibreOffice 转 PDF 才能预览；web/image/pdf = 浏览器原生可渲染；
// data = 结构化文本（CSV 用表格渲染）；text = 纯文本直读。
const GROUP_RULES = [
  { id: 'web', label: '网页', exts: ['.html', '.htm', '.xhtml'] },
  { id: 'doc', label: '文档/表格', exts: [
    '.pptx', '.ppt', '.ppsx', '.pps',
    '.docx', '.doc', '.rtf', '.odt',
    '.xlsx', '.xls', '.xlsm', '.ods',
  ] },
  { id: 'pdf', label: 'PDF', exts: ['.pdf'] },
  { id: 'image', label: '图片', exts: [
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico', '.tiff', '.avif',
  ] },
  { id: 'data', label: '数据', exts: [
    '.json', '.jsonl', '.ndjson', '.csv', '.tsv', '.yaml', '.yml', '.xml', '.toml',
  ] },
  { id: 'text', label: '文本', exts: [
    '.md', '.markdown', '.txt', '.log',
    '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.sh', '.bash',
    '.sql', '.java', '.go', '.rs', '.c', '.h', '.cpp', '.css', '.scss',
    '.ini', '.conf', '.cfg', '.env',
  ] },
]

export const GROUPS = [
  ...GROUP_RULES.map(g => ({ id: g.id, label: g.label })),
  { id: 'other', label: '其它' },
]

export function extOf(name) {
  const i = String(name || '').lastIndexOf('.')
  return i > 0 ? String(name).slice(i).toLowerCase() : ''
}

export function groupOf(name) {
  const ext = extOf(name)
  const hit = GROUP_RULES.find(g => g.exts.includes(ext))
  return hit ? hit.id : 'other'
}

export function groupLabel(id) {
  const g = GROUPS.find(x => x.id === id)
  return g ? g.label : '其它'
}

// 预览类别：决定右侧用哪种渲染器
export function previewKindOf(name) {
  const ext = extOf(name)
  // 与后端 media_type() 的判定顺序一致：纯文本优先于 office（csv/tsv 不走 LibreOffice）
  if (['.html', '.htm', '.xhtml'].includes(ext)) return 'html'
  // Markdown 单独一类：字节是纯文本（后端照常按 text/plain 返回），但前端要富文本渲染
  if (['.md', '.markdown'].includes(ext)) return 'markdown'
  if (['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.ico',
       '.tiff', '.avif'].includes(ext)) return 'image'
  if (ext === '.pdf') return 'pdf'
  if (['.csv', '.tsv'].includes(ext)) return 'table'
  if (['.pptx', '.ppt', '.ppsx', '.pps', '.docx', '.doc', '.rtf', '.odt',
       '.xlsx', '.xls', '.xlsm', '.ods'].includes(ext)) return 'office'
  if (['.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.xml', '.toml',
       '.md', '.markdown', '.txt', '.log', '.py', '.js', '.mjs', '.cjs',
       '.ts', '.tsx', '.jsx', '.sh', '.bash', '.sql', '.java', '.go', '.rs',
       '.c', '.h', '.cpp', '.css', '.scss', '.ini', '.conf', '.cfg', '.env'].includes(ext)) return 'text'
  return 'binary'
}

// 目录在前、按名升序（沿用 OutputViewer.vue 的排序规则）
export function sortEntries(entries) {
  const arr = [...(entries || [])]
  arr.sort((a, b) => {
    if (!!a.is_dir !== !!b.is_dir) return a.is_dir ? -1 : 1
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN')
  })
  return arr
}

export function fmtSize(n) {
  if (n == null || n === '') return ''
  const v = Number(n)
  if (!Number.isFinite(v)) return String(n)
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = v / 1024
  let i = 0
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1 }
  return `${x.toFixed(x >= 100 ? 0 : 1)} ${units[i]}`
}

export function fmtTime(ts) {
  if (!ts) return ''
  const d = new Date(Number(ts) * 1000)
  if (Number.isNaN(d.getTime())) return ''
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// OBS 的 LastModified 是 ISO 字符串（2026-10-23T06:20:31Z），展示成同样口径
export function fmtObsTime(iso) {
  if (!iso) return ''
  return String(iso).replace('T', ' ').replace('Z', '')
}

// ── html 预览的资源重写 ────────────────────────────────────────────────────
// 交付件里的相对资源（css/png/js）在 srcdoc 下没有 base URL，一律 404。
// 这两个函数只做纯文本处理：收集候选引用 + 用给定的 URL 表替换。
// 实际取字节由组件按目录清单决定（只取本层真实存在的文件，不下钻）。

const ASSET_ATTR_RE = /(\b(?:src|href)\s*=\s*["'])([^"'#?][^"']*)(["'])/gi

// 返回本层可直接替换的资源名（去重）。绝对 URL / data: / 锚点 / 跨目录引用一律跳过。
export function collectLocalRefs(html, availableNames) {
  const have = new Set(availableNames || [])
  const out = new Set()
  const re = new RegExp(ASSET_ATTR_RE.source, 'gi')
  let m
  while ((m = re.exec(String(html || ''))) !== null) {
    const raw = String(m[2]).trim()
    if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(raw)) continue
    const name = raw.replace(/^\.\//, '')
    if (name.includes('/')) continue
    if (have.has(name)) out.add(name)
  }
  return [...out]
}

// urlMap: name -> blob/URL。未命中的引用原样保留（浏览器会照常 404，可接受）。
export function rewriteLocalRefs(html, urlMap) {
  const map = urlMap instanceof Map ? urlMap : new Map(Object.entries(urlMap || {}))
  return String(html || '').replace(
    new RegExp(ASSET_ATTR_RE.source, 'gi'),
    (full, pre, raw, post) => {
      const name = String(raw).trim().replace(/^\.\//, '')
      const url = map.get(name)
      return url ? `${pre}${url}${post}` : full
    },
  )
}
