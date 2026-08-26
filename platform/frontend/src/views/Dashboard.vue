<template>
  <div>
    <div class="header-row">
      <h2>任务实例管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="router.push('/create')">
        <el-icon><Plus /></el-icon> 新建任务
      </el-button>
    </div>

    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background:#eef2ff"><el-icon :size="22" style="color:#6366f1"><Monitor /></el-icon></div>
        <div class="stat-info">
          <span class="stat-value">{{ instances.length }}</span>
          <span class="stat-label">总实例</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:#ecfdf5"><el-icon :size="22" style="color:#10b981"><VideoPlay /></el-icon></div>
        <div class="stat-info">
          <span class="stat-value" style="color:#10b981">{{ runningInstances.length }}</span>
          <span class="stat-label">运行中</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:#f0fdf4"><el-icon :size="22" style="color:#059669"><CircleCheck /></el-icon></div>
        <div class="stat-info">
          <span class="stat-value">{{ instances.filter(i => ['completed','finished'].includes(i.status)).length }}</span>
          <span class="stat-label">已完成</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:#fef2f2"><el-icon :size="22" style="color:#ef4444"><CircleClose /></el-icon></div>
        <div class="stat-info">
          <span class="stat-value">{{ instances.filter(i => i.status === 'stopped').length }}</span>
          <span class="stat-label">已停止</span>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background:#fffbeb"><el-icon :size="22" style="color:#f59e0b"><Box /></el-icon></div>
        <div class="stat-info">
          <span class="stat-value" style="color:#f59e0b">{{ totalRunningPods }}</span>
          <span class="stat-label">运行中沙箱</span>
        </div>
      </div>
    </div>

    <div class="filter-bar">
      <el-select v-model="statusFilter" placeholder="状态筛选" clearable size="default" style="width:140px">
        <el-option label="运行中" value="running" />
        <el-option label="准备中" value="preparing" />
        <el-option label="已完成" value="completed" />
        <el-option label="已结束" value="finished" />
        <el-option label="已停止" value="stopped" />
        <el-option label="待启动" value="created" />
      </el-select>
      <el-select v-model="harnessFilter" placeholder="Harness筛选" clearable size="default" style="width:140px">
        <el-option label="Openclaw" value="openclaw" />
        <el-option label="Hermes" value="hermes" />
        <el-option label="Claude Code" value="claude-code" />
        <el-option label="Jiuwen Claw" value="openjiuwen" />
        <el-option label="OpenCode" value="opencode" />
        <el-option label="Codex" value="codex" />
        <el-option label="Pi" value="pi" />
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
        <el-table-column prop="harness_type" label="Harness" width="120" align="center" resizable>
          <template #default="{row}">
            <el-tag :color="harnessColor(row.harness_type)" :style="{borderColor: harnessColor(row.harness_type)}" effect="dark" size="small">
              {{ harnessLabel(row.harness_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" resizable>
          <template #default="{row}">
            <el-tag :type="statusColor(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
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
                    <el-dropdown-item v-if="!['running','preparing'].includes(row.status)" command="start">启动</el-dropdown-item>
                    <el-dropdown-item v-if="row.status === 'running'" command="stop">停止</el-dropdown-item>
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
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Search, Monitor, VideoPlay, CircleCheck, CircleClose, Box } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const instances = ref([])
const loading = ref(false)
const timeEstimates = ref({})
const prepareProgress = ref({})
const statusFilter = ref('')
const harnessFilter = ref('')
const userFilter = ref('')
const dateRange = ref(null)
const nameSearch = ref('')
const currentPage = ref(1)
const pageSize = ref(20)
const router = useRouter()
let timer = null

const progressColor = [
  { color: '#6366f1', percentage: 50 },
  { color: '#8b5cf6', percentage: 80 },
  { color: '#10b981', percentage: 100 },
]

const runningInstances = computed(() => instances.value.filter(i => i.status === 'running'))

const userOptions = computed(() => {
  const set = new Set()
  instances.value.forEach(i => { if (i.created_by) set.add(i.created_by) })
  return Array.from(set).sort()
})

const filteredInstances = computed(() => {
  let list = instances.value
  if (statusFilter.value) list = list.filter(i => i.status === statusFilter.value)
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
  return list
})

const pagedInstances = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredInstances.value.slice(start, start + pageSize.value)
})

