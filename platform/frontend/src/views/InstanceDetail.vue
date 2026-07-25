<template>
  <div v-loading="loading">
    <div class="header-row">
      <el-page-header @back="$router.push('/dashboard')">
        <template #content>{{ inst.name || '实例详情' }}</template>
      </el-page-header>
      <div style="display:flex;gap:8px">
        <el-button @click="$router.push(`/logs/${inst.id}`)">查看日志</el-button>
        <el-button @click="$router.push(`/outputs/${inst.id}`)">查看输出</el-button>
        <el-button type="success" v-if="authStore.isOperator && !['running','preparing'].includes(inst.status)" @click="startInstance">启动</el-button>
        <el-button type="danger" v-if="authStore.isOperator && inst.status === 'running'" @click="stopInstance">停止</el-button>
        <el-button type="warning" v-if="authStore.isOperator && inst.failed_tasks > 0 && !['running','preparing'].includes(inst.status)" @click="retryFailed">重跑失败</el-button>
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

    <div class="time-info glass-card" style="padding:12px 20px;margin-bottom:20px"
      v-if="overview.elapsed_seconds != null || overview.avg_task_seconds || overview.estimated_remaining_seconds != null">
      <span v-if="overview.elapsed_seconds != null">已用时间: <strong>{{ formatDuration(overview.elapsed_seconds) }}</strong></span>
      <span v-if="overview.avg_task_seconds">平均耗时: <strong>{{ formatDuration(overview.avg_task_seconds) }}</strong></span>
      <span v-if="overview.estimated_remaining_seconds != null && inst.status === 'running'">预计剩余: <strong>{{ formatDuration(overview.estimated_remaining_seconds) }}</strong></span>
      <span v-if="overview.estimated_finish_time && inst.status === 'running'" style="color:var(--text-muted)">
        (预计完成: {{ overview.estimated_finish_time?.replace('T', ' ') }})
      </span>
    </div>

    <div v-if="inst.status === 'running'" class="glass-card" style="padding:16px 20px;margin-bottom:20px">
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
          </el-descriptions>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card header="任务执行情况">
          <div v-if="Object.keys(overview.error_breakdown || {}).length">
            <div v-for="(count, category) in overview.error_breakdown" :key="category"
              :class="['breakdown-row', category.startsWith('└') ? 'breakdown-sub' : '']">
              <span>{{ category }}</span>
              <el-tag :type="taskTagType(category)" size="small">{{ count }}</el-tag>
            </div>
          </div>
          <el-empty v-else description="暂无数据" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-card header="创建参数" style="margin-top:20px" v-if="Object.keys(createParams).length">
      <el-collapse v-model="expandedPanels">
        <el-collapse-item title="基本配置" name="basic">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="任务标识">{{ createParams.task_name || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Harness类型">
              <el-tag size="small" :type="createParams.harness_type === 'hermes' ? 'warning' : createParams.harness_type === 'claude-code' ? 'success' : 'primary'">
                {{ createParams.harness_type || 'openclaw' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="Invite Code">{{ createParams.invite_code || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Harness配置ID">{{ createParams.harness_config_id || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item title="OBS配置" name="obs">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="技能目录">{{ createParams.skill_dir || '-' }}</el-descriptions-item>
            <el-descriptions-item label="默认技能">{{ createParams.default_skills || '-' }}</el-descriptions-item>
            <el-descriptions-item label="Agent目录">{{ createParams.agent_dir || '-' }}</el-descriptions-item>
            <el-descriptions-item label="用户Config目录">{{ createParams.user_config_dir || '-' }}</el-descriptions-item>
            <el-descriptions-item label="用户Profile目录">{{ createParams.user_profile_dir || '-' }}</el-descriptions-item>
            <el-descriptions-item label="轨迹保存路径">{{ createParams.traj_save_path || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item title="模型配置" name="model">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="Base URL">{{ createParams.model_base_url || '-' }}</el-descriptions-item>
            <el-descriptions-item label="API Key">{{ maskKey(createParams.model_api_key) }}</el-descriptions-item>
            <el-descriptions-item label="API类型">{{ createParams.model_api_type || '-' }}</el-descriptions-item>
            <el-descriptions-item label="模型ID">{{ createParams.model_id || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>

        <el-collapse-item title="用户模拟配置" name="agents">
          <template v-if="createParams.agents && createParams.agents.length">
            <div v-for="(ag, idx) in createParams.agents" :key="idx" :style="idx > 0 ? 'margin-top:12px' : ''">
              <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text-secondary)">Agent {{ idx + 1 }}: {{ ag.name || '-' }}</div>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="Provider">{{ ag.provider || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Base URL">{{ ag.base_url || '-' }}</el-descriptions-item>
                <el-descriptions-item label="API Key">{{ maskKey(ag.api_key) }}</el-descriptions-item>
                <el-descriptions-item label="API类型">{{ ag.api || '-' }}</el-descriptions-item>
                <el-descriptions-item label="模型ID">{{ ag.model || '-' }}</el-descriptions-item>
              </el-descriptions>
            </div>
          </template>
          <template v-else-if="createParams.user_proxy_model_name || createParams.user_proxy_base_url">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="模型名称">{{ createParams.user_proxy_model_name || '-' }}</el-descriptions-item>
              <el-descriptions-item label="Base URL">{{ createParams.user_proxy_base_url || '-' }}</el-descriptions-item>
              <el-descriptions-item label="API Key">{{ maskKey(createParams.user_proxy_api_key) }}</el-descriptions-item>
            </el-descriptions>
          </template>
          <el-empty v-else description="未配置" :image-size="40" />
        </el-collapse-item>

        <el-collapse-item title="高级配置" name="advanced">
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="起始索引">{{ createParams.start_index ?? 0 }}</el-descriptions-item>
            <el-descriptions-item label="任务总数">{{ createParams.total_num ? createParams.total_num : '不限' }}</el-descriptions-item>
            <el-descriptions-item label="镜像名称">{{ createParams.image_name || '-' }}</el-descriptions-item>
            <el-descriptions-item label="代码仓ID">{{ createParams.code_repo_id || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-collapse-item>
      </el-collapse>
    </el-card>

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
const expandedPanels = ref([])
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

function statusColor(s) { return { running: 'success', preparing: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || '' }
function statusText(s) { return { running: '运行中', preparing: '准备中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s }
function taskTagType(category) {
  const map = {
    '任务成功': 'success',
    '任务失败': 'danger',
    '任务异常': 'warning',
    '└ C 客户端': 'warning',
    '└ S 服务端': 'warning',
    '└ T 任务侧': 'warning',
    '└ X 未分类': 'warning',
    '未执行': 'info',
  }
  return map[category] || ''
}
function formatDuration(seconds) {
  if (seconds == null) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h${m}m`
  return `${m}min`
}
function maskKey(key) {
  if (!key || key.length <= 8) return key || '-'
  return key.slice(0, 4) + '***' + key.slice(-4)
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
.breakdown-sub {
  padding: 4px 0 4px 16px;
  border-bottom: none;
  font-size: 12px;
  color: var(--text-muted);
}
.config-preview {
  background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: var(--radius-sm);
  max-height: 500px; overflow: auto; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}
</style>
