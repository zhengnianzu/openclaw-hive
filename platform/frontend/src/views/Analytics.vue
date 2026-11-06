<template>
  <div class="analytics" :class="{ fullscreen: isFullscreen }" ref="rootEl">
    <!-- 页头：标题 + 全局控件（时间区间 / 资源池 / 自动刷新 / 全屏） -->
    <div class="header-row">
      <div class="header-title">
        <h2>资源看板</h2>
        <div class="sub-hint">
          <template v-if="activeTab === 'overview'">
            实时掌握资源使用、沙箱运行、任务调度与系统健康
            <span v-if="generatedAt"> · 更新于 {{ generatedAt }}</span>
          </template>
          <template v-else>Worker 进程状态、队列积压与预计完成时间</template>
        </div>
      </div>
      <div v-if="activeTab === 'overview'" class="header-controls">
        <el-radio-group v-model="hours" size="small" @change="load">
          <el-radio-button :value="1">最近 1h</el-radio-button>
          <el-radio-button :value="24">最近 24h</el-radio-button>
          <el-radio-button :value="168">最近 7d</el-radio-button>
          <el-radio-button :value="720">最近 30d</el-radio-button>
        </el-radio-group>
        <el-select v-model="poolFilter" size="small" class="pool-select">
          <el-option label="全部资源池" value="" />
          <el-option v-for="p in pools" :key="p.namespace" :label="p.namespace" :value="p.namespace" />
        </el-select>
        <!-- 自动刷新开关：关掉后停轮询，长时间盯屏时不被 30s 跳动打断 -->
        <button class="refresh-toggle" :class="{ on: autoRefresh }" @click="toggleAuto">
          <i class="dot" :style="{ background: autoRefresh ? '#10b981' : '#c4c7cc' }"></i>
          {{ autoRefresh ? '自动刷新 30s' : '自动刷新已关' }}
        </button>
        <el-button :icon="Refresh" size="small" circle @click="load" :loading="refreshing" title="立即刷新" />
        <el-button :icon="FullScreen" size="small" circle @click="toggleFullscreen" title="全屏" />
      </div>
    </div>

    <!-- 两个视角分开：资源总览看配置推算的资源占用，调度监控看 worker 与队列实况 -->
    <el-tabs v-model="activeTab" class="analytics-tabs">
      <el-tab-pane label="资源总览" name="overview">
        <div v-loading="loading" class="overview">

        <!-- ① KPI 行：6 张统计磁贴 -->
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#eef2ff;color:#6366f1"><Files /></span>
              <span class="kpi-label">总资源数</span>
            </div>
            <div class="kpi-value">{{ fmtCompact(fSummary.instances_total) }}</div>
            <div class="kpi-sub">
              CPU {{ fSummary.total_cpu ? fmtNum(fSummary.total_cpu) + ' 核' : '未配置' }}
              · 内存 {{ fSummary.total_memory_gb ? fmtGi(fSummary.total_memory_gb) : '未配置' }}
            </div>
          </div>

          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#ecfdf5;color:#10b981"><VideoPlay /></span>
              <span class="kpi-label">运行实例</span>
            </div>
            <div class="kpi-value">{{ fmtCompact(fSummary.running) }}</div>
            <div class="kpi-sub">准备 {{ fSummary.preparing }} · 排队 {{ fSummary.queued }}</div>
          </div>

          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#f5f3ff;color:#8b5cf6"><Box /></span>
              <span class="kpi-label">运行沙箱</span>
            </div>
            <div class="kpi-value">{{ fmtCompact(fSummary.pods) }}</div>
            <div class="kpi-sub">
              <template v-if="fSummary.max_sandboxes">
                容量利用率 {{ pct(fSummary.pods, fSummary.max_sandboxes) }}%
              </template>
              <template v-else>上限未配置</template>
            </div>
          </div>

          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#eff6ff;color:#3b82f6"><Cpu /></span>
              <span class="kpi-label">CPU 利用率</span>
            </div>
            <div class="kpi-row">
              <div class="kpi-value">{{ pct(fSummary.used_cpu, fSummary.total_cpu) }}%</div>
              <EChart
                v-if="cpuSpark.length > 1"
                :option="sparkOption(cpuSpark, '#3b82f6')"
                height="40px" class="kpi-spark" />
            </div>
            <div class="kpi-sub">
              {{ fmtNum(fSummary.used_cpu) }} / {{ fSummary.total_cpu ? fmtNum(fSummary.total_cpu) : '—' }} 核
            </div>
          </div>

          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#eef2ff;color:#6366f1"><Monitor /></span>
              <span class="kpi-label">内存利用率</span>
            </div>
            <div class="kpi-row">
              <div class="kpi-value">{{ pct(fSummary.used_memory_gb, fSummary.total_memory_gb) }}%</div>
              <EChart
                v-if="memSpark.length > 1"
                :option="sparkOption(memSpark, '#10b981')"
                height="40px" class="kpi-spark" />
            </div>
            <div class="kpi-sub">
              {{ fmtNum(fSummary.used_memory_gb) }} / {{ fSummary.total_memory_gb ? fmtNum(fSummary.total_memory_gb) : '—' }} GiB
            </div>
          </div>

          <div class="kpi-card">
            <div class="kpi-head">
              <span class="kpi-icon" style="background:#f0f9ff;color:#0ea5e9"><User /></span>
              <span class="kpi-label">活跃用户</span>
            </div>
            <div class="kpi-value">{{ fSummary.active_users }}</div>
            <div class="kpi-sub">有资源占用的用户数</div>
          </div>
        </div>

        <!-- ② 趋势（宽） + 资源池容量（窄） -->
        <div class="grid-row row-2">
          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">
                资源使用趋势
                <el-tooltip placement="top" content="按实例启停时间重建，非真实采样">
                  <InfoFilled class="info-icon" />
                </el-tooltip>
              </span>
              <el-radio-group v-model="trendMetric" size="small">
                <el-radio-button value="cpu">CPU</el-radio-button>
                <el-radio-button value="memory_gb">内存</el-radio-button>
                <el-radio-button value="pods">沙箱数量</el-radio-button>
              </el-radio-group>
            </div>
            <div v-if="trend.truncated" class="panel-hint trend-hint">
              实例过多，仅统计最近部分，曲线可能偏低
            </div>
            <el-empty
              v-if="trend.points.length < 2"
              description="该区间内没有已启动的实例，暂无趋势" :image-size="70" />
            <!-- 曲线 + 右侧读数面板：把「当前值 / 峰值 / 均值」从 tooltip 里提出来常驻 -->
            <div v-else class="trend-body">
              <EChart :option="trendOption" height="300px" class="trend-chart" />
              <div class="trend-stats">
                <div class="stats-block">
                  <div class="stats-title">当前值</div>
                  <div class="stats-line">
                    <i class="dot" style="background:#3b82f6"></i>
                    <span class="stats-key">CPU</span>
                    <span class="stats-val">{{ pct(fSummary.used_cpu, fSummary.total_cpu) }}%</span>
                  </div>
                  <div class="stats-line">
                    <i class="dot" style="background:#10b981"></i>
                    <span class="stats-key">内存</span>
                    <span class="stats-val">{{ pct(fSummary.used_memory_gb, fSummary.total_memory_gb) }}%</span>
                  </div>
                </div>
                <div class="stats-block">
                  <div class="stats-title">{{ rangeLabel }} 统计</div>
                  <div v-for="s in trendStats" :key="s.key" class="stats-line">
                    <span class="stats-key wide">{{ s.key }}</span>
                    <span class="stats-val">{{ s.value }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">资源池容量</span>
              <span class="panel-hint">{{ poolFilter || '全部资源池' }}</span>
            </div>
            <el-empty v-if="!fPools.length" description="未配置资源池" :image-size="70" />
            <template v-else>
              <!-- 三条总量计量条：CPU / 内存 / 沙箱，口径与 KPI 一致 -->
              <div v-for="m in poolMeters" :key="m.key" class="meter-row">
                <span class="meter-icon"><component :is="m.icon" /></span>
                <div class="meter-main">
                  <div class="meter-top">{{ m.text }}</div>
                  <div class="meter-track">
                    <div
                      class="meter-fill"
                      :style="{ width: m.percent + '%', background: percentColor(m.percent) }"></div>
                  </div>
                </div>
                <span class="meter-pct">{{ m.percent }}%</span>
              </div>

              <div class="pool-foot">
                <span class="pool-foot-item"><i class="dot" style="background:#10b981"></i>运行 {{ fSummary.running }}</span>
                <span class="pool-divider"></span>
                <span class="pool-foot-item">准备 {{ fSummary.preparing }}</span>
                <span class="pool-divider"></span>
                <span class="pool-foot-item">排队 {{ fSummary.queued }}</span>
              </div>

              <!-- 多池时逐池列出，单池已由上面三条覆盖 -->
              <div v-if="fPools.length > 1" class="pool-list">
                <div v-for="p in fPools" :key="p.namespace" class="pool-line">
                  <span class="pool-name">{{ p.namespace }}</span>
                  <div class="meter-track slim">
                    <div
                      class="meter-fill"
                      :style="{ width: poolPercent(p.used_cpu, p.total_cpu) + '%',
                                background: percentColor(poolPercent(p.used_cpu, p.total_cpu)) }"></div>
                  </div>
                  <span class="pool-line-val">
                    {{ fmtNum(p.used_cpu) }} / {{ p.configured ? fmtNum(p.total_cpu) : '—' }} 核
                  </span>
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- ③ 用户排行（宽） + Harness 分布 + 沙箱操作系统分布 -->
        <div class="grid-row row-3">
          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">用户资源排行榜 <span class="title-note">Top 10</span></span>
              <el-radio-group v-model="rankMetric" size="small">
                <el-radio-button value="cpu">CPU</el-radio-button>
                <el-radio-button value="memory_gb">内存</el-radio-button>
                <el-radio-button value="pods">沙箱数</el-radio-button>
              </el-radio-group>
            </div>
            <el-empty v-if="!ranked.length" description="暂无占用中的实例" :image-size="70" />
            <el-table v-else :data="ranked" size="small" class="rank-table">
              <el-table-column label="#" width="40" align="center">
                <template #default="{ $index }">
                  <span class="rank-no" :class="'rank-' + ($index + 1)">{{ $index + 1 }}</span>
                </template>
              </el-table-column>
              <el-table-column label="用户" min-width="100">
                <template #default="{ row }">
                  <i class="dot user-dot" :style="{ background: userColor(row.user) }"></i>{{ row.user }}
                </template>
              </el-table-column>
              <el-table-column prop="running" label="运行" width="52" align="right" />
              <el-table-column prop="queued" label="排队" width="52" align="right" />
              <el-table-column label="沙箱数" width="64" align="right">
                <template #default="{ row }">{{ fmtCompact(row.pods) }}</template>
              </el-table-column>
              <el-table-column label="CPU(核)" width="76" align="right">
                <template #default="{ row }">{{ fmtNum(row.cpu) }}</template>
              </el-table-column>
              <el-table-column label="内存(GiB)" width="84" align="right">
                <template #default="{ row }">{{ fmtNum(row.memory_gb) }}</template>
              </el-table-column>
              <el-table-column label="占比" min-width="120">
                <template #default="{ row }">
                  <div class="share-cell">
                    <span class="share-pct">{{ rankShare(row) }}%</span>
                    <div class="meter-track slim">
                      <div
                        class="meter-fill"
                        :style="{ width: rankShare(row) + '%', background: userColor(row.user) }"></div>
                    </div>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">Harness 类型分布</span>
              <span class="panel-hint">按沙箱数</span>
            </div>
            <el-empty v-if="!harnessDist.length" description="无数据" :image-size="70" />
            <template v-else>
              <!-- 单一整体拆分，用分段条比饼图更易读出份额 -->
              <div class="seg-bar">
                <div
                  v-for="h in harnessShare" :key="h.key"
                  class="seg"
                  :style="{ width: h.share + '%', background: h.color }"
                  :title="`${h.label} · ${fmtCompact(h.pods)}（${h.share}%）`"></div>
              </div>
              <div class="dist-list">
                <div v-for="h in harnessShare" :key="h.key" class="dist-line">
                  <i class="dot" :style="{ background: h.color }"></i>
                  <span class="dist-label">{{ h.label }}</span>
                  <span class="dist-val">{{ fmtCompact(h.pods) }}</span>
                  <span class="dist-pct">{{ h.share }}%</span>
                </div>
              </div>
            </template>
          </div>

          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">沙箱操作系统分布</span>
            </div>
            <el-empty v-if="!sandboxDist.length" description="无数据" :image-size="70" />
            <div v-else class="donut-row">
              <div class="donut-wrap">
                <EChart :option="sandboxOption" height="170px" />
                <div class="donut-center">
                  <div class="donut-num">{{ fmtCompact(sandboxTotal) }}</div>
                  <div class="donut-cap">沙箱总数</div>
                </div>
              </div>
              <div class="dist-list">
                <div v-for="s in sandboxShare" :key="s.key" class="dist-line">
                  <i class="dot" :style="{ background: s.color }"></i>
                  <span class="dist-label">{{ s.label }}</span>
                  <span class="dist-val">{{ fmtCompact(s.pods) }}</span>
                  <span class="dist-pct">{{ s.share }}%</span>
                </div>
              </div>
            </div>
            <div v-if="inferredCount" class="panel-foot">
              {{ inferredCount }} 个实例的类型由配置推断得出
            </div>
          </div>
        </div>

        <!-- ④ 沙箱规格分布 + 代理实例 + 沙箱状态分布 -->
        <div class="grid-row row-4">
          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">沙箱规格分布</span>
              <span class="panel-hint">按沙箱数 · request 口径</span>
            </div>
            <el-empty v-if="!specDist.length" description="无数据" :image-size="70" />
            <EChart v-else :option="specOption" :height="specChartHeight" />
            <div v-if="specDist.length" class="panel-foot">
              规格取自沙箱配置的 cpu_request / memory_request，未配置时回退 2 核 / 6 GiB
            </div>
          </div>

          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">代理实例</span>
              <el-select v-model="proxyRole" size="small" class="role-select">
                <el-option label="全部角色" value="" />
                <el-option v-for="r in PROXY_ROLES" :key="r" :label="r" :value="r" />
              </el-select>
            </div>
            <el-empty v-if="!proxyRows.length" description="暂无运行中实例" :image-size="70" />
            <table v-else class="proxy-table">
              <thead>
                <tr>
                  <th>接入 Agent</th>
                  <th class="num">负载</th>
                  <th class="num">实例</th>
                  <th class="num">沙箱</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="p in proxyRows" :key="p.role + p.host">
                  <td>
                    <span class="proxy-name">{{ p.proxy_name || p.host }}</span>
                    <span v-if="p.proxy_name" class="proxy-ip">{{ p.host }}</span>
                  </td>
                  <td class="num">
                    <div class="meter-track slim">
                      <div
                        class="meter-fill"
                        :style="{ width: proxyPercent(p) + '%', background: proxyColor(p.role) }"></div>
                    </div>
                  </td>
                  <td class="num">{{ p.instances }}</td>
                  <td class="num strong">{{ fmtCompact(p.pods) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="proxyRows.length" class="panel-foot">
              代理名来自导出服务实时接口，未登记的 IP 直接显示地址
            </div>
          </div>

          <div class="glass-card panel">
            <div class="panel-head">
              <span class="panel-title">沙箱状态分布</span>
              <span class="panel-hint">全部实例</span>
            </div>
            <el-empty v-if="!statusCount" description="暂无实例" :image-size="70" />
            <div v-else class="donut-row">
              <div class="donut-wrap">
                <EChart :option="statusOption" height="170px" />
                <div class="donut-center">
                  <div class="donut-num">{{ fmtCompact(statusTotal) }}</div>
                  <div class="donut-cap">总数</div>
                </div>
              </div>
              <div class="dist-list">
                <div v-for="s in statusShare" :key="s.key" class="dist-line">
                  <i class="dot" :style="{ background: s.color }"></i>
                  <span class="dist-label">{{ s.label }}</span>
                  <span class="dist-val">{{ fmtCompact(s.count) }}</span>
                  <span class="dist-pct">{{ s.share }}%</span>
                </div>
              </div>
            </div>
            <div class="panel-foot">
              「已结束」表示有任务失败；「准备中」已占用资源；「排队中」不占资源
            </div>
          </div>
        </div>

        <!-- ⑤ Linux / Windows 资源对比：操作系统维度的 CPU / 内存绝对值 -->
        <div class="glass-card panel">
          <div class="panel-head">
            <span class="panel-title">Linux / Windows 资源对比</span>
            <span class="panel-hint">占用中实例</span>
          </div>
          <EChart :option="osOption" height="220px" />
        </div>
        </div>
      </el-tab-pane>
      <el-tab-pane label="调度监控" name="worker">
        <WorkerMonitor :is-operator="authStore.isOperator" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Box, Cpu, Files, FullScreen, InfoFilled, Monitor, Refresh, User, VideoPlay,
} from '@element-plus/icons-vue'
import api from '../api'
import EChart from '../components/EChart.vue'
import WorkerMonitor from '../components/WorkerMonitor.vue'
import { useHarnessStore } from '../stores/harness'
import { useAuthStore } from '../stores/auth'

const harnessStore = useHarnessStore()
const authStore = useAuthStore()

const rootEl = ref(null)
const activeTab = ref('overview')
const loading = ref(true)
const refreshing = ref(false)
const hours = ref(24)
const trendMetric = ref('cpu')
const rankMetric = ref('pods')
const poolFilter = ref('')
const proxyRole = ref('')
const autoRefresh = ref(true)
const isFullscreen = ref(false)

const summary = ref({
  active: 0, running: 0, preparing: 0, queued: 0, pods: 0,
  used_cpu: 0, used_memory_gb: 0, instances_total: 0, active_users: 0,
  total_cpu: 0, total_memory_gb: 0, max_sandboxes: 0,
})
const statusDist = ref([])
const harnessDist = ref([])
const sandboxDist = ref([])
const specDist = ref([])
const osCompare = ref({ linux: {}, windows: {} })
const leaderboard = ref([])
const pools = ref([])
const proxyUsage = ref([])
const trend = ref({ points: [], truncated: false })
const generatedAt = ref('')

let timer = null

// ---------- 格式化 ----------
function fmtNum(v) {
  const n = Number(v || 0)
  return Number.isInteger(n) ? n.toLocaleString('en-US') : n.toLocaleString('en-US', { maximumFractionDigits: 1 })
}
function fmtGi(v) {
  return `${fmtNum(v)} GiB`
}
// KPI 数值位数差异大（4 ~ 183,808），统一加千分位，上万不缩写以免丢精度
function fmtCompact(v) {
  return Number(v || 0).toLocaleString('en-US')
}
function pct(used, total) {
  if (!total) return 0
  return Math.min(100, Math.round((used / total) * 100))
}
function poolPercent(used, total) {
  return pct(used, total)
}
function percentColor(p) {
  if (p >= 90) return '#d03b3b'
  if (p >= 70) return '#fab219'
  return '#2a78d6'
}

// ---------- 资源池筛选 ----------
// 顶部下拉选中某个池时，整页的 KPI / 计量条都跟着只看这个池；
// 分布类数据后端未按池拆分，保持全量（面板标题已标注口径）。
const fPools = computed(() =>
  poolFilter.value ? pools.value.filter(p => p.namespace === poolFilter.value) : pools.value)

const fSummary = computed(() => {
  if (!poolFilter.value) return summary.value
  const p = fPools.value[0]
  if (!p) return summary.value
  return {
    ...summary.value,
    instances_total: p.instances,
    pods: p.pods,
    used_cpu: p.used_cpu,
    used_memory_gb: p.used_memory_gb,
    total_cpu: p.configured ? p.total_cpu : 0,
    total_memory_gb: p.configured ? p.total_memory_gb : 0,
    max_sandboxes: p.max_sandboxes,
  }
})

const poolMeters = computed(() => {
  const s = fSummary.value
  return [
    {
      key: 'cpu', icon: Cpu,
      percent: pct(s.used_cpu, s.total_cpu),
      text: `${fmtNum(s.used_cpu)} / ${s.total_cpu ? fmtNum(s.total_cpu) : '—'} 核`,
    },
    {
      key: 'mem', icon: Monitor,
      percent: pct(s.used_memory_gb, s.total_memory_gb),
      text: `${fmtNum(s.used_memory_gb)} / ${s.total_memory_gb ? fmtNum(s.total_memory_gb) : '—'} GiB`,
    },
    {
      key: 'pods', icon: Box,
      percent: pct(s.pods, s.max_sandboxes),
      text: `${fmtNum(s.pods)} / ${s.max_sandboxes ? fmtNum(s.max_sandboxes) : '—'}`,
    },
  ]
})

// ---------- 代理负载 ----------
// 角色顺序与后端 PROXY_ROLES 一致
const PROXY_ROLES = ['主Agent', '用户模拟', 'Evaluator']
const PROXY_ROLE_COLOR = {
  '主Agent': '#6366f1',
  '用户模拟': '#10b981',
  'Evaluator': '#8b5cf6',
}

const proxyRows = computed(() => {
  const rows = proxyRole.value
    ? proxyUsage.value.filter(p => p.role === proxyRole.value)
    : proxyUsage.value
  return [...rows].sort((a, b) => (b.pods || 0) - (a.pods || 0))
})
// 条形只在当前可见行内比较，分母取可见最大值：不同角色量级差很大，
// 共用分母会让小角色整条塌成看不见。
function proxyPercent(p) {
  const max = Math.max(...proxyRows.value.map(x => x.pods || 0), 0)
  return max ? Math.round(((p.pods || 0) / max) * 100) : 0
}
function proxyColor(role) {
  return PROXY_ROLE_COLOR[role] || '#2a78d6'
}

const inferredCount = computed(() =>
  sandboxDist.value.reduce((s, x) => s + (x.inferred_count || 0), 0))

// ---------- 图表 option ----------
const AXIS_LABEL = '#64748b'
const SPLIT_LINE = { lineStyle: { color: '#f1f5f9' } }

const RANK_METRICS = {
  cpu: { field: 'cpu', unit: '核' },
  memory_gb: { field: 'memory_gb', unit: 'GiB' },
  pods: { field: 'pods', unit: '个沙箱' },
}

// 用户配色：按用户名哈希取固定槽位，不用 Math.random()。
// 看板每 30s 轮询一次，随机会在每次刷新时换色，用户会以为数据变了。
// 8 个槽位取自校验通过的分类色板；超出 8 人循环复用（同屏最多 10 人，
// 已有人名标签直接标注，颜色只做辅助识别）。
const USER_COLORS = [
  '#2a78d6', '#eb6834', '#1baf7a', '#eda100',
  '#e87ba4', '#008300', '#6250d6', '#e34948',
]

function userColor(name) {
  let h = 0
  const s = String(name || '')
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return USER_COLORS[h % USER_COLORS.length]
}

// 设计稿是 Top 10，超出部分不渲染（后端已按占用排序返回全部）
const ranked = computed(() => {
  const m = RANK_METRICS[rankMetric.value]
  return [...leaderboard.value]
    .sort((a, b) => (b[m.field] || 0) - (a[m.field] || 0) || (b.queued || 0) - (a.queued || 0))
    .slice(0, 10)
})

// 占比分母用全量榜单之和，不是 Top10 之和——否则截断会让占比虚高到 100%
const rankTotal = computed(() => {
  const m = RANK_METRICS[rankMetric.value]
  return leaderboard.value.reduce((s, r) => s + (r[m.field] || 0), 0)
})
function rankShare(row) {
  const m = RANK_METRICS[rankMetric.value]
  const t = rankTotal.value
  return t ? Math.round(((row[m.field] || 0) / t) * 1000) / 10 : 0
}

// KPI 磁贴的迷你走势：无坐标轴无标签，只给形状
const cpuSpark = computed(() => trend.value.points.map(p => p.used_cpu || 0))
const memSpark = computed(() => trend.value.points.map(p => p.used_memory_gb || 0))

function sparkOption(data, color) {
  return {
    grid: { left: 0, right: 0, top: 4, bottom: 4 },
    xAxis: { type: 'category', show: false, data: data.map((_, i) => i), boundaryGap: false },
    yAxis: { type: 'value', show: true, min: 'dataMin', max: 'dataMax' },
    series: [{
      type: 'line', data, smooth: true, showSymbol: false,
      lineStyle: { width: 2, color },
      areaStyle: { color: `${color}1a` },
    }],
  }
}

const RANGE_LABEL = { 1: '1h', 24: '24h', 168: '7d', 720: '30d' }
const rangeLabel = computed(() => RANGE_LABEL[hours.value] || `${hours.value}h`)

// 趋势卡右栏的常驻读数：峰值 / 均值按百分比口径，与左侧曲线同一单位
const trendStats = computed(() => {
  const pts = trend.value.points
  if (!pts.length) return []
  const totalCpu = fSummary.value.total_cpu
  const totalMem = fSummary.value.total_memory_gb
  const cpuPct = pts.map(p => pct(p.used_cpu || 0, totalCpu))
  const memPct = pts.map(p => pct(p.used_memory_gb || 0, totalMem))
  const avg = (a) => (a.length ? Math.round(a.reduce((s, x) => s + x, 0) / a.length) : 0)
  return [
    { key: 'CPU 峰值', value: `${Math.max(...cpuPct, 0)}%` },
    { key: 'CPU 均值', value: `${avg(cpuPct)}%` },
    { key: '内存 峰值', value: `${Math.max(...memPct, 0)}%` },
    { key: '内存 均值', value: `${avg(memPct)}%` },
  ]
})

// Linux / Windows 用与趋势图一致的槽位，避免同一概念两种颜色
const sandboxColor = { linux: '#2a78d6', windows: '#eb6834' }
const sandboxLabel = { linux: 'Linux', windows: 'Windows' }

// 状态色固定在状态语义上（不是系列色）。8 个状态一直同时出现在图例里，
// 图例带文字，因此不单靠色相区分；这里只保证相邻状态在视觉上可辨。
// 两种「灰」必须拉开亮度差：stopped（人工停止）深灰，created（未启动）浅灰。
const STATUS_COLOR = {
  running: '#0ca30c',   // good
  preparing: '#fab219', // warning
  queued: '#2a78d6',    // 蓝
  completed: '#1baf7a', // aqua
  finished: '#6250d6',  // violet
  stopped: '#5f6368',   // 深灰
  failed: '#d03b3b',    // critical
  created: '#c4c7cc',   // 浅灰
}

// 份额列表的通用构造：按值降序，百分比留一位小数
function shareList(items, { key, value, label, color }) {
  const total = items.reduce((s, x) => s + (x[value] || 0), 0)
  return [...items]
    .sort((a, b) => (b[value] || 0) - (a[value] || 0))
    .map(it => ({
      key: it[key],
      label: label(it[key]),
      color: color(it[key]),
      pods: it[value] || 0,
      count: it[value] || 0,
      share: total ? Math.round(((it[value] || 0) / total) * 1000) / 10 : 0,
    }))
}

const harnessShare = computed(() =>
  shareList(harnessDist.value, {
    key: 'harness_type', value: 'pods',
    label: t => harnessStore.label(t),
    color: t => harnessStore.color(t),
  }))

const sandboxShare = computed(() =>
  shareList(sandboxDist.value, {
    key: 'sandbox_type', value: 'pods',
    label: t => sandboxLabel[t] || t,
    color: t => sandboxColor[t] || '#909399',
  }))

const sandboxTotal = computed(() =>
  sandboxDist.value.reduce((s, x) => s + (x.pods || 0), 0))

const statusWithData = computed(() => statusDist.value.filter(s => s.count > 0))
const statusCount = computed(() => statusWithData.value.length)
const statusTotal = computed(() =>
  statusWithData.value.reduce((s, x) => s + (x.count || 0), 0))

const statusShare = computed(() =>
  shareList(statusWithData.value, {
    key: 'status', value: 'count',
    label: s => (statusDist.value.find(x => x.status === s) || {}).label || s,
    color: s => STATUS_COLOR[s] || '#c4c7cc',
  }))

// 甜甜圈：中心读数由外层 DOM 叠加，图自身不画 label
function donutOption(rows) {
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => `${p.name}<br/><b>${fmtCompact(p.value)}</b>（${p.percent}%）`,
    },
    series: [{
      type: 'pie',
      radius: ['64%', '88%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      // 2px 表面间隙把相邻扇区分开，不用描边
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      data: rows.map(r => ({
        name: r.label,
        value: r.pods ?? r.count,
        itemStyle: { color: r.color },
      })),
    }],
  }
}

