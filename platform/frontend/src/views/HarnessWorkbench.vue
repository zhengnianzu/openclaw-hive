<template>
  <div class="workbench">
    <div class="wb-head">
      <div class="wb-head-left">
        <el-button text @click="$router.push('/harness-configs')">
          <el-icon><ArrowLeft /></el-icon> 返回列表
        </el-button>
        <div class="wb-title">
          <el-tag v-if="currentType" :color="harnessStore.color(currentType)" effect="dark"
            :style="{ border: 'none' }">{{ harnessStore.label(currentType) }}</el-tag>
          <span class="wb-title-text">{{ currentScheme ? currentScheme.name : '选择配置方案' }}</span>
          <el-tag v-if="currentScheme?.is_default" size="small" type="success" effect="plain">默认方案</el-tag>
          <el-tag v-if="viewingVersion" size="small" :type="statusType(viewingVersion.status)" effect="plain">
            查看 v{{ viewingVersion.version }}
          </el-tag>
          <el-tag v-else size="small" type="warning" effect="plain">工作台</el-tag>
        </div>
      </div>
      <div class="wb-head-actions">
        <el-button v-if="authStore.isOperator && !viewingVersion" @click="saveFile"
          :disabled="!currentFile || !isDirty" :loading="saving">保存</el-button>
        <el-button v-if="authStore.isOperator" type="primary" @click="openCreateVersion"
          :disabled="!currentScheme">创建版本</el-button>
      </div>
    </div>

    <div class="wb-bar">
      <div class="wb-bar-left">
        <el-select v-model="currentType" style="width:190px" @change="onTypeChange">
          <el-option v-for="t in harnessStore.types" :key="t"
            :label="harnessStore.label(t)" :value="t" />
        </el-select>
        <el-select v-model="currentSchemeId" style="width:240px" placeholder="选择配置方案"
          :loading="listLoading" @change="onSchemeSelect">
          <el-option v-for="c in typeSchemes" :key="c.id" :label="c.name" :value="c.id" />
        </el-select>
        <el-button v-if="authStore.isOperator" text size="small" @click="openCreateScheme">
          <el-icon><Plus /></el-icon> 新建方案
        </el-button>
      </div>
    </div>

    <div class="wb-body">
      <!-- 左：目录树（Workspace = md，Runtime = 配置文件） -->
      <aside class="wb-col wb-col-left">
        <div class="col-title">
          文件
          <span v-if="viewingVersion" class="col-hint">v{{ viewingVersion.version }}（只读）</span>
          <span v-else class="col-hint">工作台</span>
        </div>
        <div class="tree-wrap" v-loading="fileLoading">
          <div v-for="group in treeGroups" :key="group.key" class="tree-group">
            <div class="tree-group-head" @click="toggleGroup(group.key)">
              <el-icon class="tree-caret" :class="{ collapsed: collapsedGroups[group.key] }"><ArrowDown /></el-icon>
              <el-icon><Folder /></el-icon>
              <span>{{ group.label }}</span>
              <span class="tree-count">{{ group.items.filter(i => i.exists).length }}/{{ group.items.length }}</span>
            </div>
            <div v-show="!collapsedGroups[group.key]" class="tree-group-body">
              <div v-for="f in group.items" :key="f.key"
                :class="['tree-item', { active: currentFile === f.key, missing: !f.exists }]"
                :title="f.exists ? '' : '点击从模板初始化'"
                @click="onTreeClick(f)">
                <el-icon><Document /></el-icon>
                <span class="tree-name">{{ f.label }}</span>
                <el-tag v-if="!f.exists" size="small" type="info">未初始化</el-tag>
              </div>
            </div>
          </div>
          <div v-if="!treeGroups.length" class="empty-hint">请选择配置方案</div>
        </div>
        <div v-if="currentScheme" class="file-tree-foot">
          <div>配置 {{ configFilesPresent }}/{{ fileList.length }} · md {{ mdFilesPresent }}/{{ mdFileList.length }}</div>
        </div>
      </aside>

      <!-- ② 编辑区 -->
      <section class="wb-col wb-col-main">
        <div class="col-title">
          {{ viewingVersion ? `编辑区 · v${viewingVersion.version}（只读）` : '编辑区 · 工作台' }}
        </div>
        <template v-if="currentScheme && treeGroups.length">
          <div class="editor-toolbar" v-if="!diffVisible">
            <el-radio-group v-model="editorMode" size="small">
              <el-radio-button value="edit">编辑</el-radio-button>
              <el-radio-button value="preview">预览</el-radio-button>
            </el-radio-group>
            <span v-if="isDirty" class="dirty-flag">● 未保存</span>
            <div style="flex:1"></div>
            <el-button v-if="editorMode === 'edit' && isJson && canEdit" size="small" @click="formatJson">格式化</el-button>
            <el-button size="small" text @click="reloadFile"><el-icon><Refresh /></el-icon></el-button>
          </div>
          <div class="editor-area diff-mode" v-if="diffVisible" v-loading="diffLoading">
            <div class="diff-head">
              <span class="diff-title">{{ diffTitle }}</span>
              <el-button size="small" text @click="closeDiff">关闭对比</el-button>
            </div>
            <div v-if="diffData && diffData.identical" class="empty-hint">与工作台完全一致</div>
            <el-tabs v-else-if="diffData" v-model="diffFile" class="diff-tabs">
              <el-tab-pane v-for="f in changedFiles" :key="f.file" :label="f.file" :name="f.file">
                <DiffView :lines="f.lines" />
              </el-tab-pane>
            </el-tabs>
          </div>
          <div class="editor-area" v-else v-loading="fileLoading">
            <template v-if="currentFile">
              <el-input v-if="editorMode === 'edit'" v-model="fileContent" type="textarea"
                :autosize="{ minRows: 22, maxRows: 40 }" :readonly="!canEdit"
                class="wb-editor" spellcheck="false" />
              <div v-else class="preview-pane">
                <MarkdownView v-if="isMarkdown" :content="fileContent" />
                <pre v-else>{{ fileContent }}</pre>
              </div>
            </template>
            <div v-else class="empty-hint">选择上方文件开始{{ canEdit ? '编辑' : '查看' }}</div>
          </div>
        </template>
        <div v-else class="empty-hint" style="padding-top:120px">请先在左侧选择配置方案</div>
      </section>

      <!-- ③ 版本 / 发布 -->
      <aside class="wb-col wb-col-right">
        <div class="col-title">版本</div>

        <template v-if="viewingVersion">
          <div class="version-card">
            <div class="version-head">
              <span class="version-no">v{{ viewingVersion.version }}</span>
              <el-tag size="small" :type="statusType(viewingVersion.status)">{{ statusText(viewingVersion.status) }}</el-tag>
            </div>
            <div class="version-meta">
              <div><span>创建于</span>{{ formatTime(viewingVersion.created_at) }}</div>
              <div><span>创建人</span>{{ viewingVersion.created_by || '—' }}</div>
              <div v-if="viewingVersion.published_at"><span>发布于</span>{{ formatTime(viewingVersion.published_at) }}</div>
              <div><span>文件数</span>{{ versionFileCount(viewingVersion) }} 个</div>
            </div>
            <div class="version-desc">{{ viewingVersion.description || '无说明' }}</div>
          </div>

        </template>

        <template v-else-if="currentScheme">
          <div class="version-card">
            <div class="version-head">
              <span class="version-no">工作台</span>
              <el-tag size="small" type="warning" effect="plain">编辑中</el-tag>
            </div>
            <div class="version-meta">
              <div><span>所属方案</span>{{ currentScheme.name }}</div>
              <div><span>文件数</span>{{ draftFilesPresent }} / {{ fileList.length }} 个</div>
              <div><span>已发布</span>{{ currentScheme.published_count }} 个版本</div>
            </div>
            <div class="version-desc">
              工作台是可编辑的工作副本，改动不影响已有实例。点「创建版本」冻结快照，再「发布」后才能在创建实例时选用。
            </div>
          </div>
        </template>

        <div class="col-title" style="margin-top:18px">
          版本列表
          <span class="col-hint">{{ versions.length }} 个 · {{ publishedCount }} 已发布</span>
        </div>
        <div class="version-item-wrap">
          <div :class="['version-item', { active: !viewingVersion }]" @click="viewDraft">
            <span class="history-dot on"></span>
            <div class="version-body">
              <div class="version-label">工作台</div>
              <div class="version-sub">{{ draftSummary }}</div>
            </div>
            <el-tag size="small" type="warning" effect="plain">编辑中</el-tag>
          </div>
        </div>
        <div class="history-list" v-loading="versionsLoading">
          <div v-for="v in versions" :key="v.id"
            :class="['history-item', { active: viewingVersion?.id === v.id }]" @click="selectVersion(v)">
            <div class="history-left">
              <span class="history-dot" :class="{ on: v.status === 'published' }"></span>
              <div>
                <div class="history-version">v{{ v.version }}</div>
                <div class="history-time">{{ formatTime(v.created_at) }} · {{ v.created_by || '—' }}</div>
              </div>
            </div>
            <div class="history-right" @click.stop>
              <el-tag size="small" :type="statusType(v.status)" effect="plain">{{ statusText(v.status) }}</el-tag>
              <el-dropdown trigger="click"
                @command="cmd => handleVersionCmd(cmd, v)">
                <el-button size="small" text><el-icon><MoreFilled /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="diff">与工作台对比</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator" command="apply"
                      divided>覆盖到工作台</el-dropdown-item>
                    <el-dropdown-item v-if="authStore.isOperator && v.status !== 'published'"
                      command="publish">发布</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>
          <div v-if="!versions.length && currentScheme && !versionsLoading" class="empty-hint">
            还没有版本，在工作台改完后点「创建版本」
          </div>
        </div>
      </aside>
    </div>

    <!-- 创建版本 -->
    <el-dialog v-model="createVerVisible" title="创建版本（冻结当前工作台）" width="440px">
      <el-form label-width="90px">
        <el-form-item label="版本号" required>
          <el-input v-model="createVerForm.version" placeholder="例如：1.1、1.2" maxlength="40" />
          <div class="field-hint">仅支持字母、数字、点、下划线、短横线；创建后内容不可修改</div>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="createVerForm.description" type="textarea" :rows="2"
            placeholder="例如：优化角色定位和工作原则" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVerVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingVer" @click="doCreateVersion">创建</el-button>
      </template>
    </el-dialog>

    <!-- 新建方案 -->
    <el-dialog v-model="createSchemeVisible" title="新建配置方案" width="440px">
      <el-form label-width="90px">
        <el-form-item label="方案名称" required>
          <el-input v-model="createSchemeForm.name" placeholder="例如：研究版本、编码版本" maxlength="60" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="createSchemeForm.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createSchemeVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingScheme" @click="doCreateScheme">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Plus, Document, Refresh, CircleCheckFilled, MoreFilled } from '@element-plus/icons-vue'
