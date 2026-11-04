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
        <!-- 产物已改纯点击驱动：artifact_pending/downloading 结构上恒为 0
             （没人点就不入队），盯这两个数永远是 0，没有信息量。
             改成看「待点开」与「已下载」—— 前者是可下载但没人要的量。 -->
        <div class="wm-kpi-label">交付物待点开</div>
        <div class="wm-kpi-value">{{ fmtNum(counts.artifact_pending_click) }}</div>
        <div class="wm-kpi-sub">
          排队中 {{ counts.artifact_pending ?? 0 }} · 下载中 {{ counts.artifact_downloading ?? 0 }}
          · 失败 {{ counts.artifact_failed ?? 0 }}
        </div>
      </div>
      <div class="wm-kpi">
        <div class="wm-kpi-label">已下载</div>
        <div class="wm-kpi-value">{{ counts.artifact_done ?? '—' }}</div>
        <div class="wm-kpi-sub">累计 {{ counts.artifact_done_bytes ? fmtSize(counts.artifact_done_bytes) : '—' }}</div>
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
          <div class="panel-head">
            <span class="panel-title">车道负载</span>
            <span class="panel-hint" :class="{ bad: anyOverloaded }">
              {{ anyOverloaded ? '有车道过载' : '正常' }}
            </span>
          </div>
          <!-- 原来这里是「浅层分级进度」：只给全库累计的已分级/待分级（1000 万量级
               的静态数字），看不出此刻堵不堵。改成按车道看实际负载 ——
               排队多少 / 吞吐多少 / 多久清完 / 新增会不会压过消化。 -->
          <div v-if="!lanes.length" class="wm-dim" style="margin-top:8px">加载中…</div>
          <div v-for="L in lanes" :key="L.lane" class="wm-lane">
            <div class="wm-lane-head">
              <span class="wm-lane-name">{{ L.label }}</span>
              <el-tag v-if="L.overloaded" type="danger" size="small" effect="plain">
                {{ L.throughput_per_min > 0 ? '积压中' : '无吞吐' }}
              </el-tag>
              <span v-else-if="!L.pending" class="wm-lane-ok">空闲</span>
            </div>
            <div class="wm-lane-nums">
              <span><b>{{ fmtNum(L.pending) }}</b> 待办</span>
              <span>消化 <b>{{ L.throughput_per_min }}</b>/分</span>
              <span :class="{ bad: L.overloaded && L.inflow_per_min > L.throughput_per_min }">
                新增 <b>{{ L.inflow_per_min }}</b>/分
              </span>
              <span>预计 <b>{{ fmtEta(L.eta_seconds) }}</b></span>
            </div>
          </div>
          <div class="wm-dim" style="margin-top:8px">
            「新增 &gt; 消化」且有待办 = 会持续积压；吞吐为 0 但有待办 = worker 可能卡住
          </div>
        </div>
      </el-col>
    </el-row>

    <!-- ===== 负载趋势 ===== -->
    <!-- 原来这一行右侧是「执行时间估计」面板：它的三个数（预计总耗时/任务每分钟/
         待下载）全部取自产物车道，而产物已改纯点击驱动、队列恒为 0，算出来永远是
         「—」。而且「车道负载」面板已经按车道给出了同样的信息且更全（含新增速率
         与过载判定），所以删掉，趋势图占满整行。 -->
    <el-row :gutter="16">
      <el-col :span="24">
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
    </el-row>

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
const queueItems = ref([])
const queueTab = ref('active')
const trendPoints = ref([])
// 按 worker 拆开的曲线（后端 /worker-monitor/trend 的 series 字段）。
// 每条与 trendPoints 的 ts 一一对齐，缺失桶已由后端补 0。
const trendSeries = ref([])
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

// 千分位格式化；null/undefined 显示「—」而不是 0，避免把「还没数据」误显示成 0
// 车道负载（/worker-monitor/load）：排队 / 吞吐 / 新增 / 预计清完
const lanes = ref([])
const anyOverloaded = computed(() => lanes.value.some(l => l.overloaded))