const sandboxOption = computed(() => donutOption(sandboxShare.value))
const statusOption = computed(() => donutOption(statusShare.value))

// 沙箱规格：组合数可能上双，饼图会挤成一团，改用横向条形图。
// 颜色仍按 label 哈希取固定槽位——30s 轮询一次，随机色会让人以为数据变了。
function specColor(label) {
  return userColor(label)
}

// 条形图高度随条目数走：通常只有 2~3 类，固定高度会在卡里留一大片空白。
const specChartHeight = computed(() =>
  `${Math.max(140, specDist.value.length * 34 + 32)}px`)

const specOption = computed(() => {
  const rows = [...specDist.value].reverse()   // 横向柱状图自下而上，最大值排最上
  const total = specDist.value.reduce((s, x) => s + (x.pods || 0), 0)
  return {
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = ps[0]
        const it = specDist.value.find(x => x.label === p.name)
        if (!it) return p.name
        const share = total ? ((it.pods / total) * 100).toFixed(0) : 0
        return `${it.label}<br/><b>${fmtCompact(it.pods)}</b> 个沙箱（${share}%）<br/>` +
          `${it.count} 个实例 · ${fmtNum(it.cpu)} 核 / ${fmtGi(it.memory_gb)}`
      },
    },
    xAxis: {
      type: 'value',
      axisLabel: { color: AXIS_LABEL, fontSize: 11, formatter: (v) => fmtCompact(v) },
      splitLine: SPLIT_LINE,
    },
    yAxis: {
      type: 'category',
      data: rows.map(x => x.label),
      axisLabel: { color: AXIS_LABEL, fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      barMaxWidth: 22,
      data: rows.map(x => ({
        value: x.pods,
        itemStyle: { color: specColor(x.label), borderRadius: [0, 4, 4, 0] },
      })),
      label: {
        show: true, position: 'right', color: AXIS_LABEL, fontSize: 11,
        formatter: ({ value }) => fmtCompact(value),
      },
    }],
  }
})