import api from '../api'
import MarkdownView from '../components/MarkdownView.vue'
import DiffView from '../components/DiffView.vue'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()

// ---- 方案层 ----
const allSchemes = ref([])
const listLoading = ref(false)
const currentType = ref(route.query.type || 'openclaw')
const currentSchemeId = ref(null)
const currentScheme = ref(null)

// ---- 版本层 ----
const versions = ref([])
const versionsLoading = ref(false)
const publishedCount = computed(() => versions.value.filter(v => v.status === 'published').length)
const viewingVersion = ref(null)      // null = 工作台

// ---- 文件 ----
const fileList = ref([])
const mdFileList = ref([])
const currentFile = ref('')
const collapsedGroups = ref({ workspace: false, runtime: false })
const fileContent = ref('')
const savedContent = ref('')
const fileLoading = ref(false)
const saving = ref(false)
const editorMode = ref('edit')

// ---- 弹窗 ----
const createVerVisible = ref(false)
const creatingVer = ref(false)
const createVerForm = ref({ version: '', description: '' })
const createSchemeVisible = ref(false)
const creatingScheme = ref(false)
const createSchemeForm = ref({ name: '', description: '' })
const diffVisible = ref(false)
const diffLoading = ref(false)
const diffData = ref(null)
const diffFile = ref('')
const publishing = ref(false)

