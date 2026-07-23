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

    <div v-if="inst.status === 'running'" class="glass-card" style="padding:16px 20px;margin-bottom:20px">
      <el-progress :percentage="progressPct" :stroke-width="16" :status="progressStatus"
        :color="[{color:'#6366f1',percentage:50},{color:'#8b5cf6',percentage:80},{color:'#10b981',percentage:100}]" />
    </div>

    <!-- 统计面板 -->
    <el-card style="margin-bottom:20px" v-loading="evalLoading">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span>评估统计</span>
          <el-button size="small" @click="loadEvalStats" :loading="evalLoading">刷新</el-button>
        </div>
      </template>
      <div v-if="evalStats.available">
        <div class="eval-two-col">
          <!-- 左列: 6个指标 -->
          <div class="eval-stat-grid">
            <div class="eval-stat-item">
              <span class="eval-stat-num">{{ evalStats.total_samples }}</span>
              <span class="eval-stat-label">总样本量</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#6366f1">{{ evalStats.uploaded_trajs }}</span>
              <span class="eval-stat-label">上传轨迹</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#10b981">{{ taskCompletedCountDisplay }}</span>
              <span class="eval-stat-label">任务完成数</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#10b981">{{ taskCompletedRateDisplay }}</span>
              <span class="eval-stat-label">任务完成率</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#10b981">{{ evalCount }}</span>
              <span class="eval-stat-label">已评估</span>
            </div>
            <div class="eval-stat-item" style="cursor:pointer" @click="scoreDetailVisible = true">
              <span class="eval-stat-num avg-score-link">{{ evalAvgScore != null ? (evalAvgScore * 100).toFixed(1) + '%' : '-' }}</span>
              <span class="eval-stat-label">平均分</span>
            </div>
          </div>
          <!-- 右列: 柱状图 -->
          <div v-if="evalCount > 0" class="score-chart">
            <div class="score-chart-title">分数分布</div>
            <div class="score-chart-body">
              <div v-for="bucket in distributionBuckets" :key="bucket.label" class="score-chart-col">
                <span class="score-chart-count">{{ bucket.count || '' }}</span>
                <div class="score-chart-bar-wrap">
                  <div class="score-chart-bar" :style="{height: bucket.pct + '%'}" />
                </div>
                <span class="score-chart-label">{{ bucket.label }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else description="暂无评估数据（evaluator_use.log 不存在或为空）" :image-size="40" />
    </el-card>

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

    <el-card header="配置文件" style="margin-top:20px">
      <el-tabs v-model="activeConfigTab" @tab-change="loadConfigContent">
        <el-tab-pane v-for="cf in configFiles" :key="cf.name" :label="cf.name" :name="cf.name" />
      </el-tabs>
      <el-button size="small" style="margin-bottom:12px" @click="loadConfigFiles" :loading="configLoading">刷新</el-button>
      <pre class="config-preview" v-if="configContent">{{ configContent }}</pre>
      <el-empty v-else-if="!configLoading" description="选择配置文件查看" :image-size="40" />
    </el-card>

    <!-- 分数明细弹窗 -->
    <el-dialog v-model="scoreDetailVisible" title="评估分数明细" width="900px" destroy-on-close>
      <div style="margin-bottom:12px;color:#666;font-size:13px">
        计算公式：score = Π(gate) × Σ(norm_weight × passed/total)
      </div>
      <el-table :data="scoreDetailRows" stripe border max-height="500" style="width:100%"
        :default-sort="{ prop: 'task', order: 'ascending' }">
        <el-table-column prop="task" label="任务" min-width="180" show-overflow-tooltip sortable />
        <el-table-column label="Gate" width="110" align="center" sortable :sort-by="row => row.gate">
          <template #default="{ row }">
            <div :style="{ color: row.gate === 0 ? '#f56c6c' : '#67c23a', fontWeight: 700 }">{{ row.gate }}</div>
            <div style="font-size:11px;color:#999;margin-top:2px">{{ row.gateExpr }}</div>
          </template>
        </el-table-column>
        <el-table-column label="Bucket 得分" min-width="160">
          <template #default="{ row }">
            <div>{{ row.bucketSum != null ? row.bucketSum.toFixed(4) : '-' }}</div>
            <div style="font-size:11px;color:#999;margin-top:2px">{{ row.bucketExpr }}</div>
          </template>
        </el-table-column>
        <el-table-column label="计算分" width="90" align="center" sortable :sort-by="row => row.score">
          <template #default="{ row }">
            <span :style="{ fontWeight: 700, color: row.score === 0 ? '#f56c6c' : row.score >= 1 ? '#67c23a' : '#e6a23c' }">
              {{ row.score != null ? row.score.toFixed(4) : '-' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Completion" width="110" align="center" sortable :sort-by="row => row.completion ?? -1">
          <template #default="{ row }">
            <span style="color:#999">{{ row.completion != null ? row.completion.toFixed(4) : '-' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top:12px;font-size:13px;color:#666">
        合计: {{ scoreDetailRows.length }} 个任务 ·
        平均分(计算): {{ evalAvgScore != null ? (evalAvgScore * 100).toFixed(1) + '%' : '-' }} ·
        平均分(completion): {{ completionAvgDisplay }}
      </div>
    </el-dialog>
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
const evalLoading = ref(false)
const evalStats = ref({ available: false, total_samples: 0, uploaded_trajs: 0, task_scores: {} })
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

async function loadData() {
  try { const [i, o] = await Promise.all([api.get(`/instances/${id}`), api.get(`/instances/${id}/overview`)]); inst.value = i; overview.value = o } catch {}
}
async function loadCreateParams() { try { createParams.value = await api.get(`/instances/${id}/create-params`) } catch {} }
async function startInstance() { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadData() }
async function stopInstance() { await ElMessageBox.confirm('确认停止？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadData() }
async function retryFailed() { await api.post(`/instances/${id}/retry-failed`); ElMessage.success('重跑已启动'); loadData() }

async function loadEvalStats() {
  evalLoading.value = true
  try { evalStats.value = await api.get(`/logs/${id}/eval-stats`) } catch { evalStats.value = { available: false, task_scores: {} } } finally { evalLoading.value = false }
}

const evalScores = computed(() => Object.values(evalStats.value.task_scores || {}))
const evalCount = computed(() => evalScores.value.length)
const evalAvgScore = computed(() => {
  if (!evalScores.value.length) return null
  return evalScores.value.reduce((a, b) => a + b, 0) / evalScores.value.length
})

const scoreDetailVisible = ref(false)
const scoreDetailRows = computed(() => {
  const details = evalStats.value.task_eval_details || {}
  if (!Object.keys(details).length) return []
  return Object.entries(details)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([task, d]) => ({
      task,
      gate: d.gate,
      gateExpr: d.gate_expr || '-',
      gateStatus: d.gate_status || {},
      bucketExpr: d.bucket_expr || '-',
      bucketSum: d.bucket_sum,
      score: d.score,
      completion: d.completion,
    }))
})
const completionAvgDisplay = computed(() => {
  const rows = scoreDetailRows.value.filter(r => r.completion != null)
  if (!rows.length) return '-'
  const avg = rows.reduce((a, r) => a + r.completion, 0) / rows.length
  return (avg * 100).toFixed(1) + '%'
})
const taskCompletedCountDisplay = computed(() => {
  const completed = evalStats.value.task_completed || {}
  const total = Object.keys(completed).length
  if (!total) return '-'
  return Object.values(completed).filter(Boolean).length
})
const taskCompletedRateDisplay = computed(() => {
  const completed = evalStats.value.task_completed || {}
  const total = Object.keys(completed).length
  if (!total) return '-'
  const count = Object.values(completed).filter(Boolean).length
  const pct = ((count / total) * 100).toFixed(1)
  return `${pct}%`
})
const evalScoreDistribution = computed(() => {
  const dist = {}
  for (const s of evalScores.value) {
    const bucket = Math.min(Math.floor(s * 10), 10) * 10 + '%'
    dist[bucket] = (dist[bucket] || 0) + 1
  }
  return dist
})
const distributionBuckets = computed(() => {
  const dist = evalScoreDistribution.value
  const total = evalScores.value.length || 1
  const maxCount = Math.max(1, ...Object.values(dist))
  const buckets = []
  for (let i = 0; i <= 10; i++) {
    const label = i * 10 + '%'
    const count = dist[label] || 0
    buckets.push({
      label,
      count,
      pct: (count / maxCount) * 100,
      ratio: total > 0 ? ((count / total) * 100).toFixed(0) : '0',
    })
  }
  return buckets
})

const configFiles = ref([]); const configLoading = ref(false); const activeConfigTab = ref(''); const configContent = ref('')
async function loadConfigFiles() {
  configLoading.value = true
  try { const res = await api.get(`/instances/${id}/configs`); configFiles.value = res.files || []; if (configFiles.value.length && !activeConfigTab.value) { activeConfigTab.value = configFiles.value[0].name; loadConfigContent(activeConfigTab.value) } } finally { configLoading.value = false }
}
async function loadConfigContent(filename) { if (!filename) return; try { const res = await api.get(`/instances/${id}/configs/${filename}`); configContent.value = res.content } catch { configContent.value = '' } }

onMounted(() => { loadData(); loadCreateParams(); loadConfigFiles(); loadEvalStats(); timer = setInterval(loadData, 10000) })
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
/* 评估统计: 两列布局 7:3 */
.eval-two-col { display: flex; gap: 24px; align-items: stretch; }
.eval-stat-grid {
  flex: 7;
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  align-content: center;
}
.eval-stat-item { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
.eval-stat-num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.2; }
.avg-score-link {
  color: #f59e0b !important;
  text-decoration: underline;
  text-decoration-style: dashed;
  text-underline-offset: 4px;
}
.avg-score-link:hover { text-decoration-style: solid; }
.eval-stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

/* 柱状图 */
.score-chart { flex: 3; min-width: 0; }
.score-chart-title { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; }
.score-chart-body {
  display: flex; align-items: flex-end; gap: 2px;
  height: 120px; padding-bottom: 20px; position: relative;
  border-bottom: 1px solid #e5e7eb;
}
.score-chart-col {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  height: 100%; position: relative;
}
.score-chart-count {
  font-size: 10px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; top: 0; white-space: nowrap;
}
.score-chart-bar-wrap {
  flex: 1; width: 100%; display: flex; align-items: flex-end;
  padding-top: 16px;
}
.score-chart-bar {
  width: 100%; max-width: 32px; margin: 0 auto;
  background: linear-gradient(180deg, #6366f1, #8b5cf6);
  border-radius: 3px 3px 0 0; transition: height 0.3s ease;
  min-height: 2px;
}
.score-chart-label {
  font-size: 10px; color: var(--text-muted); font-variant-numeric: tabular-nums;
  position: absolute; bottom: -18px; white-space: nowrap;
}
</style>
