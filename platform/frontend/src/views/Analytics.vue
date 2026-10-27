<template>
  <div class="analytics">
    <div class="header-row">
      <div>
        <h2>资源看板</h2>
        <div class="sub-hint">
          <template v-if="activeTab === 'overview'">
            数据口径与首页一致：已用 = 运行中 + 准备中，按并发数 × 单沙箱资源计算
            <span v-if="generatedAt"> · 更新于 {{ generatedAt }}</span>
          </template>
          <template v-else>Worker 进程状态、队列积压与预计完成时间</template>
        </div>
      </div>
      <el-button v-if="activeTab === 'overview'" :icon="Refresh" @click="load" :loading="refreshing">
        刷新
      </el-button>
    </div>

    <!-- 两个视角分开：资源总览看配置推算的资源占用，调度监控看 worker 与队列实况 -->
    <el-tabs v-model="activeTab" class="analytics-tabs">
      <el-tab-pane label="资源总览" name="overview">
        <div v-loading="loading">

        <!-- KPI -->
        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#8a8a85"></i>总资源数</div>
            <div class="kpi-value">{{ summary.instances_total }}</div>
            <div class="kpi-sub">
              CPU {{ summary.total_cpu ? fmtNum(summary.total_cpu) + ' 核' : '未配置' }}
              · 内存 {{ summary.total_memory_gb ? fmtGi(summary.total_memory_gb) : '未配置' }}
            </div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#0ca30c"></i>活跃实例</div>
            <div class="kpi-value">{{ summary.active }}</div>
            <div class="kpi-sub">运行 {{ summary.running }} · 准备 {{ summary.preparing }} · 排队 {{ summary.queued }}</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#6250d6"></i>运行中沙箱</div>
            <div class="kpi-value">{{ summary.pods }}</div>
            <div class="kpi-sub">
              上限 {{ summary.max_sandboxes ? fmtNum(summary.max_sandboxes) : '未配置' }}
              <span v-if="summary.max_sandboxes"> · {{ pct(summary.pods, summary.max_sandboxes) }}%</span>
            </div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#2a78d6"></i>CPU 占用</div>
            <div class="kpi-value">{{ fmtNum(summary.used_cpu) }}<span class="kpi-unit">核</span></div>
            <div class="kpi-sub">
              总量 {{ summary.total_cpu ? fmtNum(summary.total_cpu) + ' 核' : '未配置' }}
              <span v-if="summary.total_cpu"> · {{ pct(summary.used_cpu, summary.total_cpu) }}%</span>
            </div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#eb6834"></i>内存占用</div>
            <div class="kpi-value">{{ fmtGi(summary.used_memory_gb) }}</div>
            <div class="kpi-sub">
              总量 {{ summary.total_memory_gb ? fmtGi(summary.total_memory_gb) : '未配置' }}
              <span v-if="summary.total_memory_gb"> · {{ pct(summary.used_memory_gb, summary.total_memory_gb) }}%</span>
            </div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><i class="dot" style="background:#1baf7a"></i>活跃用户</div>
            <div class="kpi-value">{{ summary.active_users }}</div>
            <div class="kpi-sub">当前有资源占用的用户数</div>
          </div>
        </div>

        <el-row :gutter="16">
          <!-- 用户排行榜 -->
          <el-col :xs="24" :lg="12">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">用户资源占用排行</span>
                <el-radio-group v-model="rankMetric" size="small">
                  <el-radio-button value="cpu">CPU</el-radio-button>
                  <el-radio-button value="memory_gb">内存</el-radio-button>
                  <el-radio-button value="pods">沙箱数量</el-radio-button>
                </el-radio-group>
              </div>
              <el-empty v-if="!leaderboard.length" description="暂无占用中的实例" :image-size="70" />
              <template v-else>
                <EChart :option="leaderboardOption" height="300px" />
                <el-table :data="ranked" size="small" class="mini-table">
                  <el-table-column label="#" width="42" align="center">
                    <template #default="{ $index }">
                      <span class="rank-no" :class="'rank-' + ($index + 1)">{{ $index + 1 }}</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="用户" min-width="80">
                    <template #default="{ row }">
                      <i class="dot user-dot" :style="{ background: userColor(row.user) }"></i>{{ row.user }}
                    </template>
                  </el-table-column>
                  <el-table-column prop="running" label="运行" width="54" align="right" />
                  <el-table-column prop="preparing" label="准备" width="54" align="right" />
                  <el-table-column prop="queued" label="排队" width="54" align="right" />
                  <el-table-column label="沙箱" width="58" align="right">
                    <template #default="{ row }">{{ row.pods }}</template>
                  </el-table-column>
                  <el-table-column label="CPU(核)" width="76" align="right">
                    <template #default="{ row }">{{ fmtNum(row.cpu) }}</template>
                  </el-table-column>
                  <el-table-column label="内存(GiB)" width="80" align="right">
                    <template #default="{ row }">{{ fmtNum(row.memory_gb) }}</template>
                  </el-table-column>
                </el-table>
              </template>
            </div>
          </el-col>

          <!-- harness 比例 -->
          <el-col :xs="24" :md="12" :lg="6">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">Harness 类型比例</span>
                <span class="panel-hint">按沙箱数</span>
              </div>
              <el-empty v-if="!harnessDist.length" description="无数据" :image-size="70" />
              <template v-else>
                <EChart :option="harnessOption" height="300px" />
                <div class="legend-list">
                  <span v-for="h in harnessDist" :key="h.harness_type" class="legend-chip">
                    <i class="dot" :style="{ background: harnessStore.color(h.harness_type) }"></i>
                    {{ harnessStore.label(h.harness_type) }} · {{ h.pods }}
                  </span>
                </div>
              </template>
            </div>
          </el-col>

          <!-- 沙箱类型比例 -->
          <el-col :xs="24" :md="12" :lg="6">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">沙箱类型比例</span>
                <span class="panel-hint">按沙箱数</span>
              </div>
              <el-empty v-if="!sandboxDist.length" description="无数据" :image-size="70" />
              <template v-else>
                <EChart :option="sandboxOption" height="300px" />
                <div class="legend-list">
                  <span v-for="s in sandboxDist" :key="s.sandbox_type" class="legend-chip">
                    <i class="dot" :style="{ background: sandboxColor[s.sandbox_type] || '#909399' }"></i>
                    {{ sandboxLabel[s.sandbox_type] || s.sandbox_type }} · {{ s.pods }}
                  </span>
                </div>
                <div v-if="inferredCount" class="panel-foot">
                  {{ inferredCount }} 个实例的类型由配置推断得出
                </div>
              </template>
            </div>
          </el-col>

          <!-- 状态分布 -->
          <el-col :xs="24" :md="12" :lg="6">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">沙箱状态分布</span>
                <span class="panel-hint">全部实例</span>
              </div>
              <el-empty v-if="!statusCount" description="暂无实例" :image-size="70" />
              <template v-else>
                <EChart :option="statusOption" height="300px" />
                <div class="legend-list">
                  <span v-for="s in statusWithData" :key="s.status" class="legend-chip">
                    <i class="dot" :style="{ background: STATUS_COLOR[s.status] || '#c4c7cc' }"></i>
                    {{ s.label }} · {{ s.count }}
                  </span>
                </div>
              </template>
              <div class="panel-foot">
                「已结束」表示有任务失败；「准备中」已占用资源；「排队中」不占资源
              </div>
            </div>
          </el-col>

          <!-- Linux vs Windows -->
          <el-col :xs="24" :md="12" :lg="6">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">Linux / Windows</span>
                <span class="panel-hint">资源对比</span>
              </div>
              <EChart :option="osOption" height="300px" />
            </div>
          </el-col>

          <!-- 资源池：状态 6 + OS 6 + 池 12 = 24，正好填满这一行 -->
          <el-col :xs="24" :lg="12">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">资源池使用情况</span>
                <span class="panel-hint">分子 / settings/sandbox/resource_pools.yaml</span>
              </div>
              <el-empty v-if="!pools.length" description="未配置资源池" :image-size="70" />
              <div v-for="p in pools" :key="p.namespace" class="pool-item">
                <div class="pool-head">
                  <span class="pool-name">{{ p.namespace }}</span>
                  <el-tag v-if="!p.configured" size="small" type="info">未配置总量</el-tag>
                  <span class="pool-instances">{{ p.instances }} 个占用中实例</span>
                </div>
                <div class="pool-row">
                  <span class="pool-axis">CPU</span>
                  <el-progress
                    :percentage="poolPercent(p.used_cpu, p.total_cpu)"
                    :show-text="false" :stroke-width="8"
                    :color="percentColor(poolPercent(p.used_cpu, p.total_cpu))"
                    class="pool-bar" />
                  <span class="pool-val">
                    {{ fmtNum(p.used_cpu) }} / {{ p.configured ? fmtNum(p.total_cpu) : '—' }} 核
                    <span v-if="p.configured" class="pool-pct">{{ poolPercent(p.used_cpu, p.total_cpu) }}%</span>
                    <span v-if="p.queued_cpu" class="pool-queued">+{{ fmtNum(p.queued_cpu) }} 排队</span>
                  </span>
                </div>
                <div class="pool-row">
                  <span class="pool-axis">内存</span>
                  <el-progress
                    :percentage="poolPercent(p.used_memory_gb, p.total_memory_gb)"
                    :show-text="false" :stroke-width="8"
                    :color="percentColor(poolPercent(p.used_memory_gb, p.total_memory_gb))"
                    class="pool-bar" />
                  <span class="pool-val">
                    {{ fmtGi(p.used_memory_gb) }} / {{ p.configured ? fmtGi(p.total_memory_gb) : '—' }}
                    <span v-if="p.configured" class="pool-pct">{{ poolPercent(p.used_memory_gb, p.total_memory_gb) }}%</span>
                    <span v-if="p.queued_memory_gb" class="pool-queued">+{{ fmtGi(p.queued_memory_gb) }} 排队</span>
                  </span>
                </div>
                <!-- 沙箱并发上限：按机器组派生（Σ 节点数 × 单机可用沙箱数），老格式池无此数 -->
                <div class="pool-row">
                  <span class="pool-axis">沙箱</span>
                  <el-progress
                    :percentage="poolPercent(p.pods, p.max_sandboxes)"
                    :show-text="false" :stroke-width="8"
                    :color="percentColor(poolPercent(p.pods, p.max_sandboxes))"
                    class="pool-bar" />
                  <span class="pool-val">
                    {{ fmtNum(p.pods) }} / {{ p.max_sandboxes ? fmtNum(p.max_sandboxes) : '—' }}
                    <span v-if="p.max_sandboxes" class="pool-pct">{{ poolPercent(p.pods, p.max_sandboxes) }}%</span>
                    <span v-if="!p.max_sandboxes" class="pool-pct">需按机器组配置</span>
                  </span>
                </div>
              </div>
            </div>
          </el-col>

          <!-- 代理负载：按角色看模型请求压在哪台网关上 -->
          <el-col :span="24">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">代理负载（按角色）</span>
                <span class="panel-hint">仅统计运行中实例 · 负载 = 并发沙箱数</span>
              </div>
              <el-row :gutter="16">
                <el-col v-for="role in PROXY_ROLES" :key="role" :xs="24" :md="8">
                  <div class="proxy-role">
                    <div class="proxy-role-head">
                      <span class="proxy-role-name">{{ role }}</span>
                      <span class="proxy-role-sum">{{ proxyRolePods(role) }} 沙箱</span>
                    </div>
                    <el-empty
                      v-if="!proxyOfRole(role).length"
                      description="暂无运行中实例" :image-size="50" />
                    <div v-for="p in proxyOfRole(role)" :key="p.host" class="proxy-item">
                      <div class="proxy-head">
                        <span class="proxy-name">
                          {{ p.proxy_name || p.host }}
                          <span v-if="p.proxy_name" class="proxy-ip">{{ p.host }}</span>
                          <el-tag v-else size="small" type="info">未登记</el-tag>
                        </span>
                        <span class="proxy-inst">{{ p.instances }} 实例</span>
                      </div>
                      <div class="proxy-row">
                        <el-progress
                          :percentage="proxyPercent(role, p.pods)"
                          :show-text="false" :stroke-width="8"
                          :color="proxyColor(role)"
                          class="pool-bar" />
                        <span class="proxy-val">
                          {{ p.pods }} 沙箱
                          <span class="proxy-sub">{{ fmtNum(p.cpu) }} 核 · {{ fmtGi(p.memory_gb) }}</span>
                        </span>
                      </div>
                    </div>
                  </div>
                </el-col>
              </el-row>
              <div class="panel-foot">
                代理名来自 settings/proxy_name.json（IP → 名称）；未登记的 IP 直接显示地址。结尾的 <code>/v1</code> 与尾斜杠已归并
              </div>
            </div>
          </el-col>

          <!-- 趋势 -->
          <el-col :span="24">
            <div class="glass-card panel">
              <div class="panel-head">
                <span class="panel-title">资源使用趋势</span>
                <div class="head-controls">
                  <el-radio-group v-model="trendMetric" size="small">
                    <el-radio-button value="cpu">CPU</el-radio-button>
                    <el-radio-button value="memory_gb">内存</el-radio-button>
                    <el-radio-button value="pods">沙箱数量</el-radio-button>
                  </el-radio-group>
                  <el-radio-group v-model="hours" size="small" @change="load">
                    <el-radio-button :value="1">1h</el-radio-button>
                    <el-radio-button :value="6">6h</el-radio-button>
                    <el-radio-button :value="24">24h</el-radio-button>
                    <el-radio-button :value="72">3d</el-radio-button>
                    <el-radio-button :value="168">7d</el-radio-button>
                  </el-radio-group>
                </div>
              </div>
              <div class="panel-hint trend-hint">
                按实例启停时间重建，非真实采样
                <template v-if="trend.truncated"> · 实例过多，仅统计最近部分，曲线可能偏低</template>
              </div>
              <el-empty
                v-if="trend.points.length < 2"
                description="该区间内没有已启动的实例，暂无趋势" :image-size="70" />
              <EChart v-else :option="trendOption" height="300px" />
            </div>
          </el-col>
        </el-row>
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
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'
import EChart from '../components/EChart.vue'
import WorkerMonitor from '../components/WorkerMonitor.vue'
import { useHarnessStore } from '../stores/harness'
import { useAuthStore } from '../stores/auth'

