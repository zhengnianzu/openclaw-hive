<template>
  <div class="queues-page">
    <div class="header">
      <div>
        <h1>队列与任务管理</h1>
        <p class="subtitle">队列列表用于管理队列；任务列表用于筛选并导出任务详情</p>
      </div>
    </div>

    <!-- ===== 顶部：全局汇总指标 ===== -->
    <div class="panel overview-bar">
      <div class="ov-item">
        <span class="ov-label">队列数</span>
        <span class="ov-value stat">{{ queues.length }}</span>
      </div>
      <div class="ov-divider" />
      <div class="ov-item">
        <span class="ov-label">正在运行的任务数</span>
        <span class="ov-value stat hot">{{ totalRunningTasks }}</span>
      </div>
      <div class="ov-divider" />
      <div class="ov-item">
        <span class="ov-label">排队中的任务数</span>
        <span class="ov-value stat warn">{{ totalQueuedTasks }}</span>
      </div>
      <div class="ov-divider" />
      <div class="ov-item">
        <span class="ov-label">任务总数</span>
        <span class="ov-value stat">{{ instances.length }}</span>
      </div>
    </div>

    <!-- ===== 队列列表（独立面板，保留原有全部列与操作） ===== -->
    <div class="panel section">
      <div class="panel-head">
        <span class="panel-title">队列列表（{{ filteredQueues.length }}）</span>
        <el-button type="primary" size="default" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>&nbsp;新建队列
        </el-button>
      </div>

      <div class="filter-bar">
        <el-input v-model="nameSearch" placeholder="请输入队列名称" clearable size="default"
          style="width:200px" prefix-icon="Search" />
        <el-select v-model="creatorFilter" placeholder="创建人" clearable size="default" style="width:150px">
          <el-option v-for="u in creatorOptions" :key="u" :label="u" :value="u" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="状态" clearable size="default" style="width:130px">
          <el-option label="运行中" value="running" />
          <el-option label="空闲" value="idle" />
          <el-option label="已满" value="full" />
        </el-select>
        <el-button size="default" @click="resetFilters">重置</el-button>
      </div>

      <el-table :data="pagedQueues" v-loading="loading" stripe style="width:100%">
        <el-table-column prop="id" label="队列名称" min-width="120" show-overflow-tooltip />
        <el-table-column label="当前运行/并发量" min-width="118" align="center">
          <template #default="{ row }">
            <span class="mono">{{ row.active_tasks }} / {{ row.effective_concurrent }}</span>
            <el-tooltip v-if="row.effective_concurrent < row.max_concurrent"
              :content="`队列设置为 ${row.max_concurrent}，受全局上限限制为 ${row.effective_concurrent}`" placement="top">
              <el-tag type="warning" size="small" effect="plain" style="margin-left:6px">封顶</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="排队任务数" min-width="88" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.queued_tasks" type="warning" size="small">{{ row.queued_tasks }}</el-tag>
            <span v-else class="mono">0</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" min-width="94" align="center">
          <template #default="{ row }">
            <el-tag :type="queueStatusType(row)" size="small">{{ queueStatusText(row) }}</el-tag>
            <!-- 失败任务不计入 startable（避免「启动队列」反复重试同一个错误配置），
                 所以必须在这里单独提示，否则用户看不出队列里躺着失败任务。 -->
            <el-tooltip v-if="row.failed_tasks_in_queue"
              content="有任务准备/启动失败，需到任务列表里手动重试" placement="top">
              <el-tag type="danger" size="small" effect="plain" style="margin-left:6px">
                {{ row.failed_tasks_in_queue }} 失败
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="长尾跳过" min-width="100" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.tail_skip_enabled" type="success" size="small" effect="plain">
              并发 ×{{ row.tail_skip_percent }}%
            </el-tag>
            <span v-else class="mono" style="color:#999">关闭</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="110" show-overflow-tooltip />
        <el-table-column prop="created_by" label="创建人" min-width="88" show-overflow-tooltip />
        <el-table-column prop="created_at" label="创建时间" min-width="150">
          <template #default="{ row }"><span class="mono">{{ formatTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="248" fixed="right">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button size="small" @click="filterTasksByQueue(row)">查看任务</el-button>
              <el-button size="small" type="success" @click="startQueue(row)"
                :disabled="!canStartQueue(row)">启动队列</el-button>
              <el-dropdown trigger="click" @command="cmd => handleQueueCommand(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑设置</el-dropdown-item>
                    <el-dropdown-item command="export" :disabled="!row.task_count">
                      导出任务详情
                      <span v-if="!row.task_count" style="font-size:11px;color:#999">（队列为空）</span>
                    </el-dropdown-item>
                    <el-dropdown-item command="stop" :disabled="row.queued_tasks === 0">
                      停止队列
                      <span v-if="row.queued_tasks === 0" style="font-size:11px;color:#999">（无排队任务）</span>
                    </el-dropdown-item>
                    <el-dropdown-item command="delete" :disabled="row.task_count > 0" divided>
                      删除队列
                      <span v-if="row.task_count > 0" style="font-size:11px;color:#999">（队列非空）</span>
                    </el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-bar" v-if="filteredQueues.length > 0">
        <el-pagination v-model:current-page="queuePage" v-model:page-size="queuePageSize"
          :total="filteredQueues.length" :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper" background small />
      </div>
    </div>

    <!-- ===== 任务列表（独立面板：任务管理列表，用于筛选与导出） ===== -->
    <div class="panel section">
      <div class="panel-head">
        <div class="task-head-title">
          <span class="panel-title">任务列表（{{ filteredTasks.length }}）</span>
          <span class="task-head-hint">与「任务管理」同一数据源，此处用于批量筛选与导出</span>
        </div>
        <div class="bar-right">
          <span class="foot-hint">已选择 {{ selectedTasks.length }} 项</span>
          <el-button size="default" :disabled="!selectedTasks.length"
            :loading="exporting === 'tasks-selected'" @click="exportSelectedTasks">
            <el-icon><Download /></el-icon>&nbsp;导出选中任务
          </el-button>
        </div>
      </div>

      <div class="filter-bar">
        <el-input v-model="taskSearch" placeholder="请输入任务名称 / ID" clearable size="default"
          style="width:220px" prefix-icon="Search" />
        <el-select v-model="taskStatusFilter" placeholder="状态" clearable size="default" style="width:130px">
          <el-option label="全部状态" value="" />
          <el-option v-for="s in taskStatusOptions" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-select v-model="taskQueueFilter" placeholder="任务队列" clearable size="default" style="width:170px">
          <el-option label="全部队列" value="" />
          <el-option label="未入队" value="__none__" />
          <el-option v-for="q in queueOptions" :key="q" :label="q" :value="q" />
        </el-select>
        <el-select v-model="taskOwnerFilter" placeholder="创建者" clearable filterable
          size="default" style="width:150px">
          <el-option v-for="u in taskOwnerOptions" :key="u" :label="u" :value="u" />
        </el-select>
        <el-date-picker v-model="taskDateRange" type="daterange" range-separator="~"
          start-placeholder="开始" end-placeholder="结束" value-format="YYYY-MM-DD"
          size="default" style="width:220px" clearable />
        <el-button size="default" @click="resetTaskFilters">重置</el-button>
      </div>

      <el-table ref="taskTableRef" :data="pagedTasks" v-loading="loadingTasks" stripe
        style="width:100%" row-key="id" @selection-change="onTaskSelect">
        <el-table-column type="selection" width="44" :reserve-selection="true" />
        <el-table-column label="任务名称" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span>{{ row.name }}</span>
            <el-tooltip v-if="row.suffix_api_keys" content="模型 Key 带沙箱后缀（自部署模型归因）" placement="top">
              <span style="color:#f56c6c;margin-left:4px;cursor:help">✱</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column prop="id" label="任务ID" min-width="170" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column label="Harness" width="120" align="center">
          <template #default="{ row }">{{ harnessLabel(row.harness_type) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96" align="center">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="任务队列" width="120" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.queue_id" class="mono">{{ row.queue_id }}</span>
            <span v-else class="muted">未入队</span>
          </template>
        </el-table-column>
        <el-table-column label="成功" width="70" align="center">
          <template #default="{ row }"><span class="mono ok">{{ row.completed_tasks }}</span></template>
        </el-table-column>
        <el-table-column label="失败" width="70" align="center">
          <template #default="{ row }">
            <span class="mono" :class="{ bad: row.failed_tasks > 0 }">{{ row.failed_tasks }}</span>
          </template>
        </el-table-column>
        <el-table-column label="任务总数" width="84" align="center">
          <template #default="{ row }"><span class="mono">{{ row.total_tasks }}</span></template>
        </el-table-column>
        <el-table-column label="创建者" width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.created_by || '-' }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="150">
          <template #default="{ row }"><span class="mono">{{ formatTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="72" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small"
              @click="router.push(`/instance/${row.id}`)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-bar" v-if="filteredTasks.length > 0">
        <el-pagination v-model:current-page="taskPage" v-model:page-size="taskPageSize"
          :total="filteredTasks.length" :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper" background small />
      </div>
    </div>

    <!-- 创建队列对话框 -->
    <el-dialog v-model="showCreateDialog" title="新建队列" width="520px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="队列名称" required>
          <el-input v-model="createForm.id" maxlength="50" show-word-limit placeholder="英文字母/数字/下划线" />
          <div v-if="createDup" class="dup-error">
            <el-icon><CircleCloseFilled /></el-icon> 队列名称已存在，请更换
          </div>
          <div v-else-if="createForm.id" class="dup-ok">
            <el-icon><CircleCheckFilled /></el-icon> 队列名称可用
          </div>
        </el-form-item>
        <el-form-item label="并发量" required>
          <el-input-number v-model="createForm.max_concurrent" :min="1" :max="99" />
          <div class="hint">本队列同时运行的最大任务数；实际生效 = min(全局上限, 该值)</div>
        </el-form-item>
        <el-form-item label="长尾跳过">
          <el-switch v-model="createForm.tail_skip_enabled" />
          <el-input-number v-if="createForm.tail_skip_enabled"
            v-model="createForm.tail_skip_percent" :min="1" :max="20" :step="1"
            size="small" style="margin-left:10px" />
          <span v-if="createForm.tail_skip_enabled" class="tail-unit">%</span>
          <div class="hint">
            剩余任务数 &lt; 沙箱并发数 × 阈值% 时提前启动下一个任务。
            例：并发 100、阈值 5% → 剩余不足 5 个任务时触发
          </div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="3"
            maxlength="200" show-word-limit placeholder="请输入队列描述（可选）" />
        </el-form-item>
      </el-form>
      <el-alert type="info" :closable="false" show-icon
        title="队列名称需全局唯一，创建时进行去重校验。" />
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" @click="handleCreate" :loading="creating"
          :disabled="!createForm.id || createDup">确定</el-button>
      </template>
    </el-dialog>

    <!-- 编辑队列设置对话框 -->
    <el-dialog v-model="showEditDialog" :title="`编辑队列：${editForm.id}`" width="520px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="并发量">
          <el-input-number v-model="editForm.max_concurrent" :min="1" :max="99" />
          <div class="hint">本队列同时运行的最大任务数；实际生效 = min(全局上限, 该值)</div>
        </el-form-item>
        <el-form-item label="长尾跳过">
          <el-switch v-model="editForm.tail_skip_enabled" />
          <el-input-number v-if="editForm.tail_skip_enabled"
            v-model="editForm.tail_skip_percent" :min="1" :max="20" :step="1"
            size="small" style="margin-left:10px" />
          <span v-if="editForm.tail_skip_enabled" class="tail-unit">%</span>
          <div class="hint">
            剩余任务数 &lt; 沙箱并发数 × 该百分比时提前启动下一个任务。
            例：并发 100、阈值 5% → 剩余不足 5 个任务时触发。
            修改立即生效，正在排队的任务不需要重新入队。
          </div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="editForm.description" type="textarea" :rows="3"
            maxlength="200" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showEditDialog = false">取消</el-button>
        <el-button type="primary" @click="handleEditSave" :loading="saving">保存</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  CircleCloseFilled, CircleCheckFilled, ArrowDown, Search,
  Download, Plus, Refresh,
} from '@element-plus/icons-vue'
import api from '../api'
import { downloadBlob, exportStamp } from '../utils/download'
import { useHarnessStore } from '../stores/harness'