const typeSchemes = computed(() =>
  allSchemes.value.filter(c => c.harness_type === currentType.value))
const isDirty = computed(() => !!currentFile.value && fileContent.value !== savedContent.value)
const canEdit = computed(() => authStore.isOperator && !viewingVersion.value)
const isJson = computed(() => !isMdKey(currentFile.value) && /\.json$/i.test(currentFile.value))
const isMarkdown = computed(() => isMdKey(currentFile.value) || /\.(md|markdown)$/i.test(currentFile.value))
const draftFilesPresent = computed(() => fileList.value.filter(f => f.exists).length)
const mdFilesPresent = computed(() => mdFileList.value.filter(f => f.exists).length)
const draftSummary = computed(() => {
  if (!currentScheme.value) return '未选择方案'
  return `${currentScheme.value.version_count} 个版本 · ${currentScheme.value.published_count} 个已发布`
})
const changedFiles = computed(() =>
  (diffData.value?.files || []).filter(f => !f.identical))
const diffTitle = computed(() =>
  diffData.value ? `v${diffData.value.version} 对比 ${diffData.value.base}` : '版本对比')
// 与最近发布版本的差异文件名（用于 tab 上的小圆点）
const changedFileNames = computed(() => {
  const latest = versions.value.find(v => v.status === 'published')
  if (!latest || viewingVersion.value) return []
  return []
})