const harnessStore = useHarnessStore()
const authStore = useAuthStore()

const activeTab = ref('overview')
const loading = ref(true)
const refreshing = ref(false)
const hours = ref(24)
const trendMetric = ref('cpu')
const rankMetric = ref('pods')

const summary = ref({
  active: 0, running: 0, preparing: 0, queued: 0, pods: 0,
  used_cpu: 0, used_memory_gb: 0, instances_total: 0, active_users: 0,
  total_cpu: 0, total_memory_gb: 0, max_sandboxes: 0,
})
const statusDist = ref([])
const harnessDist = ref([])
const sandboxDist = ref([])
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
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}
function fmtGi(v) {
  const n = Number(v || 0)
  return `${Number.isInteger(n) ? n : n.toFixed(1)} GiB`
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

// ---------- 代理负载 ----------
// 角色顺序与后端 PROXY_ROLES 一致，缺失的角色也要占一栏（避免列数随数据跳动）
const PROXY_ROLES = ['主Agent', '用户模拟', 'Evaluator']
const PROXY_ROLE_COLOR = {
  '主Agent': '#6366f1',
  '用户模拟': '#10b981',
  'Evaluator': '#8b5cf6',
}

function proxyOfRole(role) {
  return proxyUsage.value.filter(p => p.role === role)
}
function proxyRolePods(role) {
  return proxyOfRole(role).reduce((s, p) => s + (p.pods || 0), 0)
}
// 百分比只在同一角色内比较，所以分母取本角色最大值：不同角色的总量量级差很大，
// 共用一个分母会让小角色整条条形塌成看不见。
function proxyPercent(role, pods) {
  const max = Math.max(...proxyOfRole(role).map(p => p.pods || 0), 0)
  return max ? Math.round((pods / max) * 100) : 0
}
function proxyColor(role) {
  return PROXY_ROLE_COLOR[role] || '#2a78d6'
}

// ---------- 汇总 ----------
const poolTotalCpu = computed(() =>
  pools.value.reduce((s, p) => s + (p.configured ? p.total_cpu : 0), 0))
const poolTotalMem = computed(() =>
  pools.value.reduce((s, p) => s + (p.configured ? p.total_memory_gb : 0), 0))
const inferredCount = computed(() =>
  sandboxDist.value.reduce((s, x) => s + (x.inferred_count || 0), 0))

// ---------- 图表 option ----------
const AXIS_LABEL = '#64748b'
const SPLIT_LINE = { lineStyle: { color: '#f1f5f9' } }

// 排行指标：CPU / 内存 / 沙箱数量，与设计稿的三个页签一致
const RANK_METRICS = {
  cpu: { field: 'cpu', unit: '核', color: '#6366f1' },
  memory_gb: { field: 'memory_gb', unit: 'GiB', color: '#8b5cf6' },
  pods: { field: 'pods', unit: '个沙箱', color: '#10b981' },
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

const ranked = computed(() => {
  const m = RANK_METRICS[rankMetric.value]
  return [...leaderboard.value].sort((a, b) =>
    (b[m.field] || 0) - (a[m.field] || 0) || (b.queued || 0) - (a.queued || 0))
})

const leaderboardOption = computed(() => {
  const m = RANK_METRICS[rankMetric.value]
  const rows = [...ranked.value].reverse()   // 横向柱状图自下而上
  return {
    grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: (ps) => {
        const p = ps[0]
        const r = rows[p.dataIndex]
        return `<b>${r.user}</b><br/>CPU ${fmtNum(r.cpu)} 核 · 内存 ${fmtGi(r.memory_gb)}<br/>`
          + `沙箱 ${r.pods} · 运行 ${r.running} · 准备 ${r.preparing} · 排队 ${r.queued}`
      },
    },
    xAxis: {
      type: 'value', name: m.unit, nameTextStyle: { color: AXIS_LABEL },
      axisLabel: { color: AXIS_LABEL }, splitLine: SPLIT_LINE,
    },
    yAxis: {
      type: 'category',
      data: rows.map(r => r.user),
      axisLabel: { color: '#334155', width: 90, overflow: 'truncate' },
      axisLine: { lineStyle: { color: '#e5e7eb' } },
    },
    series: [{
      type: 'bar',
      data: rows.map(r => ({
        value: r[m.field] || 0,
        itemStyle: { color: userColor(r.user) },
      })),
      barMaxWidth: 18,
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      label: {
        show: true, position: 'right', color: AXIS_LABEL,
        formatter: ({ value }) => fmtNum(value),
      },
    }],
  }
})

function pieOption(items, nameKey, valueKey, colorFn, labelFn) {
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => `${p.name}<br/><b>${p.value}</b> 个沙箱（${p.percent}%）`,
    },
    // 不设 legend 容器：改为图下方的自绘图例，项多时可自然换行，
    // 不用 echarts 的分页箭头（窄卡片里会挤出「1/3 ◀ ▶」）
    series: [{
      type: 'pie',
      radius: ['42%', '66%'],
      center: ['50%', '48%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: '#fff', borderWidth: 2 },
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 13, fontWeight: 600 } },
      data: items.map((it) => ({
        name: labelFn(it[nameKey]),
        value: it[valueKey],
        itemStyle: { color: colorFn(it[nameKey]) },
      })),
    }],
  }
}

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

