<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>日志查看 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <div class="toolbar">
      <el-select-v2 v-model="logSource" placeholder="日志源" style="width:260px"
        :options="taskSelectOptions" filterable @change="onLogSourceChange">
        <template #default="{ item }">
          <span class="task-opt">
            <span class="task-dot" :style="{ background: dotColor(item.st) }"></span>
            <span class="task-opt-name">{{ item.label }}</span>
            <el-tag v-if="markerText(item.st)" size="small" :type="markerType(item.st)"
              disable-transitions class="task-opt-tag">{{ markerText(item.st) }}</el-tag>
          </span>
        </template>
      </el-select-v2>

      <el-select v-model="categoryFilter" placeholder="错误类型筛选" clearable style="width:150px">
        <el-option label="成功" value="success" />
        <el-option label="服务侧失败 S" value="S" />
        <el-option label="任务侧失败 T" value="T" />
        <el-option label="客户端失败 C" value="C" />
        <el-option label="未分类 X" value="X" />
      </el-select>

      <el-input v-model="filterKeyword" placeholder="过滤关键词..." clearable style="width:200px" />

      <el-button @click="scrollToBottom">滚到底部</el-button>
      <el-button @click="loadLogs" :loading="loading">刷新</el-button>
      <el-button @click="clearLogs">清屏</el-button>
    </div>

    <div ref="logContainer" class="log-container" @scroll="onScroll">
      <div v-if="rawText !== null" class="log-more-hint">
        <span v-if="loadingMore">加载中…</span>
        <span v-else-if="logHasMore">上滑加载更早的日志</span>
        <span v-else>已到日志开头</span>
      </div>
      <pre v-if="rawText !== null" class="log-raw">{{ displayRawText }}</pre>
      <template v-else>
        <div v-for="(line, idx) in filteredLines" :key="idx"
          :class="['log-line', lineClass(line)]">{{ line }}</div>
        <div v-if="!filteredLines.length" style="color:#666;padding:20px;text-align:center">
          暂无日志
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()
const id = route.params.id
const inst = ref({})
const filterKeyword = ref('')
const logLines = ref([])
const logContainer = ref(null)
const loading = ref(false)
const logSource = ref('main.log')
const taskLogFiles = ref([])
const rawText = ref(null)
const taskStatusMap = ref({})   // { task_idx: {status, category, code} }
const categoryFilter = ref('')
const logStartOffset = ref(null)  // 当前已加载窗口首字节偏移（load-more 游标）
const logHasMore = ref(false)     // 上方是否还有更早日志
const loadingMore = ref(false)

// task-N.log -> 该 task 的状态对象（从 task_idx 映射）
function taskStatusOf(fname) {
  const m = /task-(\d+)\.log/.exec(fname)
  if (!m) return null
  return taskStatusMap.value[Number(m[1])] || null
}

// 彩色圆点：成功=绿 / S=红 / T=琥珀 / C=紫 / X=灰 / 未知=透明
function dotColor(st) {
  if (!st) return 'transparent'
  if (st.status === '任务成功') return '#10b981'
  switch (st.category) {
    case 'S': return '#ef4444'
    case 'T': return '#f59e0b'
    case 'C': return '#9254de'
    case 'X': return '#909399'
    default: return '#909399'
  }
}
// el-tag 语义类型（紫色 C 无对应语义色，退回 info 灰底，仅圆点区分）
function markerType(st) {
  if (!st) return 'info'
  if (st.status === '任务成功') return 'success'
  return { S: 'danger', T: 'warning', C: 'info', X: 'info' }[st.category] || 'info'
}
function markerText(st) {
  if (!st) return ''
  if (st.status === '任务成功') return '✓'
  return st.category || (st.status === '任务异常' ? 'X' : '')
}

// 按错误类型筛选下拉里的 task 列表
const visibleTaskLogFiles = computed(() => {
  if (!categoryFilter.value) return taskLogFiles.value
  return taskLogFiles.value.filter(f => {
    const st = taskStatusOf(f)
    if (!st) return false
    if (categoryFilter.value === 'success') return st.status === '任务成功'
    return st.category === categoryFilter.value
  })
})