const osOption = computed(() => {
  const l = osCompare.value.linux || {}
  const w = osCompare.value.windows || {}
  return {
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: {
      top: 0, icon: 'circle', itemWidth: 8, itemHeight: 8,
      textStyle: { color: '#475569', fontSize: 12 },
    },
    xAxis: {
      type: 'category', data: ['CPU(核)', '内存(GiB)'],
      axisLabel: { color: AXIS_LABEL }, axisLine: { lineStyle: { color: '#e5e7eb' } },
    },
    yAxis: {
      type: 'value', axisLabel: { color: AXIS_LABEL, formatter: (v) => fmtCompact(v) },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: 'Linux', type: 'bar', barMaxWidth: 24,
        data: [l.cpu || 0, l.memory_gb || 0],
        itemStyle: { color: sandboxColor.linux, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: AXIS_LABEL, formatter: ({ value }) => fmtCompact(value) },
      },
      {
        name: 'Windows', type: 'bar', barMaxWidth: 24,
        data: [w.cpu || 0, w.memory_gb || 0],
        itemStyle: { color: sandboxColor.windows, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: AXIS_LABEL, formatter: ({ value }) => fmtCompact(value) },
      },
    ],
  }
})

// 趋势指标：总量 + Linux / Windows 拆分
const TREND_METRICS = {
  cpu: { field: 'used_cpu', linuxField: 'linux_cpu', windowsField: 'windows_cpu', unit: '核' },
  memory_gb: { field: 'used_memory_gb', linuxField: 'linux_memory_gb',
               windowsField: 'windows_memory_gb', unit: 'GiB' },
  pods: { field: 'pods', linuxField: 'linux_pods', windowsField: 'windows_pods', unit: '个沙箱' },
}