const harnessOption = computed(() =>
  pieOption(harnessDist.value, 'harness_type', 'pods',
    (t) => harnessStore.color(t), (t) => harnessStore.label(t)))

// Linux / Windows 用与趋势图一致的槽位，避免同一概念两种颜色
const sandboxColor = { linux: '#2a78d6', windows: '#eb6834' }
const sandboxLabel = { linux: 'Linux', windows: 'Windows' }
const sandboxOption = computed(() =>
  pieOption(sandboxDist.value, 'sandbox_type', 'pods',
    (t) => sandboxColor[t] || '#909399', (t) => sandboxLabel[t] || t))

const statusWithData = computed(() => statusDist.value.filter(s => s.count > 0))
const statusCount = computed(() => statusWithData.value.length)

const statusOption = computed(() => {
  const items = statusWithData.value
  return pieOption(items, 'status', 'count',
    (s) => STATUS_COLOR[s] || '#c4c7cc', (s) => {
      const it = statusDist.value.find(x => x.status === s)
      return it ? it.label : s
    })
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
      type: 'value', axisLabel: { color: AXIS_LABEL }, splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: 'Linux', type: 'bar', barMaxWidth: 40,
        data: [l.cpu || 0, l.memory_gb || 0],
        itemStyle: { color: sandboxColor.linux, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: AXIS_LABEL, formatter: ({ value }) => fmtNum(value) },
      },
      {
        name: 'Windows', type: 'bar', barMaxWidth: 40,
        data: [w.cpu || 0, w.memory_gb || 0],
        itemStyle: { color: sandboxColor.windows, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: 'top', color: AXIS_LABEL, formatter: ({ value }) => fmtNum(value) },
      },
    ],
  }
})

