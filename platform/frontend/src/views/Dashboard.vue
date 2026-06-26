<template>
  <div>
    <div class="header-row">
      <h2>任务实例管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="router.push('/create')">
        <el-icon><Plus /></el-icon> 新建任务
      </el-button>
    </div>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="5">
        <el-statistic title="总实例" :value="instances.length" />
      </el-col>
      <el-col :span="5">
        <el-statistic title="运行中" :value="runningInstances.length">
          <template #suffix><span style="color:#67c23a;font-size:14px">个</span></template>
        </el-statistic>
      </el-col>
      <el-col :span="5">
        <el-statistic title="已完成" :value="instances.filter(i => ['completed','finished'].includes(i.status)).length" />
      </el-col>
      <el-col :span="5">
        <el-statistic title="已停止" :value="instances.filter(i => i.status === 'stopped').length" />
      </el-col>
      <el-col :span="4">
        <el-statistic title="运行中沙箱" :value="totalRunningPods">
          <template #suffix><span style="color:#e6a23c;font-size:14px">个</span></template>
        </el-statistic>
      </el-col>
    </el-row>

    <div style="margin-bottom:12px;display:flex;align-items:center;gap:12px">
      <el-select v-model="statusFilter" placeholder="状态筛选" clearable size="small" style="width:140px">
        <el-option label="运行中" value="running" />
        <el-option label="已完成" value="completed" />
        <el-option label="已结束" value="finished" />
        <el-option label="已停止" value="stopped" />
        <el-option label="待启动" value="created" />
      </el-select>
      <el-input v-model="nameSearch" placeholder="搜索任务名称" clearable size="small" style="width:240px" prefix-icon="Search" />
    </div>

    <el-table :data="filteredInstances" v-loading="loading" stripe style="width:100%">
      <el-table-column prop="name" label="实例名称" min-width="200" show-overflow-tooltip />
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{row}">
          <el-tag :type="statusColor(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" min-width="180">
        <template #default="{row}">
          <div style="display:flex;align-items:center;gap:8px">
            <el-progress :percentage="progress(row)" :stroke-width="10" style="flex:1" />
            <span style="font-size:12px;color:#999">{{ row.completed_tasks + row.failed_tasks }}/{{ row.total_tasks }}</span>
          </div>
          <div v-if="timeEstimates[row.id]" style="font-size:11px;color:#909399;margin-top:2px">
            {{ formatDuration(timeEstimates[row.id].estimated_remaining_seconds) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="completed_tasks" label="成功" width="60" align="center" />
      <el-table-column prop="failed_tasks" label="失败" width="60" align="center">
        <template #default="{row}">
          <span :style="{color: row.failed_tasks > 0 ? '#f56c6c' : ''}">{{ row.failed_tasks }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="concurrent_num" label="并发" width="60" align="center" />
      <el-table-column prop="created_at" label="创建时间" width="160" />
      <el-table-column label="操作" width="280">
        <template #default="{row}">
          <div style="display:flex;align-items:center;gap:4px">
            <el-button size="small" @click="router.push(`/instance/${row.id}`)">详情</el-button>
            <el-button size="small" type="primary" @click="router.push(`/logs/${row.id}`)">日志</el-button>
            <el-button size="small" type="info" @click="router.push(`/outputs/${row.id}`)">输出</el-button>
            <el-dropdown v-if="authStore.isOperator" trigger="click" @command="cmd => handleCommand(cmd, row)">
              <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="copy">复制</el-dropdown-item>
                  <el-dropdown-item v-if="row.status !== 'running'" command="start">启动</el-dropdown-item>
                  <el-dropdown-item v-if="row.status === 'running'" command="stop">停止</el-dropdown-item>
                  <el-dropdown-item v-if="row.failed_tasks > 0 && row.status !== 'running'" command="retry">重跑失败</el-dropdown-item>
                  <el-dropdown-item v-if="row.status !== 'running'" command="delete" divided>删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Search } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const instances = ref([])
const loading = ref(false)
const timeEstimates = ref({})
const statusFilter = ref('')
const nameSearch = ref('')
const router = useRouter()
let timer = null

const runningInstances = computed(() => instances.value.filter(i => i.status === 'running'))

const filteredInstances = computed(() => {
  let list = instances.value
  if (statusFilter.value) {
    list = list.filter(i => i.status === statusFilter.value)
  }
  if (nameSearch.value) {
    const keyword = nameSearch.value.toLowerCase()
    list = list.filter(i => i.name.toLowerCase().includes(keyword))
  }
  return list
})

const totalRunningPods = computed(() => {
  return runningInstances.value.reduce((sum, inst) => sum + inst.concurrent_num, 0)
})

async function loadInstances() {
  const isFirstLoad = instances.value.length === 0
  if (isFirstLoad) loading.value = true
  try {
    instances.value = await api.get('/instances')
    loadTimeEstimates()
  } finally { if (isFirstLoad) loading.value = false }
}

async function loadTimeEstimates() {
  const running = instances.value.filter(i => i.status === 'running')
  const results = await Promise.allSettled(
    running.map(inst => api.get(`/instances/${inst.id}/overview`))
  )
  results.forEach((r, idx) => {
    if (r.status === 'fulfilled' && r.value.estimated_remaining_seconds != null) {
      timeEstimates.value[running[idx].id] = r.value
    }
  })
}

function formatDuration(seconds) {
  if (seconds == null) return ''
  const s = Math.round(seconds)
  if (s < 60) return `~${s}s`
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `~${h}h${m}m`
  return `~${m}min`
}

function statusColor(s) {
  return { running: 'success', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || ''
}
function statusText(s) {
  return { running: '运行中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s
}
function progress(row) {
  if (!row.total_tasks) return 0
  return Math.round(((row.completed_tasks + row.failed_tasks) / row.total_tasks) * 100)
}

async function startInstance(id) {
  await api.post(`/instances/${id}/start`)
  ElMessage.success('已启动')
  loadInstances()
}
async function stopInstance(id) {
  await ElMessageBox.confirm('确认停止该实例？', '提示', { type: 'warning' })
  await api.post(`/instances/${id}/stop`)
  ElMessage.success('已停止')
  loadInstances()
}
async function retryFailed(id) {
  await api.post(`/instances/${id}/retry-failed`)
  ElMessage.success('重跑已启动')
  loadInstances()
}
async function deleteInstance(id) {
  await ElMessageBox.confirm('确认删除该实例？配置文件也会被删除。', '提示', { type: 'warning' })
  await api.delete(`/instances/${id}`)
  ElMessage.success('已删除')
  loadInstances()
}

function handleCommand(cmd, row) {
  const actions = {
    copy: () => router.push(`/create?copy_from=${row.id}`),
    start: () => startInstance(row.id),
    stop: () => stopInstance(row.id),
    retry: () => retryFailed(row.id),
    delete: () => deleteInstance(row.id),
  }
  actions[cmd]?.()
}

onMounted(() => {
  loadInstances()
  timer = setInterval(loadInstances, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: #303133; }
</style>
