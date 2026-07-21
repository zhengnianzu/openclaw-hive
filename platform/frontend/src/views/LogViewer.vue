<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>日志查看 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <div class="toolbar">
      <el-select v-model="logSource" placeholder="日志源" style="width:180px" @change="onLogSourceChange">
        <el-option label="主进程日志" value="main.log" />
        <el-option label="完整日志(nohup)" value="nohup.log" />
        <el-option v-for="f in taskLogFiles" :key="f"
          :label="f.replace('.log', '')" :value="f" />
      </el-select>

      <el-input v-model="filterKeyword" placeholder="过滤关键词..." clearable style="width:200px" />

      <el-button @click="scrollToBottom">滚到底部</el-button>
      <el-button @click="loadLogs" :loading="loading">刷新</el-button>
      <el-button @click="clearLogs">清屏</el-button>
    </div>

    <div ref="logContainer" class="log-container">
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
    // 所有日志源(main.log / nohup.log / task-N.log)统一走 raw 模式，1:1 展示本地日志
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: 5000, raw: true }
    })
    rawText.value = res.text ?? ''
    logLines.value = []
    scrollToBottom()
  } catch { /* handled by interceptor */ }
  finally { loading.value = false }
}

async function loadTaskLogFiles() {
  try {
    const res = await api.get(`/logs/${id}/task-log-list`)
    taskLogFiles.value = res.files || []
  } catch { /* ignore */ }
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTaskLogFiles()
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
</style>