// 筛选/每页条数变化时回到第一页；当前页因数据减少而越界时也回退，避免停在空白页
watch([statusFilter, harnessFilter, userFilter, dateRange, nameSearch, pageSize], () => {
  currentPage.value = 1
})
watch(filteredInstances, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

const totalRunningPods = computed(() => runningInstances.value.reduce((sum, inst) => sum + inst.concurrent_num, 0))

async function loadInstances() {
  const isFirstLoad = instances.value.length === 0
  if (isFirstLoad) loading.value = true
  try {
    instances.value = await api.get('/instances')
    loadTimeEstimates()
    loadPrepareProgress()
  } finally { if (isFirstLoad) loading.value = false }
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
  return { running: 'success', preparing: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || ''
}
function statusText(s) {
  return { running: '运行中', preparing: '准备中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s
}
function harnessTagType(t) {
  return { openclaw: 'primary', hermes: 'warning', 'claude-code': 'success', openjiuwen: 'danger', opencode: 'info', codex: 'warning', pi: 'success', common: 'info' }[t] || ''
}
const HARNESS_COLORS = {
  openclaw: '#409eff', hermes: '#e6a23c', 'claude-code': '#67c23a',
  openjiuwen: '#f56c6c', opencode: '#909399',
  codex: '#8e44ad', pi: '#17a2b8',
  common: '#c0c4cc',
}
function harnessColor(t) { return HARNESS_COLORS[t] || '#909399' }
function harnessLabel(t) {
  return { openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', common: '通用' }[t] || t || 'openclaw'
}
function progress(row) {
  if (!row.total_tasks) return 0
  return Math.round(((row.completed_tasks + row.failed_tasks) / row.total_tasks) * 100)
}

async function startInstance(id) { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadInstances() }
async function stopInstance(id) { await ElMessageBox.confirm('确认停止该实例？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadInstances() }
async function retryFailed(id) { await api.post(`/instances/${id}/retry-failed`); ElMessage.success('重跑已启动'); loadInstances() }
async function deleteInstance(id) { await ElMessageBox.confirm('确认删除该实例？配置文件也会被删除。', '提示', { type: 'warning' }); await api.delete(`/instances/${id}`); ElMessage.success('已删除'); loadInstances() }

function handleCommand(cmd, row) {
  const actions = { copy: () => router.push(`/create?copy_from=${row.id}`), start: () => startInstance(row.id), stop: () => stopInstance(row.id), retry: () => retryFailed(row.id), delete: () => deleteInstance(row.id) }
  actions[cmd]?.()
}

onMounted(() => { loadInstances(); timer = setInterval(loadInstances, 10000) })
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }

.stat-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 16px;
  margin-bottom: 20px;
}
.stat-card {
  background: #fff;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: 20px;
  display: flex;
  align-items: center;
  gap: 16px;
  box-shadow: var(--shadow-sm);
  transition: box-shadow 0.2s ease;
}
.stat-card:hover { box-shadow: var(--shadow-md); }
.stat-icon {
  width: 48px; height: 48px;
  border-radius: var(--radius-sm);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.stat-info { display: flex; flex-direction: column; }
.stat-value {
  font-size: 28px; font-weight: 700; line-height: 1.2;
  color: var(--text-primary); font-variant-numeric: tabular-nums;
}
.stat-label { font-size: 13px; color: var(--text-muted); margin-top: 2px; }

.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.mono-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text-secondary); white-space: nowrap; }
.pagination-bar { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