const trendOption = computed(() => {
  const m = TREND_METRICS[trendMetric.value]
  const pts = trend.value.points
  return {
    grid: { left: 8, right: 8, top: 36, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: '#cbd5e1' } },
      formatter: (ps) => {
        const head = `<b>${ps[0].axisValue}</b>`
        const body = ps.map(p => `${p.marker}${p.seriesName}：${fmtNum(p.value)}`).join('<br/>')
        return `${head}<br/>${body}`
      },
    },
    legend: {
      top: 0, icon: 'circle', itemWidth: 8, itemHeight: 8,
      textStyle: { color: '#475569', fontSize: 12 },
    },
    xAxis: {
      type: 'category',
      data: pts.map(p => p.ts),
      boundaryGap: false,
      axisLabel: {
        color: AXIS_LABEL,
        hideOverlap: true,
        // 只显示 月-日 时:分，避免长刻度互相挤压
        formatter: (v) => v.slice(5),
      },
      axisLine: { lineStyle: { color: '#e5e7eb' } },
    },
    yAxis: {
      type: 'value', name: m.unit, nameTextStyle: { color: AXIS_LABEL },
      axisLabel: { color: AXIS_LABEL, formatter: (v) => fmtCompact(v) },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: '总使用量', type: 'line', smooth: true, showSymbol: false,
        data: pts.map(p => p[m.field] || 0),
        lineStyle: { width: 2, color: '#6250d6' },
        itemStyle: { color: '#6250d6' },
        areaStyle: { color: 'rgba(98,80,214,0.10)' },
      },
      {
        name: 'Linux', type: 'line', smooth: true, showSymbol: false,
        data: pts.map(p => p[m.linuxField] || 0),
        lineStyle: { width: 2, color: sandboxColor.linux },
        itemStyle: { color: sandboxColor.linux },
      },
      {
        name: 'Windows', type: 'line', smooth: true, showSymbol: false,
        data: pts.map(p => p[m.windowsField] || 0),
        lineStyle: { width: 2, color: sandboxColor.windows },
        itemStyle: { color: sandboxColor.windows },
      },
    ],
  }
})

