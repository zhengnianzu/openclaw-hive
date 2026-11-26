<template>
  <div>
    <div class="header-row">
      <h2>代码仓管理</h2>
      <div class="header-actions">
        <el-button v-if="authStore.isOperator" @click="tagManageVisible = true">管理标签</el-button>
        <el-button v-if="authStore.isOperator" type="primary" @click="openCreateDialog">
          <el-icon><Plus /></el-icon> 新建代码仓
        </el-button>
      </div>
    </div>

    <div class="filter-bar">
      <el-input v-model="versionQuery" placeholder="搜索版本" clearable size="default" style="width:220px"
        @keyup.enter="applyFilters" @clear="applyFilters">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="tagFilter" placeholder="全部标签" clearable filterable size="default" style="width:180px"
        @change="applyFilters">
        <el-option v-for="t in tags" :key="t.id" :label="t.name" :value="t.name" />
      </el-select>
      <el-select v-model="creatorFilter" placeholder="创建者" clearable filterable size="default" style="width:160px">
        <el-option v-for="c in creatorOptions" :key="c" :label="c" :value="c" />
      </el-select>
      <el-button @click="applyFilters">搜索</el-button>
      <el-button v-if="versionQuery || tagFilter || creatorFilter" text @click="resetFilters">重置</el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="pagedRepos" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="id" label="序号" width="80" align="center" />
        <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip resizable />
        <el-table-column label="版本" min-width="180" show-overflow-tooltip resizable>
          <template #default="{row}">
            <span style="color:#f59e0b" v-if="row.is_default">★</span>
            {{ row.version }}
          </template>
        </el-table-column>
        <el-table-column prop="tag" label="标签" width="120" align="center" resizable>
          <template #default="{row}">
            <el-tag v-if="row.tag" size="small">{{ row.tag }}</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip resizable>
          <template #default="{row}">
            <span v-if="row.description">{{ row.description }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="80" align="center" resizable>
          <template #default="{row}">
            <el-switch :model-value="row.enabled !== false" @change="v => toggleEnabled(row, v)" />
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="创建者" width="100" resizable />
        <el-table-column prop="created_at" label="创建时间" width="180" resizable />
        <el-table-column label="操作" width="150" align="center" fixed="right">
          <template #default="{row}">
            <div class="op-cell">
              <el-button size="small" @click="openDetail(row)">详情</el-button>
              <el-dropdown trigger="click" @command="cmd => handleMore(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item v-if="authStore.isOperator" command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item command="download">下载</el-dropdown-item>
                    <el-dropdown-item command="status">检查状态</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator" command="delete" divided style="color:#f56c6c">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pagination-bar">
      <span class="total-text">共 {{ repos.length }} 条</span>
      <el-pagination v-model:current-page="currentPage" v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]" :total="repos.length"
        layout="sizes, prev, pager, next" background />
    </div>

    <el-dialog v-model="createVisible" title="新建代码仓" width="600px" destroy-on-close>
      <el-form :model="createForm" label-width="100px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" placeholder="例如：openclaw-task" />
        </el-form-item>
        <el-form-item label="版本" required>
          <el-input v-model="createForm.version" placeholder="例如：v1" />
        </el-form-item>
        <el-form-item label="OBS路径" required>
          <el-input v-model="createForm.obs_path" :placeholder="`例如：${joinBucket('code')}openclaw-task/v1/openclaw-task/`">
            <template #append>
              <el-button @click="openObsBrowser('obs_path')">浏览</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="入口文件" required>
          <el-input v-model="createForm.main_python_file" placeholder="例如：harness_automation.py">
            <template #append>
              <el-button @click="openObsBrowser('main_python_file')">选择</el-button>
            </template>
          </el-input>
          <div style="font-size:12px;color:#999;margin-top:4px">从OBS代码目录中选择.py入口文件，只需填文件名</div>
        </el-form-item>
        <el-form-item label="标签">
          <el-select v-model="createForm.tag" placeholder="选择标签（可选）" clearable filterable style="width:100%">
            <el-option v-for="t in tags" :key="t.id" :label="t.name" :value="t.name" />
          </el-select>
          <div class="field-hint">标签用来标记所属分支/用途；新标签请先用右上角「管理标签」创建</div>
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="createForm.is_default" />
          <div class="field-hint">全局唯一：设为默认后原默认代码仓自动取消；高级配置选完 harness 后自动填入</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="2" placeholder="可选描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="detailVisible" title="代码仓详情" width="640px" destroy-on-close>
      <el-descriptions :column="1" border size="default">
        <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="版本">{{ detail.version }}</el-descriptions-item>
        <el-descriptions-item label="标签">
          <el-tag v-if="detail.tag" size="small">{{ detail.tag }}</el-tag>
          <span v-else class="muted">—</span>
        </el-descriptions-item>
        <el-descriptions-item label="OBS路径">
          <span class="mono">{{ detail.obs_path }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="入口文件">
          <span class="mono">{{ detail.main_python_file || '—' }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="描述">{{ detail.description || '—' }}</el-descriptions-item>
        <el-descriptions-item label="创建者">{{ detail.created_by || '—' }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ detail.created_at }}</el-descriptions-item>
        <el-descriptions-item label="打包状态">
          <span v-if="detailStatusLoading" class="muted">查询中…</span>
          <template v-else-if="detailStatus">
            <el-tag v-if="detailStatus.packaged" type="success" size="small">已打包</el-tag>
            <el-tag v-else-if="detailStatus.downloaded" type="warning" size="small">已下载未打包</el-tag>
            <el-tag v-else type="info" size="small">尚未下载</el-tag>
            <div v-if="detailStatus.tar_path" class="mono status-path">{{ detailStatus.tar_path }}</div>
            <div v-else-if="detailStatus.src_path" class="mono status-path">{{ detailStatus.src_path }}</div>
          </template>
          <span v-else class="muted">查询失败</span>
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑代码仓" width="600px" destroy-on-close>
      <el-form :model="editForm" label-width="100px">
        <el-form-item label="名称">
          <el-input :model-value="editForm.name" disabled />
        </el-form-item>
        <el-form-item label="版本">
          <el-input :model-value="editForm.version" disabled />
          <div class="field-hint">名称与版本决定下载目录和打包路径，不可修改；如需更换请新建一条记录</div>
        </el-form-item>
        <el-form-item label="OBS路径" required>
          <el-input v-model="editForm.obs_path" :placeholder="`例如：${joinBucket('code')}openclaw-task/v1/openclaw-task/`">
            <template #append>
              <el-button @click="openObsBrowser('obs_path', 'edit')">浏览</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="入口文件" required>
          <el-input v-model="editForm.main_python_file" placeholder="例如：harness_automation.py">
            <template #append>
              <el-button @click="openObsBrowser('main_python_file', 'edit')">选择</el-button>
            </template>
          </el-input>
          <div class="field-hint">从OBS代码目录中选择.py入口文件，只需填文件名</div>
        </el-form-item>
        <el-form-item label="标签">
          <el-select v-model="editForm.tag" placeholder="选择标签（可选）" clearable filterable style="width:100%">
            <el-option v-for="t in tags" :key="t.id" :label="t.name" :value="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="editForm.description" type="textarea" :rows="2" placeholder="可选描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="updating" @click="handleUpdate">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="tagManageVisible" title="管理标签" width="480px" destroy-on-close>
      <div class="tag-create-row">
        <el-input v-model="newTagName" placeholder="新标签名，例如：xiaoyi" size="default"
          @keyup.enter="handleCreateTag" />
        <el-button type="primary" :loading="creatingTag" @click="handleCreateTag">新增</el-button>
      </div>
      <el-table :data="tags" v-loading="tagsLoading" size="small" max-height="320" border>
        <el-table-column prop="name" label="标签" min-width="140" />
        <el-table-column prop="created_by" label="创建者" width="110" />
        <el-table-column label="操作" width="80" align="center">
          <template #default="{row}">
            <el-button type="danger" text size="small" @click="handleDeleteTag(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="field-hint">仍被代码仓使用的标签无法删除；删除标签不会改动代码仓记录</div>
      <template #footer>
        <el-button @click="tagManageVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="obsVisible" :title="obsMode === 'main_python_file' ? '选择入口文件' : '选择OBS目录'" width="700px" destroy-on-close>
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
        <el-input v-model="obsCurrentPath" placeholder="OBS 路径" size="small" @keyup.enter="loadObsDir">
          <template #prepend>路径</template>
        </el-input>
        <el-button size="small" @click="loadObsDir" :loading="obsLoading">加载</el-button>
      </div>
      <el-table :data="filteredObsItems" v-loading="obsLoading" max-height="400" @row-dblclick="handleObsDblClick">
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="size" label="大小" width="100" />
        <el-table-column label="类型" width="80">
          <template #default="{row}">{{ row.is_dir ? '目录' : '文件' }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="obsVisible = false">取消</el-button>
        <el-button v-if="obsMode !== 'main_python_file'" type="primary" @click="confirmObsSelect">选择当前路径</el-button>
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
import { obsBucket, joinBucket } from '../api/obsConfig'

const authStore = useAuthStore()
const repos = ref([])
const loading = ref(false)
const downloadingId = ref(null)
const currentPage = ref(1)
const pageSize = ref(10)

const versionQuery = ref('')
const tagFilter = ref('')
const tags = ref([])
const tagsLoading = ref(false)
const tagManageVisible = ref(false)
const newTagName = ref('')
const creatingTag = ref(false)

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', version: 'v1', obs_path: '', description: '', main_python_file: 'harness_automation.py', tag: '', is_default: false })
const creatorFilter = ref('')
const creatorOptions = computed(() =>
  [...new Set((repos.value || []).map(r => r.created_by).filter(Boolean))].sort()
)

const editVisible = ref(false)
const updating = ref(false)
const editForm = ref({ id: null, name: '', version: '', obs_path: '', description: '', main_python_file: '', tag: '' })

const detailVisible = ref(false)
const detail = ref({})
const detailStatus = ref(null)
const detailStatusLoading = ref(false)

const creatorFiltered = computed(() =>
  !creatorFilter.value ? repos.value : repos.value.filter(r => r.created_by === creatorFilter.value)
)
const pagedRepos = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return creatorFiltered.value.slice(start, start + pageSize.value)
})

