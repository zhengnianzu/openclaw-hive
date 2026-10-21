<template>
  <div>
    <div class="header-row">
      <div>
        <h2>Harness 配置管理</h2>
        <p class="subtitle">管理各 Harness 的配置文件与版本，创建任务时按版本引用</p>
      </div>
      <div class="header-actions">
        <el-button type="primary" v-if="authStore.isOperator" @click="openCreateDialog">
          <el-icon><Plus /></el-icon> 新建配置
        </el-button>
      </div>
    </div>

    <div class="overview-row">
      <div class="ov-card">
        <div class="ov-num">{{ overview.schemeCount }}</div>
        <div class="ov-label">配置方案</div>
      </div>
      <div class="ov-card">
        <div class="ov-num">{{ overview.versionCount }}</div>
        <div class="ov-label">版本总数</div>
      </div>
      <div class="ov-card">
        <div class="ov-num">{{ overview.publishedCount }}</div>
        <div class="ov-label">已发布</div>
      </div>
      <div class="ov-card ov-card-wide">
        <div class="ov-label" style="margin-bottom:6px">各 Harness 方案 / 版本</div>
        <div class="ov-types">
          <div v-for="t in overview.byType" :key="t.type" class="ov-type">
            <span class="ov-type-dot" :style="{ background: harnessColor(t.type) }"></span>
            <span class="ov-type-name">{{ typeLabel(t.type) }}</span>
            <span class="ov-type-num">{{ t.schemes }} / {{ t.versions }}</span>
          </div>
          <div v-if="!overview.byType.length" class="muted" style="font-size:12px">暂无数据</div>
        </div>
      </div>
    </div>

    <div class="filter-bar">
      <div class="filter-left">
        <el-input v-model="keyword" placeholder="搜索配置名称、版本、描述" clearable
          class="search-input">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="typeFilter" placeholder="Harness 类型" clearable style="width:160px">
          <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
          <el-option label="通用" value="common" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width:130px">
          <el-option label="默认配置" value="default" />
          <el-option label="普通配置" value="normal" />
        </el-select>
      </div>
      <el-button @click="resetFilters">重置</el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="pagedConfigs" v-loading="loading" stripe style="width:100%" border
        row-key="id" highlight-current-row class="config-table"
        @row-click="onRowClick">
        <el-table-column type="index" label="序号" width="70" align="center" />
        <el-table-column label="方案名称" min-width="200">
          <template #default="{row}">
            <div class="name-cell">
              <span class="name-main">{{ row.name }}</span>
              <span v-if="row.is_default" class="default-badge">
                <span class="status-dot on"></span>默认
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="harness_type" label="类型" width="140">
          <template #default="{row}">
            <el-tag :color="harnessColor(row.harness_type)" effect="dark" size="default" :style="{ border: 'none' }">{{ typeLabel(row.harness_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前版本" width="150">
          <template #default="{row}">
            <template v-if="row.latest_version">
              <span class="mono-num">{{ row.latest_version }}</span>
              <el-tag size="small" :type="statusType(row.latest_status)" effect="plain" style="margin-left:6px">
                {{ statusText(row.latest_status) }}
              </el-tag>
            </template>
            <span v-else class="muted">无版本</span>
          </template>
        </el-table-column>
        <el-table-column label="版本数" width="80" align="center">
          <template #default="{row}">
            <span class="file-count">{{ row.version_count }}</span>
            <span class="muted" style="font-size:11px"> ({{ row.published_count }} 发布)</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="180" show-overflow-tooltip>
          <template #default="{row}"><span class="muted">{{ row.description || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="文件" width="90" align="center">
          <template #default="{row}">
            <span :class="['file-count', { incomplete: !fileComplete(row) }]">{{ fileCount(row) }}/{{ expectedCount(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="updated_at" label="更新时间" width="170">
          <template #default="{row}"><span class="mono-num">{{ formatTime(row.updated_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{row}">
            <div class="op-cell" @click.stop>
              <el-dropdown v-if="authStore.isOperator" trigger="click" @command="cmd => handleMore(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item command="rename">重命名</el-dropdown-item>
                    <el-dropdown-item command="copy">复制</el-dropdown-item>
                    <el-dropdown-item v-if="!row.is_default" command="setDefault">设为默认</el-dropdown-item>
                    <el-dropdown-item v-if="!row.is_default" command="delete" divided style="color:#f56c6c">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pagination-bar">
      <span class="total-text">共 {{ filteredConfigs.length }} 条</span>
      <el-pagination v-model:current-page="currentPage" v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]" :total="filteredConfigs.length"
        layout="sizes, prev, pager, next" background />
    </div>

    <!-- 新建/编辑弹窗（方案层） -->
    <el-dialog v-model="editVisible" :title="isEditing ? '编辑配置方案' : '新建配置方案'" width="560px" destroy-on-close>
      <el-form :model="editForm" label-width="140px">
        <el-form-item label="Harness 类型" v-if="!isEditing">
          <el-select v-model="editForm.harness_type" style="width:100%">
            <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
            <el-option label="通用" value="common" />
          </el-select>
        </el-form-item>
        <el-form-item label="方案名称" required>
          <el-input v-model="editForm.name" placeholder="例如：研究版本、编码版本" maxlength="60" />
          <div v-if="isEditing" class="field-hint">改名只影响显示，磁盘目录与历史版本不受影响</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="editForm.description" type="textarea" :rows="2" />
        </el-form-item>

        <el-divider content-position="left">OBS 来源路径</el-divider>

        <el-form-item :label="harnessFileLabel(editForm.harness_type)">
          <el-input v-model="editForm.obs_harness_path" :placeholder="`${joinBucket('configs')}xxx/openclaw.json`" />
        </el-form-item>
        <el-form-item label="任务配置 (config.yaml)">
          <el-input v-model="editForm.obs_task_path" :placeholder="`${joinBucket('configs')}xxx/config.yaml`" />
        </el-form-item>
        <el-form-item label="模拟配置 (user_proxy)">
          <el-input v-model="editForm.obs_proxy_path" :placeholder="`${joinBucket('configs')}xxx/user_proxy_model.json`" />
        </el-form-item>
        <div class="obs-hint">填写后创建时自动从 OBS 拉取；也可以创建后在文件管理中手动拉取或从模板初始化</div>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, ArrowDown, Search } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import { joinBucket } from '../api/obsConfig'

const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const configs = ref([])
const loading = ref(false)

// ---- 筛选 / 分页 ----
const keyword = ref('')
const typeFilter = ref('')
const statusFilter = ref('')
const currentPage = ref(1)
const pageSize = ref(10)

const overview = computed(() => {
  const schemeCount = configs.value.length
  let versionCount = 0
  let publishedCount = 0
  const map = new Map()
  for (const c of configs.value) {
    const t = c.harness_type || 'unknown'
    if (!map.has(t)) map.set(t, { type: t, schemes: 0, versions: 0 })
    const e = map.get(t)
    e.schemes += 1
    e.versions += c.version_count || 0
    versionCount += c.version_count || 0
    publishedCount += c.published_count || 0
  }
  const byType = Array.from(map.values()).sort((a, b) => b.schemes - a.schemes)
  return { schemeCount, versionCount, publishedCount, byType }
})

const filteredConfigs = computed(() => {
  let list = configs.value
  if (typeFilter.value) list = list.filter(c => c.harness_type === typeFilter.value)
  if (statusFilter.value === 'default') list = list.filter(c => c.is_default)
  else if (statusFilter.value === 'normal') list = list.filter(c => !c.is_default)
  if (keyword.value) {
    const kw = keyword.value.trim().toLowerCase()
    list = list.filter(c =>
      (c.name || '').toLowerCase().includes(kw) ||
      (c.version || '').toLowerCase().includes(kw) ||
      (c.description || '').toLowerCase().includes(kw)
    )
  }
  // 默认配置置顶，其余按更新时间倒序
  return [...list].sort((a, b) => {
    if (a.is_default !== b.is_default) return b.is_default - a.is_default
    const ta = a.updated_at || '', tb = b.updated_at || ''
    if (ta === tb) return 0
    return ta < tb ? 1 : -1
  })
})

const pagedConfigs = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredConfigs.value.slice(start, start + pageSize.value)
})

watch([keyword, typeFilter, statusFilter, pageSize], () => { currentPage.value = 1 })
// 数据量减少导致当前页越界时回退
watch(() => filteredConfigs.value.length, (n) => {
  const maxPage = Math.max(1, Math.ceil(n / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

function resetFilters() {
  keyword.value = ''
  typeFilter.value = ''
  statusFilter.value = ''
}

// ---- 编辑弹窗 ----
const editVisible = ref(false)
const isEditing = ref(false)
const saving = ref(false)
let editingId = null
const emptyForm = () => ({
  harness_type: 'openclaw', name: '', slug: '', description: '',
  obs_harness_path: '', obs_task_path: '', obs_proxy_path: '',
})
const editForm = ref(emptyForm())

// ---- 状态与格式化辅助 ----
// ---- 详情抽屉 ----
function statusText(s) {
  return { draft: '草稿', testing: '测试中', published: '已发布' }[s] || s
}
function statusType(s) {
  return { draft: 'info', testing: 'warning', published: 'success' }[s] || 'info'
}

// ---- 格式化辅助 ----
function harnessFileLabel(t) { return harnessStore.fileLabel(t) }
function harnessColor(t) { return harnessStore.color(t) }
function typeLabel(t) { return harnessStore.label(t) }

function openWorkbench(row) {
  window.location.hash = `#/harness-workbench?type=${encodeURIComponent(row.harness_type)}&scheme=${row.id}`
}

function fileCount(row) {
  try { return JSON.parse(row.config_files_json || '[]').length } catch { return 0 }
}
// 期望文件数 = agent_config + config.yaml + user_proxy_model.json
// meta 尚未加载时不做判断，避免误报"文件缺失"
function expectedCount(row) {
  if (!harnessStore.loaded) return fileCount(row)
  if (row.harness_type === 'common') return 2
  return harnessStore.agentConfig[row.harness_type] ? 3 : 2
}
function fileComplete(row) {
  return fileCount(row) >= expectedCount(row)
}
function formatTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 19)
}
// ---- 列表加载 ----
async function loadList() {
  loading.value = true
  try {
    // 一次拉全量，类型筛选在前端做，避免切换筛选时重复请求
    configs.value = await api.get('/harness-configs')
  } finally { loading.value = false }
}

// ---- CRUD ----
function openCreateDialog() {
  isEditing.value = false
  editingId = null
  editForm.value = emptyForm()
  editVisible.value = true
}

function openEditDialog(row) {
  isEditing.value = true
  editingId = row.id
  editForm.value = {
    harness_type: row.harness_type,
    name: row.name || '',
    description: row.description || '',
    obs_harness_path: row.obs_harness_path || '',
    obs_task_path: row.obs_task_path || '',
    obs_proxy_path: row.obs_proxy_path || '',
  }
  editVisible.value = true
}

async function saveConfig() {
  const name = (editForm.value.name || '').trim()
  if (!name) { ElMessage.warning('请输入方案名称'); return }

  saving.value = true
  try {
    if (isEditing.value) {
      // 名称可改（目录是 harness-<id>，与名称无关）
      const { harness_type, ...payload } = editForm.value
      await api.put(`/harness-configs/${editingId}`, { ...payload, name })
      ElMessage.success('已更新')
    } else {
      const created = await api.post('/harness-configs', { ...editForm.value, name })
      ElMessage.success('已创建')
      // 创建时若配置了 OBS 路径，后端失败是静默的，这里补一次文件检查
      if (created?.id) await warnMissingFiles(created)
    }
    editVisible.value = false
    await loadList()
  } finally { saving.value = false }
}

async function warnMissingFiles(row) {
  try {
    const res = await api.get(`/harness-configs/${row.id}/files`)
    const missing = (res.files || []).filter(f => !f.exists).map(f => f.name)
    if (missing.length) {
      ElMessage.warning(`以下文件未生成：${missing.join('、')}，请进入文件管理手动拉取或从模板初始化`)
    }
  } catch { /* 忽略检查失败 */ }
}

async function setDefault(row) {
  try {
    await api.put(`/harness-configs/${row.id}/set-default`)
    ElMessage.success('已设为默认')
    await loadList()
  } catch { /* api interceptor shows error */ }
}

async function deleteConfig(row) {
  try {
    await ElMessageBox.confirm(`确认删除配置「${row.name}」？同时删除本地配置文件`, '提示', { type: 'warning' })
  } catch { return }
  try {
    await api.delete(`/harness-configs/${row.id}`)
    ElMessage.success('已删除')
    await loadList()
  } catch { /* api interceptor shows error */ }
}

function handleMore(cmd, row) {
  if (cmd === 'edit') openEditDialog(row)
  else if (cmd === 'rename') renameScheme(row)
  else if (cmd === 'copy') copyConfig(row)
  else if (cmd === 'setDefault') setDefault(row)
  else if (cmd === 'delete') deleteConfig(row)
}

// 改名只改显示名；磁盘目录是 harness-<id>，不受影响
async function renameScheme(row) {
  let name
  try {
    const { value } = await ElMessageBox.prompt('请输入新的方案名称', '重命名方案', {
      inputValue: row.name,
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '保存',
    })
    name = value.trim()
  } catch { return }
  if (name === row.name) return
  try {
    await api.put(`/harness-configs/${row.id}`, { name })
    ElMessage.success('已重命名')
    await loadList()
  } catch { /* api interceptor shows error */ }
}

// 点行直接进该方案的工作台（不再弹详情抽屉）
function onRowClick(row) {
  if (!row) return
  openWorkbench(row)
}

async function copyConfig(row) {
  // 复制的是「方案」：把草稿文件拷成一个新方案，不复制版本历史
  let name
  try {
    const { value } = await ElMessageBox.prompt('请输入新方案名称', '复制方案', {
      inputValue: `${row.name} 副本`.slice(0, 60),
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
      confirmButtonText: '复制',
    })
    name = value
  } catch { return }
  try {
    await api.post(`/harness-configs/${row.id}/copy`, { name })
    ElMessage.success('已复制为新方案')
    await loadList()
  } catch { /* api interceptor shows error */ }
}

onMounted(() => { harnessStore.load(); loadList() })
</script>

<style scoped>
.header-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 20px;
}
.header-row h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; margin: 0; }
.header-actions { display: flex; gap: 8px; flex-shrink: 0; }
.subtitle { color: var(--text-muted); font-size: 13px; margin: 4px 0 0; }

.overview-row {
  display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap;
}
.ov-card {
  background: var(--bg-card); border: 1px solid var(--border-color);
  border-radius: 10px; padding: 12px 18px; min-width: 110px;
}
.ov-card-wide { flex: 1; min-width: 320px; }
.ov-num { font-size: 22px; font-weight: 700; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.ov-label { font-size: 12px; color: var(--text-muted); }
.ov-types { display: flex; flex-wrap: wrap; gap: 4px 18px; }
.ov-type { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.ov-type-dot { width: 8px; height: 8px; border-radius: 2px; }
.ov-type-name { color: var(--text-secondary); }
.ov-type-num { color: var(--text-primary); font-weight: 600; font-variant-numeric: tabular-nums; }

.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
}
.filter-left { display: flex; align-items: center; gap: 12px; }
.search-input { width: 260px; }

.name-cell { display: flex; align-items: center; gap: 8px; }
.name-main { font-weight: 500; color: var(--text-primary); }
.default-badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; color: #16a34a;
  background: #f0fdf4; border: 1px solid #bbf7d0;
  border-radius: 10px; padding: 1px 8px;
}
.muted { color: var(--text-muted); }
.mono-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.danger-text { color: var(--danger, #f56c6c); margin-left: 6px; font-size: 12px; }

.file-count { font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.file-count.incomplete { color: #f59e0b; font-weight: 600; }

.op-cell { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }

.pagination-bar { display: flex; align-items: center; justify-content: space-between; margin-top: 16px; }
.total-text { color: var(--text-muted); font-size: 13px; }

.field-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.5; }
.field-error { font-size: 12px; color: var(--danger, #f56c6c); margin-top: 4px; }
.obs-hint { font-size: 12px; color: var(--text-muted); padding: 0 0 8px 140px; line-height: 1.5; }

</style>
