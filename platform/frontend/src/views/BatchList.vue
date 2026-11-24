<template>
  <div>
    <div class="header-row">
      <div>
        <h2>批量实验</h2>
        <div class="subtitle">基于测试集的多 Harness 批量评测与对比</div>
      </div>
      <el-button v-if="authStore.isOperator" type="primary" @click="router.push('/batch/new')">
        <el-icon><Plus /></el-icon> 新建批量实验
      </el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <div class="toolbar">
        <div class="filters">
          <el-select v-model="statusTab" placeholder="全部状态" clearable
            style="width:150px" @change="load">
            <el-option label="全部状态" value="" />
            <el-option v-for="(v, k) in STATUS_LABEL" :key="k" :label="v" :value="k">
              <span>{{ v }}</span>
              <span class="opt-count" v-if="counts[k]">{{ counts[k] }}</span>
            </el-option>
          </el-select>
        </div>
        <el-input v-model="keyword" placeholder="搜索实验名称 / ID" clearable
          style="width:260px" @keyup.enter="load" @clear="load">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <el-table :data="rows" v-loading="loading" stripe style="width:100%" border>
        <el-table-column label="#" width="70" align="center" resizable>
          <template #default="{row}">
            <span class="seq">{{ row.seq || '—' }}</span>
          </template>
        </el-table-column>

        <el-table-column label="实验名称" min-width="220" resizable>
          <template #default="{row}">
            <el-link type="primary" @click="router.push(`/batch/${row.id}`)">{{ row.name }}</el-link>
          </template>
        </el-table-column>

        <el-table-column label="实验 ID" width="185" resizable show-overflow-tooltip>
          <template #default="{row}">
            <span class="mono">{{ row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column label="测试集" min-width="160" resizable>
          <template #default="{row}">{{ row.test_set_name || '—' }}</template>
        </el-table-column>

        <el-table-column label="Harness 数" width="100" align="center" resizable>
          <template #default="{row}">{{ row.arm_count }}</template>
        </el-table-column>

        <el-table-column label="任务数" width="100" align="center" resizable>
          <template #default="{row}">{{ row.task_total || '—' }}</template>
        </el-table-column>

        <el-table-column label="进度" width="180" resizable>
          <template #default="{row}">
            <el-progress :percentage="row.progress_percent" :stroke-width="10"
              :status="row.progress_percent >= 100 ? 'success' : undefined" />
          </template>
        </el-table-column>

        <el-table-column label="成功率" width="100" align="center" resizable>
          <template #default="{row}">
            <span :style="{color: rateColor(row.success_rate)}">
              {{ row.task_total ? row.success_rate + '%' : '—' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column prop="status" label="状态" width="100" align="center" resizable>
          <template #default="{row}">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="created_at" label="创建时间" width="160" resizable />

        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" @click="router.push(`/batch/${row.id}`)">查看</el-button>
              <el-dropdown trigger="click" @command="cmd => handleCommand(cmd, row)">
                <el-button size="small" :loading="startingId === row.id">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="start"
                      :disabled="!row.queue_id || (!row.startable_arms && row.status !== 'paused')">
                      {{ row.status === 'paused' ? '继续执行' : '启动队列' }}
                    </el-dropdown-item>
                    <el-dropdown-item command="rerun" :disabled="!row.failed_arms">
                      重跑失败<span v-if="row.failed_arms" style="color:#f56c6c">（{{ row.failed_arms }}）</span>
                    </el-dropdown-item>
                    <el-dropdown-item command="copy" divided>复制实验</el-dropdown-item>
                    <el-dropdown-item command="pause" :disabled="row.status === 'paused'" v-if="authStore.isOperator && (row.active_arms > 0 || row.status === 'paused')">
                      暂停调度
                    </el-dropdown-item>
                    <el-dropdown-item command="cancel" divided v-if="authStore.isOperator && (row.active_arms > 0 || row.status === 'paused')">
                      取消实验（停止全部任务）
                    </el-dropdown-item>
                    <el-dropdown-item command="delete" v-if="authStore.isOperator">删除实验</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>

        <template #empty>
          <el-empty description="暂无批量实验" />
        </template>
      </el-table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, ArrowDown } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()

const rows = ref([])
const loading = ref(false)
const statusTab = ref('')
const keyword = ref('')
let timer = null

const STATUS_LABEL = {
  created: '待开始', running: '运行中', paused: '已暂停',
  completed: '已完成', cancelled: '已取消',
  // failed 是由子任务状态推导出来的（见后端 _derive_status），
  // 库里原本没有这个取值，漏登记就会裸露成英文。
  failed: '有失败',
}
const STATUS_TYPE = {
  created: 'info', running: 'success', paused: 'warning',
  completed: '', cancelled: 'danger', failed: 'danger',
}
const statusLabel = s => STATUS_LABEL[s] || s || '—'
const statusType = s => STATUS_TYPE[s] ?? 'info'

function rateColor(v) {
  if (v >= 80) return '#67c23a'
  if (v >= 50) return '#e6a23c'
  return '#f56c6c'
}

// 各状态的数量：用一次「全部」查询算出来，避免为每个状态各打一次接口。
// 只算展示用，跟当前筛选无关 —— 所以筛选后计数仍是全局口径。
const counts = ref({})

function computeCounts(all) {
  const c = {}
  for (const r of all) c[r.status] = (c[r.status] || 0) + 1
  return c
}

async function load() {
  loading.value = true
  try {
    const params = {}
    if (statusTab.value) params.status = statusTab.value
    if (keyword.value) params.keyword = keyword.value
    rows.value = await api.get('/batch-experiments', { params, silent: true })

    // 没筛选时这份结果就是全量，直接算；有筛选时另拉一次全量（10s 一次，量级很小）
    if (!statusTab.value && !keyword.value) {
      counts.value = computeCounts(rows.value)
    } else {
      try {
        const all = await api.get('/batch-experiments', { silent: true })
        counts.value = computeCounts(all)
      } catch { /* 计数失败不影响主列表 */ }
    }
  } catch { /* interceptor 已提示 */ } finally {
    loading.value = false
  }
}

const startingId = ref('')

// 一键启动整批：把实验自带队列里「待启动/已停止」的子任务全部入队。
// 与单个任务的「启动」走同一条后端路径（/queues/{id}/start-all）。
async function rerunFailed(row) {
  if (!row.failed_arms) return
  try {
    await ElMessageBox.confirm(
      `重跑全部失败任务？\n只重新拉起已失败的子任务，成功跑完的不会被重跑。`,
      '重跑失败', { type: 'warning' })
  } catch { return }
  startingId.value = row.id
  try {
    const res = await api.post(`/batch-experiments/${row.id}/rerun-failed`)
    ElMessage.success(res.message || '已重跑')
    load()
  } catch { /* interceptor */ } finally {
    startingId.value = ''
  }
}

async function startQueue(row) {
  if (!row.queue_id) return
  const paused = row.status === 'paused'
  try {
    await ElMessageBox.confirm(
      paused
        ? `继续执行队列「${row.queue_name || row.queue_id}」？\n将解除暂停，并放行排队中的子任务。`
        : `启动队列「${row.queue_name || row.queue_id}」？\n该实验下待启动的子任务会按顺序入队执行。`,
      paused ? '继续执行' : '启动队列', { type: 'info' })
  } catch { return }
  startingId.value = row.id
  try {
    const res = await api.post(`/queues/${encodeURIComponent(row.queue_id)}/start-all`)
    ElMessage.success(res.message || '已启动')
    load()
  } catch { /* interceptor */ } finally {
    startingId.value = ''
  }
}

async function handleCommand(cmd, row) {
  if (cmd === 'start') {
    startQueue(row)
    return
  }
  if (cmd === 'rerun') {
    rerunFailed(row)
    return
  }
  if (cmd === 'copy') {
    // 跳到新建页并预填，而不是后台直接建 —— 复制出来的 key 必然是空的，
    // 得让用户看着表单补填，否则会建出一批没 key 的任务。
    router.push({ path: '/batch/new', query: { copy_from: row.id } })
    return
  }

  const actions = {
    pause: { url: `/batch-experiments/${row.id}/pause`, confirm: null },
    cancel: {
      url: `/batch-experiments/${row.id}/cancel`,
      confirm: `取消实验「${row.name}」？将停止所有未完成的子任务。`,
    },
  }
  if (cmd === 'delete') {
    // 默认连带删子任务（它们本来就是这个实验建出来的）。
    // 「保留子任务」是取消按钮之外的第三个选项，走 distinguishCancelAndClose。
    let keep = false
    try {
      await ElMessageBox.confirm(
        `删除实验「${row.name}」及其全部子任务？\n`
        + `子任务的日志、轨迹、产物会一并删除，不可恢复。\n`
        + `如果只想解散实验编组、保留这些任务，请点「保留子任务」。`,
        '删除确认',
        {
          type: 'warning',
          distinguishCancelAndClose: true,
          confirmButtonText: '删除实验和子任务',
          cancelButtonText: '保留子任务',
        })
    } catch (e) {
      if (e === 'cancel') keep = true   // 点了「保留子任务」
      else return                       // 关闭弹窗 / Esc → 什么都不做
    }
    try {
      const res = await api.delete(`/batch-experiments/${row.id}`,
        { params: keep ? { keep_instances: true } : {} })
      if (res.skipped && res.skipped.length) ElMessage.warning(res.message)
      else ElMessage.success(res.message || '已删除')
      load()
    } catch { /* interceptor */ }
    return
  }

  const act = actions[cmd]
  if (!act) return
  if (act.confirm) {
    try { await ElMessageBox.confirm(act.confirm, '确认', { type: 'warning' }) } catch { return }
  }
  try {
    const res = await api.post(act.url)
    ElMessage.success(res.message || '操作成功')
    load()
  } catch { /* interceptor */ }
}

onMounted(() => {
  load()
  // 与 Dashboard 一致的 10s 轮询，让运行中的实验进度自己往前走
  timer = setInterval(load, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.header-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.subtitle {
  font-size: 13px;
  color: var(--text-muted);
  margin-top: 4px;
}
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  /* 16px 左右内边距 + 14px 下内边距：筛选框与表格之间留出呼吸，
     原来 padding 为 0 时下拉框紧贴表头。 */
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-color);
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--text-secondary);
}
.toolbar :deep(.el-tabs__header) { margin-bottom: 0; }
.toolbar :deep(.el-tabs__nav-wrap::after) { display: none; }
.cell-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}
.seq {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}
.filters { display: flex; gap: 10px; align-items: center; }
.opt-count {
  float: right;
  color: var(--text-muted);
  font-size: 12px;
  margin-left: 12px;
}
</style>