// ---------- 交互 ----------
function startTimer() {
  if (!timer && autoRefresh.value) {
    timer = setInterval(() => load({ silent: true }), 30000)
  }
}
function stopTimer() {
  clearInterval(timer)
  timer = null
}
function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    load({ silent: true })
    startTimer()
  } else {
    stopTimer()
  }
}

function toggleFullscreen() {
  const el = rootEl.value
  if (!document.fullscreenElement) {
    el && el.requestFullscreen && el.requestFullscreen()
  } else {
    document.exitFullscreen()
  }
}
function onFsChange() {
  isFullscreen.value = !!document.fullscreenElement
}

// ---------- 数据 ----------
async function load({ silent = false } = {}) {
  if (!silent) refreshing.value = true
  try {
    const d = await api.get('/stats', { params: { hours: hours.value } })
    summary.value = d.summary || summary.value
    statusDist.value = d.status_distribution || []
    harnessDist.value = d.harness_distribution || []
    sandboxDist.value = d.sandbox_type_distribution || []
    specDist.value = d.spec_distribution || []
    osCompare.value = d.os_resource_comparison || { linux: {}, windows: {} }
    leaderboard.value = d.user_leaderboard || []
    pools.value = d.pool_usage || []
    proxyUsage.value = d.proxy_usage || []
    trend.value = d.trend || { points: [], truncated: false }
    generatedAt.value = (d.generated_at || '').replace('T', ' ')
    // 选中的池被删掉后回到全部，避免筛出空页面
    if (poolFilter.value && !pools.value.some(p => p.namespace === poolFilter.value)) {
      poolFilter.value = ''
    }
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function onVisibility() {
  if (document.hidden) {
    stopTimer()
  } else if (autoRefresh.value && !timer) {
    load({ silent: true })
    startTimer()
  }
}

onMounted(() => {
  harnessStore.load()
  load()
  startTimer()
  document.addEventListener('visibilitychange', onVisibility)
  document.addEventListener('fullscreenchange', onFsChange)
})

onBeforeUnmount(() => {
  stopTimer()
  document.removeEventListener('visibilitychange', onVisibility)
  document.removeEventListener('fullscreenchange', onFsChange)
})
</script>

<style scoped>
.analytics { display: flex; flex-direction: column; gap: 16px; }
.analytics.fullscreen { background: var(--bg-primary); padding: 20px; overflow: auto; }

.header-row {
  display: flex; justify-content: space-between;
  align-items: flex-start; gap: 16px; flex-wrap: wrap;
}
.header-row h2 { font-size: 20px; font-weight: 600; }
.sub-hint { color: var(--text-muted); font-size: 12px; margin-top: 4px; }
.header-controls {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
}
.pool-select { width: 150px; }
.role-select { width: 120px; }

/* 自动刷新开关：低调的胶囊，关掉时点变灰 */
.refresh-toggle {
  display: inline-flex; align-items: center; gap: 6px;
  height: 24px; padding: 0 12px;
  font-size: 12px; color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: 999px;
  cursor: pointer;
}
.refresh-toggle:hover { border-color: var(--accent-purple); color: var(--accent-purple); }
.refresh-toggle.on { color: var(--text-secondary); }

.overview { display: flex; flex-direction: column; gap: 16px; }

/* ---------- KPI ---------- */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 14px;
}
.kpi-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  padding: 16px 18px;
  box-shadow: var(--shadow-sm);
  min-width: 0;
}
.kpi-head { display: flex; align-items: center; gap: 8px; }
.kpi-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border-radius: 8px; flex: none;
}
.kpi-icon :deep(svg) { width: 15px; height: 15px; }
.kpi-label { color: var(--text-secondary); font-size: 13px; }
.kpi-row { display: flex; align-items: center; gap: 8px; }
.kpi-row .kpi-value { flex: none; }
.kpi-spark { flex: 1; min-width: 40px; }
.kpi-value {
  font-size: 26px; font-weight: 700; margin-top: 8px;
  color: var(--text-primary); line-height: 1.15;
}
.kpi-sub { color: var(--text-muted); font-size: 12px; margin-top: 6px; }

