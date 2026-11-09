// 轨迹文件 → 归一化消息块（供「沙箱交付物 → 分组视图」的气泡预览）。
//
// ⚠ 本文件是 platform/src/offline_analysis.py 的 JS 对等实现，必须与这三个函数保持同步：
//     parse_openclaw_trajectory   (offline_analysis.py:719)
//     parse_hermes_messages       (offline_analysis.py:1037)
//     parse_openjiuwen_trajectory (offline_analysis.py:958)
//   输出块结构与后端 **完全一致**：
//     {role, part_type, content, tool_name, args, isError, exitCode, details}
//   因为 TrajBlocks.vue 同时渲染两个来源：会话详情弹窗（后端解析，走
//   /deep/{traj}/detail）与分组视图（本文件解析，走 /artifacts/{id}/content）。
//   改了一侧的解析分支，另一侧要跟着改；两者的一致性由
//   platform/test/test_trajectory_js.py 的对账测试兜住。
//
// 为什么解析放前端：分组视图里任何一个 jsonl 都能即点即看，不依赖
// task_traj_records 有行、也不需要后端多开一个按文件路径解析的接口。

// ── 路径判定 ───────────────────────────────────────────────────────────────
// 后端是扫目录（os.walk + isdir 校验），前端只有一个相对路径，所以这里按
// 路径形状判定。判定必须按**路径**而不是扩展名：hermes/openjiuwen 的轨迹是
// .json，和 traj_stats_result.json 这类普通配置同扩展名，按扩展名判会误伤。

// agents/{assistant*,main}/sessions/**/*.jsonl（排除含 trajectory 的文件名）
// 对齐 find_openclaw_sessions (offline_analysis.py:366)
const OPENCLAW_RE = /(?:^|\/)agents\/([^/]+)\/sessions\/(?:.*\/)?([^/]+\.jsonl)$/
// claude-code 兜底：projects/<workspace>/*.jsonl（同函数的 fallback 分支）
const CLAUDE_CODE_RE = /(?:^|\/)projects\/[^/]+\/([^/]+\.jsonl)$/
// profiles/{assistant*,main}/sessions/**/*.json
// 对齐 find_hermes_sessions (offline_analysis.py:408)。
// 注意后端只要求 .json（不是 session_*.json），这里照同样口径，靠目录形状收敛。
const HERMES_RE = /(?:^|\/)profiles\/([^/]+)\/sessions\/(?:.*\/)?([^/]+\.json)$/
// logs/**/trajectories/**/query<N>.json
// 对齐 find_openjiuwen_sessions + _OPENJIUWEN_TRAJ_RE (offline_analysis.py:51)
const OPENJIUWEN_NAME_RE = /^query\d+\.json$/

// assistant 侧 agent 目录名（排除 evaluator）。
// 对齐 _is_assistant_agent_dir (offline_analysis.py:361)。
function isAssistantAgentDir(name) {
  return (String(name).startsWith('assistant') || name === 'main') && name !== 'evaluator'
}

/**
 * 按相对路径判定轨迹类型。
 *
 * 与后端的差异（有意为之）：后端的 find_* 只收 assistant 侧（排除 evaluator），
 * 因为它要挑**一条**主轨迹去算 stats。分组视图是文件浏览器，evaluator 的轨迹
 * 同样该能点开看 —— 这正是迁到分组视图的价值（详情弹窗只给一条）。
 * 所以这里 agent 目录名不做 assistant 过滤，只用 role 标出来给调用方。
 *
 * @returns {{kind: 'openclaw'|'hermes'|'openjiuwen', role: string}|null}
 */
export function detectTrajKind(rel) {
  const path = String(rel || '').replace(/\\/g, '/')
  if (!path) return null
  const name = path.slice(path.lastIndexOf('/') + 1)

  const oc = OPENCLAW_RE.exec(path)
  if (oc) {
    // 排除 *.trajectory.jsonl：后端 `"trajectory" not in fn`，是派生/摘要文件
    if (name.includes('trajectory')) return null
    return { kind: 'openclaw', role: agentRole(oc[1]) }
  }

  const hm = HERMES_RE.exec(path)
  if (hm) return { kind: 'hermes', role: agentRole(hm[1]) }

  // openjiuwen：路径里同时出现 logs 段与 trajectories 段，且文件名 query<N>.json
  const segs = path.split('/')
  if (segs.includes('logs') && segs.includes('trajectories')
      && OPENJIUWEN_NAME_RE.test(name)) {
    return { kind: 'openjiuwen', role: 'assistant' }
  }

  // claude-code 布局（后端是 find_openclaw_sessions 的 fallback）：按 openclaw 解析
  const cc = CLAUDE_CODE_RE.exec(path)
  if (cc && !name.includes('trajectory')) {
    return { kind: 'openclaw', role: 'assistant' }
  }

  return null
}