const router = useRouter()
const harnessStore = useHarnessStore()

// ==================== 队列列表 ====================
const queues = ref([])
const loading = ref(false)

const nameSearch = ref('')
const creatorFilter = ref('')
const statusFilter = ref('')
const queuePage = ref(1)
const queuePageSize = ref(10)

const creatorOptions = computed(() => {
  const s = new Set()
  queues.value.forEach(q => { if (q.created_by) s.add(q.created_by) })
  return [...s].sort()
})

// 顶部汇总：所有队列当前运行中的任务数、排队中的任务数
const totalRunningTasks = computed(() =>
  queues.value.reduce((s, q) => s + (q.active_tasks || 0), 0))
const totalQueuedTasks = computed(() =>
  queues.value.reduce((s, q) => s + (q.queued_tasks || 0), 0))

const filteredQueues = computed(() => queues.value.filter(q => {
  const kw = nameSearch.value.trim().toLowerCase()
  if (kw && !`${q.id} ${q.name || ''}`.toLowerCase().includes(kw)) return false
  if (creatorFilter.value && q.created_by !== creatorFilter.value) return false
  if (statusFilter.value) {
    const full = q.active_tasks >= q.effective_concurrent
    if (statusFilter.value === 'full' && !full) return false
    if (statusFilter.value === 'running' && !(q.active_tasks > 0 && !full)) return false
    if (statusFilter.value === 'idle' && q.active_tasks !== 0) return false
  }
  return true
}))

