<template>
  <div class="artifact-workspace">
    <!-- ========== 第一栏：任务列表 ========== -->
    <aside class="aw-pane aw-tasks">
      <div class="aw-pane-head">
        <div class="aw-pane-title">
          任务列表 <span class="aw-count">{{ filteredTasks.length }}</span>
          <el-button size="small" text :loading="loadingTasks" @click="loadTasks">
            <el-icon><Refresh /></el-icon>
          </el-button>
        </div>
        <el-input v-model="taskKeyword" size="small" clearable placeholder="⌕ 搜索任务名称 / ID" />
      </div>
      <div class="aw-pane-body">
        <div v-if="!tasks.length && !loadingTasks" class="aw-notice-sm">
          暂无可浏览的任务。实例还没产出交付物，或 complete.jsonl 尚未生成。
        </div>
        <div v-for="t in filteredTasks" :key="t.traj_name" class="aw-task"
          :class="{ active: t.traj_name === task, 'is-incomplete': !t.complete }"
          @click="openTask(t)">
          <!-- 整条任务行就是入口：已下载 → 直接打开文件树；未下载 → 立刻排进实时下载队列。
               未完成的行只列出、不接受点击：沙箱里还没产出，OBS 上也就没有可下的东西。
               保留它们是为了让用户看到整体进度，而不是对着空列表猜发生了什么。 -->
          <div class="aw-task-name" :title="t.downloaded ? taskDetailHint(t) : downloadHint(t)">
            <el-icon class="aw-task-icon"><Box /></el-icon>
            <span class="aw-ellipsis">{{ t.traj_name }}</span>
            <span v-if="!t.complete" class="aw-task-state incomplete">未完成</span>
            <span v-else-if="t.downloaded" class="aw-task-ok">✓</span>
            <span v-else class="aw-task-state" :class="t.status">{{ statusLabel(t) }}</span>
          </div>
        </div>
      </div>
      <div class="aw-pane-foot">
        <span v-if="tasks.length">
          已下载 {{ downloadedCount }} / 已完成 {{ completeCount }} / 共 {{ tasks.length }}
        </span>
        <span v-else-if="!loadingTasks">无任务</span>
        <span v-else>加载中…</span>
      </div>
    </aside>

    <!-- ========== 第二栏：文件导航 ========== -->
    <section class="aw-pane aw-files">
      <div class="aw-pane-head">
        <div class="aw-pane-title">
          文件导航
          <el-button size="small" text :loading="loadingTree" @click="loadTree">
            <el-icon><Refresh /></el-icon>
          </el-button>
        </div>
        <div class="aw-crumb" :title="task">{{ task || '未选择任务' }}</div>
        <el-input v-model="keyword" size="small" clearable placeholder="⌕ 搜索文件名、扩展名">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <div class="aw-filters">
          <button v-for="m in VIEW_MODES" :key="m.id" class="aw-filter"
            :class="{ active: viewMode === m.id }" @click="viewMode = m.id">{{ m.label }}</button>
        </div>
        <div v-if="viewMode === 'group'" class="aw-filters aw-filters-sub">
          <button v-for="g in filterChips" :key="g.id" class="aw-filter"
            :class="{ active: groupFilter === g.id }" @click="groupFilter = g.id">
            {{ g.label }}<span v-if="g.count != null" class="aw-chip-count">{{ g.count }}</span>
          </button>
        </div>
      </div>

      <div class="aw-pane-body" v-loading="loadingTree">
        <div v-if="treeError" class="aw-notice-sm">{{ treeError }}</div>
        <div v-else-if="treeTruncated" class="aw-notice-sm">
          文件过多，列表已截断，仅显示部分交付物。请用「下载」获取完整内容。
        </div>

        <!-- 目录视图：本地树，默认展开前两层 -->
        <el-tree
          v-else-if="viewMode === 'tree' && treeData.length"
          :key="treeKey"
          :data="treeData"
          :props="treeProps"
          node-key="path"
          highlight-current
          :default-expanded-keys="defaultExpanded"
          @node-click="onNodeClick"
        >
          <template #default="{ data }">
            <span class="aw-node">
              <el-icon v-if="data.is_dir" class="aw-ico-dir"><Folder /></el-icon>
              <el-icon v-else class="aw-ico-file"><Document /></el-icon>
              <span class="aw-node-name">{{ data.label }}</span>
              <span v-if="!data.is_dir && data.size === 0" class="aw-empty-tag"
                title="OBS 上是 0 字节文件">空</span>
              <span v-if="!data.is_dir && data.size != null" class="aw-node-size">{{ fmtSize(data.size) }}</span>
            </span>
          </template>
        </el-tree>

        <!-- 分组视图：按扩展名分组 -->
        <template v-else-if="viewMode === 'group' && grouped.length">
          <div v-for="g in visibleGroups" :key="g.id" class="aw-group">
            <div class="aw-folder" @click="toggleGroup(g.id)">
              <span class="aw-chev">{{ collapsed.has(g.id) ? '▸' : '▾' }}</span>
              <span>{{ g.label }}</span>
              <span class="aw-folder-count">{{ g.items.length }}</span>
            </div>
            <template v-if="!collapsed.has(g.id)">
              <div v-for="f in g.items" :key="f.rel" class="aw-file"
                :class="{ active: selected && selected.rel === f.rel, empty: f.size === 0 }"
                @click="onNodeClick(f)">
                <span class="aw-file-icon" :class="g.id">{{ iconOf(f.name) }}</span>
                <span class="aw-file-body">
                  <span class="aw-file-name">{{ f.name }}</span>
                  <span class="aw-file-meta">{{ f.rel }} · {{ fmtSize(f.size) }}</span>
                </span>
                <span v-if="f.size === 0" class="aw-empty-tag" title="0 字节文件">空</span>
              </div>
            </template>
          </div>
        </template>

        <div v-else-if="!loadingTree && task" class="aw-notice-sm">
          {{ keyword ? '没有匹配的文件' : '该任务没有文件' }}
        </div>
      </div>
    </section>

    <!-- ========== 第三栏：预览 ========== -->
    <main class="aw-pane aw-main">
      <template v-if="selected">
        <div class="aw-main-head">
          <div class="aw-title">
            <div class="aw-filename">{{ selected.name }}</div>
            <div class="aw-path" :title="selected.rel">{{ task }}/{{ selected.rel }}</div>
          </div>
          <div class="aw-actions">
            <el-button size="small" @click="copyPath">复制</el-button>
            <el-button size="small" type="primary" :loading="downloading" @click="download">下载</el-button>
            <el-button size="small" @click="full = !full">{{ full ? '退出全屏' : '全屏' }}</el-button>
          </div>
        </div>

        <el-tabs v-model="tab" class="aw-tabs">
          <el-tab-pane label="预览" name="preview" />
          <el-tab-pane label="原始内容" name="source" />
          <el-tab-pane label="属性" name="info" />
        </el-tabs>

        <div class="aw-body" :class="{ 'is-full': full }" v-loading="previewLoading">
          <template v-if="tab === 'preview'">
            <div v-if="previewError" class="aw-notice">
              <p>{{ previewError }}</p>
              <el-button size="small" @click="download">下载文件</el-button>
            </div>

            <iframe v-else-if="kind === 'html' && htmlSrcdoc" class="aw-frame"
              :srcdoc="htmlSrcdoc" sandbox="allow-scripts" referrerpolicy="no-referrer" />

            <div v-else-if="kind === 'image'" class="aw-image-wrap">
              <img v-if="blobUrl" :src="blobUrl" class="aw-image" :alt="selected.name" />
            </div>

            <iframe v-else-if="kind === 'pdf'" class="aw-frame" :src="blobUrl" />

            <div v-else-if="kind === 'office'" class="aw-office">
              <iframe v-if="pdfState === 'ready'" class="aw-frame" :src="blobUrl" />
              <div v-else-if="pdfState === 'unsupported'" class="aw-notice">
                <p>服务器未安装 LibreOffice，无法在线预览该格式。</p>
                <el-button size="small" @click="download">下载文件</el-button>
              </div>
              <div v-else-if="pdfState === 'error'" class="aw-notice">
                <p>{{ previewError }}</p>
                <el-button size="small" @click="download">下载文件</el-button>
              </div>
              <el-button v-else size="small" type="primary" :loading="pdfLoading" @click="loadOffice">
                转换并预览
              </el-button>
            </div>

            <div v-else-if="(kind === 'text' || kind === 'markdown') && isEmptyText" class="aw-notice">
              <p>该文件是空的（0 字节）。</p>
              <p class="aw-hint">文件在 OBS 上存在，但没有内容——多见于本次执行未触发的日志文件。</p>
              <el-button size="small" @click="download">下载文件</el-button>
            </div>

            <div v-else-if="kind === 'json' && isEmptyText" class="aw-notice">
              <p>该文件是空的（0 字节）。</p>
              <el-button size="small" @click="download">下载文件</el-button>
            </div>

            <MarkdownView v-else-if="kind === 'markdown' && !isEmptyText" class="aw-markdown"
              :content="text" />

            <!-- JSON：格式化 + 语法高亮。内容来自本实例的沙箱产出、已全部转义，
                 v-html 在这里是安全的（highlightJson 先 escapeHtml 再加 span）。 -->
            <div v-else-if="kind === 'json'" class="aw-json">
              <div v-if="jsonState && !jsonState.ok" class="aw-json-warn">{{ jsonState.error }}</div>
              <pre class="aw-json-body" v-html="jsonHtml" />
            </div>

            <pre v-else-if="kind === 'text'" class="aw-text">{{ text }}</pre>

            <!-- JSONL：每行一个 JSON 对象，逐条渲染成 block。
                 默认只出前 5 条，滚动到底/点按钮再追加 —— 单行可达十几万字符，
                 一次全展开会让浏览器卡死。 -->
            <div v-else-if="kind === 'jsonl'" class="aw-jsonl">
              <div v-if="jsonlState && !jsonlState.ok" class="aw-json-warn">
                {{ jsonlState.error }}
              </div>
              <template v-else>
                <div class="aw-jsonl-head">
                  <span>共 {{ jsonlState.total }} 条</span>
                  <span v-if="jsonlState.bad" class="aw-jsonl-bad">
                    {{ jsonlState.bad }} 条解析失败（按原文显示）
                  </span>
                  <span v-if="jsonlState.truncated">内容被截断，仅解析前 5000 条</span>
                </div>
                <div v-for="b in jsonlBlocks" :key="b.idx" class="aw-jsonl-block">
                  <div class="aw-jsonl-idx">
                    #{{ b.idx + 1 }}
                    <span v-if="!b.ok" class="aw-jsonl-bad">非 JSON，原文</span>
                  </div>
                  <pre class="aw-json-body" v-html="b.html" />
                </div>
                <div v-if="jsonlHasMore" class="aw-jsonl-more">
                  <el-button size="small" @click="jsonlMore">
                    加载更多（已显示 {{ jsonlBlocks.length }} / {{ jsonlState.blocks.length }}）
                  </el-button>
                </div>
              </template>
            </div>

            <div v-else-if="kind === 'table'" class="aw-table-wrap">
              <el-table v-if="tableRows.length" :data="tableRows" size="small"
                :max-height="520" border>
                <el-table-column v-for="(h, i) in tableHeaders" :key="i"
                  :prop="String(i)" :label="h || `列${i + 1}`"
                  min-width="120" show-overflow-tooltip />
              </el-table>
              <div v-else class="aw-notice">
                <p>表格为空或无法解析</p>
                <el-button size="small" @click="download">下载文件</el-button>
              </div>
            </div>

            <div v-else class="aw-notice">
              <p>该类型不支持在线预览，请下载后查看。</p>
              <el-button size="small" @click="download">下载文件</el-button>
            </div>
          </template>

          <pre v-else-if="tab === 'source'" class="aw-text">{{ source }}</pre>

          <div v-else class="aw-props">
            <div class="aw-prop"><span>文件名</span><span>{{ selected.name }}</span></div>
            <div class="aw-prop"><span>相对路径</span><span>{{ selected.rel }}</span></div>
            <div class="aw-prop"><span>大小</span><span>{{ fmtSize(selected.size) || '—' }}</span></div>
            <div class="aw-prop"><span>修改时间</span><span>{{ fmtTime(selected.mtime) || '—' }}</span></div>
            <div class="aw-prop"><span>分组</span><span>{{ groupLabel(groupOf(selected.name)) }}</span></div>
          </div>
        </div>
      </template>

      <div v-else class="aw-empty">
        <el-empty :description="task ? '从左侧选择一个文件预览' : '先从左侧选择一个任务'"
          :image-size="80" />
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Folder, Document, Search, Refresh, Box } from '@element-plus/icons-vue'
import api from '../api'
import MarkdownView from './MarkdownView.vue'
import {
  fmtSize, fmtTime, groupOf, groupLabel, previewKindOf,
  GROUPS, collectLocalRefs, rewriteLocalRefs,
  formatJson, highlightJson, parseJsonl,
} from '../utils/artifacts'