function agentRole(agentDir) {
  if (agentDir === 'evaluator') return 'evaluator'
  return isAssistantAgentDir(agentDir) ? 'assistant' : agentDir
}

// ── 块构造 ─────────────────────────────────────────────────────────────────
// 字段集必须与后端一致，缺字段一律显式 null（不要 undefined：JSON 序列化
// 后两边字段集不同，对账测试会假阳性）。

function blk(role, partType, content, extra = {}) {
  return {
    role,
    part_type: partType,
    content: content === undefined ? null : content,
    tool_name: null,
    args: null,
    isError: null,
    exitCode: null,
    details: null,
    ...extra,
  }
}

// 截断标记块：与后端 `{"role": None, "part_type": "truncated", ...}` 同构。
function truncatedBlk(text) {
  return blk(null, 'truncated', text)
}

// ── openclaw ───────────────────────────────────────────────────────────────

/**
 * openclaw 轨迹（jsonl）→ 块数组。对齐 parse_openclaw_trajectory。
 *
 * 两种布局：
 *   事件流  type ∈ {assistant/message, user/message, tool/result}，按 data.seq 重排
 *   legacy  type ∈ {message, assistant, user}，逐行拆 content parts
 */
export function parseOpenclawTraj(text) {
  const lines = String(text || '').split(/\r?\n/)
  const objs = []
  let isStream = false
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    let obj = null
    try { obj = JSON.parse(line) } catch { obj = null }
    objs.push(obj)
    if (obj && ['assistant/message', 'user/message', 'tool/result'].includes(obj.type)) {
      isStream = true
    }
  }
  return isStream ? parseOpenclawStream(objs) : parseOpenclawLegacy(objs)
}

function parseOpenclawStream(objs) {
  // (seq, block) 收集后排序：user/assistant/tool 结果在文件里是交错的，
  // 按 seq 重排才能还原真实时序。
  const seqBlocks = []
  for (const obj of objs) {
    if (!obj) continue
    const topType = obj.type
    const data = obj.data || {}
    const seq = obj.seq || 0

    if (topType === 'user/message') {
      for (const pt of data.content || []) {
        if (!isPlainObject(pt)) continue
        if (pt.type === 'text') {
          seqBlocks.push([seq, blk('user', 'text', pt.text)])
        }
      }
    } else if (topType === 'assistant/message') {
      const msg = data.message || {}
      for (const pt of msg.content || []) {
        if (!isPlainObject(pt)) continue
        if (pt.type === 'reasoning') {
          seqBlocks.push([seq, blk('assistant', 'thinking', pt.text)])
        } else if (pt.type === 'text') {
          seqBlocks.push([seq, blk('assistant', 'text', pt.text)])
        } else if (pt.type === 'tool-call') {
          seqBlocks.push([seq, blk('assistant', 'toolCall', null, {
            tool_name: pt.name ?? null,
            args: pt.arguments ?? null,
            tool_call_id: pt.id ?? null,
          })])
        }
      }
    } else if (topType === 'tool/result') {
      const msg = data.message || {}
      // content 可能是字符串，也可能是 [{type:'tool-result', toolCallId, content, isError}]
      let content = msg.content
      let tcid = null
      let isErr = false
      if (Array.isArray(content)) {
        for (const pt of content) {
          if (!isPlainObject(pt)) continue
          if (pt.type === 'tool-result') {
            content = pt.content
            tcid = pt.toolCallId ?? null
            isErr = Boolean(pt.isError)
            break
          }
        }
        if (typeof content !== 'string' && Array.isArray(content)) {
          content = partsToText(content)
        }
      }
      seqBlocks.push([seq, blk('toolResult', 'toolResult', content, {
        isError: isErr,
        tool_call_id: tcid,
      })])
    }
  }
  // 稳定排序：同 seq 的块要保留文件内原有先后（Array.sort 在 V8 上已稳定）
  seqBlocks.sort((a, b) => a[0] - b[0])
  return seqBlocks.map(([, b]) => b)
}

