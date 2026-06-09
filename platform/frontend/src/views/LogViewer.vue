<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>日志查看 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <el-radio-group v-model="logMode" style="margin-bottom:12px">
      <el-radio-button value="main">主进程日志</el-radio-button>
      <el-radio-button value="realtime">实时日志</el-radio-button>
      <el-radio-button value="clean">错误日志</el-radio-button>
    </el-radio-group>

    <el-select v-model="selectedTask" placeholder="按任务过滤" clearable style="width:280px;margin-left:12px;margin-bottom:12px"
      @change="onTaskFilterChange" filterable>
      <el-option v-for="t in taskList" :key="t.task_idx"
        :label="`Task ${t.task_idx} - ${t.config_name || t.env_id}`"
        :value="t.env_id || t.config_name" />
    </el-select>

    <el-input v-model="filterKeyword" placeholder="过滤关键词..." clearable style="width:200px;margin-left:12px;margin-bottom:12px" />

    <el-button @click="scrollToBottom" style="margin-left:12px">滚到底部</el-button>
    <el-button @click="clearLogs" style="margin-left:4px">清屏</el-button>

    <div ref="logContainer" class="log-container">
      <div v-for="(line, idx) in filteredLines" :key="idx"
        :class="['log-line', lineClass(line)]">{{ line }}</div>
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
const logMode = ref('main')
const filterKeyword = ref('')
const logLines = ref([])
const logContainer = ref(null)
const taskList = ref([])
const selectedTask = ref('')
let ws = null

const filteredLines = computed(() => {
  let lines = logLines.value
  if (selectedTask.value) {
    const kw = selectedTask.value.toLowerCase()
    lines = lines.filter(l => l.toLowerCase().includes(kw))
  }
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    lines = lines.filter(l => l.toLowerCase().includes(kw))
  }
  return lines
})

function onTaskFilterChange() {
  // 切换任务过滤时清空关键词避免冲突
}

function lineClass(line) {
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

function clearLogs() { logLines.value = [] }

function connectWs() {
  if (ws) { ws.close(); ws = null }
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws = new WebSocket(`${protocol}//${location.host}/api/logs/ws/${id}`)
  ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data)
      if (msg.type === 'log') {
        logLines.value.push(msg.data)
        if (logLines.value.length > 5000) logLines.value.splice(0, 1000)
        scrollToBottom()
      }
    } catch { /* ignore non-json */ }
  }
  ws.onerror = () => { setTimeout(connectWs, 3000) }
  ws.onclose = () => { setTimeout(connectWs, 3000) }
}

async function loadStaticLogs(type) {
  try {
    const res = await api.get(`/logs/${id}/${type}`, { params: { tail: 1000 } })
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

watch(logMode, (mode) => {
  logLines.value = []
  if (mode === 'realtime') connectWs()
  else {
    if (ws) { ws.close(); ws = null }
    loadStaticLogs(mode)
  }
})

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTaskList()
  loadStaticLogs('main')
})
onUnmounted(() => { if (ws) ws.close() })
</script>

<style scoped>
.log-container {
  background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 8px;
  height: calc(100vh - 260px); overflow-y: auto; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
}
.log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.log-error { color: #f56c6c; }
.log-warn { color: #e6a23c; }
.log-success { color: #67c23a; }
</style>
