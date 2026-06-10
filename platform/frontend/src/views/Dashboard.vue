<template>
  <div>
    <div class="header-row">
      <h2>任务实例管理</h2>
      <el-button type="primary" @click="$router.push('/create')">
        <el-icon><Plus /></el-icon> 新建任务
      </el-button>
    </div>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="6">
        <el-statistic title="总实例" :value="instances.length" />
      </el-col>
      <el-col :span="6">
        <el-statistic title="运行中" :value="instances.filter(i => i.status === 'running').length">
          <template #suffix><span style="color:#67c23a;font-size:14px">个</span></template>
        </el-statistic>
      </el-col>
      <el-col :span="6">
        <el-statistic title="已完成" :value="instances.filter(i => ['completed','finished'].includes(i.status)).length" />
      </el-col>
      <el-col :span="6">
        <el-statistic title="已停止" :value="instances.filter(i => i.status === 'stopped').length" />
      </el-col>
    </el-row>

    <el-table :data="instances" v-loading="loading" stripe style="width:100%">
      <el-table-column prop="name" label="实例名称" min-width="150" />
      <el-table-column prop="id" label="实例ID" width="130">
        <template #default="{row}">
          <span style="font-family:monospace;font-size:12px;color:#909399">{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="120">
        <template #default="{row}">
          <el-tag :type="statusColor(row.status)">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="进度" width="250">
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
      <el-table-column prop="completed_tasks" label="成功" width="80" />
      <el-table-column prop="failed_tasks" label="失败" width="80">
        <template #default="{row}">
          <span :style="{color: row.failed_tasks > 0 ? '#f56c6c' : ''}">{{ row.failed_tasks }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="concurrent_num" label="并发" width="80" />
      <el-table-column prop="created_at" label="创建时间" width="180" />
      <el-table-column label="操作" width="360" fixed="right">
        <template #default="{row}">
          <el-button-group>
            <el-button size="small" @click="$router.push(`/instance/${row.id}`)">详情</el-button>
            <el-button size="small" type="primary" @click="$router.push(`/logs/${row.id}`)">日志</el-button>
            <el-button size="small" type="info" @click="$router.push(`/outputs/${row.id}`)">输出</el-button>
            <el-button size="small" @click="$router.push(`/create?copy_from=${row.id}`)">复制</el-button>
            <el-button size="small" type="success" v-if="row.status !== 'running'" @click="startInstance(row.id)">启动</el-button>
            <el-button size="small" type="danger" v-if="row.status === 'running'" @click="stopInstance(row.id)">停止</el-button>
            <el-button size="small" type="warning" v-if="row.failed_tasks > 0 && row.status !== 'running'" @click="retryFailed(row.id)">重跑</el-button>
            <el-button size="small" type="danger" v-if="row.status !== 'running'" plain @click="deleteInstance(row.id)">删除</el-button>
          </el-button-group>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const instances = ref([])
const loading = ref(false)
const timeEstimates = ref({})
let timer = null

async function loadInstances() {
  loading.value = true
  try {
    instances.value = await api.get('/instances')
    loadTimeEstimates()
  } finally { loading.value = false }
}

async function loadTimeEstimates() {
  const running = instances.value.filter(i => i.status === 'running')
  for (const inst of running) {
    try {
      const ov = await api.get(`/instances/${inst.id}/overview`)
      if (ov.estimated_remaining_seconds != null) {
        timeEstimates.value[inst.id] = ov
      }
    } catch { /* ignore */ }
  }
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