const props = defineProps({
  instanceId: { type: String, required: true },
  // 从「会话列表 → 交付物」跳进来时指定要选中的任务；为空则默认第一个
  initialTask: { type: String, default: '' },
})

const VIEW_MODES = [
  { id: 'tree', label: '目录视图' },
  { id: 'group', label: '分组视图' },
]

const tasks = ref([])
const task = ref('')
const taskKeyword = ref('')
const keyword = ref('')
const loadingTasks = ref(false)
const loadingTree = ref(false)
const requesting = ref('')

const viewMode = ref('tree')
const groupFilter = ref('all')
const collapsed = ref(new Set())

const treeData = ref([])          // el-tree 的嵌套数据
const flatEntries = ref([])       // 扁平文件列表（分组视图与预览用）
const treeError = ref('')
const treeTruncated = ref(false)  // 后端遍历被截断（文件过多/超时）
const treeKey = ref(0)
const defaultExpanded = ref([])

const selected = ref(null)
const tab = ref('preview')
const full = ref(false)

const previewLoading = ref(false)
const previewError = ref('')
const text = ref('')
const source = ref('')
const htmlSrcdoc = ref('')
const blobUrl = ref('')
const downloading = ref(false)
const pdfState = ref('idle')
const pdfLoading = ref(false)

