<template>
  <div>
    <div class="header-row">
      <h2>任务实例管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="router.push('/create')">
        <el-icon><Plus /></el-icon> 新建任务
      </el-button>
    </div>

    <div class="resource-panel">
      <div class="resource-panel-head">
        <span class="resource-panel-title">当前资源使用情况</span>
        <span class="resource-panel-hint">按当前并发数 × 沙箱配置计算</span>
      </div>

      <div class="resource-panel-body">
        <!-- CPU -->
        <div class="resource-card">
          <div class="resource-card-head">
            <el-icon :size="18" style="color:#6366f1"><Cpu /></el-icon>
            <span>CPU 核数</span>
          </div>
          <div class="resource-main">
            <div class="resource-main-left">
              <div class="resource-value">
                <span class="resource-used">{{ fmtNum(usedCpu) }}</span>
                <span class="resource-sep">/</span>
                <span class="resource-total">{{ totalCpu ? fmtNum(totalCpu) : '—' }}</span>
                <span class="resource-unit">核</span>
              </div>
              <div class="resource-sub">上限 {{ fmtNum(usedCpuLimit) }} 核</div>
              <el-progress
                :percentage="cpuPercent" :show-text="false" :stroke-width="10"
                :color="percentColor(cpuPercent)" />
              <div class="resource-percent">{{ totalCpu ? cpuPercent.toFixed(0) + '%' : '未配置总量' }}</div>
            </div>
            <div class="legend">
              <div class="legend-row"><i class="dot" style="background:#6366f1"></i>运行中任务<b>{{ fmtNum(runningCpu) }} 核</b></div>
              <div class="legend-row"><i class="dot" style="background:#f59e0b"></i>排队中任务<b>{{ fmtNum(queuedCpu) }} 核</b></div>
              <div class="legend-row"><i class="dot" style="background:#cbd5e1"></i>可用<b>{{ totalCpu ? fmtNum(availCpu) + ' 核' : '—' }}</b></div>
            </div>
          </div>
        </div>

        <!-- 内存 -->
        <div class="resource-card">
          <div class="resource-card-head">
            <el-icon :size="18" style="color:#8b5cf6"><Coin /></el-icon>
            <span>内存</span>
          </div>
          <div class="resource-main">
            <div class="resource-main-left">
              <div class="resource-value">
                <span class="resource-used">{{ fmtGi(usedMem) }}</span>
                <span class="resource-sep">/</span>
                <span class="resource-total">{{ totalMem ? fmtGi(totalMem) : '—' }}</span>
              </div>
              <div class="resource-sub">上限 {{ fmtGi(usedMemLimit) }}</div>
              <el-progress
                :percentage="memPercent" :show-text="false" :stroke-width="10"
                :color="percentColor(memPercent)" />
              <div class="resource-percent">{{ totalMem ? memPercent.toFixed(0) + '%' : '未配置总量' }}</div>
            </div>
            <div class="legend">
              <div class="legend-row"><i class="dot" style="background:#8b5cf6"></i>运行中任务<b>{{ fmtGi(runningMem) }}</b></div>
              <div class="legend-row"><i class="dot" style="background:#f59e0b"></i>排队中任务<b>{{ fmtGi(queuedMem) }}</b></div>
              <div class="legend-row"><i class="dot" style="background:#cbd5e1"></i>可用<b>{{ totalMem ? fmtGi(availMem) : '—' }}</b></div>
            </div>
          </div>
        </div>

        <!-- 任务统计 -->
        <div class="resource-card task-card">
          <div class="resource-card-head">
            <el-icon :size="18" style="color:#64748b"><Histogram /></el-icon>
            <span>任务统计</span>
          </div>
          <div class="task-stats">
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#94a3b8"></i>全部</span>
              <span class="task-stat-value">{{ instances.length }}</span>
            </div>
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#10b981"></i>运行中</span>
              <span class="task-stat-value" style="color:#10b981">{{ runningInstances.length }}</span>
            </div>
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#3b82f6"></i>排队中</span>
              <span class="task-stat-value" style="color:#3b82f6">{{ queuedInstances.length }}</span>
            </div>
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#059669"></i>已完成</span>
              <span class="task-stat-value" style="color:#059669">{{ doneCount }}</span>
            </div>
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#ef4444"></i>失败</span>
              <span class="task-stat-value" style="color:#ef4444">{{ failedCount }}</span>
            </div>
            <div class="task-stat">
              <span class="task-stat-label"><i class="dot" style="background:#f59e0b"></i>运行中沙箱</span>
              <span class="task-stat-value" style="color:#f59e0b">{{ totalRunningPods }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="filter-bar">
      <el-select v-model="statusFilter" placeholder="状态" size="default" style="width:140px">
        <el-option label="全部" value="" />
        <el-option label="运行中" value="running" />
        <el-option label="准备中" value="preparing" />
        <el-option label="排队中" value="queued" />
        <el-option label="已完成" value="completed" />
        <el-option label="已结束" value="finished" />
        <el-option label="已停止" value="stopped" />
        <el-option label="失败" value="failed" />
        <el-option label="待启动" value="created" />
      </el-select>
      <el-select v-model="queueFilter" placeholder="任务队列" size="default" style="width:170px">
        <el-option label="全部" value="" />
        <el-option label="未入队" value="__none__" />
        <el-option v-for="q in queueOptions" :key="q.id" :label="q.id" :value="q.id" />
      </el-select>
      <el-select v-model="harnessFilter" placeholder="Harness筛选" clearable size="default" style="width:140px">
        <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
      </el-select>
      <el-select v-model="userFilter" placeholder="创建者筛选" clearable filterable size="default" style="width:160px">
        <el-option v-for="u in userOptions" :key="u" :label="u" :value="u" />
      </el-select>
      <el-date-picker v-model="dateRange" type="daterange" range-separator="~"
        start-placeholder="开始" end-placeholder="结束" value-format="YYYY-MM-DD"
        size="default" style="width:220px" clearable />
      <el-input v-model="nameSearch" placeholder="搜索任务名称" clearable size="default" style="width:260px" prefix-icon="Search" />
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="pagedInstances" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="name" label="实例名称" min-width="160" show-overflow-tooltip resizable />
        <el-table-column prop="harness_type" label="Harness" width="160" align="center" resizable>
          <template #default="{row}">
            <el-tag :color="harnessColor(row.harness_type)" :style="{borderColor: harnessColor(row.harness_type), maxWidth: '140px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'}" effect="dark" size="default">
              {{ harnessLabel(row.harness_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" resizable>
          <template #default="{row}">
            <el-tag :type="statusColor(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="任务队列" width="110" resizable>
          <template #default="{row}">
            <el-tag v-if="row.queue_id" size="small" effect="plain">{{ row.queue_id }}</el-tag>
            <span v-else style="color:var(--text-muted)">-</span>
          </template>
        </el-table-column>
        <el-table-column label="队列位置" width="110" align="center" resizable>
          <template #default="{row}">
            <span v-if="row.status === 'queued'" class="mono-num" style="color:#e6a23c">
              {{ queuePositionLabel(row) }}
            </span>
            <span v-else class="mono-num">-</span>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="160" resizable>
          <template #default="{row}">
            <el-progress :percentage="progress(row)" :stroke-width="8" :show-text="false"
              :color="progressColor" />
            <div v-if="timeEstimates[row.id]" style="font-size:11px;color:var(--text-muted);margin-top:2px">
              {{ formatDuration(timeEstimates[row.id].estimated_remaining_seconds) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="数字进度" width="130" align="center" resizable>
          <template #default="{row}">
            <span v-if="row.status === 'preparing'" class="mono-num" style="color:#e6a23c">
              下载 {{ prepareProgress[row.id]?.downloaded_configs ?? 0 }} 个
            </span>
            <span v-else class="mono-num">{{ row.completed_tasks + row.failed_tasks }}/{{ row.total_tasks }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="completed_tasks" label="成功" width="80" align="center" resizable>
          <template #default="{row}">
            <span class="mono-num" style="color:#059669">{{ row.completed_tasks }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="failed_tasks" label="失败" width="80" align="center" resizable>
          <template #default="{row}">
            <span class="mono-num" :style="{color: row.failed_tasks > 0 ? '#ef4444' : 'var(--text-muted)'}">{{ row.failed_tasks }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="concurrent_num" label="并发" width="70" align="center" resizable>
          <template #default="{row}"><span class="mono-num">{{ row.concurrent_num }}</span></template>
        </el-table-column>
        <el-table-column prop="created_by" label="创建者" width="120" show-overflow-tooltip resizable />
        <el-table-column prop="created_at" label="创建时间" width="180" resizable />
        <el-table-column label="操作" width="280">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" @click="router.push(`/instance/${row.id}`)">详情</el-button>
              <el-button size="small" type="primary" @click="router.push(`/instance/${row.id}?tab=logs`)">日志</el-button>
              <el-button size="small" @click="router.push(`/instance/${row.id}?tab=outputs`)">输出</el-button>
              <el-dropdown v-if="authStore.isOperator" trigger="click" @command="cmd => handleCommand(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="copy">复制</el-dropdown-item>
                    <el-dropdown-item v-if="!['running','preparing'].includes(row.status)" command="start">
                      {{ row.status === 'queued' ? '启动（重新加入排队）' : '启动' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="['running','queued'].includes(row.status)" command="stop">
                      {{ row.status === 'queued' ? '停止（退出队列）' : '停止' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="row.failed_tasks > 0 && !['running','preparing'].includes(row.status)" command="retry">重跑失败</el-dropdown-item>
                    <el-dropdown-item v-if="!['running','preparing'].includes(row.status)" command="delete" divided>删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pagination-bar" v-if="filteredInstances.length > 0">
      <el-pagination
        v-model:current-page="currentPage"
        v-model:page-size="pageSize"
        :total="filteredInstances.length"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next, jumper"
        background />
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Search, Cpu, Coin, Histogram } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'

const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const instances = ref([])
const loading = ref(false)
const timeEstimates = ref({})
const prepareProgress = ref({})
const statusFilter = ref('')
const queueFilter = ref('')
const queueOptions = ref([])
const harnessFilter = ref('')
const userFilter = ref('')
const dateRange = ref(null)
const nameSearch = ref('')
const currentPage = ref(1)
const pageSize = ref(20)
const router = useRouter()
const route = useRoute()
let timer = null

const progressColor = [
  { color: '#6366f1', percentage: 50 },
  { color: '#8b5cf6', percentage: 80 },
  { color: '#10b981', percentage: 100 },
]

const runningInstances = computed(() => instances.value.filter(i => i.status === 'running'))

// ===== 资源使用情况 =====
// 口径与后端 check_global_sandbox_capacity 一致：preparing 期间已经占住额度，
// 所以「已用」= running + preparing；「排队中」单列，不计入已用。
const resourcePools = ref({ total_cpu: 0, total_memory_gb: 0, pools: [] })
const activeInstances = computed(() => instances.value.filter(i => ['running', 'preparing'].includes(i.status)))
const queuedInstances = computed(() => instances.value.filter(i => i.status === 'queued'))
const doneCount = computed(() => instances.value.filter(i => ['completed', 'finished'].includes(i.status)).length)
// 「运行中沙箱」= 运行中任务的并发数之和（原看板口径，保留）
const totalRunningPods = computed(() => runningInstances.value.reduce((sum, inst) => sum + (inst.concurrent_num || 0), 0))
const failedCount = computed(() => instances.value.filter(i => i.status === 'failed').length)

// 每个实例占用 = 并发数 × 单沙箱资源
function sumResources(list, field) {
  return list.reduce((sum, i) => sum + (i.concurrent_num || 0) * (i[field] || 0), 0)
}
const runningCpu = computed(() => sumResources(activeInstances.value, 'cpu_request'))
const runningCpuLimit = computed(() => sumResources(activeInstances.value, 'cpu_limit'))
const runningMem = computed(() => sumResources(activeInstances.value, 'memory_request'))
const runningMemLimit = computed(() => sumResources(activeInstances.value, 'memory_limit'))
const queuedCpu = computed(() => sumResources(queuedInstances.value, 'cpu_request'))
const queuedMem = computed(() => sumResources(queuedInstances.value, 'memory_request'))

const usedCpu = runningCpu
const usedCpuLimit = runningCpuLimit
const usedMem = runningMem
const usedMemLimit = runningMemLimit
const totalCpu = computed(() => resourcePools.value.total_cpu || 0)
const totalMem = computed(() => resourcePools.value.total_memory_gb || 0)

// 可用 = 总量 - 已用 - 排队；夹到 0，避免三段图例出现负数
const availCpu = computed(() => Math.max(0, totalCpu.value - runningCpu.value - queuedCpu.value))
const availMem = computed(() => Math.max(0, totalMem.value - runningMem.value - queuedMem.value))

// 进度条与主数值同口径（只算已用），排队中的量单独列在右侧图例里
const cpuPercent = computed(() => calcPercent(runningCpu.value, totalCpu.value))
const memPercent = computed(() => calcPercent(runningMem.value, totalMem.value))
function calcPercent(used, total) {
  if (!total) return 0
  return Math.min(100, Math.max(0, (used / total) * 100))
}

function percentColor(p) {
  if (p >= 90) return '#ef4444'
  if (p >= 70) return '#f59e0b'
  return '#6366f1'
}

function fmtNum(v) {
  const n = Number(v) || 0
  return n % 1 === 0 ? n.toLocaleString() : n.toFixed(1)
}

// 内存统一用 GiB 展示（与资源池配置同单位），大数值带千分位
function fmtGi(v) {
  return fmtNum(v) + ' GiB'
}

const userOptions = computed(() => {
  const set = new Set()
  instances.value.forEach(i => { if (i.created_by) set.add(i.created_by) })
  return Array.from(set).sort()
})

const filteredInstances = computed(() => {
  let list = instances.value
  if (statusFilter.value) list = list.filter(i => i.status === statusFilter.value)
  if (queueFilter.value) {
    list = queueFilter.value === '__none__'
      ? list.filter(i => !i.queue_id)
      : list.filter(i => i.queue_id === queueFilter.value)
  }
  if (harnessFilter.value) list = list.filter(i => (i.harness_type || 'openclaw') === harnessFilter.value)
  if (userFilter.value) list = list.filter(i => i.created_by === userFilter.value)
  if (dateRange.value && dateRange.value.length === 2) {
    const [start, end] = dateRange.value
    list = list.filter(i => {
      const d = (i.created_at || '').slice(0, 10)
      return d && d >= start && d <= end
    })
  }
  if (nameSearch.value) {
    const keyword = nameSearch.value.toLowerCase()
    list = list.filter(i => i.name.toLowerCase().includes(keyword))
  }
  // 排序：按创建时间倒序，最新建立的任务排在最前。
  // 注：这只是「列表显示顺序」。队列内的**执行**顺序另由 queue_position 升序决定，
  // 因此同一队列在列表里会呈现为 queue_position 递减（最新创建的在最上面）。
  return [...list].sort((a, b) => {
    const ta = a.created_at || ''
    const tb = b.created_at || ''
    if (ta === tb) return 0
    return ta < tb ? 1 : -1
  })
})

const pagedInstances = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredInstances.value.slice(start, start + pageSize.value)
})

// 筛选/每页条数变化时回到第一页；当前页因数据减少而越界时也回退，避免停在空白页
watch([statusFilter, queueFilter, harnessFilter, userFilter, dateRange, nameSearch, pageSize], () => {
  if (restoring) return
  currentPage.value = 1
  syncQuery()
})
watch([currentPage, pageSize], () => { syncQuery() })
watch(filteredInstances, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

// 筛选状态写进 URL query，详情页返回时 query 还在即可恢复筛选态
let restoring = false
function syncQuery() {
  if (restoring) return
  const q = {}
  if (statusFilter.value) q.status = statusFilter.value
  if (queueFilter.value) q.queue = queueFilter.value
  if (harnessFilter.value) q.harness = harnessFilter.value
  if (userFilter.value) q.user = userFilter.value
  if (dateRange.value && dateRange.value.length === 2) q.date = dateRange.value.join(',')
  if (nameSearch.value) q.q = nameSearch.value
  if (currentPage.value > 1) q.page = String(currentPage.value)
  if (pageSize.value !== 20) q.size = String(pageSize.value)
  router.replace({ query: q })
}
function restoreFromQuery() {
  restoring = true
  const q = route.query
  if (q.status) statusFilter.value = String(q.status)
  if (q.queue) queueFilter.value = String(q.queue)
  if (q.harness) harnessFilter.value = String(q.harness)
  if (q.user) userFilter.value = String(q.user)
  if (q.date) dateRange.value = String(q.date).split(',')
  if (q.q) nameSearch.value = String(q.q)
  if (q.page) currentPage.value = Number(q.page) || 1
  if (q.size) pageSize.value = Number(q.size) || 20
  restoring = false
}

async function loadInstances() {
  const isFirstLoad = instances.value.length === 0
  if (isFirstLoad) loading.value = true
  try {
    instances.value = await api.get('/instances')
    loadTimeEstimates()
    loadPrepareProgress()
  } finally { if (isFirstLoad) loading.value = false }
}

async function loadQueueOptions() {
  try {
    queueOptions.value = await api.get('/queues')
  } catch {
    queueOptions.value = []
  }
}

// 资源池总量来自 settings/sandbox/resource_pools.yaml，只有配置文件变更才需要重读，
// 因此挂载时加载一次即可，不跟着 10s 轮询。
async function loadResourcePools() {
  try {
    resourcePools.value = await api.get('/resource-pools')
  } catch {
    resourcePools.value = { total_cpu: 0, total_memory_gb: 0, pools: [] }
  }
}

async function loadTimeEstimates() {
  const running = instances.value.filter(i => i.status === 'running')
  const results = await Promise.allSettled(running.map(inst => api.get(`/instances/${inst.id}/overview`)))
  results.forEach((r, idx) => {
    if (r.status === 'fulfilled' && r.value.estimated_remaining_seconds != null) {
      timeEstimates.value[running[idx].id] = r.value
    }
  })
}

async function loadPrepareProgress() {
  const preparing = instances.value.filter(i => i.status === 'preparing')
  // 清掉已不在准备中的旧进度
  Object.keys(prepareProgress.value).forEach(id => {
    if (!preparing.some(i => i.id === id)) delete prepareProgress.value[id]
  })
  const results = await Promise.allSettled(preparing.map(inst => api.get(`/instances/${inst.id}/prepare-progress`)))
  results.forEach((r, idx) => {
    if (r.status === 'fulfilled') prepareProgress.value[preparing[idx].id] = r.value
  })
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
  return { running: 'success', preparing: 'warning', queued: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', failed: 'danger', created: '' }[s] || ''
}
function statusText(s) {
  return { running: '运行中', preparing: '准备中', queued: '排队中', completed: '已完成', finished: '已结束', stopped: '已停止', failed: '失败', created: '待启动' }[s] || s
}

// 队列位置：显示「在当前排队队列里的名次」，而不是绝对序号。
// queue_position 是只增不减的序号（每次「启动」重新入队都会 +1），会有空洞，
// 直接显示会误导（例如队里第 4 个任务却显示 9）。这里按同队列 queued 任务
// 的 queue_position 排序后算出名次，与设计图的「1 (等待中)」一致。
function queuePositionLabel(row) {
  const waiting = instances.value
    .filter(i => i.queue_id === row.queue_id && i.status === 'queued')
    .sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0))
  const rank = waiting.findIndex(i => i.id === row.id) + 1
  return rank > 0 ? `${rank} (等待中)` : '-'
}
function harnessColor(t) { return harnessStore.color(t) }
function harnessLabel(t) { return harnessStore.label(t) }
function progress(row) {
  if (!row.total_tasks) return 0
  return Math.round(((row.completed_tasks + row.failed_tasks) / row.total_tasks) * 100)
}

async function startInstance(id) { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadInstances() }
async function stopInstance(id) { await ElMessageBox.confirm('确认停止该实例？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadInstances() }
async function retryFailed(id) { router.push(`/create?copy_from=${id}&copy_complete_from=${id}`) }
async function deleteInstance(id) { await ElMessageBox.confirm('确认删除该实例？配置文件也会被删除。', '提示', { type: 'warning' }); await api.delete(`/instances/${id}`); ElMessage.success('已删除'); loadInstances() }

function handleCommand(cmd, row) {
  const actions = { copy: () => router.push(`/create?copy_from=${row.id}`), start: () => startInstance(row.id), stop: () => stopInstance(row.id), retry: () => retryFailed(row.id), delete: () => deleteInstance(row.id) }
  actions[cmd]?.()
}

onMounted(() => { restoreFromQuery(); harnessStore.load(); loadInstances(); loadQueueOptions(); loadResourcePools(); timer = setInterval(loadInstances, 10000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }

/* ===== 当前资源使用情况 ===== */
.resource-panel {
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
  padding: 16px 20px 18px;
  margin-bottom: 20px;
}
.resource-panel-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin-bottom: 14px;
}
.resource-panel-title { font-size: 15px; font-weight: 600; color: var(--text-primary); }
.resource-panel-hint { font-size: 12px; color: var(--text-muted); }

.resource-panel-body {
  display: grid;
  grid-template-columns: 1.45fr 1.45fr 1fr;
  gap: 16px;
}
.resource-card {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  padding: 14px 16px;
  min-width: 0;
}
.resource-card-head {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  margin-bottom: 10px;
}
.resource-main { display: flex; gap: 16px; align-items: flex-start; }
.resource-main-left { flex: 1 1 auto; min-width: 0; }
.resource-value {
  display: flex; align-items: baseline; gap: 4px;
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.resource-used { font-size: 26px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }
.resource-sep { font-size: 18px; color: #cbd5e1; margin: 0 2px; }
.resource-total { font-size: 18px; font-weight: 600; color: var(--text-secondary); }
.resource-unit { font-size: 13px; color: var(--text-muted); margin-left: 2px; }
.resource-sub { font-size: 12px; color: var(--text-muted); margin: 2px 0 8px; }
.resource-percent { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.resource-card :deep(.el-progress-bar__outer) { background: #eef2f7; }
.resource-card .el-progress { margin-right: 4px; }

.legend { flex: 0 0 auto; display: flex; flex-direction: column; gap: 7px; padding-top: 4px; min-width: 128px; }
.legend-row {
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: var(--text-secondary); white-space: nowrap;
}
.legend-row b { margin-left: auto; font-weight: 600; color: var(--text-primary); padding-left: 10px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; display: inline-block; }

.task-card .task-stats {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px 12px;
}
.task-stat { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.task-stat-label {
  display: flex; align-items: center; gap: 5px;
  font-size: 12px; color: var(--text-muted); white-space: nowrap;
}
.task-stat-value {
  font-size: 22px; font-weight: 700; color: var(--text-primary);
  font-variant-numeric: tabular-nums; padding-left: 13px;
}

@media (max-width: 1200px) {
  .resource-panel-body { grid-template-columns: 1fr; }
}

.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.mono-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text-secondary); white-space: nowrap; }
.pagination-bar { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
