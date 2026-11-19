<template>
  <div v-loading="loading">
    <!-- 头部 -->
    <div class="head">
      <div class="head-left">
        <el-button text @click="router.push('/batch')"><el-icon><Back /></el-icon></el-button>
        <div>
          <div class="title-row">
            <h2 style="margin:0">{{ exp.name || '—' }}</h2>
            <el-tag :type="statusType(exp.status)" size="small">{{ statusLabel(exp.status) }}</el-tag>
          </div>
          <div class="sub">
            {{ exp.id }} ｜ 测试集：{{ exp.test_set_name || '—' }}（{{ exp.sample_total }} 条）
            ｜ {{ exp.arm_count }} 个子任务 ｜ 共 {{ exp.task_total }} 个任务
          </div>
        </div>
      </div>
      <div v-if="authStore.isOperator" style="display:flex;gap:8px">
        <el-button v-if="exp.status === 'paused'" @click="act('resume')">
          <el-icon><VideoPlay /></el-icon> 恢复调度
        </el-button>
        <el-button v-else @click="act('pause')">
          <el-icon><VideoPause /></el-icon> 暂停调度
        </el-button>
        <el-button type="danger" plain @click="act('cancel')">
          <el-icon><CircleClose /></el-icon> 取消实验
        </el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="glass-card tabs-card" @tab-change="onTabChange">
      <!-- ============ 执行概览 ============ -->
      <el-tab-pane label="执行概览" name="overview">
        <div class="stat-grid">
          <div class="stat"><div class="stat-k">已完成</div><div class="stat-v ok">{{ exp.completed }}</div></div>
          <div class="stat"><div class="stat-k">运行中</div><div class="stat-v run">{{ exp.running }}</div></div>
          <div class="stat"><div class="stat-k">排队中</div><div class="stat-v">{{ exp.queued }}</div></div>
          <div class="stat">
            <div class="stat-k">
              成功率
              <el-tooltip placement="top">
                <template #content>
                  DONE 口径：agent 自己打的【Task_Done】标记数 ÷ 已结算任务数。<br>
                  与「完成数」不同 —— 后者只代表流水线没抛异常。
                </template>
                <el-icon class="q"><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
            <div class="stat-v" :style="{color: rateColor(exp.success_rate)}">{{ exp.success_rate }}%</div>
          </div>
        </div>

        <div class="progress-block">
          <div class="progress-head">
            <span>整体进度</span>
            <span class="muted">{{ exp.progress_percent }}% ({{ settled }} / {{ exp.task_total }})</span>
          </div>
          <el-progress :percentage="exp.progress_percent" :stroke-width="14"
            :status="exp.progress_percent >= 100 ? 'success' : undefined" />
        </div>

        <el-table :data="items" size="small" border style="width:100%;margin-top:20px">
          <el-table-column label="Harness" min-width="200">
            <template #default="{row}">
              <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                {{ harnessStore.label(row.harness_type) }}
              </el-tag>
              <span style="margin-left:8px">{{ row.arm_label }}</span>
              <span v-if="row.repeat_idx > 1" class="muted"> · 第{{ row.repeat_idx }}轮</span>
            </template>
          </el-table-column>
          <el-table-column label="任务进度" min-width="200">
            <template #default="{row}">
              <div v-if="row.create_error" class="err">创建失败</div>
              <template v-else>
                <div class="muted">{{ row.completed_tasks + row.failed_tasks }} / {{ row.total_tasks }}</div>
                <el-progress :percentage="row.progress_percent" :stroke-width="8" :show-text="false" />
              </template>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100" align="center">
            <template #default="{row}">
              <el-tag v-if="row.status" size="small" :type="instStatusType(row.status)">{{ row.status }}</el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="DONE" width="90" align="center">
            <template #default="{row}">{{ row.done_tasks }}</template>
          </el-table-column>
          <el-table-column label="失败" width="90" align="center">
            <template #default="{row}">
              <span :style="{color: row.failed_tasks ? '#f56c6c' : ''}">{{ row.failed_tasks }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ============ 任务列表 ============ -->
      <el-tab-pane label="任务列表" name="tasks">
        <el-table :data="items" size="small" border style="width:100%">
          <el-table-column label="子任务" min-width="260">
            <template #default="{row}">
              <el-link v-if="row.instance_id" type="primary"
                @click="router.push(`/instance/${row.instance_id}`)">
                {{ row.instance_name || row.instance_id }}
              </el-link>
              <span v-else class="err">创建失败</span>
              <div v-if="row.create_error" class="err-detail">{{ row.create_error }}</div>
            </template>
          </el-table-column>
          <el-table-column label="Harness" width="150">
            <template #default="{row}">
              <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                {{ row.arm_label || harnessStore.label(row.harness_type) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center">
            <template #default="{row}">
              <el-tag v-if="row.status" size="small" :type="instStatusType(row.status)">{{ row.status }}</el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column label="完成 / 总数" width="130" align="center">
            <template #default="{row}">{{ row.completed_tasks }} / {{ row.total_tasks }}</template>
          </el-table-column>
          <el-table-column label="失败" width="80" align="center" prop="failed_tasks" />
          <el-table-column label="队列" width="160" show-overflow-tooltip prop="queue_id" />
          <el-table-column label="操作" width="160" fixed="right">
            <template #default="{row}">
              <template v-if="row.instance_id">
                <el-button size="small" @click="router.push(`/instance/${row.instance_id}`)">详情</el-button>
                <el-button size="small"
                  @click="router.push(`/instance/${row.instance_id}?tab=logs`)">日志</el-button>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ============ 结果对比 ============ -->
      <el-tab-pane label="结果对比" name="compare">
        <div class="compare-grid">
          <div>
            <div class="block-head">
              <h4 style="margin:0">总体评测结果</h4>
              <el-radio-group v-model="rateMode" size="small">
                <el-radio-button value="done">DONE 率</el-radio-button>
                <el-radio-button value="completed">完成率</el-radio-button>
              </el-radio-group>
            </div>
            <el-table :data="compare" size="small" border style="width:100%">
              <el-table-column label="Harness" min-width="170">
                <template #default="{row}">
                  <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                    {{ row.arm_label }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="rateMode === 'done' ? 'DONE 率' : '完成率'"
                width="110" align="center" sortable :sort-by="r => rateOf(r)">
                <template #default="{row}">
                  <span :style="{color: rateColor(rateOf(row))}">{{ rateOf(row) }}%</span>
                </template>
              </el-table-column>
              <el-table-column label="平均耗时" width="110" align="center"
                sortable :sort-by="r => r.avg_task_seconds ?? Infinity">
                <template #default="{row}">{{ fmtDuration(row.avg_task_seconds) }}</template>
              </el-table-column>
              <el-table-column label="平均 Token" width="110" align="center">
                <template #default="{row}">
                  <el-tooltip content="Token 统计暂未开放" placement="top">
                    <span class="muted">{{ row.total_tokens ?? '—' }}</span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="评分" width="130" align="center"
                sortable :sort-by="r => r.avg_score ?? -1">
                <template #default="{row}">
                  <span v-if="row.avg_score != null" class="score">{{ row.avg_score }}</span>
                  <span v-else class="muted">—</span>
                  <div v-if="row.unscored_count" class="unscored">{{ row.unscored_count }} 条未评估</div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div>
            <h4 style="margin:0 0 12px">关键指标对比</h4>
            <EChart v-if="compare.length" :option="chartOption" height="300px" />
            <el-empty v-else description="暂无数据" />
          </div>
        </div>

        <!-- 逐样本矩阵 -->
        <h4 style="margin:24px 0 12px">
          逐样本横向对比
          <span class="muted">（共 {{ matrix.total }} 条样本，点击格子进入任务详情）</span>
        </h4>
        <el-table :data="matrix.rows" size="small" border style="width:100%" v-loading="matrixLoading">
          <el-table-column label="样本" min-width="220" fixed show-overflow-tooltip>
            <template #default="{row}">{{ row.config_name }}</template>
          </el-table-column>
          <el-table-column v-for="col in matrix.columns" :key="col.instance_id"
            :label="col.arm_label" align="center" min-width="130">
            <template #default="{row}">
              <div v-if="row.cells[col.instance_id]" class="cell-click"
                @click="gotoCell(col.instance_id, row.config_name)">
                <el-icon v-if="row.cells[col.instance_id].task_done" color="#67c23a"><CircleCheck /></el-icon>
                <el-icon v-else color="#f56c6c"><CircleClose /></el-icon>
                <span v-if="row.cells[col.instance_id].traj_level" class="lvl">
                  {{ row.cells[col.instance_id].traj_level }}
                </span>
                <span v-if="row.cells[col.instance_id].eval_score != null" class="lvl">
                  {{ row.cells[col.instance_id].eval_score }}
                </span>
              </div>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无样本数据（任务还没跑出结果）" /></template>
        </el-table>
        <el-pagination v-if="matrix.total > matrix.page_size" style="margin-top:12px;justify-content:flex-end"
          layout="total, prev, pager, next" :total="matrix.total"
          :page-size="matrix.page_size" :current-page="matrix.page" @current-change="loadMatrix" />
      </el-tab-pane>

      <!-- ============ 实验配置 ============ -->
      <el-tab-pane label="实验配置" name="config">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="实验 ID">{{ exp.id }}</el-descriptions-item>
          <el-descriptions-item label="创建人">{{ exp.created_by }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ exp.created_at }}</el-descriptions-item>
          <el-descriptions-item label="测试集">{{ exp.test_set_name }}</el-descriptions-item>
          <el-descriptions-item label="OBS 路径" :span="2">{{ exp.test_set_path }}</el-descriptions-item>
          <el-descriptions-item label="样本数">{{ exp.sample_total }}</el-descriptions-item>
          <el-descriptions-item label="重复次数">{{ exp.repeat_times }}</el-descriptions-item>
          <el-descriptions-item label="调度策略">{{ exp.schedule_mode }}</el-descriptions-item>
          <el-descriptions-item label="并发上限">{{ exp.max_concurrent }}</el-descriptions-item>
          <el-descriptions-item label="队列">{{ exp.queue_id || '（每臂独立）' }}</el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">{{ exp.description || '—' }}</el-descriptions-item>
        </el-descriptions>

        <h4 style="margin:20px 0 8px">公共任务参数<span class="muted">（密钥已脱敏）</span></h4>
        <pre class="params">{{ prettyParams }}</pre>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Back, VideoPlay, VideoPause, CircleClose, CircleCheck, QuestionFilled,
} from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import EChart from '../components/EChart.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()

const batchId = route.params.id
const loading = ref(false)
const matrixLoading = ref(false)
const tab = ref(route.query.tab || 'overview')
const rateMode = ref('done')

const exp = ref({
  completed: 0, running: 0, queued: 0, failed: 0, done: 0,
  task_total: 0, arm_count: 0, sample_total: 0,
  success_rate: 0, progress_percent: 0, params_json: '{}',
})
const items = ref([])
const compare = ref([])
const matrix = ref({ total: 0, page: 1, page_size: 50, columns: [], rows: [] })
let timer = null

const settled = computed(() => (exp.value.completed || 0) + (exp.value.failed || 0))

const prettyParams = computed(() => {
  try { return JSON.stringify(JSON.parse(exp.value.params_json || '{}'), null, 2) }
  catch { return exp.value.params_json || '{}' }
})

const STATUS_LABEL = {
  created: '待开始', running: '运行中', paused: '已暂停',
  completed: '已完成', cancelled: '已取消',
}
const STATUS_TYPE = {
  created: 'info', running: 'success', paused: 'warning',
  completed: '', cancelled: 'danger',
}
const statusLabel = s => STATUS_LABEL[s] || s || '—'
const statusType = s => STATUS_TYPE[s] ?? 'info'

function instStatusType(s) {
  if (s === 'running') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'queued' || s === 'preparing') return 'warning'
  return 'info'
}

function rateColor(v) {
  if (v >= 80) return '#67c23a'
  if (v >= 50) return '#e6a23c'
  return '#f56c6c'
}

const rateOf = r => (rateMode.value === 'done' ? r.done_rate : r.completed_rate)

function fmtDuration(sec) {
  if (sec == null) return '—'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return m ? `${m}m ${s}s` : `${s}s`
}

function gotoCell(instanceId, configName) {
  router.push(`/instance/${instanceId}?tab=outputs&config=${encodeURIComponent(configName)}`)
}

const chartOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['成功率', '评分'] },
  grid: { left: 40, right: 16, top: 40, bottom: 40 },
  xAxis: {
    type: 'category',
    data: compare.value.map(r => r.arm_label),
    axisLabel: { interval: 0, fontSize: 11 },
  },
  yAxis: { type: 'value' },
  series: [
    {
      name: '成功率', type: 'bar',
      data: compare.value.map(r => rateOf(r)),
      itemStyle: { color: '#6366f1' }, barMaxWidth: 36,
    },
    {
      name: '评分', type: 'bar',
      // 未评估的臂不画柱子（null 在 echarts 里就是空缺），不要填 0
      data: compare.value.map(r => r.avg_score),
      itemStyle: { color: '#22d3a7' }, barMaxWidth: 36,
    },
  ],
}))