// el-select-v2 的扁平 options：两个固定源 + 全部 task；taskStatusOf 每项只算一次（虚拟化只渲染可视区）
const taskSelectOptions = computed(() => {
  const base = [
    { label: '主进程日志', value: 'main.log', st: null },
    { label: '完整日志(nohup)', value: 'nohup.log', st: null },
  ]
  for (const f of visibleTaskLogFiles.value) {
    base.push({ label: f.replace('.log', ''), value: f, st: taskStatusOf(f) })
  }
  return base
})

const filteredLines = computed(() => {
  let lines = logLines.value
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    lines = lines.filter(l => l.toLowerCase().includes(kw))
  }
  return lines
})

// raw 模式下按关键字过滤保留原换行
const displayRawText = computed(() => {
  if (rawText.value === null) return ''
  if (!filterKeyword.value) return rawText.value
  const kw = filterKeyword.value.toLowerCase()
  return rawText.value
    .split('\n')
    .filter(l => l.toLowerCase().includes(kw))
    .join('\n')
})

function onLogSourceChange() {
  loadLogs()
}

function lineClass(line) {
  if (/^\[STDERR\]/i.test(line)) return 'log-error'
  if (/error|failed|exception|traceback/i.test(line)) return 'log-error'
  if (/warning|warn/i.test(line)) return 'log-warn'
  if (/success|completed|done/i.test(line)) return 'log-success'
  return ''
}

function scrollToBottom() {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
}

function clearLogs() { logLines.value = []; rawText.value = '' }

async function loadLogs() {
  loading.value = true
  try {
    // 首屏只取最新 50 行（raw 模式，1:1 展示），后续上滑用 end 游标向上翻页
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: 50, raw: true }
    })
    rawText.value = res.text ?? ''
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    logLines.value = []
    scrollToBottom()
  } catch { /* handled by interceptor */ }
  finally { loading.value = false }
}

// 上滑到顶时向上加载更早的 50 行，前置到 rawText，并保持视觉滚动位置不跳
async function loadMore() {
  if (!logHasMore.value || loadingMore.value || logStartOffset.value == null) return
  loadingMore.value = true
  const el = logContainer.value
  const prevHeight = el ? el.scrollHeight : 0
  const prevTop = el ? el.scrollTop : 0
  try {
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: 50, raw: true, end: logStartOffset.value }
    })
    const chunk = res.text ?? ''
    if (chunk) rawText.value = chunk + rawText.value
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    await nextTick()
    if (el) el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
  } catch { /* ignore */ }
  finally { loadingMore.value = false }
}

function onScroll() {
  const el = logContainer.value
  if (el && el.scrollTop < 40 && logHasMore.value && !loadingMore.value) loadMore()
}

async function loadTaskLogFiles() {
  try {
    const res = await api.get(`/logs/${id}/task-log-list`)
    taskLogFiles.value = res.files || []
  } catch { /* ignore */ }
}

async function loadTaskStatusMap() {
  try {
    const res = await api.get(`/instances/${id}/task-status-map`)
    taskStatusMap.value = res.map || {}
  } catch { /* ignore */ }
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTaskLogFiles()
  loadTaskStatusMap()
  loadLogs()
})
onUnmounted(() => {})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.log-container {
  background: #1e293b; color: #e2e8f0; padding: 12px; border-radius: var(--radius-md);
  height: calc(100vh - 260px); overflow-y: auto; overflow-x: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
}
.log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.log-raw {
  margin: 0; padding: 0;
  color: inherit; background: transparent;
  font: inherit;
  white-space: pre;          /* 1:1: 保留所有空白与换行，不折行 */
  tab-size: 4;
}
.log-error { color: #f87171; }
.log-warn { color: #fbbf24; }
.log-success { color: #34d399; }

.log-more-hint {
  text-align: center; color: #94a3b8; font-size: 12px;
  padding: 4px 0 8px; user-select: none;
}

.task-opt { display: flex; align-items: center; gap: 8px; }
.task-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.task-opt-name { flex: 1; }
.task-opt-tag { margin-left: auto; }

</style>
