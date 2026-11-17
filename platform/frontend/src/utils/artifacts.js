// 沙箱产物浏览的纯前端工具：扩展名分类、条目排序、大小格式化。
// 只放无副作用的小函数，便于在组件外单测。

// 带 .js 后缀：Vite 不需要，但这样纯 node 也能直接 import 本模块
// （platform/test/test_trajectory_js.py 的对账测试就是拿 node 跑的）。
import { detectTrajKind } from './trajectory.js'

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
  // 「轨迹」排首位：分组视图的价值就在于它能看到 main + evaluator + 多 session
  // 的**全部**轨迹，而会话详情弹窗只认一条（取最大的那个 session 文件）。
  { id: 'traj', label: '轨迹' },
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

/**
 * 分组判定（带路径）。轨迹文件必须按**相对路径**识别，不能只看扩展名：
 * hermes 的 session_*.json 与 openjiuwen 的 query<N>.json 都是 .json，
 * 和 traj_stats_result.json 这类普通配置同扩展名，只按扩展名会误伤。
 *
 * rel 为空时退化成 groupOf(name)（纯扩展名判定）。
 */
export function groupOfEntry(rel, name) {
  if (rel && detectTrajKind(rel)) return 'traj'
  return groupOf(name || rel || '')
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

// ── JSONL 记录索引（浏览器式预览） ─────────────────────────────────────────
// parseJsonl 给的是「一条一个 pretty-print 字符串」，够纵向平铺，但不够做
// 左侧索引列表：要标签、要摘要、要能搜索，还要保留解析后的值给结构树用。
// parseJsonlRecords 在同一次扫描里把这些一并产出，避免组件再遍历一遍。

// 标签字段按优先级取第一个命中的字符串值。顺序是经验排的：
// type/role 最常见（OpenAI 风格消息、轨迹事件流都用），event/kind 次之，
// name/action 放最后 —— 它们在带 type 的记录里通常是**工具名**而不是记录类型，
// 只有在没有更靠前的字段时才拿来当标签。
const LABEL_KEYS = ['type', 'role', 'event', 'kind', 'name', 'action']

// 摘要字段：挑人能读的那一个，而不是第一个字符串字段（那常常是 id）。
const SUMMARY_KEYS = ['content', 'text', 'message', 'summary', 'description', 'title']

const SUMMARY_MAX = 80
// 搜索只扫每行前 4000 字符：单行可达十几万字符，全量 includes 在几千条上
// 会把输入框卡住。前 4 KB 足以覆盖字段名和开头的内容。
const SEARCH_SCAN = 4000

// 把任意值压成一行可读文本（用于摘要）：换行/连续空白折叠成单空格。
function flatten(s) {
  return String(s).replace(/\s+/g, ' ').trim()
}

function clip(s, n) {
  const t = flatten(s)
  return t.length > n ? `${t.slice(0, n)}…` : t
}

// 从一个值里掏出「像正文」的字符串。content 既可能是裸字符串，也可能是
// OpenAI 风格的 parts 数组（[{type:'text',text:'…'}]），两种都要认。
function textish(v) {
  if (typeof v === 'string') return v
  if (Array.isArray(v)) {
    for (const item of v) {
      const t = textish(item)
      if (t) return t
    }
    return ''
  }
  if (isPlainObj(v)) {
    for (const k of ['text', 'content', 'message']) {
      if (typeof v[k] === 'string' && v[k]) return v[k]
    }
  }
  return ''
}

function isPlainObj(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v)
}

function labelOf(value) {
  if (!isPlainObj(value)) return ''
  for (const k of LABEL_KEYS) {
    const v = value[k]
    // 只认短字符串：name 字段有时是一整段描述，当标签会把列表撑变形
    if (typeof v === 'string' && v && v.length <= 40) return v
  }
  return ''
}