async function loadMain() {
  try {
    const [e, it] = await Promise.all([
      api.get(`/batch-experiments/${batchId}`, { silent: true }),
      api.get(`/batch-experiments/${batchId}/items`, { silent: true }),
    ])
    exp.value = e
    items.value = it
  } catch { /* interceptor */ }
}

async function loadCompare() {
  try {
    compare.value = await api.get(`/batch-experiments/${batchId}/compare`, { silent: true })
  } catch { compare.value = [] }
}

async function loadMatrix(page = 1) {
  matrixLoading.value = true
  try {
    matrix.value = await api.get(`/batch-experiments/${batchId}/matrix`,
      { params: { page, page_size: matrix.value.page_size }, silent: true })
  } catch { /* interceptor */ } finally {
    matrixLoading.value = false
  }
}

function onTabChange(name) {
  router.replace({ query: { ...route.query, tab: name } })
  // 对比数据只在进入该 tab 时拉，避免概览页每 10s 顺带算一遍聚合
  if (name === 'compare') {
    loadCompare()
    loadMatrix(1)
  }
}

// 终态实验不再轮询：数据不会再变，白耗请求
const isActive = computed(() => ['created', 'running', 'paused'].includes(exp.value.status))

async function act(cmd) {
  const confirms = {
    cancel: '取消实验？将停止所有未完成的子任务。',
  }
  if (confirms[cmd]) {
    try { await ElMessageBox.confirm(confirms[cmd], '确认', { type: 'warning' }) } catch { return }
  }
  try {
    const res = await api.post(`/batch-experiments/${batchId}/${cmd}`)
    ElMessage.success(res.message || '操作成功')
    loadMain()
  } catch { /* interceptor */ }
}