const pagedQueues = computed(() => {
  const start = (queuePage.value - 1) * queuePageSize.value
  return filteredQueues.value.slice(start, start + queuePageSize.value)
})

watch([nameSearch, creatorFilter, statusFilter, queuePageSize], () => { queuePage.value = 1 })
watch(filteredQueues, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / queuePageSize.value))
  if (queuePage.value > maxPage) queuePage.value = maxPage
})

function resetFilters() {
  nameSearch.value = ''
  creatorFilter.value = ''
  statusFilter.value = ''
}

// ==================== 任务列表（任务管理列表） ====================
const instances = ref([])
const loadingTasks = ref(false)
const taskSearch = ref('')
const taskStatusFilter = ref('')
const taskQueueFilter = ref('')
const taskOwnerFilter = ref('')
const taskDateRange = ref(null)
const taskPage = ref(1)
const taskPageSize = ref(20)
const taskTableRef = ref(null)
const selectedTasks = ref([])

const taskStatusOptions = [
  { label: '运行中', value: 'running' },
  { label: '准备中', value: 'preparing' },
  { label: '排队中', value: 'queued' },
  { label: '已完成', value: 'completed' },
  { label: '已结束', value: 'finished' },
  { label: '已停止', value: 'stopped' },
  { label: '失败', value: 'failed' },
  { label: '待启动', value: 'created' },
]