function summaryOf(value, raw) {
  if (isPlainObj(value)) {
    for (const k of SUMMARY_KEYS) {
      const t = textish(value[k])
      if (t) return clip(t, SUMMARY_MAX)
    }
    // 没有正文字段（tool_call 这类纯结构记录很常见）：列顶层键名而不是
    // 原始 JSON —— `{"type":"tool_call","name":"browser.…` 这种转义串既占
    // 两行又读不出重点，`type · name · args` 反而一眼看出这条有什么。
    const keys = Object.keys(value)
    if (keys.length) return clip(keys.slice(0, 8).join(' · '), SUMMARY_MAX)
  }
  // 数组/标量/解析失败的行：退回原文，至少能看出这条长什么样
  return clip(raw, SUMMARY_MAX)
}

/**
 * 逐行解析 JSONL，并为每条产出索引列表所需的元信息。
 *
 * 与 parseJsonl 的区别：这里额外带 value（解析后的值，给结构树）、raw（原行）、
 * bytes、label、summary、searchText。text 字段与 parseJsonl 一致（pretty-print
 * 后的字符串，Raw 模式直接用）。
 *
 * @returns {{ok:boolean, records:Array, total:number, bad:number, truncated:boolean, error?:string}}
 */
export function parseJsonlRecords(raw, maxLines = JSONL_MAX_LINES) {
  const src = String(raw || '')
  if (!src.trim()) {
    return { ok: false, records: [], total: 0, bad: 0, truncated: false, error: '空文件' }
  }
  const lines = src.split(/\r?\n/).filter(l => l.trim())
  const limit = Math.min(lines.length, maxLines)
  const records = []
  let bad = 0
  for (let i = 0; i < limit; i += 1) {
    const line = lines[i]
    let value = null
    let ok = true
    let text = line
    try {
      value = JSON.parse(line)
      text = JSON.stringify(value, null, 2)
    } catch {
      ok = false
      bad += 1
      value = null
    }
    const label = ok ? labelOf(value) : ''
    records.push({
      idx: i,
      ok,
      value,
      raw: line,
      text,
      bytes: line.length,
      label,
      summary: summaryOf(value, line),
      searchText: `${label} ${line.slice(0, SEARCH_SCAN)}`.toLowerCase(),
    })
  }
  return {
    ok: records.length > 0,
    records,
    total: lines.length,
    bad,
    truncated: lines.length > maxLines,
  }
}

// ── JSON 结构树 ────────────────────────────────────────────────────────────
// 把一个值摊成「可直接渲染一层」的节点。递归交给组件（JsonTree.vue 自调用），
// 这里只负责判定类型、数children、以及长字符串的截断策略。

// 超过这个长度的字符串默认折叠。800 字符约等于预览框里 6–8 行，
// 足够看清开头在讲什么，又不会把一条 248 KB 的 content 整个灌进 DOM。
export const STRING_PREVIEW = 800

export function valueType(v) {
  if (v === null) return 'null'
  if (Array.isArray(v)) return 'array'
  const t = typeof v
  return t === 'object' ? 'object' : t   // string / number / boolean / object / array / null
}

/**
 * 摊开一层：对象 → 按键，数组 → 按下标。非容器返回空数组。
 * @returns {Array<{key:string, value:*, type:string, isContainer:boolean,
 *                  childCount:number, isLong:boolean, preview:string, full:string}>}
 */
export function jsonTreeNodes(value) {
  if (!isPlainObj(value) && !Array.isArray(value)) return []
  const entries = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value)
  return entries.map(([key, v]) => {
    const type = valueType(v)
    const isContainer = type === 'object' || type === 'array'
    const full = type === 'string' ? v : ''
    const isLong = type === 'string' && v.length > STRING_PREVIEW
    return {
      key,
      value: v,
      type,
      isContainer,
      childCount: isContainer
        ? (type === 'array' ? v.length : Object.keys(v).length)
        : 0,
      isLong,
      preview: isLong ? full.slice(0, STRING_PREVIEW) : full,
      full,
    }
  })
}

// 容器节点折叠时显示的占位：`{ … } (3 fields)` / `[ … ] (5 items)`
export function containerHint(node) {
  if (!node.isContainer) return ''
  return node.type === 'array'
    ? `[ … ] (${node.childCount} items)`
    : `{ … } (${node.childCount} fields)`
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