let assetBlobUrls = []

const kind = computed(() => (selected.value ? previewKindOf(selected.value.name) : ''))
const isEmptyText = computed(() =>
  ['text', 'table', 'markdown', 'json', 'jsonl'].includes(kind.value)
  && !text.value && selected.value?.size === 0)

// JSON 预览：格式化 + 高亮。解析失败时退回原文（大文件被后端截断是常态，
// 不该当成错误刷红让用户以为文件坏了）。
const jsonState = computed(() => (kind.value === 'json' ? formatJson(text.value) : null))
const jsonHtml = computed(() =>
  (jsonState.value ? highlightJson(jsonState.value.text) : ''))

// ── JSONL 预览：逐行解析 + 分块懒加载 ──────────────────────────────────────
// 默认只渲染前 JSONL_PAGE 条。trajectory.jsonl 单行可达十几万字符，
// 一次全展开会让浏览器卡死 —— 用户通常只需要看开头几条确认结构。
const JSONL_PAGE = 5
const jsonlLimit = ref(JSONL_PAGE)
const jsonlState = computed(() =>
  (kind.value === 'jsonl' ? parseJsonl(text.value) : null))
// 已展开的块（含高亮后的 HTML）。每条 block 单独 highlight，
// 避免把几百条拼成一个巨大字符串再正则扫一遍。
const jsonlBlocks = computed(() => {
  const st = jsonlState.value
  if (!st) return []
  return st.blocks.slice(0, jsonlLimit.value).map(b => ({
    idx: b.idx,
    ok: b.ok,
    html: highlightJson(b.text),
  }))
})
const jsonlHasMore = computed(() => {
  const st = jsonlState.value
  return !!st && st.blocks.length > jsonlLimit.value
})
function jsonlMore() {
  const st = jsonlState.value
  if (!st) return
  jsonlLimit.value = Math.min(st.blocks.length, jsonlLimit.value + JSONL_PAGE * 4)
}
// 切换文件时重置展开进度，否则上一个文件的展开量会带过来
watch(() => selected.value?.rel, () => { jsonlLimit.value = JSONL_PAGE })