// 预计清完时间：秒 → 人话。null 表示吞吐为 0 算不出来（不编假数字）。
function fmtEta(sec) {
  if (sec == null) return '—'
  if (sec < 60) return `${Math.round(sec)} 秒`
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} 小时`
  return `${(sec / 86400).toFixed(1)} 天`
}

function fmtNum(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString('en-US')
}

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

// 每个 worker 一条曲线。不同 worker 的 processed 语义完全不同：
//   tracker          = 新投递到队列的 config 数
//   shallow-realtime = 用户点击那批的两阶段产出（DONE + 定级）
//   shallow-auto     = 背景全量的两阶段产出
//   deep-realtime    = 产物下载完成数
// 混成一条线相加没有意义（浅层一轮几千、下载一轮几个）。
const WORKER_COLOR = {
  'tracker': '#8b5cf6',
  'shallow-realtime': '#dc2626',
  'shallow-auto': '#2a78d6',
  'deep-realtime': '#d97706',
  // 旧进程名（升级期间仍能看到历史心跳曲线）
  'shallow-local': '#60a5fa',
  'shallow-obs': '#0d9488',
  'shallow': '#64748b',
  'deep': '#f59e0b',
  'realtime': '#f87171',
}
const WORKER_LABEL = {
  'tracker': '发现投递',
  'shallow-realtime': '实时分析（点击）',
  'shallow-auto': '背景分析（全量）',
  'deep-realtime': '实时下载（点击）',
  // 旧名
  'shallow-local': '本地 DONE(旧)',
  'shallow-obs': 'OBS 定级(旧)',
  'shallow': '浅层(旧)',
  'deep': '产物下载(旧)',
  'realtime': '实时下载(旧)',
}

const trendOption = computed(() => {
  const series = trendSeries.value
  const x = trendPoints.value.map(p =>
    new Date(p.ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))
  // 有分 worker 数据就画分线；没有（老数据只有合并心跳）时退回单条总量线
  if (series.length) {
    return {
      grid: { left: 44, right: 16, top: 30, bottom: 26 },
      tooltip: { trigger: 'axis' },
      legend: {
        data: series.map(s => WORKER_LABEL[s.worker] || s.worker),
        right: 0, top: 0, itemWidth: 9, itemHeight: 9, textStyle: { fontSize: 11 },
      },
      xAxis: { type: 'category', data: x, axisLabel: { fontSize: 10 } },
      yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
      series: series.map(s => ({
        name: WORKER_LABEL[s.worker] || s.worker,
        type: 'line', smooth: true, showSymbol: false,
        data: s.data,
        itemStyle: { color: WORKER_COLOR[s.worker] || '#94a3b8' },
      })),
    }
  }
  return {
    grid: { left: 44, right: 16, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis' },
    legend: { data: ['处理条数', '失败'], right: 0, top: 0, itemWidth: 9, itemHeight: 9 },
    xAxis: { type: 'category', data: x, axisLabel: { fontSize: 10 } },
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
  }
})

// ── 数据加载 ──────────────────────────────────────────────────────────────

async function loadAll() {
  loading.value = true
  try {
    // 注意：这里用 Promise.all，任何一支超时都会让整块面板空白。
    // 所以每个请求都必须足够快（/queue 曾经 50s 超时，就是因为这个整片空白）。
    // 不再请求 /eta：它的消费方「执行时间估计」面板已删除（那三个数全取自
    // 产物车道，而产物纯点击驱动、队列恒为 0）。少一个请求也少一次 2.4s 的
    // _queue_counts 全表聚合（已加 5s 缓存，但能不发就不发）。
    const [ov, q, ld] = await Promise.all([
      api.get('/worker-monitor/overview', { silent: true }),
      api.get(`/worker-monitor/queue?status=${queueTab.value}&limit=50`, { silent: true }),
      api.get('/worker-monitor/load?window_minutes=10', { silent: true }),
    ])
    workers.value = ov.workers || []
    counts.value = ov.queue || {}
    disk.value = ov.disk || null
    generatedAt.value = (ov.generated_at || '').replace('T', ' ')
    queueItems.value = q.items || []
    lanes.value = ld.lanes || []
  } catch { /* 单块失败不影响其他块 */ }
  finally { loading.value = false }
  loadTrend()
}

async function loadTrend() {
  try {
    const tr = await api.get(`/worker-monitor/trend?hours=${trendHours.value}`, { silent: true })
    trendPoints.value = tr.points || []
    trendSeries.value = tr.series || []
  } catch {
    trendPoints.value = []
    trendSeries.value = []
  }
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
/* 车道负载面板 */
.wm-lane { padding: 8px 0; border-bottom: 1px solid var(--border-color, #ebeef5); }
.wm-lane:last-of-type { border-bottom: none; }
.wm-lane-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.wm-lane-name { font-size: 13px; font-weight: 500; }
.wm-lane-ok { font-size: 11px; color: var(--text-muted, #909399); }
.wm-lane-nums { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px;
  color: var(--text-muted, #909399); }
.wm-lane-nums b { color: var(--text-primary, #303133); font-variant-numeric: tabular-nums; }
.wm-lane-nums .bad b, .panel-hint.bad { color: #ef4444; }
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
