// 沙箱产物浏览的纯前端工具：扩展名分类、条目排序、大小格式化。
// 只放无副作用的小函数，便于在组件外单测。

// 扩展名 → 分组。`.md` 归「文本」而不是「文档」，因为它是纯文本、直接预览即可；
// 真正的「文档」是那些必须转换才能看的格式。
// 与后端 api/services/artifact_store.py 的扩展名分类保持一致：
// doc = 需要 LibreOffice 转 PDF 才能预览；web/image/pdf = 浏览器原生可渲染；
// data = 结构化文本（CSV 用表格渲染）；text = 纯文本直读。
const GROUP_RULES = [
  // label 用文件格式名而不是抽象类别：「HTML」比「网页」更直接 ——
  // 沙箱产出里 .html 往往是报告/看板，用户找的是「那个 html 文件」而不是「网页」。
  { id: 'web', label: 'HTML', exts: ['.html', '.htm', '.xhtml'] },
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
  // .json 单独一类：字节仍是纯文本（后端按 text/plain 返回），但前端要格式化 + 高亮。
  if (ext === '.json') return 'json'
  // .jsonl/.ndjson 是逐行 JSON：整份解析必然失败，走逐行解析 + 分页渲染。
  if (['.jsonl', '.ndjson'].includes(ext)) return 'jsonl'
  if (['.csv', '.tsv'].includes(ext)) return 'table'
  if (['.pptx', '.ppt', '.ppsx', '.pps', '.docx', '.doc', '.rtf', '.odt',
       '.xlsx', '.xls', '.xlsm', '.ods'].includes(ext)) return 'office'
  if (['.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.xml', '.toml',
       '.md', '.markdown', '.txt', '.log', '.py', '.js', '.mjs', '.cjs',
       '.ts', '.tsx', '.jsx', '.sh', '.bash', '.sql', '.java', '.go', '.rs',
       '.c', '.h', '.cpp', '.css', '.scss', '.ini', '.conf', '.cfg', '.env'].includes(ext)) return 'text'
  return 'binary'
}

// ── JSON 预览 ──────────────────────────────────────────────────────────────
// 沙箱产出里 .json 很常见（traj_stats_result.json、*.config.json 等），
// 原样平铺一行没法读。这里做两件事：缩进格式化 + 语法高亮。
// 解析失败（截断的大文件、jsonl 误判）时返回原文，让用户至少能看到内容。

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 把 JSON 文本格式化成缩进版本。
 * @returns {{ok: boolean, text: string, error?: string}}
 */
export function formatJson(raw) {
  const src = String(raw || '')
  if (!src.trim()) return { ok: false, text: src, error: '空文件' }
  try {
    return { ok: true, text: JSON.stringify(JSON.parse(src), null, 2) }
  } catch (e) {
    // 大文件被后端截断（_read_text_slice 有 200 万字符上限）时必然解析失败,
    // 这是预期情况，不该当成错误刷红。
    return { ok: false, text: src, error: 'JSON 解析失败（可能被截断或不是合法 JSON）' }
  }
}

/**
 * 语法高亮：返回可直接 v-html 的安全字符串（全部转义后再加 span）。
 * 用单次正则扫描而不是逐 token 拼接，避免长文件上的性能问题。
 */
export function highlightJson(text) {
  const escaped = escapeHtml(text)
  // 顺序重要：字符串优先，否则字符串里的 true/false 会被当成关键字着色。
  return escaped.replace(
    /(&quot;(?:[^&\\]|\\.|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon, kw, num) => {
      if (str) {
        // 后跟冒号的字符串是键名，用另一种颜色区分
        return colon
          ? `<span class="j-key">${str}</span>${colon}`
          : `<span class="j-str">${str}</span>`
      }
      if (kw) return `<span class="j-kw">${kw}</span>`
      if (num) return `<span class="j-num">${num}</span>`
      return m
    },
  )
}

// ── JSONL 预览 ─────────────────────────────────────────────────────────────
// JSONL 是「每行一个独立 JSON」，整份文件不是合法 JSON，不能走 formatJson。
// 逐行解析后按 block 渲染：每行的对象字段单独展开，一屏放不下就分页加载。
//
// 为什么分页而不是一次全渲染：trajectory.jsonl 这类文件单行可达十几万字符，
// 几百行全展开会让浏览器卡死。默认只渲染前几条，滚动到底再追加。

const JSONL_MAX_LINES = 5000      // 超过就不再解析（后端本来也只给前 200 万字符）

/**
 * 逐行解析 JSONL。
 * @returns {{ok:boolean, blocks:Array, total:number, bad:number, truncated:boolean, error?:string}}
 */
export function parseJsonl(raw, maxLines = JSONL_MAX_LINES) {
  const src = String(raw || '')
  if (!src.trim()) {
    return { ok: false, blocks: [], total: 0, bad: 0, truncated: false, error: '空文件' }
  }
  const lines = src.split(/\r?\n/).filter(l => l.trim())
  const limit = Math.min(lines.length, maxLines)
  const blocks = []
  let bad = 0
  for (let i = 0; i < limit; i += 1) {
    const line = lines[i]
    try {
      // 每行单独 pretty-print：原样一行几千字符没法读
      blocks.push({ idx: i, ok: true, text: JSON.stringify(JSON.parse(line), null, 2) })
    } catch {
      bad += 1
      blocks.push({ idx: i, ok: false, text: line })
    }
  }
  return {
    ok: blocks.length > 0,
    blocks,
    total: lines.length,
    bad,
    truncated: lines.length > maxLines,
  }
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