function statusText(s) {
  return { draft: '草稿', testing: '测试中', published: '已发布' }[s] || s
}
function statusType(s) {
  return { draft: 'info', testing: 'warning', published: 'success' }[s] || 'info'
}
function formatTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 16)
}
function versionFileCount(v) {
  try { return JSON.parse(v.files_json || '[]').length } catch { return 0 }
}
function fileBase() {
  if (!currentScheme.value) return ''
  return viewingVersion.value
    ? `/harness-configs/${currentScheme.value.id}/versions/${viewingVersion.value.id}`
    : `/harness-configs/${currentScheme.value.id}`
}

function isMdKey(key) { return typeof key === 'string' && key.startsWith('md:') }

const treeGroups = computed(() => {
  const groups = []
  if (mdFileList.value.length) {
    groups.push({
      key: 'workspace',
      label: 'Workspace',
      items: mdFileList.value.map(f => ({
        key: `md:${f.name}`, label: f.name, exists: f.exists, kind: 'md', name: f.name,
      })),
    })
  }
  if (fileList.value.length) {
    groups.push({
      key: 'runtime',
      label: 'Runtime',
      items: fileList.value.map(f => ({
        key: f.name, label: f.name, exists: f.exists, kind: 'config', name: f.name,
      })),
    })
  }
  return groups
})

function toggleGroup(key) {
  collapsedGroups.value = { ...collapsedGroups.value, [key]: !collapsedGroups.value[key] }
}

async function onTreeClick(f) {
  if (f.exists) { await switchFile(f.key); return }
  if (!authStore.isOperator || viewingVersion.value) return
  const isMd = f.kind === 'md'
  const srcHint = isMd
    ? `settings/harness_mds/${currentType.value}/${f.name}`
    : `settings/${f.name}`
  try {
    await ElMessageBox.confirm(
      `${f.name} 尚未初始化，是否从模板 ${srcHint} 复制一份？`,
      isMd ? '初始化 md 文件' : '初始化配置文件', { type: 'info', confirmButtonText: '初始化' })
  } catch { return }
  try {
    if (isMd) {
      await api.post(`/harness-configs/${currentScheme.value.id}/md/${f.name}/init`)
      await loadFiles()
      await loadFile(`md:${f.name}`)
    } else {
      await api.post(`/harness-configs/${currentScheme.value.id}/init-file`, { filename: f.name })
      await loadFiles()
      await loadFile(f.name)
    }
    ElMessage.success(`已初始化 ${f.name}`)
  } catch { /* interceptor */ }
}

// md 保存走不同接口
async function saveMd(name, content) {
  await api.put(`/harness-configs/${currentScheme.value.id}/md/${name}`, content,
    { headers: { 'Content-Type': 'text/plain' } })
}