const queueOptions = computed(() => queues.value.map(q => q.id).sort())

const taskOwnerOptions = computed(() => {
  const s = new Set()
  instances.value.forEach(i => { if (i.created_by) s.add(i.created_by) })
  return [...s].sort()
})

const filteredTasks = computed(() => {
  let list = instances.value
  const kw = taskSearch.value.trim().toLowerCase()
  if (kw) list = list.filter(i => `${i.name || ''} ${i.id || ''}`.toLowerCase().includes(kw))
  if (taskStatusFilter.value) list = list.filter(i => i.status === taskStatusFilter.value)
  if (taskQueueFilter.value) {
    list = taskQueueFilter.value === '__none__'
      ? list.filter(i => !i.queue_id)
      : list.filter(i => i.queue_id === taskQueueFilter.value)
  }
  if (taskOwnerFilter.value) list = list.filter(i => i.created_by === taskOwnerFilter.value)
  if (taskDateRange.value && taskDateRange.value.length === 2) {
    const [start, end] = taskDateRange.value
    list = list.filter(i => {
      const d = String(i.created_at || '').slice(0, 10)
      return d && d >= start && d <= end
    })
  }
  // 与任务管理页一致：按创建时间倒序
  return [...list].sort((a, b) => {
    const ta = a.created_at || ''
    const tb = b.created_at || ''
    if (ta === tb) return 0
    return ta < tb ? 1 : -1
  })
})