// 换每页条数时回到第一页，否则会停在一个空页上
watch(pageSize, () => { currentPage.value = 1 })
// 删除导致当前页越界时回退（例如在最后一页删掉唯一一条）
watch(() => repos.value.length, (n) => {
  const maxPage = Math.max(1, Math.ceil(n / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

// 启用开关: 只影响创建页下拉可见性; 存量实例引用的是版本快照, 不受影响

async function toggleEnabled(row, v) {
  try {
    await api.patch(`/code-repos/${row.id}`, { enabled: v })
    row.enabled = v
    ElMessage.success(v ? '已启用' : '已停用（创建页不再列出）')
  } catch { ElMessage.error('切换失败') }
}

async function loadRepos() {
  loading.value = true
  try {
    // 筛选交给后端做，避免前端只在当前页内过滤
    const params = {}
    if (versionQuery.value.trim()) params.version = versionQuery.value.trim()
    if (tagFilter.value) params.tag = tagFilter.value
    repos.value = await api.get('/code-repos', { params })
  } finally {
    loading.value = false
  }
}

async function loadTags() {
  tagsLoading.value = true
  try {
    tags.value = await api.get('/code-repos/tags')
  } catch {
    tags.value = []
  } finally {
    tagsLoading.value = false
  }
}

function applyFilters() {
  currentPage.value = 1  // 换筛选条件后页码要归零，否则可能停在空页
  loadRepos()
}

function resetFilters() {
  versionQuery.value = ''
  tagFilter.value = ''
  creatorFilter.value = ''
  applyFilters()
}

async function handleCreateTag() {
  const name = newTagName.value.trim()
  if (!name) {
    ElMessage.warning('请输入标签名')
    return
  }
  creatingTag.value = true
  try {
    await api.post('/code-repos/tags', { name })
    ElMessage.success('标签已新增')
    newTagName.value = ''
    loadTags()
  } finally {
    creatingTag.value = false
  }
}

async function handleDeleteTag(row) {
  try {
    await ElMessageBox.confirm(`确认删除标签 "${row.name}"？`, '确认删除', { type: 'warning' })
  } catch { return }
  await api.delete(`/code-repos/tags/${row.id}`)
  ElMessage.success('已删除')
  loadTags()
  // 删掉的正是当前筛选项时，清掉筛选免得列表空着没解释
  if (tagFilter.value === row.name) resetFilters()
}

function openCreateDialog() {
  createForm.value = { name: '', version: 'v1', obs_path: '', description: '', main_python_file: 'harness_automation.py', tag: '' }
  createVisible.value = true
}

async function handleCreate() {
  if (!createForm.value.name || !createForm.value.obs_path) {
    ElMessage.warning('请填写名称和OBS路径')
    return
  }
  if (!createForm.value.main_python_file) {
    ElMessage.warning('请指定入口文件')
    return
  }
  creating.value = true
  try {
    await api.post('/code-repos', createForm.value)
    ElMessage.success('创建成功')
    createVisible.value = false
    // 后端按 created_at DESC 排序，新代码仓在第一页；留在原页会看不到刚建的那条
    currentPage.value = 1
    loadRepos()
  } finally {
    creating.value = false
  }
}

function handleMore(cmd, row) {
  if (cmd === 'edit') openEdit(row)
  else if (cmd === 'download') downloadRepo(row)
  else if (cmd === 'status') checkStatus(row)
  else if (cmd === 'delete') deleteRepo(row)
}

async function openDetail(row) {
  detail.value = { ...row }
  detailStatus.value = null
  detailVisible.value = true
  // 顺带查一次打包状态，省得再单独点「检查状态」
  detailStatusLoading.value = true
  try {
    detailStatus.value = await api.get(`/code-repos/${row.id}/status`)
  } catch {
    detailStatus.value = null
  } finally {
    detailStatusLoading.value = false
  }
}

function openEdit(row) {
  editForm.value = {
    id: row.id,
    name: row.name,
    version: row.version,
    obs_path: row.obs_path,
    description: row.description || '',
    main_python_file: row.main_python_file || '',
    tag: row.tag || '',
  }
  editVisible.value = true
}

async function handleUpdate() {
  const f = editForm.value
  if (!f.obs_path.trim()) {
    ElMessage.warning('请填写OBS路径')
    return
  }
  if (!f.main_python_file.trim()) {
    ElMessage.warning('请指定入口文件')
    return
  }
  updating.value = true
  try {
    // 不传 name / version —— 后端也不接受，它们决定下载与打包目录
    await api.patch(`/code-repos/${f.id}`, {
      obs_path: f.obs_path.trim(),
      main_python_file: f.main_python_file.trim(),
      description: f.description.trim(),
      tag: f.tag || '',
    })
    ElMessage.success('已保存')
    editVisible.value = false
    loadRepos()
  } finally {
    updating.value = false
  }
}

async function downloadRepo(row) {
  downloadingId.value = row.id
  try {
    const res = await api.post(`/code-repos/${row.id}/download`)
    if (res.already_exists) {
      ElMessage.info(`已打包: ${res.tar_path}`)
    } else {
      ElMessage.success(`下载并打包成功: ${res.tar_path}`)
    }
  } finally {
    downloadingId.value = null
  }
}

async function checkStatus(row) {
  try {
    const res = await api.get(`/code-repos/${row.id}/status`)
    if (res.packaged) {
      ElMessage.success(`已打包: ${res.tar_path}`)
    } else if (res.downloaded) {
      ElMessage.warning('已下载但未打包，请点击"下载"触发打包')
    } else {
      ElMessage.warning('尚未下载到本地')
    }
  } catch {
    ElMessage.error('检查状态失败')
  }
}

async function deleteRepo(row) {
  await ElMessageBox.confirm('确认删除该代码仓记录？（不会删除本地已下载的文件）', '提示', { type: 'warning' })
  await api.delete(`/code-repos/${row.id}`)
  ElMessage.success('已删除')
  loadRepos()
}

// OBS Browser
const obsVisible = ref(false)
const obsLoading = ref(false)
const obsItems = ref([])
const obsCurrentPath = ref(joinBucket('code'))
const obsMode = ref('obs_path')
// 浏览器现在要服务新建和编辑两个弹窗，记住这次是替哪个表单选的
const obsTarget = ref('create')

const obsTargetForm = () => obsTarget.value === 'edit' ? editForm.value : createForm.value

const filteredObsItems = computed(() => {
  let items = obsItems.value.filter(item => {
    if (!item.is_dir) return true
    return !item.name.startsWith('.') && item.name !== '__pycache__' && item.name !== 'node_modules'
  })
  if (obsMode.value === 'main_python_file') {
    items = items.filter(item => item.is_dir || item.name.endsWith('.py'))
  }
  return items
})

function openObsBrowser(mode, target = 'create') {
  obsMode.value = mode
  obsTarget.value = target
  obsCurrentPath.value = obsTargetForm().obs_path || joinBucket('code')
  obsItems.value = []
  obsVisible.value = true
  loadObsDir()
}

async function loadObsDir() {
  obsLoading.value = true
  try {
    const showFiles = obsMode.value === 'main_python_file'
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value, show_files: showFiles } })
    obsItems.value = res.items || []
  } finally {
    obsLoading.value = false
  }
}

function handleObsDblClick(row) {
  if (row.is_dir) {
    obsCurrentPath.value = row.path
    if (!obsCurrentPath.value.endsWith('/')) obsCurrentPath.value += '/'
    loadObsDir()
  } else if (obsMode.value === 'main_python_file' && row.name.endsWith('.py')) {
    obsTargetForm().main_python_file = row.name
    obsVisible.value = false
    ElMessage.success(`已选择入口文件: ${row.name}`)
  }
}

function confirmObsSelect() {
  obsTargetForm().obs_path = obsCurrentPath.value
  obsVisible.value = false
}

onMounted(() => { loadTags(); loadRepos() })
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.filter-bar { display: flex; gap: 12px; margin-bottom: 16px; align-items: center; }
.tag-create-row { display: flex; gap: 8px; margin-bottom: 12px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }

.pagination-bar { display: flex; align-items: center; justify-content: space-between; margin-top: 16px; }
.total-text { color: var(--text-muted); font-size: 13px; }
/* 操作区：强制不换行，否则「更多」下拉会被挤到第二行、撑高整行 */
.op-cell { display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }
.muted { color: #999; }
.field-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.5; }
/* OBS 路径等长字符串用等宽字体，便于逐段核对 */
.mono { font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); font-size: 12px; word-break: break-all; }
.status-path { color: var(--text-muted); margin-top: 4px; }
</style>
