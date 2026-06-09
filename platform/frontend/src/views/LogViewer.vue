<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>日志查看 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <div class="toolbar">
      <el-select v-model="selectedTask" placeholder="按任务过滤" clearable style="width:280px"
        @change="onTaskFilterChange" filterable>
        <el-option v-for="t in taskList" :key="t.task_idx"
          :label="`Task ${t.task_idx} - ${t.config_name || t.env_id}`"
          :value="t.env_id || t.config_name" />
      </el-select>

      <el-input v-model="filterKeyword" placeholder="过滤关键词..." clearable style="width:200px" />

      <el-button @click="scrollToBottom">滚到底部</el-button>
      <el-button @click="clearLogs">清屏</el-button>
      <el-switch v-model="verboseMode" active-text="详细日志" inactive-text="简洁" style="margin-left:4px" />
    </div>

    <div ref="logContainer" class="log-container">
      <div v-for="(line, idx) in filteredLines" :key="idx"
        :class="['log-line', lineClass(line)]">{{ verboseMode ? line : cleanLine(line) }}</div>
      <div v-if="!filteredLines.length" style="color:#666;padding:20px;text-align:center">
        {{ logMode === 'realtime' ? '等待日志...' : '暂无日志' }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()
const id = route.params.id
const inst = ref({})
const filterKeyword = ref('')
const logLines = ref([])
const logContainer = ref(null)
const taskList = ref([])
const selectedTask = ref('')
const verboseMode = ref(false)

const filteredLines = computed(() => {
  let lines = logLines.value
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    lines = lines.filter(l => l.toLowerCase().includes(kw))
  }
  return lines
})

function onTaskFilterChange() {
  loadLogs()
}

function lineClass(line) {
  if (/error|failed|exception|traceback/i.test(line)) return 'log-error'
  if (/warning|warn/i.test(line)) return 'log-warn'
  if (/success|completed|done/i.test(line)) return 'log-success'
  return ''
}

function cleanLine(line) {
  const m = line.match(/\|(?:INFO|WARNING|ERROR|DEBUG)\|[^|]*\|[^|]*\|(.+)/)
  if (m) return m[1]
  return line.replace(/^\[node_id:\d+\]/, '').trim()
}

function scrollToBottom() {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
}

function clearLogs() { logLines.value = [] }

async function loadLogs() {
  try {
    const params = { tail: 1000 }
    if (selectedTask.value) {
      params.task_filter = selectedTask.value
    }
    const res = await api.get(`/logs/${id}/main`, { params })
    logLines.value = res.lines || []
    scrollToBottom()
  } catch { /* handled by interceptor */ }
}

async function loadTaskList() {
  try {
    const res = await api.get(`/logs/${id}/tasks`)
    taskList.value = res.tasks || []
  } catch { /* ignore */ }
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTaskList()
  loadLogs()
})
onUnmounted(() => {})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.log-container {
  background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 8px;
  height: calc(100vh - 260px); overflow-y: auto; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
}
.log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.log-error { color: #f56c6c; }
.log-warn { color: #e6a23c; }
.log-success { color: #67c23a; }
</style>