.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }

/* ---------- 面板与栅格 ---------- */
.grid-row { display: grid; gap: 16px; align-items: stretch; }
.row-2 { grid-template-columns: 2fr 1fr; }
.row-3 { grid-template-columns: 1.5fr 1fr 1fr; }
.row-4 { grid-template-columns: 1fr 1.2fr 1fr; }

.panel { display: flex; flex-direction: column; height: 100%; padding: 20px; min-width: 0; }
.panel-head {
  display: flex; justify-content: space-between;
  align-items: center; margin-bottom: 12px; gap: 8px;
}
.panel-title {
  font-size: 15px; font-weight: 600; color: var(--text-primary);
  display: inline-flex; align-items: center; gap: 6px;
}
.title-note { font-size: 12px; font-weight: 500; color: var(--text-muted); }
.info-icon { width: 14px; height: 14px; color: var(--text-muted); }
.panel-hint { font-size: 12px; color: var(--text-muted); }
.panel-foot { font-size: 11px; color: var(--text-muted); margin-top: 10px; line-height: 1.5; }
.trend-hint { margin: -6px 0 6px; }

/* ---------- 趋势卡：左图右读数 ---------- */
.trend-body { display: flex; gap: 16px; align-items: stretch; }
.trend-chart { flex: 1; min-width: 0; }
.trend-stats {
  width: 170px; flex: none;
  display: flex; flex-direction: column; gap: 10px;
  padding: 12px; border: 1px solid var(--border-color);
  border-radius: var(--radius-md); background: var(--bg-hover);
  align-self: center;
}
.stats-title { font-size: 12px; font-weight: 600; color: var(--text-primary); margin-bottom: 6px; }
.stats-line {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-secondary); padding: 2px 0;
}
.stats-key { flex: 1; }
.stats-key.wide { color: var(--text-muted); }
.stats-val { font-weight: 600; color: var(--text-primary); }