function parseOpenclawLegacy(objs) {
  const blocks = []
  for (const obj of objs) {
    if (!obj || !['message', 'assistant', 'user'].includes(obj.type)) continue
    const msg = obj.message || {}
    let role = msg.role
    if (!role) role = ['assistant', 'user'].includes(obj.type) ? obj.type : null
    const content = msg.content

    if (role === 'assistant') {
      if (typeof content === 'string') {
        blocks.push(blk(role, 'text', content))
      } else if (Array.isArray(content)) {
        for (const p of content) {
          if (!isPlainObject(p)) continue
          const ptype = p.type
          const toolCall = p.toolCall
          let contentText = null
          let toolName = null
          let args = null

          if (ptype === 'thinking') {
            contentText = p.thinking ?? null
          } else if (ptype === 'toolCall' || ptype === 'tool_use') {
            // 三种真实布局（offline_analysis.py:874-891）：
            //   嵌套 {type, toolCall:{function:{name, arguments}}}
            //   嵌套 {type, toolCall:{name, arguments}}
            //   扁平 {type, id, name, arguments}（claude-code 用 input 当 args）
            if (isPlainObject(toolCall)) {
              const fn = toolCall.function || {}
              toolName = fn.name ?? null
              args = fn.arguments ?? null
              if (toolName == null && 'name' in toolCall) {
                toolName = toolCall.name ?? null
                args = toolCall.arguments ?? null
              }
            } else {
              toolName = p.name ?? null
              args = p.arguments ?? null
              if (args == null) args = p.input ?? null
            }
          } else {
            contentText = p.text ?? null
          }
          blocks.push(blk(role, ptype, contentText, {
            tool_name: toolName,
            args,
          }))
        }
      }
    } else if (role === 'toolResult') {
      // ⚠ 这一支必须与 `role === 'assistant'` 平级。后端历史上把它误缩进到
      // assistant 的 Array.isArray(content) 分支里 → 永不执行，详情页完全看不到
      // 工具返回（offline_analysis.py:899-901 的注释记着这个 bug）。别重现。
      //
      // details 原样透传（后端 `details = msg.get("details") or {}`）：真实数据里
      // 它可能是 tools 定义的**数组**，也可能是 {}，不是只有 dict。只有取
      // exitCode 时才要求是对象 —— 这里对齐后端的 isinstance 判断。
      const details = msg.details ?? {}
      blocks.push(blk(role, 'toolResult', msg.content, {
        tool_name: (msg.tool || {}).name ?? null,
        isError: Boolean(msg.isError),
        exitCode: isPlainObject(details) ? (details.exitCode ?? null) : null,
        details,
      }))
    } else {
      // 后端这一支把 content **原样**透传（不拍平数组、不判 role 非空），
      // 对齐 offline_analysis.py 的 else 分支。
      blocks.push(blk(role ?? null, 'text', content))
    }
  }
  return blocks
}

/** tool-result 正文是 parts 数组时拍平成文本。对齐 _parts_to_text。 */
function partsToText(parts) {
  const out = []
  for (const pt of parts || []) {
    if (!isPlainObject(pt)) continue
    if (pt.type === 'text' && pt.text) {
      out.push(pt.text)
    } else if (typeof pt.content === 'string' || Array.isArray(pt.content)) {
      out.push(typeof pt.content === 'string' ? pt.content : partsToText(pt.content))
    }
  }
  return out.join('\n')
}

// ── hermes ─────────────────────────────────────────────────────────────────

/**
 * hermes session（单个 JSON dict 里的 messages[]）→ 块数组。
 * 对齐 parse_hermes_messages。
 *
 * ⚠ 工具调用在 assistant 消息的**顶层 tool_calls[]**（OpenAI chat 风格），
 *   不在 content parts 里 —— 源仓 analyze_hermes_session 按 content parts 数
 *   导致 tool_calls 恒 0，就是踩了这个。
 */
export function parseHermesMessages(text) {
  let data = null
  try { data = JSON.parse(String(text || '')) } catch { return [] }
  if (!isPlainObject(data)) return []

  const blocks = []
  for (const msg of data.messages || []) {
    if (!isPlainObject(msg)) continue
    const role = msg.role
    if (role === 'assistant') {
      const content = msg.content
      const reasoning = msg.reasoning_content || msg.reasoning
      if (reasoning) blocks.push(blk(role, 'thinking', reasoning))
      if (typeof content === 'string') {
        if (content.trim()) blocks.push(blk(role, 'text', content))
      } else if (Array.isArray(content)) {
        for (const p of content) {
          if (!isPlainObject(p)) continue
          blocks.push(blk(role, p.type ?? null, p.text || p.content))
        }
      }
      if (Array.isArray(msg.tool_calls)) {
        for (const tc of msg.tool_calls) {
          if (!isPlainObject(tc)) continue
          // content = arguments（后端把入参放 content，args 放整个 function 对象），
          // 别"优化"成只放 arguments —— 详情页两侧必须渲染出同样的东西。
          const fn = tc.function || {}
          blocks.push(blk(role, 'toolCall', fn.arguments || '', {
            tool_name: fn.name ?? null,
            args: fn,
          }))
        }
      }
    } else if (role === 'tool') {
      blocks.push(blk(role, 'toolResult', msg.content, {
        tool_name: msg.name || msg.tool_name || null,
      }))
    } else {
      blocks.push(blk(role, 'text', msg.content))
    }
  }
  return blocks
}