const dirtyGuard = async (msg = '当前文件有未保存的修改，切换后将丢失，是否继续？') => {
  if (!isDirty.value) return true
  try {
    await ElMessageBox.confirm(msg, '未保存的修改',
      { type: 'warning', confirmButtonText: '放弃修改' })
    return true
  } catch { return false }
}

async function loadSchemes() {
  listLoading.value = true
  try { allSchemes.value = await api.get('/harness-configs') }
  finally { listLoading.value = false }
}

async function selectType(t) {
  if (!(await dirtyGuard())) return
  currentType.value = t
  currentSchemeId.value = null
  router.replace({ query: { ...route.query, type: t, scheme: undefined } })
  currentScheme.value = null
  viewingVersion.value = null
  versions.value = []
  fileList.value = []
  currentFile.value = ''
  const first = typeSchemes.value[0]
  if (first) await selectScheme(first)
}

function onTypeChange(t) { selectType(t) }

async function onSchemeSelect(id) {
  const target = allSchemes.value.find(c => c.id === id)
  if (target) await selectScheme(target)
}

async function selectScheme(c) {
  if (currentScheme.value?.id === c.id) return
  if (!(await dirtyGuard())) return
  currentScheme.value = c
  currentSchemeId.value = c.id
  currentType.value = c.harness_type
  router.replace({ query: { ...route.query, type: c.harness_type, scheme: c.id } })
  viewingVersion.value = null
  currentFile.value = ''
  fileContent.value = ''
  savedContent.value = ''
  fileList.value = []
  await Promise.all([loadVersions(c.id), loadFiles()])
  const firstExisting = fileList.value.find(f => f.exists)
  if (firstExisting) await loadFile(firstExisting.name)
  else {
    const md = mdFileList.value.find(f => f.exists)
    if (md) await loadFile(`md:${md.name}`)
  }
}

async function loadVersions(schemeId) {
  versionsLoading.value = true
  try {
    versions.value = await api.get(`/harness-configs/${schemeId}/versions`)
  } catch { versions.value = [] }
  finally { versionsLoading.value = false }
}

async function loadFiles() {
  if (!currentScheme.value) return
  try {
    const res = await api.get(`${fileBase()}/files`)
    fileList.value = res.files || []
    mdFileList.value = res.md_files || []
  } catch { fileList.value = []; mdFileList.value = [] }
}

async function viewDraft() {
  if (!viewingVersion.value) return
  closeDiff()
  if (!(await dirtyGuard())) return
  viewingVersion.value = null
  await loadFiles()
  const first = fileList.value.find(f => f.exists)
  if (first) await loadFile(first.name)
  else {
    const md = mdFileList.value.find(f => f.exists)
    if (md) await loadFile(`md:${md.name}`)
    else { currentFile.value = ''; fileContent.value = ''; savedContent.value = '' }
  }
}

async function selectVersion(v) {
  if (viewingVersion.value?.id === v.id) return
  closeDiff()
  if (!(await dirtyGuard())) return
  viewingVersion.value = v
  await loadFiles()
  const first = fileList.value.find(f => f.exists)
  if (first) await loadFile(first.name)
  else {
    const md = mdFileList.value.find(f => f.exists)
    if (md) await loadFile(`md:${md.name}`)
    else { currentFile.value = ''; fileContent.value = ''; savedContent.value = '' }
  }
}

async function switchFile(fname) {
  if (fname === currentFile.value) return
  if (!(await dirtyGuard())) return
  await loadFile(fname)
}

async function loadFile(key) {
  fileLoading.value = true
  currentFile.value = key
  try {
    const url = isMdKey(key)
      ? `${fileBase()}/md/${key.slice(3)}`
      : `${fileBase()}/files/${key}`
    const res = await api.get(url)
    fileContent.value = res.content || ''
    savedContent.value = fileContent.value
  } catch {
    fileContent.value = ''
    savedContent.value = ''
    ElMessage.warning('加载文件失败')
  } finally { fileLoading.value = false }
}

async function reloadFile() {
  if (!currentFile.value) return
  if (isDirty.value) {
    try { await ElMessageBox.confirm('重新加载将丢弃当前修改，是否继续？', '确认', { type: 'warning' }) }
    catch { return }
  }
  await loadFile(currentFile.value)
}

