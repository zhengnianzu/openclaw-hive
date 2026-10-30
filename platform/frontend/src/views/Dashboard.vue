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
        <span class="resource-panel-hint">按当前存活沙箱数 × 沙箱配置计算</span>
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

        <!-- 沙箱并发：池最多能同时跑多少个沙箱（按机器组派生），分母来自资源池配置 -->
        <div class="resource-card">
          <div class="resource-card-head">
            <el-icon :size="18" style="color:#0ea5e9"><Grid /></el-icon>
            <span>沙箱并发</span>
          </div>
          <div class="resource-main">
            <div class="resource-main-left">
              <div class="resource-value">
                <span class="resource-used">{{ fmtNum(totalRunningPods) }}</span>
                <span class="resource-sep">/</span>
                <span class="resource-total">{{ maxSandboxes ? fmtNum(maxSandboxes) : '—' }}</span>
                <span class="resource-unit">个</span>
              </div>
              <div class="resource-sub">
                可用 {{ maxSandboxes ? fmtNum(availSandboxes) + ' 个' : '—' }}
              </div>
              <el-progress
                :percentage="sandboxPercent" :show-text="false" :stroke-width="10"
                :color="percentColor(sandboxPercent)" />
              <div class="resource-percent">{{ maxSandboxes ? sandboxPercent.toFixed(0) + '%' : '未配置总量' }}</div>
            </div>
          </div>
        </div>

        <!-- 任务统计：与「沙箱并发」卡同构（左侧数值 + 进度条，右侧图例）。
             进度条口径是「最近 200 条任务里，运行 + 排队占多少」，
             全量会被历史终态任务稀释，看不出当下的负载。 -->
        <div class="resource-card task-card">
          <div class="resource-card-head">
            <el-icon :size="18" style="color:#64748b"><Histogram /></el-icon>
            <span>任务统计</span>
          </div>
          <div class="resource-main">
            <div class="resource-main-left">
              <div class="resource-value">
                <span class="resource-used">{{ fmtNum(recentRunning) }}</span>
                <span class="resource-sep">/</span>
                <span class="resource-total">{{ fmtNum(recentQueued) }}</span>
                <span class="resource-unit">运行 / 排队</span>
              </div>
              <div class="resource-sub">最近 {{ recentTasks.length }} 条任务</div>
              <el-progress
                :percentage="activePercent" :show-text="false" :stroke-width="10"
                :color="percentColor(activePercent)" />
              <div class="resource-percent">
                运行 {{ recentRunning }} / 排队 {{ recentQueued }} ·
                {{ recentActive ? activePercent.toFixed(0) + '%' : '无活跃任务' }}
              </div>
            </div>
            <div class="legend">
              <div class="legend-row"><i class="dot" style="background:#94a3b8"></i>全部<b>{{ fmtNum(instances.length) }}</b></div>
              <div class="legend-row"><i class="dot" style="background:#059669"></i>已完成<b>{{ fmtNum(doneCount) }}</b></div>
              <div class="legend-row"><i class="dot" style="background:#ef4444"></i>失败<b>{{ fmtNum(failedCount) }}</b></div>
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
      <!-- filterable：队列可能上百个，下拉里直接带输入框过滤，按 id 子串匹配。
           clearable 让「取消队列筛选」不用再滚到顶部点「全部」。 -->
      <el-select v-model="queueFilter" placeholder="任务队列" clearable filterable
        size="default" style="width:200px" :filter-method="onQueueFilterInput">
        <el-option label="全部" value="" />
        <el-option label="未入队" value="__none__" />
        <el-option v-for="q in matchedQueueOptions" :key="q.id" :label="q.id" :value="q.id" />
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
        <el-table-column prop="name" label="实例名称" min-width="160" show-overflow-tooltip resizable>
          <template #default="{row}">
            <span>{{ row.name }}</span>
            <el-tooltip v-if="row.suffix_api_keys" content="模型 Key 带沙箱后缀（自部署模型归因）" placement="top">
              <span style="color:#f56c6c;margin-left:4px;cursor:help">✱</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="harness_type" label="Harness" width="160" align="center" resizable>
          <template #default="{row}">
            <!-- 柔和标签：浅底 + 同色深字。原先 effect="dark" 是主色铺底 + 白字，
                 遇到 Grok 的 #00d084、通用的 #c0c4cc 这类高明度色时白字几乎看不清，
                 整列排开非常刺眼。样式统一由 store.tagStyle 派生。 -->
            <el-tag :style="{ ...harnessTagStyle(row.harness_type), maxWidth: '140px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }" size="default">
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
        <el-table-column label="队列位置" width="130" align="center" resizable>
          <template #default="{row}">
            <span v-if="row.status === 'queued'" class="mono-num" style="color:#e6a23c">
              {{ queuePositionLabel(row) }}
            </span>
            <span v-else class="mono-num">-</span>
            <el-tooltip v-if="row.status === 'queued' && isJumped(row)"
              content="已插队：下一个空出的槽位优先放行" placement="top">
              <el-tag type="danger" size="small" effect="plain" style="margin-left:4px">插队</el-tag>
            </el-tooltip>
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
              <!-- 「更多」对所有人可见：里面的「队列信息 / 沙箱交付物」是只读页面跳转，
                   不该被 operator 权限挡住。写操作（插队/启动/停止/删除）各自带
                   v-if="authStore.isOperator" 单独控制。 -->
              <el-dropdown trigger="click" @command="cmd => handleCommand(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="row.queue_id"
                      @click="router.push(`/instance/${row.id}?tab=queue`)">
                      队列信息
                    </el-dropdown-item>
                    <el-dropdown-item @click="router.push(`/instance/${row.id}?tab=artifacts`)">
                      沙箱交付物
                    </el-dropdown-item>
                    <!-- 插队：只对「排队中」的任务显示。已插队的换成「取消插队」，
                         否则给了入口也只会报错（后端只接受 queued 状态）。 -->
                    <el-dropdown-item v-if="authStore.isOperator && row.queue_id
                        && row.status === 'queued' && !row.is_jumped"
                      command="jump" divided>插队</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && row.queue_id
                        && row.status === 'queued' && row.is_jumped"
                      command="cancelJump" divided>取消插队</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator" command="copy" divided>复制</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && !['running','preparing'].includes(row.status)" command="start">
                      {{ row.status === 'queued' ? '启动（重新加入排队）' : '启动' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && ['running','queued'].includes(row.status)" command="stop">
                      {{ row.status === 'queued' ? '停止（退出队列）' : '停止' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && row.failed_tasks > 0 && !['running','preparing'].includes(row.status)" command="retry">重跑失败</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && !['running','preparing'].includes(row.status)" command="delete" divided>删除</el-dropdown-item>
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
import { ArrowDown, Search, Cpu, Coin, Histogram, Grid } from '@element-plus/icons-vue'
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
// 队列下拉的实时搜索词（el-select 的 filter-method 给的输入）。
// 只做「下拉里显示哪些选项」的过滤，不改 queueFilter 的选中值 ——
// 用内联 filter 时用户打完字一失焦，选项会缩成一条，看起来像筛选失效了。
const queueSearch = ref('')
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

// ===== 资源使用情况 =====
// 口径与后端 stats.effective_pods 一致：「已用」= running + preparing（preparing
// 期间已占住队列槽位，仍全额计入）；「排队中」单列，不计入已用。
// 每个实例的沙箱数不是配置并发，而是此刻实际活着的数量（见 effectivePods）。
const resourcePools = ref({ total_cpu: 0, total_memory_gb: 0, max_sandboxes: 0, pools: [] })
const activeInstances = computed(() => instances.value.filter(i => ['running', 'preparing'].includes(i.status)))
const queuedInstances = computed(() => instances.value.filter(i => i.status === 'queued'))
const doneCount = computed(() => instances.value.filter(i => ['completed', 'finished'].includes(i.status)).length)

// ===== 任务统计（进度条口径：最近 200 条）=====
// instances 由后端按 created_at DESC 返回，所以「最近」= 列表前 200 条。
// 用固定样本而不是全量：全量被大量历史终态任务稀释，进度条永远贴着 100%，
// 看不出当下的运行/排队状况。
const RECENT_TASK_WINDOW = 200
const recentTasks = computed(() => instances.value.slice(0, RECENT_TASK_WINDOW))
const recentRunning = computed(() => recentTasks.value.filter(i => i.status === 'running').length)
const recentQueued = computed(() => recentTasks.value.filter(i => i.status === 'queued').length)
const recentDone = computed(() =>
  recentTasks.value.filter(i => ['completed', 'finished'].includes(i.status)).length)

// 进度条 = 运行 + 排队 占样本的比例，和「运行 xx / 排队 xx」是同一个分母，
// 避免出现「写了 40% 但分子对不上」的困惑。
const recentActive = computed(() => recentRunning.value + recentQueued.value)
const activePercent = computed(() => {
  const n = recentTasks.value.length
  if (!n) return 0
  return Math.min(100, (recentActive.value / n) * 100)
})

// 实例此刻真实占用的沙箱数，与后端 stats.effective_pods 同口径。
// hive.py 是 concurrent_num 个 worker 各领一个任务、每个任务一个沙箱，所以尾部
// 只剩 total - finished 个沙箱活着；total 未知（0）时回退到配置并发。
function effectivePods(i) {
  const n = i.concurrent_num || 0
  if (i.status !== 'running') return n
  const total = i.total_tasks || 0
  if (total <= 0) return n
  const remaining = total - ((i.completed_tasks || 0) + (i.failed_tasks || 0))
  return Math.max(0, Math.min(n, remaining))
}

// 「运行中沙箱」= 活跃实例（运行中 + 准备中）此刻存活沙箱之和
const totalRunningPods = computed(() => activeInstances.value.reduce((sum, inst) => sum + effectivePods(inst), 0))
const failedCount = computed(() => instances.value.filter(i => i.status === 'failed').length)

// 每个实例占用 = 当前存活沙箱数 × 单沙箱资源
function sumResources(list, field) {
  return list.reduce((sum, i) => sum + effectivePods(i) * (i[field] || 0), 0)
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
// 沙箱并发上限：资源池按机器组派生（Σ 节点数 × 单机可用沙箱数）。老格式池为 0。
const maxSandboxes = computed(() => resourcePools.value.max_sandboxes || 0)

// 可用 = 总量 - 已用 - 排队；夹到 0，避免三段图例出现负数
const availCpu = computed(() => Math.max(0, totalCpu.value - runningCpu.value - queuedCpu.value))
const availMem = computed(() => Math.max(0, totalMem.value - runningMem.value - queuedMem.value))
const availSandboxes = computed(() =>
  Math.max(0, maxSandboxes.value - totalRunningPods.value))

// 进度条与主数值同口径（只算已用），排队中的量单独列在右侧图例里
const cpuPercent = computed(() => calcPercent(runningCpu.value, totalCpu.value))
const memPercent = computed(() => calcPercent(runningMem.value, totalMem.value))
const sandboxPercent = computed(() => calcPercent(totalRunningPods.value, maxSandboxes.value))
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

// 队列下拉的搜索：按 id 子串过滤候选项（队列常有上百个，纯下拉翻不动）。
// 「全部 / 未入队」两个固定项始终保留，否则搜到一半就没法清筛选了。
const matchedQueueOptions = computed(() => {
  const kw = queueSearch.value.trim().toLowerCase()
  if (!kw) return queueOptions.value
  return queueOptions.value.filter(q => (q.id || '').toLowerCase().includes(kw))
})
function onQueueFilterInput(val) {
  queueSearch.value = val || ''
}

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
    await loadOverviews()
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

// 运行中/准备中实例的概览（剩余时间、下载进度）走批量接口。
// 之前每个实例单独发一次 /overview，30+ 个实例 → 每轮 30+ 个并发请求；
// 现在一次请求拿全，后端也只做一遍扫描。
async function loadOverviews() {
  const ids = instances.value
    .filter(i => i.status === 'running' || i.status === 'preparing')
    .map(i => i.id)
  if (!ids.length) {
    timeEstimates.value = {}
    prepareProgress.value = {}
    return
  }
  let res
  try {
    res = await api.post('/instances/overview-batch', { ids }, { silent: true })
  } catch {
    return
  }
  const nextEstimates = {}
  Object.entries(res.overviews || {}).forEach(([id, ov]) => {
    if (ov.estimated_remaining_seconds != null) nextEstimates[id] = ov
  })
  timeEstimates.value = nextEstimates
  prepareProgress.value = res.prepare_progress || {}
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
// 直接显示会误导（例如队里第 4 个任务却显示 9）。这里算出名次，与设计图的
// 「1 (等待中)」一致。
//
// 排序口径必须与调度器（queue_scheduler._try_claim_next）完全一致：
//   ORDER BY queue_priority DESC, queue_position ASC
// 只按 position 排的话，插队任务会被算成名次靠后，与它「下一个就被放行」的实际
// 行为相反 —— 用户插完队看到名次反而变大，会以为没生效。
function queuePositionLabel(row) {
  const waiting = instances.value
    .filter(i => i.queue_id === row.queue_id && i.status === 'queued')
    .sort((a, b) =>
      ((b.queue_priority || 0) - (a.queue_priority || 0))
      || ((a.queue_position || 0) - (b.queue_position || 0)))
  const rank = waiting.findIndex(i => i.id === row.id) + 1
  return rank > 0 ? `${rank} (等待中)` : '-'
}
// 已插队标记：名次本身看不出「为什么排第一」，列表里补一个角标说明
function isJumped(row) {
  return (row.queue_priority || 0) > 0
}
function harnessColor(t) { return harnessStore.color(t) }
function harnessTagStyle(t) { return harnessStore.tagStyle(t) }
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
  const actions = {
    copy: () => router.push(`/create?copy_from=${row.id}`),
    start: () => startInstance(row.id),
    stop: () => stopInstance(row.id),
    retry: () => retryFailed(row.id),
    delete: () => deleteInstance(row.id),
    jump: () => jumpTask(row),
    cancelJump: () => cancelJumpTask(row),
  }
  actions[cmd]?.()
}

// 插队 / 取消插队：成功后立即重拉列表，否则首页要等下一轮 20s 轮询才更新名次，
// 用户点完看不到变化会以为没生效。
async function jumpTask(row) {
  try {
    await api.post(`/queues/${encodeURIComponent(row.queue_id)}/tasks/${encodeURIComponent(row.id)}/jump`)
    ElMessage.success('已插队，将优先放行')
    loadInstances()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '插队失败')
  }
}
async function cancelJumpTask(row) {
  try {
    await api.post(`/queues/${encodeURIComponent(row.queue_id)}/tasks/${encodeURIComponent(row.id)}/cancel-jump`)
    ElMessage.success('已取消插队')
    loadInstances()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '取消插队失败')
  }
}

// 轮询间隔从 10s 放宽到 20s：首页数据本身不是秒级敏感，而每轮都要拉
// 1000+ 条实例，间隔减半等于后端负载减半。标签页隐藏时暂停，切回时补拉一次。
const POLL_INTERVAL = 20000
function startPolling() {
  stopPolling()
  timer = setInterval(loadInstances, POLL_INTERVAL)
}
function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}
function handleVisibility() {
  if (document.hidden) {
    stopPolling()
  } else {
    loadInstances()
    startPolling()
  }
}

onMounted(() => {
  restoreFromQuery()
  document.addEventListener('visibilitychange', handleVisibility)
  harnessStore.load()
  loadInstances()
  loadQueueOptions()
  loadResourcePools()
  startPolling()
})
onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', handleVisibility)
})
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
  grid-template-columns: 1.45fr 1.45fr 1.15fr 1fr;
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

/* 任务卡已改成与「沙箱并发」同构（resource-main + legend），不再需要
   .task-stats / .task-stat* 这套独立网格样式。 */

@media (max-width: 1400px) {
  /* 四张卡挤一行会压扁数值，先退成两行两列 */
  .resource-panel-body { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 1200px) {
  .resource-panel-body { grid-template-columns: 1fr; }
}

.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.mono-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text-secondary); white-space: nowrap; }
.pagination-bar { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