// ── openjiuwen ─────────────────────────────────────────────────────────────

/**
 * openjiuwen query<N>.json（turns[]）→ 块数组。
 * 对齐 parse_openjiuwen_trajectory。
 */
export function parseOpenjiuwenTraj(text) {
  let data = null
  try { data = JSON.parse(String(text || '')) } catch { return [] }
  if (!isPlainObject(data)) return []

  const blocks = []
  for (const t of data.turns || []) {
    if (!isPlainObject(t)) continue
    blocks.push(blk('user', 'text', t.user_input))
    if (String(t.agent_content || '').trim()) {
      blocks.push(blk('assistant', 'text', t.agent_content))
    }
    for (const tc of t.tool_calls || []) {
      if (!isPlainObject(tc)) continue
      blocks.push(blk('assistant', 'toolCall', tc.output || '', {
        tool_name: tc.tool ?? null,
        args: tc.input ?? null,
      }))
    }
    const files = t.files || []
    if (files.length) {
      const names = files.filter(isPlainObject).map(f => f.name || '').join(', ')
      blocks.push(blk('assistant', 'text', `[产物]: ${names}`))
    }
  }
  return blocks
}

// ── 分派 ───────────────────────────────────────────────────────────────────

/**
 * 按 kind 解析轨迹文本。任何解析失败都返回 []，由调用方回落到原始 JSON 渲染
 * ——「解析器没认出来」不该表现成一片空白。
 *
 * @param {string} kind  detectTrajKind().kind
 * @param {string} text  文件文本（可能被后端按字符截断）
 * @param {{truncated?: boolean}} [opts] truncated=true 时尾部追加截断标记块
 */
export function parseTrajectory(kind, text, opts = {}) {
  let blocks = []
  try {
    if (kind === 'openclaw') blocks = parseOpenclawTraj(text)
    else if (kind === 'hermes') blocks = parseHermesMessages(text)
    else if (kind === 'openjiuwen') blocks = parseOpenjiuwenTraj(text)
  } catch {
    return []
  }
  if (opts.truncated && blocks.length) {
    blocks = blocks.concat([truncatedBlk('[截断] 文件过大，仅解析已加载的部分')])
  }
  return blocks
}

// ── markdown 渲染判定 ──────────────────────────────────────────────────────
// 放在这里而不是 TrajBlocks.vue 里：.vue 不能被纯 node import，挪到本模块
// 才能进 platform/test/test_trajectory_js.py 的覆盖（这条规则决定了哪些块
// 会被 v-html 渲染，必须有测试锁住）。

// 只有「给人读」的自然语言正文才渲染 markdown（text / thinking，
// assistant 与 user 两侧都算）。排除 toolResult（ls 输出 / JSON / 日志）
// 与 toolCall 的 args（JSON）—— markdown 会把 *、_、# 当语法吃掉，
// 缩进也会被重排。
const MD_PART_TYPES = ['text', 'thinking']

// 没有任何 markdown 标记时不渲染：纯文本经 markdown-it 处理会丢掉连续空行与
// 行首缩进（相邻行合并进同一个 <p>），反而比 <pre> 难读。
const MD_MARKUP_RE = /(^#{1,6}\s)|(^\s*[-*+]\s)|(^\s*\d+[.)]\s)|(\*\*[^\n*]+\*\*)|(`[^\n`]+`)|(^\s*>\s)|(^\s*\|.*\|)|(^\s*```)/m

/** 文本里是否含 markdown 标记（标题/列表/加粗/行内代码/引用/表格/围栏）。 */
export function hasMarkdownMarkup(s) {
  return typeof s === 'string' && MD_MARKUP_RE.test(s)
}

/** 该块的正文是否该按 markdown 渲染。 */
export function isMarkdownBlock(b) {
  return !!b
    && MD_PART_TYPES.includes(b.part_type)
    && typeof b.content === 'string'
    && hasMarkdownMarkup(b.content)
}

function isPlainObject(v) {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

export const HARNESS_LABELS = {
  openclaw: 'openclaw',
  hermes: 'hermes',
  openjiuwen: 'openjiuwen',
}
