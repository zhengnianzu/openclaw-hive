<template>
  <div class="wm-wrap">
    <!-- ===== 顶部 KPI 横条（对应参考图顶部 5 卡）===== -->
    <div class="wm-kpi-row">
      <div class="wm-kpi">
        <div class="wm-kpi-label">总任务</div>
        <div class="wm-kpi-value">{{ counts.instances_active + counts.instances_queued }}</div>
        <div class="wm-kpi-sub">运行 {{ counts.instances_active }} · 排队 {{ counts.instances_queued }}</div>
      </div>
      <div class="wm-kpi">
        <div class="wm-kpi-label">子任务</div>
        <div class="wm-kpi-value">{{ totalSubtasks }}</div>
        <div class="wm-kpi-sub">已完成 {{ counts.artifact_not_complete != null
          ? (totalSubtasks - counts.artifact_not_complete) : '—' }} · 未跑 {{ counts.artifact_not_complete }}</div>
      </div>
      <div class="wm-kpi">
        <div class="wm-kpi-label">下载队列</div>
        <div class="wm-kpi-value warn">{{ counts.artifact_pending }}</div>
        <div class="wm-kpi-sub">下载中 {{ counts.artifact_downloading }} · 失败 {{ counts.artifact_failed }}</div>
      </div>
      <div class="wm-kpi">
        <div class="wm-kpi-label">平均下载速度</div>
        <div class="wm-kpi-value">{{ avgSpeed != null ? avgSpeed : '—' }}<span class="wm-unit">MB/s</span></div>
        <div class="wm-kpi-sub">峰值 {{ peakSpeed != null ? peakSpeed : '—' }} MB/s</div>
      </div>
      <div class="wm-kpi">
        <div class="wm-kpi-label">磁盘使用</div>
        <div class="wm-kpi-value">{{ disk ? fmtSize(disk.used_bytes) : '—' }}</div>
        <div class="wm-kpi-sub">
          <template v-if="disk">共 {{ fmtSize(disk.total_bytes) }} · {{ disk.used_percent }}%</template>
          <template v-if="disk && disk.output_cache_bytes != null">
            · 产物 {{ fmtSize(disk.output_cache_bytes) }}
          </template>
        </div>
      </div>
    </div>

    <!-- ===== Worker 状态 + 状态分布 ===== -->
    <el-row :gutter="16">
      <el-col :xs="24" :lg="16">
        <div class="glass-card panel">
          <div class="panel-head">
            <span class="panel-title">Worker 资源状态 <span class="wm-count">{{ workers.length }}</span></span>
            <span class="panel-hint">本机进程实测（psutil / /proc）</span>
          </div>
          <el-table :data="workers" size="small" class="wm-table">
            <el-table-column label="" width="34" align="center">
              <template #default="{ row }"><i class="wm-dot" :class="row.state"></i></template>
            </el-table-column>
            <el-table-column prop="label" label="Worker" min-width="130" />
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag size="small" :type="stateTagType(row.state)">{{ stateLabel(row.state) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="CPU" width="72" align="right">
              <template #default="{ row }">{{ row.cpu_percent != null ? row.cpu_percent + '%' : '—' }}</template>
            </el-table-column>
            <el-table-column label="内存" width="82" align="right">
              <template #default="{ row }">{{ row.memory_mb != null ? row.memory_mb + ' MB' : '—' }}</template>
            </el-table-column>
            <el-table-column label="已运行" width="92" align="right">
              <template #default="{ row }">{{ row.uptime_seconds ? fmtDuration(row.uptime_seconds) : '—' }}</template>
            </el-table-column>
            <el-table-column label="上轮" min-width="150">
              <template #default="{ row }">
                <span v-if="row.last_round_at" class="wm-dim">
                  {{ row.last_round_at.slice(11, 19) }} · {{ row.last_duration }}s
                  · {{ row.last_processed }} 条
                </span>
                <span v-else class="wm-dim">尚无心跳</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-col>

      <el-col :xs="24" :lg="8">
        <div class="glass-card panel">
          <div class="panel-head"><span class="panel-title">任务状态分布</span></div>
          <EChart v-if="statusDist.length" :option="statusPieOption" height="230px" />
          <el-empty v-else description="无数据" :image-size="50" />
        </div>
      </el-col>
    </el-row>

    <!-- ===== 负载趋势 + 预计完成 ===== -->
    <el-row :gutter="16">
      <el-col :xs="24" :lg="16">
        <div class="glass-card panel">
          <div class="panel-head">
            <span class="panel-title">下载趋势（近 {{ trendHours }} 小时）</span>
            <el-radio-group v-model="trendHours" size="small" @change="loadTrend">
              <el-radio-button :value="1">1h</el-radio-button>
              <el-radio-button :value="6">6h</el-radio-button>
              <el-radio-button :value="24">24h</el-radio-button>
            </el-radio-group>
          </div>
          <EChart v-if="trendPoints.length" :option="trendOption" height="220px" />
          <el-empty v-else description="暂无心跳数据（worker 跑过一轮后出现）" :image-size="50" />
        </div>
      </el-col>

      <el-col :xs="24" :lg="8">
        <div class="glass-card panel">
          <div class="panel-head"><span class="panel-title">执行时间估计</span></div>
          <div class="wm-eta-grid">
            <div class="wm-eta-item">
              <span class="wm-eta-num">{{ eta?.remaining_seconds ? fmtDuration(eta.remaining_seconds) : '—' }}</span>
              <span class="wm-eta-label">预计总耗时</span>
            </div>
            <div class="wm-eta-item">
              <span class="wm-eta-num">{{ gantt?.avg_seconds ? gantt.avg_seconds + 's' : '—' }}</span>
              <span class="wm-eta-label">单任务均值</span>
            </div>
            <div class="wm-eta-item">
              <span class="wm-eta-num">{{ gantt?.median_seconds ? gantt.median_seconds + 's' : '—' }}</span>
              <span class="wm-eta-label">中位数</span>
            </div>
            <div class="wm-eta-item">
              <span class="wm-eta-num">{{ eta?.pending ?? '—' }}</span>
              <span class="wm-eta-label">待下载</span>
            </div>
          </div>
          <div class="wm-dim" style="margin-top:10px">
            预计完成：
            <b v-if="eta?.estimated_finish_at">{{ eta.estimated_finish_at.replace('T', ' ') }}</b>
            <span v-else>无法估算（近期无下载产出）</span>
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- ===== 甘特图：每任务实际耗时 ===== -->
    <div class="glass-card panel">
      <div class="panel-head">
        <span class="panel-title">任务执行时间（甘特）</span>
        <span class="panel-hint">真实起止时间，来自 artifact_started_at / downloaded_at</span>
      </div>
      <EChart v-if="ganttBars.length" :option="ganttOption" :height="ganttHeight" />
      <el-empty v-else description="暂无已完成的下载" :image-size="50" />
    </div>

    <!-- ===== 队列明细 ===== -->
    <div class="glass-card panel">
      <div class="panel-head">
        <span class="panel-title">下载队列 <span class="wm-count">{{ queueItems.length }}</span></span>
        <div style="display:flex;gap:8px;align-items:center">
          <el-radio-group v-model="queueTab" size="small" @change="loadQueue">
            <el-radio-button value="active">活跃</el-radio-button>
            <el-radio-button value="done">已完成</el-radio-button>
          </el-radio-group>
          <el-button v-if="isOperator" size="small" :loading="reclaiming" @click="reclaim">
            回收卡住任务
          </el-button>
        </div>
      </div>
      <el-table v-if="queueItems.length" :data="queueItems" size="small" max-height="340">
        <el-table-column label="实例" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <div>{{ row.instance_name }}</div>
            <div class="wm-dim" v-if="row.instance_total">
              产物 {{ row.instance_done }}/{{ row.instance_total }}
              <span v-if="row.sandbox_total"> · 沙箱 {{ row.sandbox_completed }}/{{ row.sandbox_total }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="traj_name" label="子任务" min-width="150" show-overflow-tooltip />
        <el-table-column label="状态" width="88" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已下载 / 文件数" width="120" align="right">
          <template #default="{ row }">
            {{ row.total_bytes ? fmtSize(row.total_bytes) : '—' }}
            <span class="wm-dim" v-if="row.file_count">/ {{ row.file_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="速度" width="90" align="right">
          <template #default="{ row }">
            {{ row.speed_mbps != null ? row.speed_mbps + ' MB/s' : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="排队位置" width="88" align="center">
          <template #default="{ row }">{{ row.queue_pos ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="80" align="right">
          <template #default="{ row }">{{ durationOf(row) }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.error" class="wm-err">{{ row.error }}</span>
            <span v-else class="wm-dim">—</span>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="队列为空" :image-size="50" />
    </div>

    <div class="wm-foot">
      <span class="wm-dim" v-if="generatedAt">更新于 {{ generatedAt }}</span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="loadAll">刷新</el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'
import EChart from './EChart.vue'

const props = defineProps({
  isOperator: { type: Boolean, default: false },
})

const loading = ref(false)
const reclaiming = ref(false)
const workers = ref([])
const counts = ref({})
const disk = ref(null)
const eta = ref(null)
const gantt = ref(null)
const queueItems = ref([])
const queueTab = ref('active')
const trendPoints = ref([])
const trendHours = ref(1)
const generatedAt = ref('')

let timer = null

const STATE_LABEL = { running: '运行中', stalled: '疑似卡住', offline: '离线' }
const stateLabel = s => STATE_LABEL[s] || s
const stateTagType = s => ({ running: 'success', stalled: 'warning', offline: 'info' }[s] || 'info')

const STATUS_LABEL = {
  queued: '排队', downloading: '下载中', done: '已完成',
  failed: '失败', skipped: '已跳过',
}
const statusLabel = s => STATUS_LABEL[s] || s
const statusTagType = s => ({
  queued: 'warning', downloading: 'primary', done: 'success',
  failed: 'danger', skipped: 'info',
}[s] || 'info')

function fmtSize(bytes) {
  const v = Number(bytes || 0)
  if (!v) return '0 B'
  if (v < 1024) return `${v} B`
  const u = ['KB', 'MB', 'GB', 'TB']
  let x = v / 1024, i = 0
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i += 1 }
  return `${x.toFixed(x >= 100 ? 0 : 1)} ${u[i]}`
}
function fmtDuration(sec) {
  const s = Number(sec || 0)
  if (!s) return '0s'
  if (s < 60) return `${Math.round(s)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}
function durationOf(row) {
  const t0 = row.started_at, t1 = row.downloaded_at
  if (!t0) return '—'
  const end = t1 || (row.status === 'downloading' ? Date.now() / 1000 : null)
  if (!end) return '—'
  return fmtDuration(end - t0)
}

// 子任务总数 = 各实例 artifact_complete 计数之和 + 未完成
const totalSubtasks = computed(() => {
  const c = counts.value
  const done = (c.artifact_done || 0) + (c.artifact_pending || 0)
    + (c.artifact_downloading || 0) + (c.artifact_failed || 0)
    + (c.artifact_oversize || 0)
  return done + (c.artifact_not_complete || 0)
})

// 平均/峰值下载速度：从甘特数据（真实耗时）算
const avgSpeed = computed(() => {
  const sp = (gantt.value?.items || []).map(i => i.speed_mbps).filter(v => v)
  if (!sp.length) return null
  return (sp.reduce((a, b) => a + b, 0) / sp.length).toFixed(2)
})
const peakSpeed = computed(() => {
  const sp = (gantt.value?.items || []).map(i => i.speed_mbps).filter(v => v)
  return sp.length ? Math.max(...sp).toFixed(2) : null
})

const statusDist = computed(() => {
  const c = counts.value
  return [
    { name: '已完成', value: c.artifact_done || 0, color: '#159a74' },
    { name: '下载中', value: c.artifact_downloading || 0, color: '#2a78d6' },
    { name: '排队', value: c.artifact_pending || 0, color: '#d68e1b' },
    { name: '失败', value: c.artifact_failed || 0, color: '#d03b3b' },
    { name: '未跑', value: c.artifact_not_complete || 0, color: '#b6bfca' },
  ].filter(x => x.value > 0)
})

const statusPieOption = computed(() => ({
  tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
  legend: { bottom: 0, itemWidth: 9, itemHeight: 9, textStyle: { fontSize: 11 } },
  series: [{
    type: 'pie', radius: ['48%', '70%'], center: ['50%', '44%'],
    avoidLabelOverlap: true, label: { show: false },
    data: statusDist.value.map(x => ({
      name: x.name, value: x.value, itemStyle: { color: x.color },
    })),
  }],
}))

const trendOption = computed(() => ({
  grid: { left: 44, right: 16, top: 30, bottom: 26 },
  tooltip: { trigger: 'axis' },
  legend: { data: ['处理条数', '失败'], right: 0, top: 0, itemWidth: 9, itemHeight: 9 },
  xAxis: {
    type: 'category',
    data: trendPoints.value.map(p =>
      new Date(p.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })),
    axisLabel: { fontSize: 10 },
  },
  yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
  series: [
    {
      name: '处理条数', type: 'line', smooth: true, showSymbol: false,
      data: trendPoints.value.map(p => p.processed),
      areaStyle: { opacity: 0.15 }, itemStyle: { color: '#2a78d6' },
    },
    {
      name: '失败', type: 'line', smooth: true, showSymbol: false,
      data: trendPoints.value.map(p => p.failed),
      itemStyle: { color: '#d03b3b' },
    },
  ],
}))

// 甘特：每任务一条横杠，从最早开始时间算相对偏移
const ganttBars = computed(() => {
  const items = (gantt.value?.items || [])
    .filter(i => i.started_at && i.finished_at && i.duration_seconds)
    .slice(0, 30)
    .reverse()
  return items
})

const ganttHeight = computed(() => `${Math.max(180, Math.min(560, ganttBars.value.length * 22 + 60))}px`)

const ganttOption = computed(() => {
  const bars = ganttBars.value
  if (!bars.length) return {}
  const t0 = Math.min(...bars.map(b => b.started_at))
  const names = bars.map(b => b.traj_name)
  const offsets = bars.map(b => +(b.started_at - t0).toFixed(0))
  return {
    grid: { left: 150, right: 60, top: 12, bottom: 30 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const i = ps[0].dataIndex
        const b = bars[i]
        return `${b.traj_name}<br/>耗时 ${b.duration_seconds}s<br/>`
          + `${fmtSize(b.bytes)} · ${b.files} 文件<br/>`
          + `${b.speed_mbps ? b.speed_mbps + ' MB/s' : ''}`
      },
    },
    xAxis: {
      type: 'value', name: '秒', axisLabel: { fontSize: 10 },
      max: Math.max(...offsets.map((o, i) => o + bars[i].duration_seconds)),
    },
    yAxis: {
      type: 'category', data: names,
      axisLabel: { fontSize: 10 }, axisTick: { show: false },
    },
    series: [{
      type: 'bar', stack: 'g', silent: true,
      itemStyle: { color: 'transparent' },
      data: offsets,
    }, {
      type: 'bar', stack: 'g',
      itemStyle: {
        color: (p) => (bars[p.dataIndex].status === 'done' ? '#2a78d6' : '#d03b3b'),
        borderRadius: [3, 3, 3, 3],
      },
      label: { show: true, position: 'right', fontSize: 10, formatter: p => `${bars[p.dataIndex].duration_seconds}s` },
      data: bars.map(b => b.duration_seconds),
    }],
  }
})

// ── 数据加载 ──────────────────────────────────────────────────────────────

async function loadAll() {
  loading.value = true
  try {
    const [ov, et, ga, q] = await Promise.all([
      api.get('/worker-monitor/overview', { silent: true }),
      api.get('/worker-monitor/eta', { silent: true }),
      api.get('/worker-monitor/gantt?limit=40', { silent: true }),
      api.get(`/worker-monitor/queue?status=${queueTab.value}&limit=50`, { silent: true }),
    ])
    workers.value = ov.workers || []
    counts.value = ov.queue || {}
    disk.value = ov.disk || null
    generatedAt.value = (ov.generated_at || '').replace('T', ' ')
    eta.value = et
    gantt.value = ga
    queueItems.value = q.items || []
  } catch { /* 单块失败不影响其他块 */ }
  finally { loading.value = false }
  loadTrend()
}

async function loadTrend() {
  try {
    const tr = await api.get(`/worker-monitor/trend?hours=${trendHours.value}`, { silent: true })
    trendPoints.value = tr.points || []
  } catch { trendPoints.value = [] }
}

async function loadQueue() { loadAll() }

async function reclaim() {
  reclaiming.value = true
  try {
    const r = await api.post('/worker-monitor/reclaim')
    ElMessage.success(r.reclaimed ? `已回收 ${r.reclaimed} 条卡住任务` : '没有需要回收的任务')
    await loadAll()
  } catch { /* interceptor 已提示 */ }
  finally { reclaiming.value = false }
}

onMounted(() => {
  loadAll()
  timer = setInterval(loadAll, 30000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.wm-wrap { display: flex; flex-direction: column; gap: 16px; }
.wm-count {
  background: #edf1f6; color: #7c8a9e; border-radius: 6px;
  padding: 0 6px; font-size: 11px; font-weight: 400;
}
.wm-dim { color: var(--text-muted, #909399); font-size: 11px; }
.wm-err { color: #d03b3b; font-size: 11px; }
.wm-unit { font-size: 13px; font-weight: 400; margin-left: 3px; color: var(--text-muted, #909399); }

.wm-kpi-row {
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
}
@media (max-width: 1400px) { .wm-kpi-row { grid-template-columns: repeat(3, 1fr); } }
@media (max-width: 900px) { .wm-kpi-row { grid-template-columns: repeat(2, 1fr); } }
.wm-kpi {
  background: #fff; border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 10px; padding: 14px 16px;
}
.wm-kpi-label { font-size: 12px; color: #8f9aab; }
.wm-kpi-value {
  font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums;
  margin: 4px 0 2px; color: #303133;
}
.wm-kpi-value.warn { color: #d68e1b; }
.wm-kpi-sub { font-size: 11px; color: #a0acbb; }

.wm-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; background: #25bc8b; }
.wm-dot.stalled { background: #e6a23c; }
.wm-dot.offline { background: #c0c4cc; }

.wm-eta-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.wm-eta-item {
  background: #fafbfd; border-radius: 8px; padding: 12px 10px; text-align: center;
}
.wm-eta-num {
  display: block; font-size: 18px; font-weight: 700;
  font-variant-numeric: tabular-nums; color: #303133;
}
.wm-eta-label { font-size: 11px; color: #8f9aab; }

.wm-foot { display: flex; justify-content: flex-end; align-items: center; gap: 10px; }
.wm-table :deep(.el-table__cell) { padding: 6px 0; }
</style>