// CSV/TSV 表格预览：只做朴素切分（带引号的复杂 CSV 交给下载后处理，
// 这里是为了「扫一眼」而不是替代 Excel）
const tableHeaders = computed(() => {
  const lines = text.value.split(/\r?\n/).filter(l => l.trim())
  if (!lines.length) return []
  return parseCsvLine(lines[0])
})

const tableRows = computed(() => {
  const lines = text.value.split(/\r?\n/).filter(l => l.trim())
  if (lines.length < 2) return []
  return lines.slice(1, 501).map(line => {
    const cells = parseCsvLine(line)
    const row = {}
    // 列数按表头对齐，多出的补列，少的留空
    const n = Math.max(cells.length, tableHeaders.value.length)
    for (let i = 0; i < n; i += 1) row[String(i)] = cells[i] ?? ''
    return row
  })
})

function parseCsvLine(line) {
  const out = []
  let cur = ''
  let inQuote = false
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (ch === '"') {
      if (inQuote && line[i + 1] === '"') { cur += '"'; i += 1 }
      else inQuote = !inQuote
    } else if ((ch === ',' || ch === '\t') && !inQuote) {
      out.push(cur); cur = ''
    } else {
      cur += ch
    }
  }
  out.push(cur)
  return out
}

const filteredTasks = computed(() => {
  const kw = taskKeyword.value.trim().toLowerCase()
  return kw ? tasks.value.filter(t => t.traj_name.toLowerCase().includes(kw)) : tasks.value
})
const downloadedCount = computed(() => tasks.value.filter(t => t.downloaded).length)
// 已完成 = 沙箱里跑完了（complete=true）。后端已按「已完成优先」排好序，
// 底部三个数字让用户一眼看出 1419 / 7161 这种进度，而不是面对空列表。
const completeCount = computed(() => tasks.value.filter(t => t.complete).length)
const treeProps = { label: 'label', children: 'children', isLeaf: 'leaf' }

// 已下载不再走这里（模板里直接渲染一个 ✓），故只覆盖未完成的状态。
// 状态词表与后端 worker 状态机保持一致（queued/downloading/done/failed/skipped），
// oversize 在后端归为 skipped，这里保留 oversize 映射以兼容旧响应。
function statusLabel(t) {
  return { queued: '排队中', pending: '排队中', downloading: '下载中',
           failed: '下载失败', skipped: '已跳过', oversize: '体积超限',
           // empty = 沙箱没产出，OBS 前缀本身是空的。用中性文案，别让用户以为平台出错。
           empty: '无产出',
           done: '已下载' }[t.status] || t.status
}

// 单行展示后，文件数与体积改到 hover 提示里（不丢信息，也不占行）
function taskDetailHint(t) {
  const bits = []
  if (t.file_count != null) bits.push(`${t.file_count} 个文件`)
  if (t.total_bytes) bits.push(fmtSize(t.total_bytes))
  return bits.join(' · ')
}

// 下载图标按钮的悬停提示：图标没文字，必须把「失败原因」和「当前状态」说清楚，
// 否则用户只知道点不动，不知道为什么。
function downloadHint(t) {
  if (!t.complete) return '该任务尚未跑完，完成后才能下载交付物'
  if (requesting.value === t.traj_name) return '正在提交…'
  // empty = OBS 前缀确认存在但一个文件都没有（沙箱没产出），不是下载失败。
  // 提示语必须说清楚，否则用户会以为平台坏了，反复重试一个注定为空的目录。
  if (t.status === 'empty') return '该任务没有产出交付物（OBS 上没有任何文件）'
  if (t.status === 'failed') return `下载失败，点击重试${t.error ? '：' + t.error : ''}`
  if (t.status === 'skipped') return `已跳过，点击重试${t.error ? '：' + t.error : ''}`
  if (t.status === 'downloading') return '正在下载…'
  if (t.status === 'queued' || t.status === 'pending') return '已排队，点击重新排队'
  return '下载此任务'
}

// ── 数据加载（全部走本地磁盘，无 obsutil） ─────────────────────────────────

async function loadTasks(opts = {}) {
  const quiet = !!opts.quiet
  if (!quiet) loadingTasks.value = true
  try {
    const res = await api.get(`/artifacts/${props.instanceId}/tasks`, { silent: true })
    tasks.value = res.tasks || []
    const prefer = props.initialTask
      && tasks.value.some(t => t.traj_name === props.initialTask) ? props.initialTask : ''
    if (prefer && prefer !== task.value) selectTask(prefer)
    else if (!task.value && tasks.value.length) selectTask(tasks.value[0].traj_name)
  } catch (e) {
    // 轮询期间的偶发失败不该把文件栏刷成报错
    if (!quiet) treeError.value = e?.response?.data?.detail || '任务列表加载失败'
  } finally {
    if (!quiet) loadingTasks.value = false
  }
}

function selectTask(name, force = false) {
  // force=true 用于「下载完成后重新加载」：同名也要重新拉树，
  // 否则用户点完下载、再点同一个任务名没有任何反应（原来直接 return）。
  if (name === task.value && !force) return
  task.value = name
  selected.value = null
  revokeBlob()
  resetPreview()
  loadTree()
}