function formatJson() {
  try {
    fileContent.value = JSON.stringify(JSON.parse(fileContent.value), null, 2)
    ElMessage.success('已格式化')
  } catch (e) { ElMessage.error(`JSON 格式错误：${e.message}`) }
}

async function saveFile() {
  if (!currentFile.value || !currentScheme.value || viewingVersion.value) return
  if (isJson.value) {
    try { JSON.parse(fileContent.value) } catch (e) {
      ElMessage.error(`JSON 格式错误，未保存：${e.message}`); return
    }
  }
  if (!fileContent.value.trim()) { ElMessage.error('文件内容为空，未保存'); return }
  saving.value = true
  try {
    if (isMdKey(currentFile.value)) {
      await saveMd(currentFile.value.slice(3), fileContent.value)
    } else {
      await api.put(`/harness-configs/${currentScheme.value.id}/files/${currentFile.value}`,
        fileContent.value, { headers: { 'Content-Type': 'text/plain' } })
    }
    savedContent.value = fileContent.value
    ElMessage.success('工作台已保存')
    await Promise.all([loadSchemes(), loadFiles()])
    const fresh = allSchemes.value.find(c => c.id === currentScheme.value.id)
    if (fresh) currentScheme.value = fresh
  } finally { saving.value = false }
}

function openCreateVersion() {
  if (!currentScheme.value) return
  const nums = versions.value
    .map(v => Number(String(v.version).replace(/^v/i, '')))
    .filter(n => !Number.isNaN(n))
  const next = nums.length ? (Math.max(...nums) + 1).toFixed(1) : '1.0'
  createVerForm.value = { version: next, description: '' }
  createVerVisible.value = true
}

async function doCreateVersion() {
  const v = (createVerForm.value.version || '').trim()
  if (!/^[A-Za-z0-9._-]{1,40}$/.test(v)) {
    ElMessage.error('版本号仅支持字母、数字、点、下划线、短横线'); return
  }
  creatingVer.value = true
  try {
    const created = await api.post(`/harness-configs/${currentScheme.value.id}/versions`, {
      version: v, description: createVerForm.value.description,
    })
    ElMessage.success(`已创建版本 v${v}`)
    createVerVisible.value = false
    await Promise.all([loadVersions(currentScheme.value.id), loadSchemes()])
    const fresh = allSchemes.value.find(c => c.id === currentScheme.value.id)
    if (fresh) currentScheme.value = fresh
    const target = versions.value.find(x => x.id === created.id)
    if (target) await selectVersion(target)
  } finally { creatingVer.value = false }
}

async function publishVersion() {
  if (!viewingVersion.value) return
  publishing.value = true
  try {
    await api.put(`/harness-configs/${currentScheme.value.id}/versions/${viewingVersion.value.id}/publish`)
    ElMessage.success(`v${viewingVersion.value.version} 已发布`)
    await loadVersions(currentScheme.value.id)
    const fresh = versions.value.find(v => v.id === viewingVersion.value.id)
    if (fresh) viewingVersion.value = fresh
    await loadSchemes()
    const s = allSchemes.value.find(c => c.id === currentScheme.value.id)
    if (s) currentScheme.value = s
  } finally { publishing.value = false }
}

async function openDiff() {
  if (!viewingVersion.value) return
  diffVisible.value = true
  diffLoading.value = true
  diffData.value = null
  try {
    diffData.value = await api.get(
      `/harness-configs/${currentScheme.value.id}/versions/${viewingVersion.value.id}/diff`,
      { params: { base: 'draft' } })
    const changed = (diffData.value.files || []).filter(f => !f.identical)
    diffFile.value = changed[0]?.file || ''
  } catch { diffData.value = null }
  finally { diffLoading.value = false }
}

async function handleVersionCmd(cmd, v) {
  // 覆盖到工作台不切换当前查看版本，直接执行
  if (cmd === 'apply') { await applyToDraft(v); return }

  // 其余操作都针对该版本，先切过去（只读查看），再执行
  if (viewingVersion.value?.id !== v.id) {
    await selectVersion(v)
    // 切换被未保存提示取消时直接放弃后续操作
    if (viewingVersion.value?.id !== v.id) return
  }
  if (cmd === 'diff') await openDiff()
  else if (cmd === 'publish') await publishVersion()
}

