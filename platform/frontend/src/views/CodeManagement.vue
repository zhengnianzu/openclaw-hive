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
        <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip resizable>
          <template #default="{row}">
            <el-tag :type="isGit(row) ? 'success' : 'info'" size="small" effect="plain"
              style="margin-right:6px">{{ isGit(row) ? 'Git' : 'OBS' }}</el-tag>
            {{ row.name }}
          </template>
        </el-table-column>
        <el-table-column label="版本" min-width="180" show-overflow-tooltip resizable>
          <template #default="{row}">
            <span style="color:#f59e0b" v-if="row.is_default">★</span>
            {{ row.version }}
          </template>
        </el-table-column>
        <el-table-column label="当前 commit" width="120" align="center" resizable>
          <template #default="{row}">
            <span v-if="!isGit(row)" class="muted">—</span>
            <el-icon v-else-if="row.syncing" class="is-loading"><Loading /></el-icon>
            <span v-else-if="row.resolved_commit" class="mono">{{ shortSha(row.resolved_commit) }}</span>
            <el-tag v-else type="info" size="small">未同步</el-tag>
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
            <!-- pre-line：首行是同步写入的 commit 摘要，换行后是用户说明，
                 默认的空白折叠会把两行并成一行 -->
            <span v-if="row.description" style="white-space:pre-line">{{ row.description }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90" align="center" resizable>
          <template #default="{row}">
            <el-tag v-if="row.syncing" type="warning" size="small">同步中</el-tag>
            <el-tag v-else-if="row.enabled !== false" type="success" size="small">已启用</el-tag>
            <el-tag v-else type="info" size="small">已停用</el-tag>
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
                    <el-dropdown-item v-if="authStore.isOperator && isGit(row)" command="sync" :disabled="row.syncing">
                      {{ row.syncing ? '同步中…' : '同步最新' }}
                    </el-dropdown-item>
                    <el-dropdown-item command="download">下载</el-dropdown-item>
                    <el-dropdown-item command="status">检查状态</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator" command="toggle" divided>
                      {{ row.enabled !== false ? '停用' : '启用' }}
                    </el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator" command="delete" style="color:#f56c6c">删除</el-dropdown-item>
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
        <el-form-item label="来源" required>
          <el-radio-group v-model="createForm.source_type">
            <el-radio-button value="obs">OBS</el-radio-button>
            <el-radio-button value="git">GitHub</el-radio-button>
          </el-radio-group>
          <div class="field-hint">
            OBS：由 obsutil 从对象存储拉取；GitHub：平台自己 clone，可随时「同步最新」拉到新 commit
          </div>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" placeholder="例如：openclaw-task" />
        </el-form-item>
        <el-form-item label="版本" required>
          <el-input v-model="createForm.version" :placeholder="createForm.source_type === 'git' ? '例如：main（建议用分支名）' : '例如：v1'" />
        </el-form-item>
        <template v-if="createForm.source_type === 'git'">
          <el-form-item label="仓库地址" required>
            <el-input v-model="createForm.repo_url" placeholder="例如：https://github.com/org/openclaw-task.git" />
            <div class="field-hint">私有仓需在后端 .env 配置 GITHUB_TOKEN</div>
          </el-form-item>
          <el-form-item label="分支" required>
            <el-input v-model="createForm.branch" placeholder="默认 main" />
          </el-form-item>
          <el-form-item label="子目录">
            <el-input v-model="createForm.subdir" placeholder="可选，例如：packages/openclaw-task；留空表示仓库根目录" />
          </el-form-item>
        </template>
        <el-form-item v-else label="OBS路径" required>
          <el-input v-model="createForm.obs_path" :placeholder="`例如：${joinBucket('code')}openclaw-task/v1/openclaw-task/`">
            <template #append>
              <el-button @click="openObsBrowser('obs_path')">浏览</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="入口文件" required>
          <el-input v-model="createForm.main_python_file" placeholder="例如：harness_automation.py">
            <template #append>
              <el-button v-if="createForm.source_type !== 'git'" @click="openObsBrowser('main_python_file')">选择</el-button>
            </template>
          </el-input>
          <div style="font-size:12px;color:#999;margin-top:4px">
            {{ createForm.source_type === 'git' ? '仓库内的 .py 入口文件名' : '从OBS代码目录中选择.py入口文件，只需填文件名' }}
          </div>
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
        <el-descriptions-item label="来源">
          <el-tag :type="isGit(detail) ? 'success' : 'info'" size="small" effect="plain">
            {{ isGit(detail) ? 'GitHub' : 'OBS' }}
          </el-tag>
        </el-descriptions-item>
        <template v-if="isGit(detail)">
          <el-descriptions-item label="仓库地址">
            <span class="mono">{{ detail.repo_url || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="分支">
            <span class="mono">{{ detail.branch || 'main' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="子目录">
            <span class="mono">{{ detail.subdir || '（仓库根目录）' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="当前 commit">
            <span v-if="detail.resolved_commit" class="mono">{{ detail.resolved_commit }}</span>
            <el-tag v-else type="info" size="small">未同步</el-tag>
            <div v-if="remoteHeadLoading" class="field-hint">正在探测远端…</div>
            <div v-else-if="remoteHead && remoteHead.behind" class="field-hint" style="color:#e6a23c">
              远端有新 commit（{{ shortSha(remoteHead.remote_commit) }}），点「同步最新」拉取
            </div>
            <div v-else-if="remoteHead && remoteHead.supported" class="field-hint" style="color:#67c23a">
              已是远端最新
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="最近同步">{{ detail.synced_at || '—' }}</el-descriptions-item>
        </template>
        <el-descriptions-item v-else label="OBS路径">
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
        <el-form-item label="来源">
          <el-tag :type="isGit(editForm) ? 'success' : 'info'" size="small" effect="plain">
            {{ isGit(editForm) ? 'GitHub' : 'OBS' }}
          </el-tag>
          <div class="field-hint">来源不可切换（两种来源的目录规则不同，切换会让已打包产物对不上）；如需更换请新建一条记录</div>
        </el-form-item>
        <template v-if="isGit(editForm)">
          <el-form-item label="仓库地址" required>
            <el-input v-model="editForm.repo_url" placeholder="例如：https://github.com/org/openclaw-task.git" />
          </el-form-item>
          <el-form-item label="分支" required>
            <el-input v-model="editForm.branch" placeholder="默认 main" />
            <div class="field-hint">改分支后需点「同步最新」才会拉取新代码</div>
          </el-form-item>
          <el-form-item label="子目录">
            <el-input v-model="editForm.subdir" placeholder="可选；留空表示仓库根目录" />
          </el-form-item>
        </template>
        <el-form-item v-else label="OBS路径" required>
          <el-input v-model="editForm.obs_path" :placeholder="`例如：${joinBucket('code')}openclaw-task/v1/openclaw-task/`">
            <template #append>
              <el-button @click="openObsBrowser('obs_path', 'edit')">浏览</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="入口文件" required>
          <el-input v-model="editForm.main_python_file" placeholder="例如：harness_automation.py">
            <template #append>
              <el-button v-if="!isGit(editForm)" @click="openObsBrowser('main_python_file', 'edit')">选择</el-button>
            </template>
          </el-input>
          <div class="field-hint">
            {{ isGit(editForm) ? '仓库内的 .py 入口文件名' : '从OBS代码目录中选择.py入口文件，只需填文件名' }}
          </div>
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
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, ArrowDown, Search, Loading } from '@element-plus/icons-vue'
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
const emptyCreateForm = () => ({
  name: '', version: 'v1', source_type: 'obs', obs_path: '',
  repo_url: '', branch: 'main', subdir: '',
  description: '', main_python_file: 'harness_automation.py', tag: '', is_default: false,
})
const createForm = ref(emptyCreateForm())
const creatorFilter = ref('')
const creatorOptions = computed(() =>
  [...new Set((repos.value || []).map(r => r.created_by).filter(Boolean))].sort()
)

const editVisible = ref(false)
const updating = ref(false)
const editForm = ref({
  id: null, name: '', version: '', source_type: 'obs', obs_path: '',
  repo_url: '', branch: '', subdir: '',
  description: '', main_python_file: '', tag: '',
})

const detailVisible = ref(false)
const detail = ref({})
const detailStatus = ref(null)
const detailStatusLoading = ref(false)
const remoteHead = ref(null)
const remoteHeadLoading = ref(false)

// 存量记录没有 source_type 这一列（后端默认 'obs'），缺值一律按 OBS 处理
const isGit = (row) => (row?.source_type || 'obs') === 'git'
const shortSha = (sha) => (sha || '').slice(0, 7)

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
//
// 入口是「更多」里的一个菜单项（文案随当前状态在「启用/停用」间切换），
// 而不是表格里的 el-switch —— 开关自带新值，菜单项得自己按当前状态取反。

async function toggleEnabled(row) {
  const next = row.enabled === false
  try {
    await api.patch(`/code-repos/${row.id}`, { enabled: next })
    row.enabled = next
    ElMessage.success(next ? '已启用' : '已停用（创建页不再列出）')
  } catch { ElMessage.error('切换失败') }
}

async function loadRepos({ silent = false } = {}) {
  if (!silent) loading.value = true
  try {
    // 筛选交给后端做，避免前端只在当前页内过滤
    const params = {}
    if (versionQuery.value.trim()) params.version = versionQuery.value.trim()
    if (tagFilter.value) params.tag = tagFilter.value
    repos.value = await api.get('/code-repos', { params })
    // 有同步在跑就自动续上轮询，全结束就停，避免空转
    if (hasSyncing()) startSyncPolling()
    else stopSyncPollingIfIdle()
  } finally {
    if (!silent) loading.value = false
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
  createForm.value = emptyCreateForm()
  createVisible.value = true
}

async function handleCreate() {
  const f = createForm.value
  if (!f.name) {
    ElMessage.warning('请填写名称')
    return
  }
  // 必填项按来源分支判断：git 要仓库地址，obs 要 OBS 路径
  if (f.source_type === 'git') {
    if (!f.repo_url.trim()) {
      ElMessage.warning('请填写仓库地址')
      return
    }
  } else if (!f.obs_path) {
    ElMessage.warning('请填写OBS路径')
    return
  }
  if (!f.main_python_file) {
    ElMessage.warning('请指定入口文件')
    return
  }
  creating.value = true
  try {
    await api.post('/code-repos', f)
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
  else if (cmd === 'sync') syncRepo(row)
  else if (cmd === 'download') downloadRepo(row)
  else if (cmd === 'status') checkStatus(row)
  else if (cmd === 'toggle') toggleEnabled(row)
  else if (cmd === 'delete') deleteRepo(row)
}

async function openDetail(row) {
  detail.value = { ...row }
  detailStatus.value = null
  remoteHead.value = null
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
  // git 仓再探一次远端，好在详情里直接告诉用户「是否落后」。
  // 单独一个请求、失败就静默：它要走网络（ls-remote），不该拖住或弄坏详情本身。
  if (!isGit(row)) return
  remoteHeadLoading.value = true
  try {
    remoteHead.value = await api.get(`/code-repos/${row.id}/remote-head`)
  } catch {
    remoteHead.value = null
  } finally {
    remoteHeadLoading.value = false
  }
}

function openEdit(row) {
  editForm.value = {
    id: row.id,
    name: row.name,
    version: row.version,
    source_type: row.source_type || 'obs',
    obs_path: row.obs_path || '',
    repo_url: row.repo_url || '',
    branch: row.branch || '',
    subdir: row.subdir || '',
    description: row.description || '',
    main_python_file: row.main_python_file || '',
    tag: row.tag || '',
  }
  editVisible.value = true
}

async function handleUpdate() {
  const f = editForm.value
  const git = isGit(f)
  if (git) {
    if (!f.repo_url.trim()) {
      ElMessage.warning('请填写仓库地址')
      return
    }
  } else if (!f.obs_path.trim()) {
    ElMessage.warning('请填写OBS路径')
    return
  }
  if (!f.main_python_file.trim()) {
    ElMessage.warning('请指定入口文件')
    return
  }
  updating.value = true
  try {
    // 不传 name / version / source_type —— 后端也不接受，它们决定下载与打包目录
    const payload = {
      main_python_file: f.main_python_file.trim(),
      description: f.description.trim(),
      tag: f.tag || '',
    }
    // 只提交当前来源用得上的地址字段，免得把另一种来源的值清空
    if (git) {
      payload.repo_url = f.repo_url.trim()
      payload.branch = f.branch.trim()
      payload.subdir = f.subdir.trim()
    } else {
      payload.obs_path = f.obs_path.trim()
    }
    await api.patch(`/code-repos/${f.id}`, payload)
    ElMessage.success('已保存')
    editVisible.value = false
    loadRepos()
  } finally {
    updating.value = false
  }
}

async function syncRepo(row) {
  if (row.syncing) return
  downloadingId.value = row.id
  // 同步是阻塞的（POST 直到拉取+打包结束才返回），先把本地状态点亮，
  // 让「同步中」立刻可见；同时后台轮询，别的页面/别的用户也能看到。
  row.syncing = true
  startSyncPolling()
  try {
    const res = await api.post(`/code-repos/${row.id}/sync`)
    if (res.updated) {
      ElMessage.success(res.message)
    } else {
      ElMessage.info(res.message)
    }
  } catch {
    // 409 = 已有同步在跑；其余交给全局拦截器提示，这里只保证状态归位
  } finally {
    downloadingId.value = null
    await loadRepos()  // resolved_commit / synced_at / 描述里的 commit 都变了
    stopSyncPollingIfIdle()
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

// ---- 同步中的轮询 ----
// 同步可能要十几秒到一分钟（取决于到 GitHub 的链路）。轮询只做一件事：
// 让「同步中」这个状态在别处也能看见，并在同步结束时自动刷新成新 commit。
let syncPollTimer = null

const hasSyncing = () => (repos.value || []).some(r => r.syncing)

function startSyncPolling() {
  if (syncPollTimer) return
  // silent：轮询刷新不该让表格每 3 秒闪一次 loading 遮罩
  syncPollTimer = setInterval(() => loadRepos({ silent: true }), 3000)
}

function stopSyncPollingIfIdle() {
  if (syncPollTimer && !hasSyncing()) {
    clearInterval(syncPollTimer)
    syncPollTimer = null
  }
}

onUnmounted(() => {
  if (syncPollTimer) { clearInterval(syncPollTimer); syncPollTimer = null }
})

async function checkStatus(row) {
  try {
    const res = await api.get(`/code-repos/${row.id}/status`)
    if (res.packaged) {
      ElMessage.success(`已打包: ${res.tar_path}`)
    } else if (res.downloaded) {
      ElMessage.warning('已下载但未打包，请点击"下载"触发打包')
    } else if (isGit(row)) {
      ElMessage.warning('尚未同步，请点击"同步最新"拉取代码')
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
