<template>
  <div>
    <div class="header-row">
      <h2>数据集管理</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <el-button type="primary" @click="router.push('/task-register')">
          <el-icon><Plus /></el-icon> 新建
        </el-button>
      </div>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
    <el-table :data="registrations" v-loading="loading" stripe style="width:100%" border>
      <el-table-column prop="seq" label="序号" width="70" resizable />
      <el-table-column prop="created_at" label="创建时间" width="160" resizable />
      <el-table-column prop="task_name" label="任务名称" min-width="160" show-overflow-tooltip resizable />
      <el-table-column prop="created_by" label="创建人" width="100" resizable />
      <el-table-column prop="task_path_obs" label="用户Config目录" min-width="220"
        show-overflow-tooltip resizable />
      <el-table-column label="样本数" width="110" align="center">
        <template #default="{row}">
          <span v-if="row.count_status === 'counting'" class="count-cell">
            <el-icon class="loading-icon"><Loading /></el-icon>统计中
          </span>
          <el-tooltip v-else-if="row.count_status === 'error'" :content="row.count_error || '统计失败'" placement="top">
            <span class="count-cell error" @click="recount(row)">失败，点击重试</span>
          </el-tooltip>
          <span v-else>{{ row.data_total || 0 }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="160" show-overflow-tooltip resizable />
      <el-table-column label="本地副本" width="130" align="center">
        <template #default="{row}">
          <span v-if="row.download_status === 'downloading'" class="count-cell">
            <el-icon class="loading-icon"><Loading /></el-icon>下载中
          </span>
          <el-tooltip v-else-if="row.download_status === 'error'"
            :content="row.download_error || '下载失败'" placement="top">
            <span class="count-cell error" @click="recount(row)">下载失败</span>
          </el-tooltip>
          <el-tooltip v-else-if="row.modified_count > 0"
            :content="`${row.modified_count} 个文件已本地修改，等待同步到云端`" placement="top">
            <el-tag size="small" type="warning" effect="plain">{{ row.modified_count }} 已改</el-tag>
          </el-tooltip>
          <span v-else-if="row.download_status === 'done'" class="muted">已就绪</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="90" align="center">
        <template #default="{row}">
          <span v-if="row.current_version" class="mono-num">v{{ row.current_version }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="最新变更" min-width="170" show-overflow-tooltip resizable>
        <template #default="{row}">
          <span v-if="row.latest_change" class="muted">{{ row.latest_change }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="170" resizable>
        <template #default="{row}">
          <span class="mono-num">{{ formatTime(row.updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{row}">
          <div style="display:flex;align-items:center;gap:4px">
            <el-button size="small" @click="showDetail(row)">详情</el-button>
            <el-button v-if="authStore.isAdmin" size="small" type="warning" @click="openEditDialog(row)">编辑</el-button>
            <el-dropdown v-if="authStore.isAdmin" trigger="click" @command="cmd => handleCommand(cmd, row)">
              <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="browse">查看目录内容</el-dropdown-item>
                  <el-dropdown-item command="versions">版本历史</el-dropdown-item>
                  <el-dropdown-item command="copy">复制</el-dropdown-item>
                  <el-dropdown-item command="recount">重新统计</el-dropdown-item>
                  <el-dropdown-item command="redownload">重新下载</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </template>
      </el-table-column>
    </el-table>
    </div>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="数据集详情" width="650px">
      <el-descriptions :column="1" border v-if="currentReg">
        <el-descriptions-item label="序号">{{ currentReg.seq || '-' }}</el-descriptions-item>
        <el-descriptions-item label="任务名称">{{ currentReg.task_name }}</el-descriptions-item>
        <el-descriptions-item label="创建人">{{ currentReg.created_by }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ currentReg.created_at }}</el-descriptions-item>
        <el-descriptions-item label="技能目录">{{ currentReg.skill_dir_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="默认技能">{{ currentReg.default_skills || '-' }}</el-descriptions-item>
        <el-descriptions-item label="Agent 目录">{{ currentReg.agent_dir_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="用户Config目录">{{ currentReg.task_path_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="用户Profile目录">{{ currentReg.user_folder_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="轨迹保存路径">{{ currentReg.traj_save_path || '-' }}</el-descriptions-item>
        <el-descriptions-item label="备注">{{ currentReg.remark || '-' }}</el-descriptions-item>
        <el-descriptions-item label="样本数">{{ currentReg.data_total || 0 }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑数据集" width="550px">
      <el-form :model="editForm" label-width="140px">
        <el-form-item label="任务名称">
          <el-input v-model="editForm.task_name" />
        </el-form-item>
        <el-form-item label="技能目录">
          <el-input v-model="editForm.skill_dir_obs" />
        </el-form-item>
        <el-form-item label="默认技能">
          <el-input v-model="editForm.default_skills" placeholder="逗号分隔" />
        </el-form-item>
        <el-form-item label="Agent 目录">
          <el-input v-model="editForm.agent_dir_obs" />
        </el-form-item>
        <el-form-item label="用户Config目录">
          <el-input v-model="editForm.task_path_obs" />
        </el-form-item>
        <el-form-item label="用户Profile目录">
          <el-input v-model="editForm.user_folder_obs" />
        </el-form-item>
        <el-form-item label="轨迹保存路径">
          <el-input v-model="editForm.traj_save_path" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.remark" type="textarea" :rows="2" />
        </el-form-item>
        <el-divider content-position="left">版本</el-divider>
        <el-form-item label="变更说明">
          <el-input v-model="editForm.change_summary" placeholder="可选，例如：换成 260520 版本的 configs"
            maxlength="120" />
          <div class="field-hint">每次保存只要字段有变化就会自动生成一个新版本；不填说明时按改动的字段自动生成</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 数据集目录浏览：看 OBS 上这个数据集里到底有哪些文件，并点开看内容 -->
    <el-dialog v-model="browseVisible" :title="`数据集目录 · ${browseReg?.task_name || ''}`"
      width="1000px" top="6vh" destroy-on-close @closed="resetBrowse">
      <template v-if="browseReg?.task_path_obs">
        <div class="browse-bar">
          <el-button size="small" :disabled="!browseRel" @click="browseUp">
            <el-icon><Top /></el-icon>&nbsp;上一级
          </el-button>
          <el-button size="small" :disabled="!browseRel" @click="loadBrowse('')">回到根目录</el-button>
          <el-button size="small" type="primary" plain @click="openFullPage">全屏查看</el-button>
          <span class="local-path" :title="browseLocalPath">
            <el-icon><FolderOpened /></el-icon>&nbsp;{{ browseLocalPath || '本地目录' }}
          </span>
        </div>

        <div class="browse-layout">
          <!-- 左：本地目录/文件列表 -->
          <div class="browse-list" v-loading="browseLoading">
            <div v-for="it in browseItems" :key="it.rel"
              :class="['browse-item', { active: openedFile?.rel === it.rel }]"
              @click="onBrowseClick(it)">
              <el-icon :class="it.is_dir ? 'dir-icon' : 'file-icon'">
                <Folder v-if="it.is_dir" /><Document v-else />
              </el-icon>
              <span class="browse-name">{{ it.name }}</span>
              <span v-if="it.size" class="browse-size">{{ formatBytes(it.size) }}</span>
            </div>
            <div v-if="!browseItems.length && !browseLoading" class="empty-hint">该目录为空</div>
          </div>

          <!-- 右：文件内容（可切换为编辑） -->
          <div class="browse-content" v-loading="fileLoading">
            <template v-if="openedFile">
              <div class="content-head">
                <span class="content-name">{{ openedFile.name }}</span>
                <el-tag v-if="fileStatus === 'modified'" size="small" type="warning" effect="plain">
                  已修改，待同步
                </el-tag>
                <span class="content-meta">{{ formatBytes(openedFile.size) }}</span>
                <div class="content-actions">
                  <template v-if="!editing">
                    <el-button size="small" @click="startEdit" :disabled="!!fileError">编辑</el-button>
                  </template>
                  <template v-else>
                    <el-button size="small" @click="cancelEdit">取消</el-button>
                    <el-button size="small" type="primary" :loading="saving"
                      :disabled="!dirty" @click="saveFile">保存</el-button>
                  </template>
                </div>
              </div>

              <el-alert v-if="fileError" :title="fileError" type="error" :closable="false" show-icon />
              <template v-else>
                <el-input v-if="editing" v-model="editContent" type="textarea" :rows="20"
                  class="content-editor" spellcheck="false" />
                <pre v-else class="content-body">{{ fileContent }}</pre>
                <div v-if="editing" class="edit-hint">
                  保存会写入 OBS（权威源）并同步本地缓存；正在运行的实例若已读到该文件不受影响，
                  后续新建的实例会用新内容。
                </div>
              </template>
            </template>
            <div v-else class="empty-hint">从左侧点一个文件查看内容</div>
          </div>
        </div>
      </template>
      <el-empty v-else description="该数据集未填写用户Config目录" />
    </el-dialog>

    <!-- 版本历史抽屉 -->
    <el-drawer v-model="versionDrawerVisible" size="720px" :destroy-on-close="true">
      <template #header>
        <div class="drawer-head">
          <span>版本历史</span>
          <span class="drawer-sub" v-if="versionReg">{{ versionReg.task_name }}</span>
        </div>
      </template>

      <div class="version-layout" v-loading="versionsLoading">
        <!-- 左：版本列表 -->
        <div class="version-list">
          <div v-for="v in versions" :key="v.id"
            :class="['version-item', { active: selectedVersion?.id === v.id }]"
            @click="selectVersion(v)">
            <div class="version-row">
              <span class="version-no">v{{ v.version }}</span>
              <span v-if="v.version === versionReg?.current_version" class="cur-badge">当前</span>
            </div>
            <div class="version-summary">{{ v.summary || '（无说明）' }}</div>
            <div class="version-meta">{{ formatTime(v.created_at) }} · {{ v.created_by || '—' }}</div>
          </div>
          <div v-if="!versions.length && !versionsLoading" class="empty-hint">暂无版本记录</div>
        </div>

        <!-- 右：版本详情 -->
        <div class="version-detail">
          <template v-if="selectedVersion">
            <div class="detail-title">
              v{{ selectedVersion.version }}
              <span class="detail-sub">{{ formatTime(selectedVersion.created_at) }} · {{ selectedVersion.created_by || '—' }}</span>
            </div>
            <div class="detail-summary">{{ selectedVersion.summary || '（无说明）' }}</div>

            <div class="section-label">本版相对上一版的改动</div>
            <div v-if="diffPrev.length" class="diff-list">
              <div v-for="d in diffPrev" :key="d.field" class="diff-row">
                <div class="diff-field">{{ fieldLabel(d.field) }}</div>
                <div class="diff-values">
                  <div class="diff-old">{{ d.old || '（空）' }}</div>
                  <div class="diff-new">{{ d.new || '（空）' }}</div>
                </div>
              </div>
            </div>
            <div v-else class="empty-hint">{{ selectedVersion.version === 1 ? '首个版本，无上一版可比' : '与上一版一致' }}</div>

            <div class="section-label">当前内容相对本版的改动</div>
            <div v-if="diffCur.length" class="diff-list">
              <div v-for="d in diffCur" :key="d.field" class="diff-row">
                <div class="diff-field">{{ fieldLabel(d.field) }}</div>
                <div class="diff-values">
                  <div class="diff-old">{{ d.old || '（空）' }}</div>
                  <div class="diff-new">{{ d.new || '（空）' }}</div>
                </div>
              </div>
            </div>
            <div v-else class="empty-hint">当前内容与本版一致</div>

            <div class="section-label">本版字段快照</div>
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item v-for="f in Object.keys(snapshotData)" :key="f" :label="fieldLabel(f)">
                <span class="mono-num">{{ snapshotData[f] || '—' }}</span>
              </el-descriptions-item>
            </el-descriptions>

            <div class="drawer-footer">
              <el-button v-if="authStore.isAdmin"
                :disabled="selectedVersion.version === versionReg?.current_version"
                :loading="restoring"
                @click="restoreSelected">恢复到这一版</el-button>
            </div>
          </template>
          <div v-else class="empty-hint">从左侧选择一个版本</div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, ArrowDown, Loading, Folder, Document, Top, FolderOpened } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const registrations = ref([])
const loading = ref(false)

const detailVisible = ref(false)
const currentReg = ref(null)

const editVisible = ref(false)
const editForm = ref({ task_name: '', skill_dir_obs: '', default_skills: '', agent_dir_obs: '', task_path_obs: '', user_folder_obs: '', traj_save_path: '', remark: '', change_summary: '' })
const editLoading = ref(false)
let editingId = null

// ---- 版本历史 ----
const versionDrawerVisible = ref(false)
const versionReg = ref(null)
const versions = ref([])
const versionsLoading = ref(false)
const selectedVersion = ref(null)
const snapshotData = ref({})
const diffPrev = ref([])
const diffCur = ref([])
const restoring = ref(false)
// 字段中文名由后端 /diff 一并返回，这里留一份兜底，避免接口异常时显示英文键名
const FALLBACK_LABELS = {
  task_name: '任务名称', requester: '申请者', task_path_obs: '用户Config目录',
  skill_dir_obs: '技能目录', agent_dir_obs: 'Agent 目录', user_folder_obs: '用户Profile目录',
  model_name: '模型', eval_model_name: '评估模型', user_proxy_model_name: '用户模拟模型',
  harness_type: 'Harness 类型', base_url: 'Base URL', eval_config_model: '评估配置模型',
  eval_config_base_url: '评估配置 Base URL', eval_config_api: '评估配置 API',
  default_skills: '默认技能', traj_save_path: '轨迹保存路径', remark: '备注',
}
let fieldLabels = { ...FALLBACK_LABELS }

function fieldLabel(f) { return fieldLabels[f] || FALLBACK_LABELS[f] || f }

// ---- 数据集目录浏览 ----
const browseVisible = ref(false)
const browseReg = ref(null)
// 当前浏览位置：相对数据集根目录的路径（'' = 根）。
// 浏览的是**本地副本**，不是 OBS —— 界面看到的和能编辑的是同一份内容。
const browseRel = ref('')
const browseLocalPath = ref('')
const browseItems = ref([])
const browseLoading = ref(false)
const openedFile = ref(null)
const fileContent = ref('')
const fileLoading = ref(false)
const fileError = ref('')
// 文件相对云端是否已改：clean | modified。
// 编辑只写本地，云端由 sync_worker 按哈希异步回传，所以这里能明确告诉用户
// 「改动还在本地、已排队等同步」。
const fileStatus = ref('clean')
// 编辑态
const editing = ref(false)
const editContent = ref('')
const saving = ref(false)
const dirty = computed(() => editing.value && editContent.value !== fileContent.value)

function formatBytes(size) {
  if (!size) return ''
  if (typeof size === 'string') return size   // /obs/list 直接给 "1.2KB"
  if (size < 1024) return `${size}B`
  if (size < 1048576) return `${(size / 1024).toFixed(1)}KB`
  return `${(size / 1048576).toFixed(1)}MB`
}

function resetBrowse() {
  browseReg.value = null
  browseRel.value = ''
  browseLocalPath.value = ''
  browseItems.value = []
  openedFile.value = null
  fileContent.value = ''
  fileError.value = ''
  fileStatus.value = 'clean'
  editing.value = false
  editContent.value = ''
}

async function openBrowse(row) {
  resetBrowse()
  browseReg.value = row
  browseVisible.value = true
  await loadBrowse('')
}

// 列本地副本。返回的 local_path 是真实磁盘路径，直接显示给用户 ——
// 想用命令行看/改的人照着这个路径就能找到。
async function loadBrowse(rel) {
  return withLoading(browseLoading, async () => {
    openedFile.value = null
    fileContent.value = ''
    fileError.value = ''
    try {
      const res = await api.get(`/registrations/${browseReg.value.id}/tree`, {
        params: { rel: rel || '' }, silent: true,
      })
      browseRel.value = res.rel || ''
      browseLocalPath.value = res.local_path || ''
      browseItems.value = res.items || []
    } catch (e) {
      browseItems.value = []
      browseLocalPath.value = ''
      ElMessage.error(e.response?.data?.detail || '列目录失败')
    }
  })
}

function browseUp() {
  if (!browseRel.value) return
  const parts = browseRel.value.split('/')
  parts.pop()
  loadBrowse(parts.join('/'))
}

function onBrowseClick(it) {
  if (it.is_dir) return loadBrowse(it.rel)
  openFile(it)
}

// 跳到独立的全屏页面：整页高度、JSON 格式化、行号，适合认真看/改配置。
// 带上 rel 直接定位到当前正在看的文件。
function openFullPage() {
  const id = browseReg.value?.id
  if (!id) return
  const q = openedFile.value ? { rel: openedFile.value.rel } : {}
  router.push({ name: 'DatasetFiles', params: { id }, query: q })
}

// 只读本地副本：数据集注册后已整份下载，平台上的浏览/编辑全部走本地。
// 后端在本地找不到文件时会明确报「请先重新下载」，不会悄悄回落到 OBS ——
// 那样会把「本地已改」的哈希基线搞乱。
async function openFile(it) {
  openedFile.value = it
  fileContent.value = ''
  fileError.value = ''
  fileStatus.value = 'clean'
  editing.value = false
  editContent.value = ''
  return withLoading(fileLoading, async () => {
    try {
      const res = await api.get(`/registrations/${browseReg.value.id}/file`, {
        params: { rel: it.rel }, silent: true,
      })
      fileContent.value = res.content || ''
      fileStatus.value = res.status || 'clean'
    } catch (e) {
      fileError.value = e.response?.data?.detail || '读取文件失败'
    }
  })
}

function startEdit() {
  editContent.value = fileContent.value
  editing.value = true
}

function cancelEdit() {
  if (dirty.value) {
    // 取消会丢改动，问一句
    ElMessageBox.confirm('放弃未保存的修改？', '提示', { type: 'warning' })
      .then(() => { editing.value = false; editContent.value = '' })
      .catch(() => {})
    return
  }
  editing.value = false
  editContent.value = ''
}

async function saveFile() {
  if (!dirty.value) return
  saving.value = true
  try {
    const res = await api.put(`/registrations/${browseReg.value.id}/file`, {
      rel: openedFile.value.rel,
      content: editContent.value,
      validate_json: true,
    })
    fileContent.value = editContent.value
    fileStatus.value = res.status || 'clean'
    editing.value = false
    editContent.value = ''
    ElMessage.success(res.detail || '已保存')
    // 大小可能变了；列表的「已改」计数也随之变
    openedFile.value = { ...openedFile.value, size: res.size }
    loadRegistrations()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

async function withLoading(flag, fn) {
  flag.value = true
  try { return await fn() } finally { flag.value = false }
}

function formatTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 19)
}

// 统计中的行需要轮询。离开页面时清掉。
let pollTimer = null

async function loadRegistrations() {
  loading.value = true
  try {
    registrations.value = await api.get('/registrations')
  } finally {
    loading.value = false
  }
  schedulePoll()
}

// 下载/统计都是后台异步跑的，任一在进行中就轮询；
// 另外本地改了文件后 sync_worker 会异步回传，modified_count 会自己归零，
// 所以有已修改的文件时也保持轮询，让「已改」标记及时消失。
function schedulePoll() {
  if (pollTimer) clearInterval(pollTimer)
  const busy = r =>
    r.count_status === 'counting' ||
    r.download_status === 'downloading' ||
    (r.modified_count || 0) > 0
  if (!registrations.value.some(busy)) return
  pollTimer = setInterval(async () => {
    try {
      const fresh = await api.get('/registrations')
      registrations.value = fresh
      if (!fresh.some(busy)) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    } catch { /* ignore */ }
  }, 5000)
}

function showDetail(row) {
  currentReg.value = row
  detailVisible.value = true
}

function openEditDialog(row) {
  editingId = row.id
  editForm.value = {
    task_name: row.task_name || '',
    skill_dir_obs: row.skill_dir_obs || '',
    default_skills: row.default_skills || '',
    agent_dir_obs: row.agent_dir_obs || '',
    task_path_obs: row.task_path_obs || '',
    user_folder_obs: row.user_folder_obs || '',
    traj_save_path: row.traj_save_path || '',
    remark: row.remark || '',
    change_summary: '',
  }
  editVisible.value = true
}

async function saveEdit() {
  editLoading.value = true
  try {
    await api.put(`/registrations/${editingId}`, { ...editForm.value })
    ElMessage.success('保存成功')
    editVisible.value = false
    loadRegistrations()
  } finally {
    editLoading.value = false
  }
}

// ---- 版本历史 ----
async function openVersions(row) {
  versionReg.value = row
  versionDrawerVisible.value = true
  versions.value = []
  selectedVersion.value = null
  snapshotData.value = {}
  diffPrev.value = []
  diffCur.value = []
  versionsLoading.value = true
  try {
    versions.value = await api.get(`/registrations/${row.id}/versions`)
    if (versions.value.length) await selectVersion(versions.value[0])
  } finally {
    versionsLoading.value = false
  }
}

async function selectVersion(v) {
  selectedVersion.value = v
  snapshotData.value = {}
  diffPrev.value = []
  diffCur.value = []
  try {
    const res = await api.get(`/registrations/${versionReg.value.id}/versions/${v.id}/diff`)
    diffPrev.value = res.previous || []
    diffCur.value = res.current || []
    if (res.labels) fieldLabels = res.labels
  } catch { /* 差异取不到时仍展示快照 */ }
  try {
    snapshotData.value = JSON.parse(v.snapshot_json || '{}')
  } catch { snapshotData.value = {} }
}

async function restoreSelected() {
  const v = selectedVersion.value
  if (!v) return
  try {
    await ElMessageBox.confirm(
      `把数据集恢复到 v${v.version}？当前内容会被覆盖，并生成一个新版本（历史不会丢失）。`,
      '恢复版本', { type: 'warning' })
  } catch { return }
  restoring.value = true
  try {
    await api.post(`/registrations/${versionReg.value.id}/versions/${v.id}/restore`)
    ElMessage.success(`已恢复到 v${v.version}`)
    const rowId = versionReg.value.id
    await loadRegistrations()
    versionReg.value = registrations.value.find(r => r.id === rowId) || versionReg.value
    versions.value = await api.get(`/registrations/${rowId}/versions`)
    if (versions.value.length) await selectVersion(versions.value[0])
  } finally {
    restoring.value = false
  }
}

async function deleteReg(row) {
  await ElMessageBox.confirm('确认删除该数据集？', '提示', { type: 'warning' })
  try {
    await api.delete(`/registrations/${row.id}`)
    ElMessage.success('已删除')
    loadRegistrations()
  } catch (e) {
    const detail = e.response?.data?.detail || e.message || '删除失败'
    ElMessage.error(detail)
  }
}

async function recount(row) {
  try {
    await api.post(`/registrations/${row.id}/count-sample`)
    ElMessage.success('已触发重新统计')
    loadRegistrations()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '触发失败')
  }
}

async function redownload(row) {
  try {
    await ElMessageBox.confirm(
      '从 OBS 重新整份下载，覆盖本地副本并重建哈希基线？本地未同步的改动会丢失。',
      '重新下载', { type: 'warning' })
  } catch { return }
  try {
    await api.post(`/registrations/${row.id}/redownload`)
    ElMessage.success('已开始下载')
    loadRegistrations()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '触发失败')
  }
}

function handleCommand(cmd, row) {
  const actions = {
    browse: () => openBrowse(row),
    versions: () => openVersions(row),
    copy: () => router.push(`/task-register?copy_from=${row.id}`),
    recount: () => recount(row),
    redownload: () => redownload(row),
    delete: () => deleteReg(row),
  }
  actions[cmd]?.()
}

onMounted(() => { loadRegistrations() })
onBeforeUnmount(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
.count-cell { display: inline-flex; align-items: center; gap: 4px; color: var(--text-secondary, #909399); }
.count-cell.error { color: var(--el-color-danger, #f56c6c); cursor: pointer; }
.loading-icon { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

.mono-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.muted { color: var(--text-muted); }
.field-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.5; }

/* ---- 版本历史抽屉 ---- */
.drawer-head { display: flex; align-items: baseline; gap: 10px; }
.drawer-head > span:first-child { font-size: 16px; font-weight: 600; color: var(--text-primary); }
.drawer-sub { font-size: 12px; color: var(--text-muted); }

.version-layout { display: flex; gap: 16px; height: 100%; align-items: flex-start; }
.version-list {
  width: 220px; flex-shrink: 0; max-height: calc(100vh - 160px); overflow-y: auto;
  border-right: 1px solid var(--border-color); padding-right: 8px;
}
.version-item {
  padding: 8px 10px; border-radius: 8px; cursor: pointer; margin-bottom: 6px;
  border: 1px solid transparent; transition: background .15s, border-color .15s;
}
.version-item:hover { background: var(--el-fill-color-light, #f5f7fa); }
.version-item.active { background: var(--el-fill-color-light, #f5f7fa); border-color: var(--el-color-primary-light-7, #a0cfff); }
.version-row { display: flex; align-items: center; gap: 6px; }
.version-no { font-weight: 600; font-size: 13px; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.cur-badge {
  font-size: 11px; color: #16a34a; background: #f0fdf4;
  border: 1px solid #bbf7d0; border-radius: 8px; padding: 0 6px;
}
.version-summary {
  font-size: 12px; color: var(--text-secondary); margin-top: 3px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.version-meta { font-size: 11px; color: var(--text-muted); margin-top: 2px; font-variant-numeric: tabular-nums; }

.version-detail { flex: 1; min-width: 0; max-height: calc(100vh - 130px); overflow-y: auto; padding-right: 4px; }
.detail-title { font-size: 15px; font-weight: 600; color: var(--text-primary); display: flex; align-items: baseline; gap: 8px; }
.detail-sub { font-size: 12px; font-weight: 400; color: var(--text-muted); }
.detail-summary { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }

.section-label {
  font-size: 12px; font-weight: 600; color: var(--text-muted);
  margin: 16px 0 6px; padding-bottom: 4px; border-bottom: 1px solid var(--border-color);
}
.diff-list { display: flex; flex-direction: column; gap: 6px; }
.diff-row { display: flex; gap: 10px; font-size: 12px; }
.diff-field { width: 130px; flex-shrink: 0; color: var(--text-secondary); }
.diff-values { flex: 1; min-width: 0; }
.diff-old {
  color: #b91c1c; background: #fef2f2; text-decoration: line-through;
  padding: 2px 6px; border-radius: 4px; word-break: break-all;
}
.diff-new {
  color: #15803d; background: #f0fdf4;
  padding: 2px 6px; border-radius: 4px; margin-top: 2px; word-break: break-all;
}
.empty-hint { font-size: 12px; color: var(--text-muted); padding: 6px 0; }
.drawer-footer { margin-top: 20px; padding-top: 12px; border-top: 1px solid var(--border-color); }

/* ---- 数据集目录浏览 ---- */
.browse-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.browse-layout { display: flex; gap: 12px; height: calc(100vh - 220px); min-height: 380px; }
.browse-list {
  width: 340px; flex-shrink: 0; overflow-y: auto; border: 1px solid var(--border-color);
  border-radius: 8px; padding: 6px;
}
.browse-item {
  display: flex; align-items: center; gap: 6px; padding: 5px 8px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
.browse-item:hover { background: var(--el-fill-color-light, #f5f7fa); }
.browse-item.active { background: var(--el-color-primary-light-9, #ecf5ff); }
.browse-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.browse-size { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.dir-icon { color: #e6a23c; }
.file-icon { color: #909399; }

.browse-content {
  flex: 1; min-width: 0; overflow: auto; border: 1px solid var(--border-color);
  border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column;
}
.content-head {
  display: flex; align-items: baseline; gap: 10px; padding-bottom: 8px;
  margin-bottom: 8px; border-bottom: 1px solid var(--border-color); flex-shrink: 0;
}
.content-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.content-meta { font-size: 11px; color: var(--text-muted); }
.content-actions { margin-left: auto; display: flex; gap: 6px; }
/* 编辑器撑满剩余高度。
   原来 textarea 只有固定 rows=20 的高度，点「编辑」后面板会缩一截、
   下面留一块空白 —— 这里用 flex 让它和只读态一样占满。 */
.content-editor {
  flex: 1; min-height: 0; display: flex;
}
.content-editor :deep(.el-textarea) { flex: 1; display: flex; }
.content-editor :deep(textarea) {
  flex: 1; height: 100% !important; resize: none;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.6;
}
.edit-hint { font-size: 11px; color: var(--text-muted); margin-top: 8px; line-height: 1.6; flex-shrink: 0; }
.content-body {
  flex: 1; margin: 0; overflow: auto; font-size: 12px; line-height: 1.6;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  white-space: pre-wrap; word-break: break-all; color: var(--text-secondary);
}
</style>
