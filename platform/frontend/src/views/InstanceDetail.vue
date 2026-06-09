<template>
  <div v-loading="loading">
    <div class="header-row">
      <el-page-header @back="$router.push('/dashboard')">
        <template #content>{{ inst.name || '实例详情' }}</template>
      </el-page-header>
      <div>
        <el-button type="primary" @click="$router.push(`/logs/${inst.id}`)">查看日志</el-button>
        <el-button type="success" v-if="inst.status !== 'running'" @click="startInstance">启动</el-button>
        <el-button type="danger" v-if="inst.status === 'running'" @click="stopInstance">停止</el-button>
        <el-button type="warning" v-if="inst.failed_tasks > 0 && inst.status !== 'running'" @click="retryFailed">重跑失败</el-button>
      </div>
    </div>

    <el-row :gutter="16" style="margin-bottom:24px">
      <el-col :span="4"><el-statistic title="总任务" :value="overview.total" /></el-col>
      <el-col :span="4"><el-statistic title="已完成" :value="overview.completed" /></el-col>
      <el-col :span="4">
        <el-statistic title="失败">
          <template #default><span :style="{color: overview.failed > 0 ? '#f56c6c' : ''}">{{ overview.failed }}</span></template>
        </el-statistic>
      </el-col>
      <el-col :span="4"><el-statistic title="运行中" :value="overview.running" /></el-col>
      <el-col :span="4"><el-statistic title="队列中" :value="overview.pending" /></el-col>
      <el-col :span="4"><el-statistic title="成功率" :value="overview.success_rate + '%'" /></el-col>
    </el-row>

    <el-progress :percentage="progressPct" :stroke-width="20" style="margin-bottom:24px" :status="progressStatus" />

    <el-row :gutter="20">
      <el-col :span="12">
        <el-card header="实例信息">
          <el-descriptions :column="1" border>
            <el-descriptions-item label="实例ID">{{ inst.id }}</el-descriptions-item>
            <el-descriptions-item label="状态">
              <el-tag :type="statusColor(inst.status)">{{ statusText(inst.status) }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="PID">{{ inst.pid || '-' }}</el-descriptions-item>
            <el-descriptions-item label="配置文件">{{ inst.config_path }}</el-descriptions-item>
            <el-descriptions-item label="并发数">{{ inst.concurrent_num }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ inst.created_at }}</el-descriptions-item>
            <el-descriptions-item label="启动时间">{{ inst.started_at || '-' }}</el-descriptions-item>
            <el-descriptions-item label="创建者">{{ inst.created_by }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card header="错误分布">
          <div v-if="Object.keys(overview.error_breakdown || {}).length">
            <div v-for="(count, category) in overview.error_breakdown" :key="category"
              style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f0f0f0">
              <span>{{ category }}</span>
              <el-tag type="danger" size="small">{{ count }}</el-tag>
            </div>
          </div>
          <el-empty v-else description="暂无错误" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 配置文件查看 -->
    <el-card header="配置文件" style="margin-top:20px">
      <el-tabs v-model="activeConfigTab" @tab-change="loadConfigContent">
        <el-tab-pane v-for="cf in configFiles" :key="cf.name" :label="cf.name" :name="cf.name" />
      </el-tabs>
      <el-button size="small" style="margin-bottom:12px" @click="loadConfigFiles" :loading="configLoading">刷新</el-button>
      <pre class="config-preview" v-if="configContent">{{ configContent }}</pre>
      <el-empty v-else-if="!configLoading" description="选择配置文件查看" :image-size="40" />
    </el-card>

    <!-- OBS Logs Section -->
    <el-card header="OBS 历史日志" style="margin-top:20px" v-if="['completed','finished','stopped'].includes(inst.status)">
      <el-button @click="loadObsLogs" :loading="obsLoading" style="margin-bottom:12px">加载OBS日志列表</el-button>
      <el-table :data="obsLogs" v-if="obsLogs.length">
        <el-table-column prop="name" label="文件名" />
        <el-table-column label="操作" width="200">
          <template #default="{row}">
            <el-button size="small" @click="viewObsLog(row)">查看</el-button>
            <el-button size="small" type="primary" @click="downloadObsLog(row)">下载</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-dialog v-model="obsLogVisible" title="日志内容" width="80%" destroy-on-close>
        <pre class="log-preview">{{ obsLogContent }}</pre>
      </el-dialog>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'

const route = useRoute()
const router = useRouter()
const id = route.params.id
const loading = ref(false)
const inst = ref({})
const overview = ref({ total: 0, completed: 0, failed: 0, running: 0, pending: 0, success_rate: 0, error_breakdown: {} })
let timer = null

const progressPct = computed(() => {
  if (!overview.value.total) return 0
  return Math.round(((overview.value.completed + overview.value.failed) / overview.value.total) * 100)
})
const progressStatus = computed(() => {
  if (overview.value.failed > 0) return 'warning'
  if (progressPct.value === 100) return 'success'
  return ''
})

function statusColor(s) {
  return { running: 'success', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || ''
}
function statusText(s) {
  return { running: '运行中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s
}

async function loadData() {
  try {
    const [i, o] = await Promise.all([api.get(`/instances/${id}`), api.get(`/instances/${id}/overview`)])
    inst.value = i
    overview.value = o
  } catch (e) { /* will be shown by interceptor */ }
}

async function startInstance() {
  await api.post(`/instances/${id}/start`)
  ElMessage.success('已启动')
  loadData()
}
async function stopInstance() {
  await ElMessageBox.confirm('确认停止？', '提示', { type: 'warning' })
  await api.post(`/instances/${id}/stop`)
  ElMessage.success('已停止')
  loadData()
}
async function retryFailed() {
  await api.post(`/instances/${id}/retry-failed`)
  ElMessage.success('重跑已启动')
  loadData()
}

// OBS Logs
const obsLogs = ref([])
const obsLoading = ref(false)
const obsLogVisible = ref(false)
const obsLogContent = ref('')

// Config Files
const configFiles = ref([])
const configLoading = ref(false)
const activeConfigTab = ref('')
const configContent = ref('')

async function loadConfigFiles() {
  configLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/configs`)
    configFiles.value = res.files || []
    if (configFiles.value.length && !activeConfigTab.value) {
      activeConfigTab.value = configFiles.value[0].name
      loadConfigContent(activeConfigTab.value)
    }
  } finally { configLoading.value = false }
}

async function loadConfigContent(filename) {
  if (!filename) return
  try {
    const res = await api.get(`/instances/${id}/configs/${filename}`)
    configContent.value = res.content
  } catch { configContent.value = '' }
}

async function loadObsLogs() {
  obsLoading.value = true
  try {
    const res = await api.get(`/logs/${id}/obs-logs`)
    obsLogs.value = res.items || []
  } finally { obsLoading.value = false }
}

async function viewObsLog(row) {
  const res = await api.get(`/logs/${id}/obs-view`, { params: { file_path: row.path, tail: 500 } })
  obsLogContent.value = (res.lines || []).join('\n')
  obsLogVisible.value = true
}

function downloadObsLog(row) {
  const token = localStorage.getItem('token')
  window.open(`/api/logs/${id}/obs-download?file_path=${encodeURIComponent(row.path)}&token=${token}`, '_blank')
}

onMounted(() => {
  loadData()
  loadConfigFiles()
  timer = setInterval(loadData, 10000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
.log-preview, .config-preview { background: #1e1e1e; color: #d4d4d4; padding: 16px; border-radius: 8px; max-height: 500px; overflow: auto; font-family: monospace; font-size: 13px; white-space: pre-wrap; word-break: break-all; }
</style>