// 趋势指标：总量 + Linux / Windows 拆分（与设计稿三页签一致）
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
      axisLabel: { color: AXIS_LABEL }, splitLine: SPLIT_LINE,
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

// ---------- 数据 ----------
async function load({ silent = false } = {}) {
  if (!silent) refreshing.value = true
  try {
    const d = await api.get('/stats', { params: { hours: hours.value } })
    summary.value = d.summary || summary.value
    statusDist.value = d.status_distribution || []
    harnessDist.value = d.harness_distribution || []
    sandboxDist.value = d.sandbox_type_distribution || []
    osCompare.value = d.os_resource_comparison || { linux: {}, windows: {} }
    leaderboard.value = d.user_leaderboard || []
    pools.value = d.pool_usage || []
    proxyUsage.value = d.proxy_usage || []
    trend.value = d.trend || { points: [], truncated: false }
    generatedAt.value = (d.generated_at || '').replace('T', ' ')
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

function onVisibility() {
  if (document.hidden) {
    clearInterval(timer)
    timer = null
  } else if (!timer) {
    load({ silent: true })
    timer = setInterval(() => load({ silent: true }), 30000)
  }
}

onMounted(() => {
  harnessStore.load()
  load()
  timer = setInterval(() => load({ silent: true }), 30000)
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<style scoped>
.analytics { display: flex; flex-direction: column; gap: 16px; }
.header-row { display: flex; justify-content: space-between; align-items: flex-start; }
.header-row h2 { font-size: 20px; font-weight: 600; }
.sub-hint { color: var(--text-muted); font-size: 12px; margin-top: 4px; }

.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
  gap: 14px;
}
.kpi-card {
  background: var(--bg-card);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  padding: 18px 20px;
  box-shadow: var(--shadow-sm);
}
.kpi-label { color: var(--text-secondary); font-size: 13px; display: flex; align-items: center; gap: 6px; }
.kpi-value { font-size: 26px; font-weight: 700; margin-top: 8px; color: var(--text-primary); }
.kpi-unit { font-size: 13px; font-weight: 500; margin-left: 4px; color: var(--text-muted); }
.kpi-sub { color: var(--text-muted); font-size: 12px; margin-top: 4px; }

.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

.panel { display: flex; flex-direction: column; height: 100%; margin-bottom: 16px; padding: 20px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; gap: 8px; }
.head-controls { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
.panel-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.panel-hint { font-size: 12px; color: var(--text-muted); }
.panel-foot { font-size: 11px; color: var(--text-muted); margin-top: 8px; line-height: 1.5; }
.trend-hint { margin-top: 0; margin-bottom: 4px; }

.mini-table { margin-top: 8px; }

.legend-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 2px;
}
.legend-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--text-secondary);
}

.rank-no {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
  background: #f1f5f9;
}
.rank-no.rank-1 { background: #2a78d6; color: #fff; }
.rank-no.rank-2 { background: #eb6834; color: #fff; }
.rank-no.rank-3 { background: #1baf7a; color: #fff; }
.user-dot { margin-right: 6px; }

.pool-item { padding: 10px 0; border-bottom: 1px solid var(--border-color-light); }
.pool-item:last-child { border-bottom: none; }
.pool-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.pool-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.pool-instances { margin-left: auto; font-size: 12px; color: var(--text-muted); }
.pool-row { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
.pool-axis { width: 34px; font-size: 12px; color: var(--text-muted); flex: none; }
.pool-bar { flex: 1; }
.pool-val { width: 260px; white-space: nowrap; text-align: right; font-size: 12px; color: var(--text-secondary); flex: none; }
.pool-queued { color: #eb6834; margin-left: 6px; }
.pool-pct { color: var(--text-muted); margin-left: 6px; }

/* 代理负载：每个角色一栏，栏内各代理按并发沙箱数降序 */
.proxy-role { padding: 4px 0 12px; }
.proxy-role-head {
  display: flex; align-items: baseline; justify-content: space-between;
  padding-bottom: 8px; margin-bottom: 4px;
  border-bottom: 1px solid var(--border-color-light);
}
.proxy-role-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.proxy-role-sum { font-size: 12px; color: var(--text-muted); }
.proxy-item { padding: 8px 0; }
.proxy-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.proxy-name { font-size: 13px; font-weight: 500; color: var(--text-primary); }
.proxy-ip { margin-left: 6px; font-size: 11px; color: var(--text-muted); font-weight: 400; }
.proxy-inst { margin-left: auto; font-size: 12px; color: var(--text-muted); }
.proxy-row { display: flex; align-items: center; gap: 10px; }
.proxy-val { width: 150px; flex: none; text-align: right; font-size: 12px; color: var(--text-secondary); white-space: nowrap; }
.proxy-sub { display: block; font-size: 11px; color: var(--text-muted); }

@media (max-width: 768px) {
  .proxy-row { flex-wrap: wrap; }
  .proxy-val { width: auto; text-align: left; }
}

@media (max-width: 768px) {
  .pool-row { flex-wrap: wrap; }
  .pool-val { width: auto; text-align: left; }
}
</style>