// 点击任务行：已下载就正常打开；未下载就直接触发下载并开始轮询，
// 让用户「点一下就能拿到」，而不是先点任务、再去找右边的小按钮。
async function openTask(t) {
  // 未完成的任务没有可下的东西：沙箱里还没产出，OBS 前缀上也没有。
  // 点下去只会排进队列、下一轮拿到空目录再判 failed，纯属浪费。
  if (!t.complete) {
    ElMessage.info('该任务尚未跑完，完成后才能下载交付物')
    return
  }
  if (t.downloaded) {
    selectTask(t.traj_name)
    return
  }
  // 失败/跳过状态允许再点（显式重试会清零 attempts 和优先级）
  selectTask(t.traj_name)
  await requestDownload(t)
}

async function loadTree() {
  if (!task.value) return
  loadingTree.value = true
  treeError.value = ''
  try {
    const res = await api.get(`/artifacts/${props.instanceId}/tree`, {
      params: { task: task.value }, silent: true,
    })
    flatEntries.value = res.entries || []
    // 后端在文件数超上限或遍历超时时会截断（单个 task 实测可达 3.3 万文件）。
    // 必须显式提示，否则用户以为自己看到的是全部交付物。
    treeTruncated.value = !!res.truncated
    const { nodes, expanded } = buildTree(flatEntries.value)
    treeData.value = nodes
    defaultExpanded.value = expanded
    treeKey.value += 1
  } catch (e) {
    const detail = e?.response?.data?.detail || '目录加载失败'
    treeError.value = detail
    flatEntries.value = []
    treeData.value = []
    treeTruncated.value = false
  } finally {
    loadingTree.value = false
  }
}

// 扁平 [{rel,name,size,mtime}] → el-tree 嵌套结构；同时算出「前两层」用于默认展开
function buildTree(entries) {
  const root = { children: new Map() }
  for (const e of entries) {
    const segs = e.rel.split('/')
    let cur = root
    segs.forEach((seg, i) => {
      const last = i === segs.length - 1
      if (!cur.children.has(seg)) {
        cur.children.set(seg, {
          label: seg,
          path: segs.slice(0, i + 1).join('/'),
          is_dir: !last,
          leaf: last,
          children: new Map(),
          size: last ? e.size : null,
          mtime: last ? e.mtime : null,
          rel: segs.slice(0, i + 1).join('/'),
          name: seg,
        })
      }
      cur = cur.children.get(seg)
    })
  }

  const expanded = []
  function toArr(node, depth) {
    if (!node.children || node.children.size === 0) {
      return { label: node.label, path: node.path, rel: node.rel, name: node.name,
               is_dir: false, leaf: true, size: node.size, mtime: node.mtime }
    }
    const dirs = []
    const files = []
    for (const child of node.children.values()) {
      const conv = toArr(child, depth + 1)
      if (conv.is_dir) dirs.push(conv); else files.push(conv)
    }
    dirs.sort((a, b) => a.label.localeCompare(b.label, 'zh-Hans-CN'))
    files.sort((a, b) => a.label.localeCompare(b.label, 'zh-Hans-CN'))
    const out = { label: node.label, path: node.path, rel: node.rel,
                  is_dir: true, leaf: false, children: [...dirs, ...files] }
    // 默认展开前两层，避免「打开只看到 4 个目录」的错觉
    if (depth < 2) expanded.push(out.path)
    return out
  }
  const top = []
  for (const child of root.children.values()) {
    top.push(toArr(child, 0))
  }
  top.sort((a, b) => {
    if (!!a.is_dir !== !!b.is_dir) return a.is_dir ? -1 : 1
    return a.label.localeCompare(b.label, 'zh-Hans-CN')
  })
  return { nodes: top, expanded }
}

async function requestDownload(t) {
  requesting.value = t.traj_name
  try {
    // 后端会返回当前真实状态（可能已经是 queued/downloading/done），
    // 不能一律当 queued —— 实测已下载完成的任务再点会被误显示成「排队中」。
    const res = await api.post(`/artifacts/${props.instanceId}/request`, null,
      { params: { task: t.traj_name } })
    t.status = res.data?.status || 'queued'
    if (t.status === 'done') {
      // 服务端已下完：直接刷新，不需要等 worker
      await loadTasks()
      await selectTask(t.traj_name, true)
      return
    }
    ElMessage.success('已排进下载队列，完成后自动打开')
    startPoll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '提交失败')
  } finally {
    requesting.value = ''
  }
}

// ── 下载状态轮询 ────────────────────────────────────────────────────────────
// 单 task 下载实测中位 1.6s（P90 2.2s），但前端原来完全没有轮询 ——
// 点完「下载此任务」状态永远停在「排队中」，用户以为没反应，只能手动刷新。
// 这里每 2 秒刷一次任务列表，发现当前 task 变 done 就自动加载文件树。
const POLL_MS = 2000
const POLL_MAX = 150          // 最多轮询 5 分钟（下载 P99 6.6s，留足余量）
let pollTimer = null
let pollCount = 0

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  pollCount = 0
}

