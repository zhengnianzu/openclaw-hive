<template>
  <!-- 加入队列窗口：选择已有队列 / 新建队列（设计图 ② ③） -->
  <el-dialog v-model="visible" title="选择任务队列" width="760px" destroy-on-close @closed="onClosed">
    <el-tabs v-model="activeTab">
      <!-- ========== 选择已有队列 ========== -->
      <el-tab-pane label="选择已有队列" name="select">
        <div class="q-filter">
          <el-input v-model="search" placeholder="请输入队列名称" clearable size="default" style="width:200px" prefix-icon="Search" />
          <el-select v-model="statusF" placeholder="状态" clearable size="default" style="width:120px">
            <el-option label="全部" value="" />
            <el-option label="运行中" value="running" />
            <el-option label="空闲" value="idle" />
            <el-option label="已满" value="full" />
          </el-select>
          <el-select v-model="creatorF" placeholder="创建人" clearable size="default" style="width:140px">
            <el-option v-for="u in creatorOptions" :key="u" :label="u" :value="u" />
          </el-select>
        </div>

        <el-table :data="filteredQueues" v-loading="loading" height="240" highlight-current-row
          @current-change="onPick" style="width:100%">
          <el-table-column width="40">
            <template #default="{row}">
              <el-radio :model-value="pickedId" :value="row.id" @change="onPick(row)"><span></span></el-radio>
            </template>
          </el-table-column>
          <el-table-column prop="id" label="队列名称" min-width="120" />
          <el-table-column label="当前运行/并发量" width="130" align="center">
            <template #default="{row}">{{ row.active_tasks }} / {{ row.max_concurrent }}</template>
          </el-table-column>
          <el-table-column label="排队任务数" width="100" align="center">
            <template #default="{row}">{{ row.queued_tasks }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{row}">
              <el-tag :type="queueStatusType(row)" size="small">{{ queueStatusText(row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="created_by" label="创建人" width="100" show-overflow-tooltip />
          <el-table-column label="创建时间" width="150">
            <template #default="{row}"><span class="mono">{{ fmtTime(row.created_at) }}</span></template>
          </el-table-column>
        </el-table>

        <!-- 队列详情 -->
        <div v-if="picked" class="q-detail">
          <div class="q-detail-title">队列详情：{{ picked.id }}</div>
          <div class="q-detail-stats">
            <span>并发量：<b>{{ picked.max_concurrent }}</b></span>
            <span>当前运行任务：<b>{{ picked.active_tasks }}</b></span>
            <span>排队中任务：<b>{{ picked.queued_tasks }}</b></span>
          </div>
          <el-table :data="pickedTasks" v-loading="loadingTasks" height="180" size="small" style="width:100%">
            <el-table-column prop="queue_position" label="序号" width="60" align="center" />
            <el-table-column prop="name" label="任务名称" min-width="120" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{row}">
                <el-tag :type="taskStatusType(row.status)" size="small">{{ taskStatusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="150">
              <template #default="{row}"><span class="mono">{{ fmtTime(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="预计开始时间" width="150">
              <template #default="{row}">
                <span class="mono">{{ row.estimated_start_at ? fmtTime(row.estimated_start_at) : '-' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ========== 新建队列 ========== -->
      <el-tab-pane label="新建队列" name="create">
        <el-form :model="newQueue" label-width="90px" style="max-width:520px">
          <el-form-item label="队列名称" required>
            <el-input v-model="newQueue.id" maxlength="50" placeholder="例如 queue1" show-word-limit />
            <div v-if="dupMsg" class="q-dup-error">
              <el-icon><CircleCloseFilled /></el-icon> {{ dupMsg }}
            </div>
            <div v-else-if="newQueue.id" class="q-dup-ok">
              <el-icon><CircleCheckFilled /></el-icon> 队列名称可用
            </div>
          </el-form-item>
          <el-form-item label="并发量" required>
            <el-input-number v-model="newQueue.max_concurrent" :min="1" :max="99" />
            <div class="q-hint">同一时间允许运行的最大任务数</div>
          </el-form-item>
          <el-form-item label="描述">
            <el-input v-model="newQueue.description" type="textarea" :rows="3" maxlength="200"
              show-word-limit placeholder="请输入队列描述（可选）" />
          </el-form-item>
        </el-form>
        <el-alert type="info" :closable="false" show-icon
          title="队列名称需全局唯一，创建时进行去重校验。" />
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button v-if="activeTab === 'select'" type="primary" :disabled="!pickedId" @click="confirmPick">确定</el-button>
      <el-button v-else type="primary" :loading="creating" :disabled="!canCreate" @click="createQueue">确定</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCloseFilled, CircleCheckFilled } from '@element-plus/icons-vue'
import api from '../api'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  selectedId: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'picked'])

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const activeTab = ref('select')
const queues = ref([])
const loading = ref(false)
const search = ref('')
const statusF = ref('')
const creatorF = ref('')

const pickedId = ref('')
const picked = ref(null)
const pickedTasks = ref([])
const loadingTasks = ref(false)

const newQueue = ref({ id: '', max_concurrent: 1, description: '' })
const creating = ref(false)
const dupMsg = ref('')

watch(() => props.modelValue, (open) => {
  if (open) {
    activeTab.value = 'select'
    pickedId.value = props.selectedId || ''
    picked.value = null
    pickedTasks.value = []
    newQueue.value = { id: '', max_concurrent: 1, description: '' }
    dupMsg.value = ''
    loadQueues()
  }
})

// 队列名去重校验：本地先判一次（即时反馈），再向后端确认一次（权威）
watch(() => newQueue.value.id, async (id) => {
  if (!id) { dupMsg.value = ''; return }
  if (queues.value.some(q => q.id === id)) {
    dupMsg.value = '队列名称已存在，请更换'
    return
  }
  dupMsg.value = ''
  try {
    const r = await api.get(`/queues/check-id/${encodeURIComponent(id)}`)
    dupMsg.value = r.available ? '' : '队列名称已存在，请更换'
  } catch { /* 校验失败不阻塞，提交时后端还会再拦一次 */ }
})

const creatorOptions = computed(() => {
  const s = new Set()
  queues.value.forEach(q => { if (q.created_by) s.add(q.created_by) })
  return [...s].sort()
})

const filteredQueues = computed(() => queues.value.filter(q => {
  if (search.value && !q.id.toLowerCase().includes(search.value.toLowerCase())) return false
  if (creatorF.value && q.created_by !== creatorF.value) return false
  if (statusF.value) {
    const full = q.active_tasks >= q.max_concurrent
    if (statusF.value === 'full' && !full) return false
    if (statusF.value === 'running' && !(q.active_tasks > 0 && !full)) return false
    if (statusF.value === 'idle' && q.active_tasks !== 0) return false
  }
  return true
}))

const canCreate = computed(() => !!newQueue.value.id && !dupMsg.value)

function queueStatusType(q) {
  if (q.active_tasks >= q.max_concurrent) return 'danger'
  return q.active_tasks > 0 ? 'success' : 'info'
}
function queueStatusText(q) {
  if (q.active_tasks >= q.max_concurrent) return '已满'
  return q.active_tasks > 0 ? '运行中' : '空闲'
}
function taskStatusType(s) {
  return { running: 'success', preparing: 'warning', queued: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', failed: 'danger' }[s] || ''
}
function taskStatusText(s) {
  return { running: '运行中', preparing: '准备中', queued: '排队中', completed: '已完成', finished: '已结束', stopped: '已停止', failed: '失败', created: '待启动' }[s] || s
}
function fmtTime(t) {
  if (!t) return '-'
  const d = new Date(t)
  if (isNaN(d)) return String(t).slice(0, 16).replace('T', ' ')
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadQueues() {
  loading.value = true
  try {
    queues.value = await api.get('/queues')
    if (pickedId.value) {
      const hit = queues.value.find(q => q.id === pickedId.value)
      if (hit) loadTasks(hit)
    }
  } catch {
    queues.value = []
  } finally { loading.value = false }
}

async function loadTasks(q) {
  loadingTasks.value = true
  try {
    pickedTasks.value = await api.get(`/queues/${encodeURIComponent(q.id)}/tasks`)
  } catch {
    pickedTasks.value = []
  } finally { loadingTasks.value = false }
}

function onPick(row) {
  if (!row) return
  pickedId.value = row.id
  picked.value = row
  loadTasks(row)
}

function confirmPick() {
  emit('picked', pickedId.value)
  visible.value = false
}

async function createQueue() {
  if (!canCreate.value) return
  creating.value = true
  try {
    await api.post('/queues', {
      id: newQueue.value.id,
      name: newQueue.value.id,
      max_concurrent: newQueue.value.max_concurrent,
      description: newQueue.value.description,
    })
    ElMessage.success(`队列 ${newQueue.value.id} 创建成功`)
    emit('picked', newQueue.value.id)
    visible.value = false
  } catch (e) {
    dupMsg.value = e?.response?.data?.detail || '创建失败'
  } finally { creating.value = false }
}

function onClosed() {
  pickedTasks.value = []
}
</script>

<style scoped>
.q-filter { display: flex; gap: 10px; margin-bottom: 12px; }
.q-detail { margin-top: 18px; border-top: 1px solid var(--border-color); padding-top: 14px; }
.q-detail-title { font-weight: 600; margin-bottom: 10px; color: var(--text-primary); }
.q-detail-stats { display: flex; gap: 24px; margin-bottom: 12px; font-size: 13px; color: var(--text-secondary); }
.q-detail-stats b { color: var(--text-primary); }
.q-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.q-dup-error { font-size: 12px; color: #f56c6c; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.q-dup-ok { font-size: 12px; color: #67c23a; margin-top: 4px; display: flex; align-items: center; gap: 4px; }
.mono { font-variant-numeric: tabular-nums; font-size: 13px; color: var(--text-secondary); }
:deep(.el-radio__label) { display: none; }
</style>