// 把该版本内容覆盖到工作台（不改版本历史，相当于基于它继续改）
async function applyToDraft(v) {
  try {
    await ElMessageBox.confirm(
      `将把版本 ${v.version} 的全部配置文件覆盖到工作台，工作台现有内容会被替换。\n` +
      `版本历史不受影响，覆盖后可直接改再创建新版本。`,
      '覆盖到工作台', { type: 'warning', confirmButtonText: '覆盖' })
  } catch { return }
  try {
    const res = await api.post(
      `/harness-configs/${currentScheme.value.id}/versions/${v.id}/apply-to-draft`)
    ElMessage.success(res.detail || '已覆盖到工作台')
    closeDiff()
    viewingVersion.value = null
    await Promise.all([loadFiles(), loadSchemes()])
    const fresh = allSchemes.value.find(c => c.id === currentScheme.value.id)
    if (fresh) currentScheme.value = fresh
    const first = fileList.value.find(f => f.exists)
    if (first) await loadFile(first.name)
  } catch { /* interceptor */ }
}

function closeDiff() {
  diffVisible.value = false
  diffData.value = null
}

async function openCreateScheme() {
  createSchemeForm.value = { name: '', description: '' }
  createSchemeVisible.value = true
}

async function doCreateScheme() {
  const name = (createSchemeForm.value.name || '').trim()
  if (!name) { ElMessage.error('请输入方案名称'); return }
  creatingScheme.value = true
  try {
    const created = await api.post('/harness-configs', {
      name,
      harness_type: currentType.value,
      description: createSchemeForm.value.description,
    })
    ElMessage.success('已创建方案')
    createSchemeVisible.value = false
    await loadSchemes()
    const target = allSchemes.value.find(c => c.id === created.id)
    if (target) await selectScheme(target)
  } finally { creatingScheme.value = false }
}

onMounted(async () => {
  await harnessStore.load()
  if (!harnessStore.types.includes(currentType.value)) {
    currentType.value = harnessStore.types[0] || 'openclaw'
  }
  await loadSchemes()
  // 从列表页跳进来时 URL 带 scheme=<id>，优先定位到该方案
  const wantedId = Number(route.query.scheme)
  const target = wantedId
    ? allSchemes.value.find(c => c.id === wantedId)
    : typeSchemes.value[0]
  if (target) await selectScheme(target)
})
</script>

<style scoped>
.workbench { display: flex; flex-direction: column; height: calc(100vh - 108px); }

.wb-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding-bottom: 12px; margin-bottom: 12px;
  border-bottom: 1px solid var(--border-color);
}
.wb-head-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.wb-title { display: flex; align-items: center; gap: 8px; min-width: 0; }
.wb-title-text {
  font-size: 18px; font-weight: 700; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.wb-head-actions { display: flex; gap: 8px; flex-shrink: 0; }

.wb-bar {
  display: flex; align-items: center; gap: 10px;
  padding-bottom: 12px; margin-bottom: 12px;
  border-bottom: 1px solid var(--border-color);
}
.wb-bar-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

.wb-body { display: flex; gap: 16px; flex: 1; min-height: 0; }
.wb-col { display: flex; flex-direction: column; min-height: 0; }
.wb-col-left {
  width: 280px; flex-shrink: 0; overflow-y: auto;
  padding-right: 16px; border-right: 1px solid var(--border-color);
}
.wb-col-main { flex: 1; min-width: 0; padding-right: 16px; border-right: 1px solid var(--border-color); }
.wb-col-right { width: 300px; flex-shrink: 0; overflow-y: auto; }

.col-title {
  display: flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--text-secondary);
  margin-bottom: 10px;
}
.col-num {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--accent-purple, #6366f1); color: #fff;
  font-size: 11px; font-weight: 700;
}
.loading-text { font-size: 11px; color: var(--text-muted); font-weight: 400; }
.col-hint { margin-left: auto; font-size: 11px; color: var(--text-muted); font-weight: 400; }

