<template>
  <div class="queues-page">
    <div class="header">
      <h1>队列管理</h1>
      <el-button type="primary" @click="showCreateDialog = true">创建队列</el-button>
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
      <el-table-column prop="id" label="队列名称" min-width="150" show-overflow-tooltip />
      <el-table-column label="当前运行/并发量" min-width="140" align="center">
        <template #default="{ row }">
          <span class="mono">{{ row.active_tasks }} / {{ row.effective_concurrent }}</span>
          <el-tooltip v-if="row.effective_concurrent < row.max_concurrent"
            :content="`队列设置为 ${row.max_concurrent}，受全局上限限制为 ${row.effective_concurrent}`" placement="top">
            <el-tag type="warning" size="small" effect="plain" style="margin-left:6px">封顶</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="排队任务数" min-width="110" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.queued_tasks" type="warning" size="small">{{ row.queued_tasks }}</el-tag>
          <span v-else class="mono">0</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" min-width="100" align="center">
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
      <el-table-column label="长尾跳过" min-width="120" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.tail_skip_enabled" type="success" size="small" effect="plain">
            剩余 {{ row.tail_skip_percent }}%
          </el-tag>
          <span v-else class="mono" style="color:#999">关闭</span>
        </template>
      </el-table-column>
      <el-table-column prop="description" label="描述" min-width="160" show-overflow-tooltip />
      <el-table-column prop="created_by" label="创建人" min-width="110" show-overflow-tooltip />
      <el-table-column prop="created_at" label="创建时间" min-width="160">
        <template #default="{ row }">
          <span class="mono">{{ formatTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="200" fixed="right">
        <template #default="{ row }">
          <div class="op-cell">
            <el-button size="small" @click="viewQueueTasks(row)">查看任务</el-button>
            <el-button size="small" type="success" @click="startQueue(row)"
              :disabled="!canStartQueue(row)">启动队列</el-button>
            <el-dropdown trigger="click" @command="cmd => handleQueueCommand(cmd, row)">
              <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="edit">编辑设置</el-dropdown-item>
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
      <el-pagination v-model:current-page="currentPage" v-model:page-size="pageSize"
        :total="filteredQueues.length" :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next, jumper" background small />
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
            v-model="createForm.tail_skip_percent" :min="1" :max="50" :step="5"
            size="small" style="margin-left:10px" />
          <span v-if="createForm.tail_skip_enabled" class="tail-unit">%</span>
          <div class="hint">
            开启后，当前任务剩余量 ≤ 阈值时提前启动下一个任务（同队列最多 2 个并行）
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
            v-model="editForm.tail_skip_percent" :min="1" :max="50" :step="5"
            size="small" style="margin-left:10px" />
          <span v-if="editForm.tail_skip_enabled" class="tail-unit">%</span>
          <div class="hint">
            当前任务剩余量 ≤ 该百分比时提前启动下一个任务；同队列最多 2 个并行。
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
    <el-dialog v-model="showTasksDialog" :title="`队列详情：${currentQueue?.id}`" width="900px">
      <div class="detail-stats">
        <span>并发量：<b>{{ currentQueue?.effective_concurrent }}</b><template v-if="currentQueue?.effective_concurrent < currentQueue?.max_concurrent">（队列设置 {{ currentQueue.max_concurrent }}，受全局上限限制）</template></span>
        <span>当前运行任务：<b>{{ currentQueue?.active_tasks }}</b></span>
        <span>排队中任务：<b>{{ currentQueue?.queued_tasks }}</b></span>
      </div>
      <el-table :data="queueTasks" v-loading="loadingTasks" stripe max-height="500px">
        <el-table-column prop="queue_position" label="入队序号" width="90" align="center" />
        <el-table-column prop="name" label="任务名称" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="position_label" label="队列位置" width="110" align="center" />
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }"><span class="mono">{{ formatTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="预计开始时间" width="170">
          <template #default="{ row }">
            <span class="mono">{{ row.estimated_start_at ? formatTime(row.estimated_start_at) : '-' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCloseFilled, CircleCheckFilled, ArrowDown, Search } from '@element-plus/icons-vue'
import api from '../api'

const queues = ref([])
const loading = ref(false)
const showCreateDialog = ref(false)
const creating = ref(false)
const createForm = ref({
  id: '',
  max_concurrent: 1,
  description: '',
  tail_skip_enabled: false,
  tail_skip_percent: 10,
})

// 编辑队列设置
const showEditDialog = ref(false)
const saving = ref(false)
const editForm = ref({ id: '', max_concurrent: 1, description: '', tail_skip_enabled: false, tail_skip_percent: 10 })
const createDup = ref(false)

// 筛选 + 分页
const nameSearch = ref('')
const creatorFilter = ref('')
const statusFilter = ref('')
const currentPage = ref(1)
const pageSize = ref(10)

const creatorOptions = computed(() => {
  const s = new Set()
  queues.value.forEach(q => { if (q.created_by) s.add(q.created_by) })
  return [...s].sort()
})

const filteredQueues = computed(() => queues.value.filter(q => {
  if (nameSearch.value && !q.id.toLowerCase().includes(nameSearch.value.toLowerCase())) return false
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
  const start = (currentPage.value - 1) * pageSize.value
  return filteredQueues.value.slice(start, start + pageSize.value)
})

// 筛选/每页条数变化回到第一页
watch([nameSearch, creatorFilter, statusFilter, pageSize], () => { currentPage.value = 1 })
// 当前页因数据减少而越界时回退，避免停在空白页
watch(filteredQueues, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

function resetFilters() {
  nameSearch.value = ''
  creatorFilter.value = ''
  statusFilter.value = ''
}

function canStartQueue(q) {
  // 「启动队列」做的是 created/stopped → queued，与队列里是否已有任务在跑无关。
  // 只要有可启动的任务就允许点（往运行中的队列追加任务是常见用法）。
  return (q.startable_tasks || 0) > 0
}

function handleQueueCommand(cmd, row) {
  if (cmd === 'delete') deleteQueue(row)
  else if (cmd === 'stop') stopQueue(row)
  else if (cmd === 'edit') openEdit(row)
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

// 队列名去重：本地即时判定 + 后端权威校验
watch(() => createForm.value.id, async (id) => {
  if (!id) { createDup.value = false; return }
  if (queues.value.some(q => q.id === id)) { createDup.value = true; return }
  createDup.value = false
  try {
    const r = await api.get(`/queues/check-id/${encodeURIComponent(id)}`)
    createDup.value = !r.available
  } catch {}
})

const showTasksDialog = ref(false)
const loadingTasks = ref(false)
const currentQueue = ref(null)
const queueTasks = ref([])

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
    createForm.value = { id: '', max_concurrent: 1, description: '', tail_skip_enabled: false, tail_skip_percent: 10 }
    loadQueues()
  } catch (err) {
    // error shown by interceptor
  } finally {
    creating.value = false
  }
}

async function viewQueueTasks(queue) {
  currentQueue.value = queue
  showTasksDialog.value = true
  loadingTasks.value = true

  try {
    queueTasks.value = await api.get(`/queues/${queue.id}/tasks`)
  } catch (err) {
    ElMessage.error('加载队列任务失败')
  } finally {
    loadingTasks.value = false
  }
}

async function startQueue(queue) {
  try {
    await ElMessageBox.confirm(
      `确定启动队列 "${queue.id}" 吗？队列中「待启动」的任务将全部加入执行（第一个运行，其余排队）。`,
      '启动队列', { type: 'info' }
    )
    const r = await api.post(`/queues/${queue.id}/start-all`)
    if (!r.started_count) {
      ElMessage.warning(r.message || '队列中没有待启动的任务')
    } else {
      ElMessage.success(r.message || '已启动队列')
    }
    loadQueues()
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
    const r = await api.post(`/queues/${queue.id}/stop-all`)
    ElMessage.success(r.message || '已停止队列')
    loadQueues()
  } catch (err) {
    if (err !== 'cancel') { /* error shown by interceptor */ }
  }
}

async function deleteQueue(queue) {
  try {
    await ElMessageBox.confirm(
      `确定删除队列 "${queue.id}" 吗？队列中的任务不会被删除。`,
      '确认删除',
      { type: 'warning' }
    )

    await api.delete(`/queues/${queue.id}`)
    ElMessage.success('队列已删除')
    loadQueues()
  } catch (err) {
    if (err !== 'cancel') {
      // error shown by interceptor
    }
  }
}

function formatTime(time) {
  if (!time) return '-'
  const d = new Date(time)
  if (isNaN(d)) return String(time).slice(0, 16).replace('T', ' ')
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function getStatusType(status) {
  const map = {
    running: 'success',
    preparing: 'warning',
    queued: 'warning',
    created: 'info',
    stopped: 'danger',
    failed: 'danger',
    completed: 'info',
    finished: 'warning',
  }
  return map[status] || 'info'
}

function statusText(status) {
  return {
    running: '运行中', preparing: '准备中', queued: '排队中', completed: '已完成',
    finished: '已结束', stopped: '已停止', failed: '失败', created: '待启动'
  }[status] || status
}

// 队列状态：按「当前运行 / 并发量」派生，与设计图一致（已满 / 运行中 / 空闲）
function queueStatusType(q) {
  if (q.active_tasks >= q.effective_concurrent) return 'danger'
  return q.active_tasks > 0 ? 'success' : 'info'
}
function queueStatusText(q) {
  if (q.active_tasks >= q.effective_concurrent) return '已满'
  return q.active_tasks > 0 ? '运行中' : '空闲'
}

onMounted(() => {
  loadQueues()
})
</script>

<style scoped>
.queues-page {
  padding: 20px;
}

.mono { font-variant-numeric: tabular-nums; color: var(--text-secondary); font-size: 13px; }
/* 操作区：强制不换行，否则「更多」下拉会被挤到第二行、撑高整行 */
.op-cell { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }
.filter-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.pagination-bar { display: flex; justify-content: flex-end; margin-top: 16px; }
.hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.tail-unit { font-size: 13px; color: var(--text-secondary); margin-left: 4px; }
.dup-error { font-size: 12px; color: #f56c6c; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.dup-ok { font-size: 12px; color: #67c23a; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.detail-stats { display: flex; gap: 24px; margin-bottom: 14px; font-size: 13px; color: var(--text-secondary); }
.detail-stats b { color: var(--text-primary); }

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

h1 {
  font-size: 24px;
  font-weight: 600;
  margin: 0;
}
</style>