function startPoll() {
  if (pollTimer) return           // 已在轮询，避免叠加
  pollCount = 0
  pollTimer = setInterval(async () => {
    pollCount += 1
    if (pollCount > POLL_MAX) { stopPoll(); return }
    const before = task.value
    await loadTasks({ quiet: true })
    const cur = tasks.value.find(x => x.traj_name === before)
    if (!cur) return
    // 当前选中的 task 下完了 → 立刻拉文件树并停止轮询
    if (cur.downloaded) {
      stopPoll()
      if (before === task.value) await loadTree()
      return
    }
    // 全部到达终态（完成 / 空产出 / 失败 / 跳过）就停止轮询，避免空转。
    // empty 也是终态：OBS 前缀确实没有文件，再等也不会变。
    const pending = tasks.value.some(
      x => !x.downloaded && !['done', 'empty', 'failed', 'skipped'].includes(x.status))
    if (!pending) stopPoll()
  }, POLL_MS)
}

// ── 分组视图（纯前端，同一份树数据） ──────────────────────────────────────

const grouped = computed(() => {
  const map = new Map(GROUPS.map(g => [g.id, []]))
  const kw = keyword.value.trim().toLowerCase()
  for (const f of flatEntries.value) {
    if (kw && !`${f.name} ${f.rel}`.toLowerCase().includes(kw)) continue
    const gid = groupOf(f.name)
    ;(map.get(gid) || map.get('other')).push(f)
  }
  return GROUPS.map(g => ({ ...g, items: map.get(g.id) || [] })).filter(g => g.items.length)
})

const filterChips = computed(() => {
  const all = grouped.value
  return [{ id: 'all', label: '全部', count: all.reduce((n, g) => n + g.items.length, 0) },
    ...all.map(g => ({ id: g.id, label: g.label, count: g.items.length }))]
})

const visibleGroups = computed(() =>
  groupFilter.value === 'all' ? grouped.value
    : grouped.value.filter(g => g.id === groupFilter.value))

function toggleGroup(id) {
  const s = new Set(collapsed.value)
  s.has(id) ? s.delete(id) : s.add(id)
  collapsed.value = s
}

function iconOf(name) {
  return { html: 'H', markdown: 'M', json: '{}', image: '▧', office: 'W', pdf: 'PDF',
           text: '≡', binary: '?' }[
    previewKindOf(name)] || '?'
}

// ── 预览 ──────────────────────────────────────────────────────────────────

function revokeBlob() {
  for (const u of assetBlobUrls) URL.revokeObjectURL(u)
  assetBlobUrls = []
  if (blobUrl.value) { URL.revokeObjectURL(blobUrl.value); blobUrl.value = '' }
}

function resetPreview() {
  previewError.value = ''
  text.value = ''
  source.value = ''
  htmlSrcdoc.value = ''
  pdfState.value = 'idle'
}

async function onNodeClick(data) {
  if (data.is_dir) return
  selected.value = data
  tab.value = 'preview'
  revokeBlob()
  resetPreview()
  await loadPreview()
}

async function loadPreview() {
  if (!selected.value) return
  const rel = selected.value.rel
  previewLoading.value = true
  try {
    // json / jsonl 也走文本读取：后端 media_type() 返回 text/plain，
    // 字节层与 text 完全相同，只是前端多做一步解析 + 高亮。
    if (kind.value === 'text' || kind.value === 'table'
        || kind.value === 'markdown' || kind.value === 'json'
        || kind.value === 'jsonl') {
      const res = await api.get(`/artifacts/${props.instanceId}/content`, {
        params: { task: task.value, key: rel }, silent: true,
      })
      text.value = res.text || ''
      source.value = text.value
    } else if (kind.value === 'html') {
      const res = await api.get(`/artifacts/${props.instanceId}/inline`, {
        params: { task: task.value, key: rel }, responseType: 'text', silent: true,
      })
      // srcdoc 与父页面同源，但 iframe 的 sandbox 属性会切断 origin：
      // 页面自身 JS 能跑，却拿不到父页面的 localStorage。
      htmlSrcdoc.value = await rewriteSiblingAssets(res, rel)
      source.value = res
    } else if (kind.value === 'image' || kind.value === 'pdf') {
      const blob = await api.get(`/artifacts/${props.instanceId}/inline`, {
        params: { task: task.value, key: rel }, responseType: 'blob', silent: true,
      })
      blobUrl.value = URL.createObjectURL(blob)
    }
  } catch (e) {
    previewError.value = e?.response?.data?.detail || '预览加载失败'
  } finally {
    previewLoading.value = false
  }
}

// 交付件里的相对资源在 srcdoc 下没有 base URL，会 404；把同目录下真实存在的
// 引用取回来换成 blob: URL 再注入。
async function rewriteSiblingAssets(html, rel) {
  const dir = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : ''
  const names = flatEntries.value
    .filter(e => {
      const d = e.rel.includes('/') ? e.rel.slice(0, e.rel.lastIndexOf('/')) : ''
      return d === dir
    })
    .map(e => e.name)
  const refs = collectLocalRefs(html, names)
  if (!refs.length) return html

  const urlMap = new Map()
  await Promise.all(refs.map(async name => {
    try {
      const blob = await api.get(`/artifacts/${props.instanceId}/inline`, {
        params: { task: task.value, key: (dir ? dir + '/' : '') + name },
        responseType: 'blob', silent: true,
      })
      const u = URL.createObjectURL(blob)
      assetBlobUrls.push(u)
      urlMap.set(name, u)
    } catch { /* 取不到就保留原引用 */ }
  }))
  return rewriteLocalRefs(html, urlMap)
}