const pagedTasks = computed(() => {
  const start = (taskPage.value - 1) * taskPageSize.value
  return filteredTasks.value.slice(start, start + taskPageSize.value)
})

watch([taskSearch, taskStatusFilter, taskQueueFilter, taskOwnerFilter, taskDateRange, taskPageSize], () => {
  taskPage.value = 1
  // 筛选变了，之前勾选的行可能已不在结果集里
  if (taskTableRef.value) taskTableRef.value.clearSelection()
})
watch(filteredTasks, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / taskPageSize.value))
  if (taskPage.value > maxPage) taskPage.value = maxPage
})

function resetTaskFilters() {
  taskSearch.value = ''
  taskStatusFilter.value = ''
  taskQueueFilter.value = ''
  taskOwnerFilter.value = ''
  taskDateRange.value = null
}

function onTaskSelect(rows) {
  selectedTasks.value = rows
}

// 队列行的「查看任务」：只是把任务列表的队列筛选器设成该队列，两个面板互不从属
function filterTasksByQueue(queue) {
  taskQueueFilter.value = queue.id
  taskPage.value = 1
  document.querySelector('.task-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// ==================== 队列操作 ====================
const showCreateDialog = ref(false)
const creating = ref(false)
const createDup = ref(false)
const createForm = ref({
  id: '', max_concurrent: 1, description: '',
  tail_skip_enabled: false, tail_skip_percent: 10,
})

const showEditDialog = ref(false)
const saving = ref(false)
const editForm = ref({
  id: '', max_concurrent: 1, description: '',
  tail_skip_enabled: false, tail_skip_percent: 10,
})

function canStartQueue(q) {
  // 「启动队列」做的是 created/stopped → queued，与队列里是否已有任务在跑无关。
  // 只要有可启动的任务就允许点（往运行中的队列追加任务是常见用法）。
  return (q.startable_tasks || 0) > 0
}

function handleQueueCommand(cmd, row) {
  if (cmd === 'delete') deleteQueue(row)
  else if (cmd === 'stop') stopQueue(row)
  else if (cmd === 'edit') openEdit(row)
  else if (cmd === 'export') exportQueue(row)
}

function openEdit(row) {
  editForm.value = {
    id: row.id,
    max_concurrent: row.max_concurrent ?? 1,
    description: row.description || '',
    tail_skip_enabled: !!row.tail_skip_enabled,
    tail_skip_percent: row.tail_skip_percent ?? 10,
  }
  showEditDialog.value = true
}

async function loadQueues() {
  loading.value = true
  try {
    queues.value = await api.get('/queues')
  } catch (err) {
    ElMessage.error('加载队列列表失败')
  } finally {
    loading.value = false
  }
}

async function loadInstances() {
  loadingTasks.value = true
  try {
    instances.value = await api.get('/instances')
  } catch (err) {
    ElMessage.error('加载任务列表失败')
  } finally {
    loadingTasks.value = false
  }
}

async function reloadAll() {
  await Promise.all([loadQueues(), loadInstances()])
}

watch(() => createForm.value.id, async (id) => {
  if (!id) { createDup.value = false; return }
  if (queues.value.some(q => q.id === id)) { createDup.value = true; return }
  createDup.value = false
  try {
    const r = await api.get(`/queues/check-id/${encodeURIComponent(id)}`)
    createDup.value = !r.available
  } catch {}
})

async function handleCreate() {
  if (!createForm.value.id || createDup.value) {
    ElMessage.warning('请填写有效的队列名称')
    return
  }
  creating.value = true
  try {
    await api.post('/queues', {
      id: createForm.value.id,
      name: createForm.value.id,
      max_concurrent: createForm.value.max_concurrent,
      description: createForm.value.description,
      tail_skip_enabled: createForm.value.tail_skip_enabled,
      tail_skip_percent: createForm.value.tail_skip_percent,
    })
    ElMessage.success('队列创建成功')
    showCreateDialog.value = false
    createForm.value = {
      id: '', max_concurrent: 1, description: '',
      tail_skip_enabled: false, tail_skip_percent: 10,
    }
    loadQueues()
  } catch (err) {
    // error shown by interceptor
  } finally {
    creating.value = false
  }
}

async function handleEditSave() {
  saving.value = true
  try {
    await api.patch(`/queues/${encodeURIComponent(editForm.value.id)}`, {
      max_concurrent: editForm.value.max_concurrent,
      description: editForm.value.description,
      tail_skip_enabled: editForm.value.tail_skip_enabled,
      tail_skip_percent: editForm.value.tail_skip_percent,
    })
    ElMessage.success('队列设置已保存')
    showEditDialog.value = false
    loadQueues()
  } catch (err) {
    // error shown by interceptor
  } finally {
    saving.value = false
  }
}

async function startQueue(queue) {
  try {
    await ElMessageBox.confirm(
      `确定启动队列 "${queue.id}" 吗？队列中「待启动」的任务将全部加入执行（第一个运行，其余排队）。`,
      '启动队列', { type: 'info' }
    )
    const r = await api.post(`/queues/${encodeURIComponent(queue.id)}/start-all`)
    if (!r.started_count) {
      ElMessage.warning(r.message || '队列中没有待启动的任务')
    } else {
      ElMessage.success(r.message || '已启动队列')
    }
    reloadAll()
  } catch (err) {
    if (err !== 'cancel') { /* error shown by interceptor */ }
  }
}

async function stopQueue(queue) {
  try {
    await ElMessageBox.confirm(
      `确定停止队列 "${queue.id}" 吗？队列中「排队中」的任务将重置为待启动。正在运行的任务不受影响。`,
      '停止队列', { type: 'warning' }
    )
    const r = await api.post(`/queues/${encodeURIComponent(queue.id)}/stop-all`)
    ElMessage.success(r.message || '已停止队列')
    reloadAll()
  } catch (err) {
    if (err !== 'cancel') { /* error shown by interceptor */ }
  }
}

async function deleteQueue(queue) {
  try {
    await ElMessageBox.confirm(
      `确定删除队列 "${queue.id}" 吗？队列中的任务不会被删除。`,
      '确认删除', { type: 'warning' }
    )
    await api.delete(`/queues/${encodeURIComponent(queue.id)}`)
    ElMessage.success('队列已删除')
    reloadAll()
  } catch (err) {
    if (err !== 'cancel') { /* error shown by interceptor */ }
  }
}

// ==================== 导出 ====================
const exporting = ref('')

async function exportSelectedTasks() {
  const ids = selectedTasks.value.map(t => t.id)
  if (!ids.length) return
  exporting.value = 'tasks-selected'
  try {
    await downloadBlob('/export/instances.xlsx', {
      method: 'post',
      data: { instance_ids: ids },
      filename: `任务详情_${ids.length}条_${exportStamp()}.xlsx`,
    })
  } catch { /* downloadBlob 已提示 */ }
  finally { exporting.value = '' }
}

// 单队列导出（队列行「更多 → 导出任务详情」）
async function exportQueue(queue) {
  if (!queue?.id) return
  exporting.value = 'queue-tasks'
  try {
    await downloadBlob(`/export/queues/${encodeURIComponent(queue.id)}.xlsx`, {
      filename: `队列_${queue.id}_任务详情_${exportStamp()}.xlsx`,
    })
  } catch { /* downloadBlob 已提示 */ }
  finally { exporting.value = '' }
}

// ==================== 格式化 ====================
function formatTime(time) {
  if (!time) return '-'
  const d = new Date(time)
  if (isNaN(d)) return String(time).slice(0, 16).replace('T', ' ')
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function harnessLabel(t) {
  return harnessStore.label ? harnessStore.label(t) : (t || 'openclaw')
}

function getStatusType(status) {
  return {
    running: 'success',
    preparing: 'warning',
    queued: 'warning',
    created: 'info',
    stopped: 'danger',
    failed: 'danger',
    completed: 'info',
    finished: 'warning',
  }[status] || 'info'
}

function statusText(status) {
  return {
    running: '运行中', preparing: '准备中', queued: '排队中', completed: '已完成',
    finished: '已结束', stopped: '已停止', failed: '失败', created: '待启动',
  }[status] || status
}

// 队列状态：按「当前运行 / 有效并发」派生（已满 / 运行中 / 空闲）
function queueStatusType(q) {
  if (q.active_tasks >= q.effective_concurrent) return 'danger'
  return q.active_tasks > 0 ? 'success' : 'info'
}
function queueStatusText(q) {
  if (q.active_tasks >= q.effective_concurrent) return '已满'
  return q.active_tasks > 0 ? '运行中' : '空闲'
}

onMounted(async () => {
  await reloadAll()
})
</script>

<style scoped>
.queues-page { padding: 20px; }

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 16px;
}
h1 { font-size: 24px; font-weight: 600; margin: 0; }
.subtitle { margin: 6px 0 0; font-size: 13px; color: var(--text-muted); }

/* ---- 面板 ---- */
.panel {
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border-color, #e5e7eb);
  border-radius: 10px;
  padding: 14px 16px;
}
.section { margin-bottom: 16px; }
.overview-bar { display: flex; align-items: center; gap: 18px; margin-bottom: 16px; }
.ov-value.stat.hot { color: var(--el-color-success); }
.ov-value.stat.warn { color: var(--el-color-warning); }

.ov-item { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.ov-label { font-size: 12px; color: var(--text-muted); }
.ov-value { font-size: 14px; color: var(--text-primary); display: flex; align-items: center; }
.ov-value.stat { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; }
.ov-divider { width: 1px; align-self: stretch; background: var(--border-color, #e5e7eb); }

.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.panel-title { font-size: 16px; font-weight: 600; }
.task-head-title { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.task-head-hint { font-size: 12px; color: var(--text-muted); }

.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.bar-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.pagination-bar { display: flex; justify-content: flex-end; margin-top: 16px; }

.mono { font-variant-numeric: tabular-nums; color: var(--text-secondary); font-size: 13px; }
.mono.ok { color: var(--el-color-success); }
.mono.bad { color: var(--el-color-danger); font-weight: 600; }
.muted { color: var(--text-muted); font-size: 13px; }
/* 操作区：强制不换行，否则「更多」下拉会被挤到第二行、撑高整行 */
.op-cell { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }
.foot-hint { font-size: 13px; color: var(--text-muted); }
.caret { margin-left: 2px; }

.hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.tail-unit { font-size: 13px; color: var(--text-secondary); margin-left: 4px; }
.dup-error { font-size: 12px; color: #f56c6c; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.dup-ok { font-size: 12px; color: #67c23a; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
</style>