/* ---------- 计量条 ---------- */
.meter-row { display: flex; align-items: center; gap: 12px; padding: 10px 0; }
.meter-icon {
  display: inline-flex; width: 20px; height: 20px; flex: none;
  color: var(--text-muted);
}
.meter-icon :deep(svg) { width: 18px; height: 18px; }
.meter-main { flex: 1; min-width: 0; }
.meter-top {
  font-size: 12px; color: var(--text-secondary);
  text-align: right; margin-bottom: 4px;
}
.meter-track {
  height: 8px; border-radius: 999px;
  background: #eef2f7; overflow: hidden;
}
.meter-track.slim { height: 6px; }
.meter-fill { height: 100%; border-radius: 999px; transition: width .3s; }
.meter-pct {
  width: 48px; flex: none; text-align: right;
  font-size: 16px; font-weight: 700; color: var(--text-primary);
}

.pool-foot {
  display: flex; align-items: center; gap: 10px;
  margin-top: 12px; padding-top: 12px;
  border-top: 1px solid var(--border-color-light);
  font-size: 12px; color: var(--text-secondary);
}
.pool-foot-item { display: inline-flex; align-items: center; gap: 6px; }
.pool-divider { width: 1px; height: 12px; background: var(--border-color); }

.pool-list { margin-top: 10px; display: flex; flex-direction: column; gap: 8px; }
.pool-line { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.pool-name { width: 90px; flex: none; color: var(--text-primary); font-weight: 500;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pool-line .meter-track { flex: 1; }
.pool-line-val { width: 120px; flex: none; text-align: right; color: var(--text-muted); white-space: nowrap; }

/* ---------- 排行榜 ---------- */
.rank-table { margin-top: 2px; }
.rank-no {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  font-size: 11px; font-weight: 600;
  color: var(--text-secondary); background: #f1f5f9;
}
.rank-no.rank-1 { background: #2a78d6; color: #fff; }
.rank-no.rank-2 { background: #eb6834; color: #fff; }
.rank-no.rank-3 { background: #1baf7a; color: #fff; }
.user-dot { margin-right: 6px; }
.share-cell { display: flex; align-items: center; gap: 8px; }
.share-pct { width: 44px; flex: none; font-size: 12px; color: var(--text-secondary); text-align: right; }
.share-cell .meter-track { flex: 1; }

/* ---------- 分布：分段条 / 甜甜圈 / 列表 ---------- */
.seg-bar {
  display: flex; gap: 2px; height: 14px;
  border-radius: 4px; overflow: hidden; margin-bottom: 14px;
}
.seg { height: 100%; min-width: 2px; }

.donut-row { display: flex; align-items: center; gap: 14px; }
.donut-wrap { position: relative; width: 170px; flex: none; }
.donut-center {
  position: absolute; inset: 0;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  pointer-events: none;
}
.donut-num { font-size: 18px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.donut-cap { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

.dist-list { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.dist-line { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.dist-label {
  flex: 1; min-width: 0; color: var(--text-secondary);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.dist-val { font-weight: 600; color: var(--text-primary); text-align: right; }
.dist-pct { width: 44px; flex: none; text-align: right; color: var(--text-muted); }

/* ---------- 代理表 ---------- */
.proxy-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.proxy-table th {
  text-align: left; font-weight: 500; color: var(--text-muted);
  padding: 6px 8px; border-bottom: 1px solid var(--border-color-light);
}
.proxy-table th.num, .proxy-table td.num { text-align: right; }
.proxy-table td { padding: 8px; border-bottom: 1px solid var(--border-color-light); color: var(--text-secondary); }
.proxy-table tr:last-child td { border-bottom: none; }
.proxy-table td.num .meter-track { min-width: 70px; }
.proxy-table td.strong { font-weight: 600; color: var(--text-primary); }
.proxy-name { color: var(--text-primary); font-weight: 500; }
.proxy-ip { margin-left: 6px; font-size: 11px; color: var(--text-muted); }

/* ---------- 响应式 ---------- */
@media (max-width: 1400px) {
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
  .row-2, .row-3, .row-4 { grid-template-columns: 1fr 1fr; }
  .row-2 > .panel:first-child,
  .row-3 > .panel:first-child,
  .row-4 > .panel:nth-child(2) { grid-column: span 2; }
}
@media (max-width: 900px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .grid-row { grid-template-columns: 1fr !important; }
  .grid-row > .panel { grid-column: auto !important; }
  .trend-body { flex-direction: column; }
  .trend-stats { width: 100%; }
  .donut-row { flex-direction: column; align-items: stretch; }
  .donut-wrap { width: 100%; }
}
</style>
