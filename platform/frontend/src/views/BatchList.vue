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
        <el-tabs v-model="statusTab" @tab-change="load">
          <el-tab-pane label="全部" name="" />
          <el-tab-pane label="运行中" name="running" />
          <el-tab-pane label="已完成" name="completed" />
          <el-tab-pane label="已暂停" name="paused" />
          <el-tab-pane label="已取消" name="cancelled" />
        </el-tabs>
        <el-input v-model="keyword" placeholder="搜索实验名称 / ID" clearable
          style="width:260px" @keyup.enter="load" @clear="load">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <el-table :data="rows" v-loading="loading" stripe style="width:100%" border>
        <el-table-column label="实验名称" min-width="220" resizable>
          <template #default="{row}">
            <div>
              <el-link type="primary" @click="router.push(`/batch/${row.id}`)">{{ row.name }}</el-link>
            </div>
            <div class="cell-sub">{{ row.id }}</div>
          </template>
        </el-table-column>

        <el-table-column label="测试集" min-width="200" resizable>
          <template #default="{row}">
            <div>{{ row.test_set_name || '—' }}</div>
            <div class="cell-sub">{{ row.sample_total }} 条样本</div>
          </template>
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

        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" @click="router.push(`/batch/${row.id}`)">查看</el-button>
              <el-dropdown v-if="authStore.isOperator" trigger="click"
                @command="cmd => handleCommand(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="pause" :disabled="row.status === 'paused'">暂停调度</el-dropdown-item>
                    <el-dropdown-item command="resume" :disabled="row.status !== 'paused'">恢复调度</el-dropdown-item>
                    <el-dropdown-item command="cancel" divided>取消实验</el-dropdown-item>
                    <el-dropdown-item command="delete">删除实验</el-dropdown-item>
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
}
const STATUS_TYPE = {
  created: 'info', running: 'success', paused: 'warning',
  completed: '', cancelled: 'danger',
}
const statusLabel = s => STATUS_LABEL[s] || s || '—'
const statusType = s => STATUS_TYPE[s] ?? 'info'

function rateColor(v) {
  if (v >= 80) return '#67c23a'
  if (v >= 50) return '#e6a23c'
  return '#f56c6c'
}

async function load() {
  loading.value = true
  try {
    const params = {}
    if (statusTab.value) params.status = statusTab.value
    if (keyword.value) params.keyword = keyword.value
    rows.value = await api.get('/batch-experiments', { params, silent: true })
  } catch { /* interceptor 已提示 */ } finally {
    loading.value = false
  }
}

async function handleCommand(cmd, row) {
  const actions = {
    pause: { url: `/batch-experiments/${row.id}/pause`, confirm: null },
    resume: { url: `/batch-experiments/${row.id}/resume`, confirm: null },
    cancel: {
      url: `/batch-experiments/${row.id}/cancel`,
      confirm: `取消实验「${row.name}」？将停止所有未完成的子任务。`,
    },
  }
  if (cmd === 'delete') {
    try {
      await ElMessageBox.confirm(
        `删除实验「${row.name}」？\n子任务（已跑过的任务、日志、轨迹）会保留，只删除实验编组。`,
        '删除确认', { type: 'warning' })
    } catch { return }
    try {
      const res = await api.delete(`/batch-experiments/${row.id}`)
      ElMessage.success(res.message || '已删除')
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
  padding: 0 16px;
  border-bottom: 1px solid var(--border-color);
}
.toolbar :deep(.el-tabs__header) { margin-bottom: 0; }
.toolbar :deep(.el-tabs__nav-wrap::after) { display: none; }
.cell-sub {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}
</style>