watch(isActive, v => {
  if (!v && timer) { clearInterval(timer); timer = null }
})

onMounted(async () => {
  harnessStore.load()
  loading.value = true
  await loadMain()
  loading.value = false
  if (tab.value === 'compare') { loadCompare(); loadMatrix(1) }
  timer = setInterval(() => { if (isActive.value) loadMain() }, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.head-left { display: flex; gap: 10px; align-items: flex-start; }
.title-row { display: flex; align-items: center; gap: 10px; }
.sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.tabs-card { padding: 0 20px 20px; }
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 8px 0 20px; }
.stat { border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 16px; }
.stat-k { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 4px; }
.stat-v { font-size: 28px; font-weight: 700; margin-top: 4px; }
.stat-v.ok { color: #10b981; }
.stat-v.run { color: #3b82f6; }
.q { cursor: help; }
.progress-block { margin-top: 8px; }
.progress-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px; }
.muted { color: var(--text-muted); font-size: 12px; }
.err { color: #f56c6c; font-size: 12px; }
.err-detail { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
.compare-grid { display: grid; grid-template-columns: 1.3fr 1fr; gap: 20px; align-items: start; }
.block-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.score { color: #10b981; font-weight: 600; }
.unscored { font-size: 11px; color: #e6a23c; margin-top: 2px; }
.cell-click { cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 4px; }
.cell-click:hover { opacity: .7; }
.lvl { font-size: 11px; color: var(--text-secondary); }
.params {
  background: var(--bg-primary); border: 1px solid var(--border-color);
  border-radius: 6px; padding: 12px; font-size: 12px;
  max-height: 360px; overflow: auto; margin: 0;
}
</style>
