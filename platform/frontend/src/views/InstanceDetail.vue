<template>
  <div v-loading="loading">
    <div class="header-row">
      <el-page-header @back="$router.push('/dashboard')">
        <template #content>{{ inst.name || '实例详情' }}</template>
      </el-page-header>
      <div style="display:flex;gap:8px">
        <el-button @click="$router.push(`/logs/${inst.id}`)">查看日志</el-button>
        <el-button @click="$router.push(`/outputs/${inst.id}`)">查看输出</el-button>
        <el-button type="success" v-if="authStore.isOperator && inst.status !== 'running'" @click="startInstance">启动</el-button>
        <el-button type="danger" v-if="authStore.isOperator && inst.status === 'running'" @click="stopInstance">停止</el-button>
        <el-button type="warning" v-if="authStore.isOperator && inst.failed_tasks > 0 && inst.status !== 'running'" @click="retryFailed">重跑失败</el-button>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><span class="stat-num">{{ overview.total }}</span><span class="stat-label">总任务</span></div>
      <div class="stat-card"><span class="stat-num" style="color:#10b981">{{ overview.completed }}</span><span class="stat-label">已完成</span></div>
      <div class="stat-card"><span class="stat-num" :style="{color: overview.failed > 0 ? '#ef4444' : ''}">{{ overview.failed }}</span><span class="stat-label">失败</span></div>
      <div class="stat-card"><span class="stat-num" style="color:#f59e0b">{{ overview.running }}</span><span class="stat-label">运行中</span></div>
      <div class="stat-card"><span class="stat-num">{{ overview.pending }}</span><span class="stat-label">队列中</span></div>
      <div class="stat-card"><span class="stat-num" style="color:#6366f1">{{ overview.success_rate }}%</span><span class="stat-label">成功率</span></div>
    </div>

    <div class="time-info glass-card" style="padding:12px 20px;margin-bottom:20px">
      <span v-if="overview.elapsed_seconds != null">已用时间: <strong>{{ formatDuration(overview.elapsed_seconds) }}</strong></span>
      <span v-if="overview.avg_task_seconds">平均耗时: <strong>{{ formatDuration(overview.avg_task_seconds) }}</strong></span>
      <span v-if="overview.estimated_remaining_seconds != null && inst.status === 'running'">预计剩余: <strong>{{ formatDuration(overview.estimated_remaining_seconds) }}</strong></span>
      <span v-if="overview.estimated_finish_time && inst.status === 'running'" style="color:var(--text-muted)">
        (预计完成: {{ overview.estimated_finish_time?.replace('T', ' ') }})
      </span>
    </div>

    <div class="glass-card" style="padding:16px 20px;margin-bottom:20px">
      <el-progress :percentage="progressPct" :stroke-width="16" :status="progressStatus"
        :color="[{color:'#6366f1',percentage:50},{color:'#8b5cf6',percentage:80},{color:'#10b981',percentage:100}]" />
    </div>

    <el-row :gutter="20">
      <el-col :span="12">
        <el-card header="实例信息">
          <el-descriptions :column="1" border>
            <el-descriptions-item label="实例ID">{{ inst.id }}</el-descriptions-item>
            <el-descriptions-item label="状态"><el-tag :type="statusColor(inst.status)">{{ statusText(inst.status) }}</el-tag></el-descriptions-item>
            <el-descriptions-item label="PID">{{ inst.pid || '-' }}</el-descriptions-item>
            <el-descriptions-item label="配置文件">{{ inst.config_path }}</el-descriptions-item>
            <el-descriptions-item label="并发数">{{ inst.concurrent_num }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ inst.created_at }}</el-descriptions-item>
            <el-descriptions-item label="启动时间">{{ inst.started_at || '-' }}</el-descriptions-item>
            <el-descriptions-item label="创建者">{{ inst.created_by }}</el-descriptions-item>
            <el-descriptions-item label="模型 API Key">{{ createParams.model_api_key || '默认值' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card header="任务执行情况">
          <div v-if="Object.keys(overview.error_breakdown || {}).length">
            <div v-for="(count, category) in overview.error_breakdown" :key="category" class="breakdown-row">
              <span>{{ category }}</span>
              <el-tag :type="taskTagType(category)" size="small">{{ count }}</el-tag>
            </div>
          </div>
          <el-empty v-else description="暂无数据" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-card header="配置文件" style="margin-top:20px">
      <el-tabs v-model="activeConfigTab" @tab-change="loadConfigContent">
        <el-tab-pane v-for="cf in configFiles" :key="cf.name" :label="cf.name" :name="cf.name" />
      </el-tabs>
      <el-button size="small" style="margin-bottom:12px" @click="loadConfigFiles" :loading="configLoading">刷新</el-button>
      <pre class="config-preview" v-if="configContent">{{ configContent }}</pre>
      <el-empty v-else-if="!configLoading" description="选择配置文件查看" :image-size="40" />
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()
const id = route.params.id
const loading = ref(false)
const inst = ref({})
const overview = ref({ total: 0, completed: 0, failed: 0, running: 0, pending: 0, success_rate: 0, error_breakdown: {} })
const createParams = ref({})
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

function statusColor(s) { return { running: 'success', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || '' }
function statusText(s) { return { running: '运行中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s }
function taskTagType(category) { return { '任务成功': 'success', '任务失败': 'danger', '异常退出': 'warning', '未执行': 'info' }[category] || '' }
function formatDuration(seconds) {
  if (seconds == null) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h${m}m`
  return `${m}min`
}

async function loadData() {
  try { const [i, o] = await Promise.all([api.get(`/instances/${id}`), api.get(`/instances/${id}/overview`)]); inst.value = i; overview.value = o } catch {}
}
async function loadCreateParams() { try { createParams.value = await api.get(`/instances/${id}/create-params`) } catch {} }
async function startInstance() { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadData() }
async function stopInstance() { await ElMessageBox.confirm('确认停止？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadData() }
async function retryFailed() { await api.post(`/instances/${id}/retry-failed`); ElMessage.success('重跑已启动'); loadData() }

const configFiles = ref([]); const configLoading = ref(false); const activeConfigTab = ref(''); const configContent = ref('')
async function loadConfigFiles() {
  configLoading.value = true
  try { const res = await api.get(`/instances/${id}/configs`); configFiles.value = res.files || []; if (configFiles.value.length && !activeConfigTab.value) { activeConfigTab.value = configFiles.value[0].name; loadConfigContent(activeConfigTab.value) } } finally { configLoading.value = false }
}
async function loadConfigContent(filename) { if (!filename) return; try { const res = await api.get(`/instances/${id}/configs/${filename}`); configContent.value = res.content } catch { configContent.value = '' } }

onMounted(() => { loadData(); loadCreateParams(); loadConfigFiles(); timer = setInterval(loadData, 10000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
.stat-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px; }
.stat-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 16px 20px; display: flex; flex-direction: column; align-items: center;
  box-shadow: var(--shadow-sm);
}
.stat-num { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
.time-info { display: flex; gap: 24px; font-size: 13px; color: var(--text-secondary); flex-wrap: wrap; }
.time-info strong { color: var(--text-primary); }
.breakdown-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); }
.breakdown-row:last-child { border-bottom: none; }
.config-preview {
  background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: var(--radius-sm);
  max-height: 500px; overflow: auto; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}
</style>