async function loadOffice() {
  if (!selected.value) return
  pdfLoading.value = true
  pdfState.value = 'loading'
  try {
    const blob = await api.get(`/artifacts/${props.instanceId}/pdf`, {
      params: { task: task.value, key: selected.value.rel },
      responseType: 'blob', silent: true,
    })
    blobUrl.value = URL.createObjectURL(blob)
    pdfState.value = 'ready'
  } catch (e) {
    previewError.value = e?.response?.data?.detail || '转换失败'
    pdfState.value = e?.response?.status === 503 ? 'unsupported' : 'error'
  } finally {
    pdfLoading.value = false
  }
}

async function download() {
  if (!selected.value) return
  downloading.value = true
  try {
    const blob = await api.get(`/artifacts/${props.instanceId}/raw`, {
      params: { task: task.value, key: selected.value.rel },
      responseType: 'blob', silent: true,
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = selected.value.name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '下载失败')
  } finally {
    downloading.value = false
  }
}

async function copyPath() {
  if (!selected.value) return
  const p = `${task.value}/${selected.value.rel}`
  try {
    await navigator.clipboard.writeText(p)
    ElMessage.success('路径已复制')
  } catch { ElMessage.info(p) }
}

// 已在本 tab 时再点别的会话的「交付物」：initialTask 变化即切换（组件不重建）
watch(() => props.initialTask, (name) => {
  if (!name) return
  if (tasks.value.some(t => t.traj_name === name)) selectTask(name)
})

onMounted(loadTasks)
// 离开页面必须清掉轮询，否则组件销毁后定时器仍在打接口
onBeforeUnmount(() => { stopPoll(); revokeBlob() })
</script>

<style scoped>
.artifact-workspace {
  display: flex; height: calc(100vh - 260px); min-height: 540px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 8px; background: #fff; overflow: hidden;
}
.aw-pane { display: flex; flex-direction: column; min-width: 0; }
.aw-pane + .aw-pane { border-left: 1px solid var(--el-border-color-lighter, #ebeef5); }
.aw-tasks { width: 258px; flex-shrink: 0; }
.aw-files { width: 310px; flex-shrink: 0; }
.aw-main { flex: 1; }

.aw-pane-head {
  padding: 12px 12px 10px; border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
  display: flex; flex-direction: column; gap: 8px; flex-shrink: 0;
}
.aw-pane-title {
  display: flex; justify-content: space-between; align-items: center;
  font-size: 13px; font-weight: 600;
}
.aw-count {
  background: #edf1f6; color: #7c8a9e; border-radius: 6px;
  padding: 1px 7px; font-size: 11px; font-weight: 400;
}
.aw-pane-body { flex: 1; overflow: auto; padding: 8px; }
.aw-pane-foot {
  padding: 6px 12px; border-top: 1px solid var(--el-border-color-lighter, #ebeef5);
  font-size: 11px; color: var(--text-muted, #909399); flex-shrink: 0;
}

.aw-task { padding: 9px 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.aw-task:hover { background: #f7f9fd; }
.aw-task.active { background: #f0f5ff; border-color: #dce7ff; }
/* 未完成的行只列不点：沙箱里还没产出，点下去也没有可下载的内容。
   用降透明 + 默认光标表达「看得见但不可操作」，不要整行隐藏 ——
   用户需要看到 1419 / 7161 的整体进度。 */
.aw-task.is-incomplete { cursor: default; opacity: 0.55; }
.aw-task.is-incomplete:hover { background: transparent; }
.aw-task-name { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
.aw-task-icon { color: #6e83a1; flex-shrink: 0; }
.aw-task.active .aw-task-icon { color: #396ef0; }
.aw-ellipsis { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aw-task-state {
  margin-left: auto; flex-shrink: 0; font-size: 11px; font-weight: 400;
  color: #159a74;
}
.aw-task-ok { margin-left: auto; flex-shrink: 0; color: #159a74; font-weight: 700; }
.aw-task-state.pending { color: #d68e1b; }
.aw-task-state.failed, .aw-task-state.oversize { color: #e26163; }
.aw-task-state.incomplete { color: #9aa7b8; }

.aw-crumb {
  font-family: monospace; font-size: 11px; color: #7b899b;
  background: #f8fafd; border: 1px solid #ebeff5; border-radius: 6px;
  padding: 5px 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.aw-filters { display: flex; gap: 5px; flex-wrap: wrap; }
.aw-filter {
  border: 0; background: #f5f7fb; padding: 4px 9px; border-radius: 6px;
  color: #7f8da1; font-size: 11px; cursor: pointer;
}
.aw-filter.active { background: #e8efff; color: #3064e7; font-weight: 600; }
.aw-chip-count { margin-left: 4px; opacity: .7; }

.aw-node { display: flex; align-items: center; gap: 5px; width: 100%; min-width: 0; }
.aw-ico-dir { color: #e6a23c; }
.aw-ico-file { color: #909399; }
.aw-node-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.aw-node-size { color: #b0b3b8; font-size: 10px; flex-shrink: 0; }

.aw-group { margin-bottom: 6px; }
.aw-folder {
  display: flex; align-items: center; gap: 6px; padding: 7px 8px;
  font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 6px;
}
.aw-folder:hover { background: #f7f9fd; }
.aw-chev { color: #92a3ba; width: 10px; }
.aw-folder-count { margin-left: auto; font-size: 11px; color: #a4b1c1; font-weight: 400; }
.aw-file {
  display: flex; align-items: center; gap: 8px; padding: 6px 8px 6px 24px;
  border-radius: 6px; cursor: pointer;
}
.aw-file:hover { background: #f7f9ff; }
.aw-file.active { background: #ebf2ff; }
.aw-file-icon {
  width: 22px; height: 22px; display: grid; place-items: center; flex-shrink: 0;
  border-radius: 5px; background: #eaf0fe; color: #5273c8; font-size: 9px; font-weight: 700;
}
.aw-file-icon.html { color: #a863cd; background: #f6edff; }
.aw-file-icon.image { color: #448cce; background: #e9f6ff; }
.aw-file-icon.office { color: #d36b36; background: #fff0e5; }
.aw-file-icon.pdf { color: #e26163; background: #fff0f1; }
.aw-file-icon.text { color: #16a184; background: #e4f8f0; }
.aw-file-body { min-width: 0; flex: 1; }
.aw-file-name { display: block; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aw-file-meta { display: block; font-size: 10px; color: #a0acbb; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.aw-main-head {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 10px 14px; border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
}
.aw-title { min-width: 0; }
.aw-filename { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aw-path {
  font-family: monospace; font-size: 11px; color: #909399;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.aw-actions { display: flex; gap: 6px; flex-shrink: 0; }
.aw-tabs { padding: 0 14px; }
.aw-body { flex: 1; min-height: 0; overflow: auto; padding: 12px 14px; }
.aw-body.is-full { position: fixed; inset: 0; z-index: 2000; background: #fff; padding: 12px; }
.aw-frame { width: 100%; height: 100%; min-height: 420px; border: 1px solid #eee; border-radius: 6px; background: #fff; }
.aw-image-wrap { text-align: center; }
.aw-image { max-width: 100%; max-height: 100%; }
.aw-table-wrap { min-height: 200px; }
.aw-text {
  margin: 0; white-space: pre-wrap; word-break: break-word;
  font: 12px/1.7 Consolas, Monaco, monospace; color: #c9d5e4;
  background: #142136; border-radius: 8px; padding: 16px; min-height: 320px;
}

/* JSON 预览：与 aw-text 同一套深色终端风格，只多语法高亮配色。
   配色按语义分组（键/字符串/数字/关键字），不要超过四种颜色，否则反而难读。 */
.aw-json { display: flex; flex-direction: column; gap: 8px; }
.aw-json-body {
  margin: 0; white-space: pre; overflow: auto; max-height: 620px;
  font: 12px/1.7 Consolas, Monaco, monospace; color: #c9d5e4;
  background: #142136; border-radius: 8px; padding: 16px; min-height: 320px;
}
.aw-json-body :deep(.j-key) { color: #7fd1ff; }
.aw-json-body :deep(.j-str) { color: #a5e075; }
.aw-json-body :deep(.j-num) { color: #ffb454; }
.aw-json-body :deep(.j-kw)  { color: #ff7b9c; }
.aw-json-warn {
  padding: 8px 12px; border-radius: 6px; font-size: 12px;
  color: #8a6d1f; background: #fdf6e3; border: 1px solid #f2e3b3;
}

/* JSONL：每条一个 block，块间留白 + 左侧序号，便于对照「第几条」。 */
.aw-jsonl { display: flex; flex-direction: column; gap: 12px; }
.aw-jsonl-head {
  display: flex; gap: 14px; flex-wrap: wrap;
  font-size: 12px; color: #6e83a1;
}
.aw-jsonl-bad { color: #d68e1b; }
.aw-jsonl-block {
  border: 1px solid #e6ecf5; border-radius: 8px; overflow: hidden;
}
.aw-jsonl-idx {
  display: flex; gap: 8px; align-items: center;
  padding: 4px 10px; font-size: 11px; color: #7b899b;
  background: #f7f9fd; border-bottom: 1px solid #e6ecf5;
}
/* block 内的 pre 去掉圆角与最小高度，由外层圆角统一裁切 */
.aw-jsonl-block .aw-json-body {
  border-radius: 0; min-height: 0; max-height: 480px;
}
.aw-jsonl-more { display: flex; justify-content: center; padding: 4px 0 8px; }
.aw-markdown {
  /* MarkdownView 渲染的是文档正文：深色 <pre> 显然不合适，用白底卡片 + 全局
     .markdown-body 排版（样式在 App.vue 全局段，scoped 命不中 v-html 内容）。 */
  background: #fff; border: 1px solid #ebeef5; border-radius: 8px;
  padding: 20px 24px; min-height: 320px; overflow: auto;
}
.aw-notice { padding: 40px; text-align: center; color: #909399; }
.aw-hint { font-size: 12px; color: #b0b8c4; margin-top: -8px; }
.aw-empty-tag {
  flex-shrink: 0; font-size: 10px; line-height: 1;
  color: #b07d2f; background: #fdf3e3; border: 1px solid #f3e0c2;
  border-radius: 4px; padding: 2px 4px;
}
.aw-file.empty .aw-file-name { color: #97a2b0; }
.aw-notice-sm { padding: 20px 10px; text-align: center; color: #a0acbb; font-size: 12px; }
.aw-props { max-width: 760px; }
.aw-prop {
  display: flex; justify-content: space-between; gap: 16px; padding: 12px 0;
  border-bottom: 1px solid #f0f2f5; font-size: 13px;
}
.aw-prop > span:first-child { color: #909399; flex-shrink: 0; }
.aw-break { overflow-wrap: anywhere; text-align: right; }
.aw-empty { flex: 1; display: flex; align-items: center; justify-content: center; }
</style>