.tree-wrap { display: flex; flex-direction: column; gap: 6px; overflow-y: auto; }
.tree-group {}
.tree-group-head {
  display: flex; align-items: center; gap: 6px;
  padding: 4px 6px; cursor: pointer; border-radius: 4px;
  font-size: 12px; font-weight: 600; color: var(--text-secondary);
}
.tree-group-head:hover { background: var(--bg-hover); }
.tree-caret { transition: transform .15s; font-size: 12px; }
.tree-caret.collapsed { transform: rotate(-90deg); }
.tree-count { margin-left: auto; font-weight: 400; color: var(--text-muted); font-size: 11px; }
.tree-group-body { padding-left: 16px; display: flex; flex-direction: column; gap: 2px; }
.file-tree-foot {
  margin-top: auto; padding-top: 10px;
  border-top: 1px solid var(--border-color-light);
  font-size: 11px; color: var(--text-muted);
}
.version-item-wrap { display: flex; flex-direction: column; }

.type-list { display: flex; flex-direction: column; gap: 6px; }
.type-card {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border: 1px solid var(--border-color);
  border-radius: 8px; cursor: pointer; transition: all .15s;
}
.type-card:hover { background: var(--bg-hover); }
.type-card.active { border-color: var(--accent-purple, #6366f1); background: #eef2ff; }
.type-dot { width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }
.type-text { min-width: 0; }
.type-name { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.type-file { font-size: 11px; color: var(--text-muted); }

.plan-list { display: flex; flex-direction: column; gap: 4px; }
.plan-item {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border-radius: 6px; cursor: pointer; color: var(--text-primary);
}
.plan-item:hover { background: var(--bg-hover); }
.plan-item.active { background: #eef2ff; color: var(--accent-purple, #6366f1); }
.plan-icon { color: var(--text-muted); flex-shrink: 0; }
.plan-body { flex: 1; min-width: 0; }
.plan-name {
  font-size: 13px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.plan-meta { font-size: 11px; color: var(--text-muted); }

.version-list { display: flex; flex-direction: column; gap: 2px; }
.version-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 6px; cursor: pointer;
}
.version-item:hover { background: var(--bg-hover); }
.version-item.active { background: #eef2ff; }
.version-body { flex: 1; min-width: 0; }
.version-label { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.version-sub { font-size: 11px; color: var(--text-muted); }

.diff-mode { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.diff-head {
  display: flex; align-items: center; justify-content: space-between;
  padding-bottom: 8px; margin-bottom: 8px;
  border-bottom: 1px solid var(--border-color);
}
.diff-title { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.diff-tabs { flex: 1; min-height: 0; overflow: auto; }

.editor-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.dirty-flag { font-size: 12px; color: #f59e0b; }

.editor-area { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.wb-editor { flex: 1; }
.wb-editor :deep(.el-textarea__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.7; border-radius: 8px;
}
.preview-pane {
  flex: 1; overflow: auto; padding: 16px;
  background: var(--bg-hover); border-radius: 8px;
}
.preview-pane pre {
  margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.7; white-space: pre-wrap; word-break: break-word;
  color: var(--text-primary);
}

.version-card { border: 1px solid var(--border-color); border-radius: 8px; padding: 12px; }
.version-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.version-no { font-size: 15px; font-weight: 700; color: var(--text-primary); }
.version-meta { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-secondary); }
.version-meta span { display: inline-block; width: 56px; color: var(--text-muted); }
.version-desc {
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-color-light);
  font-size: 12px; color: var(--text-muted); line-height: 1.6;
}

.history-list { display: flex; flex-direction: column; gap: 4px; }
.history-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 10px; border-radius: 6px; cursor: pointer;
}
.history-item:hover { background: var(--bg-hover); }
.history-item.active { background: #eef2ff; }
.history-left { display: flex; align-items: center; gap: 8px; min-width: 0; }
.history-right { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.history-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border-color); flex-shrink: 0; }
.history-dot.on { background: #22c55e; }
.history-version { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.history-time { font-size: 11px; color: var(--text-muted); }

.empty-hint { color: var(--text-muted); font-size: 13px; text-align: center; padding: 20px 0; }
.field-hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; line-height: 1.5; }

@media (max-width: 1400px) {
  .wb-col-left { width: 240px; }
  .wb-col-right { width: 260px; }
}
</style>
