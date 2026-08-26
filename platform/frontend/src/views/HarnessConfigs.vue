<template>
  <div>
    <div class="header-row">
      <h2>Harness 配置管理</h2>
      <el-button type="primary" v-if="authStore.isOperator" @click="openCreateDialog">
        <el-icon><Plus /></el-icon> 新建
      </el-button>
    </div>

    <div style="margin-bottom:16px">
      <el-select v-model="typeFilter" placeholder="全部类型" clearable style="width:160px" @change="loadList">
        <el-option label="OpenClaw" value="openclaw" />
        <el-option label="Hermes" value="hermes" />
        <el-option label="Claude Code" value="claude-code" />
        <el-option label="Jiuwen Claw" value="openjiuwen" />
        <el-option label="OpenCode" value="opencode" />
        <el-option label="Codex" value="codex" />
        <el-option label="Pi" value="pi" />
        <el-option label="通用" value="common" />
      </el-select>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="configs" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="harness_type" label="类型" width="120">
          <template #default="{row}">
            <el-tag :color="harnessColor(row.harness_type)" effect="dark" size="small">{{ typeLabel(row.harness_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="version" label="版本" width="80" />
        <el-table-column prop="description" label="描述" min-width="160" show-overflow-tooltip />
        <el-table-column label="文件数" width="80" align="center">
          <template #default="{row}">{{ fileCount(row) }}</template>
        </el-table-column>
        <el-table-column prop="updated_at" label="更新时间" width="160" />
        <el-table-column label="操作" width="200" fixed="right" v-if="authStore.isOperator">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" type="primary" @click="openFileManager(row)">文件管理</el-button>
              <el-dropdown trigger="click" @command="cmd => handleMore(cmd, row)">
                <el-button size="small">更多 ▾</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item command="copy">复制</el-dropdown-item>
                    <el-dropdown-item v-if="!row.is_default" command="setDefault">设为默认</el-dropdown-item>
                    <el-dropdown-item v-if="row.version !== '默认'" command="delete" divided style="color:#f56c6c">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新建/编辑弹窗 -->
    <el-dialog v-model="editVisible" :title="isEditing ? '编辑配置' : '新建 Harness 配置'" width="560px" destroy-on-close>
      <el-form :model="editForm" label-width="140px">
        <el-form-item label="Harness 类型" v-if="!isEditing">
          <el-select v-model="editForm.harness_type" style="width:100%">
            <el-option label="OpenClaw" value="openclaw" />
            <el-option label="Hermes" value="hermes" />
            <el-option label="Claude Code" value="claude-code" />
            <el-option label="Jiuwen Claw" value="openjiuwen" />
            <el-option label="OpenCode" value="opencode" />
            <el-option label="Codex" value="codex" />
            <el-option label="Pi" value="pi" />
            <el-option label="通用" value="common" />
          </el-select>
        </el-form-item>
        <el-form-item label="版本" required>
          <el-input v-model="editForm.version" placeholder="例如：v1、xy" />
          <div v-if="!isEditing" style="font-size:12px;color:#999;margin-top:4px">
            配置名称自动生成为 {{ editForm.harness_type }}_{{ editForm.version || '?' }}
          </div>
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
        <div style="font-size:12px;color:#999;padding:0 0 8px 140px">填写后创建时自动从 OBS 拉取；也可以创建后在文件管理中手动拉取或从模板初始化</div>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

    <!-- 文件管理弹窗 -->
    <el-dialog v-model="fileVisible" title="文件管理" width="900px" destroy-on-close>
      <div style="display:flex;gap:16px;min-height:500px">
        <!-- 左侧文件列表 -->
        <div style="width:220px;flex-shrink:0;border-right:1px solid #eee;padding-right:12px">
          <div style="margin-bottom:8px;font-weight:600;font-size:13px;color:#606266">配置文件</div>
          <div v-for="f in fileList" :key="f.name" class="file-item-wrap">
            <div :class="['file-item', { active: currentFile === f.name, disabled: !f.exists }]"
              @click="f.exists && loadFile(f.name)">
              <div style="display:flex;align-items:center;gap:4px">
                <el-tag :type="f.type === 'yaml' ? 'warning' : 'primary'" size="small">{{ f.type }}</el-tag>
                <span style="font-size:13px" :style="{ color: f.exists ? '' : '#c0c4cc' }">{{ f.name }}</span>
              </div>
              <el-tag v-if="!f.exists" size="small" type="info" style="margin-top:2px">未初始化</el-tag>
            </div>
            <div class="file-actions">
              <el-button size="small" text type="primary" @click="initFile(f.name)">
                {{ f.exists ? '重新初始化' : '从模板初始化' }}
              </el-button>
              <el-button size="small" text type="warning" v-if="f.obs_path" @click="pullObsFile(f.category)">
                OBS拉取
              </el-button>
            </div>
          </div>
        </div>
        <!-- 右侧编辑区 -->
        <div style="flex:1;display:flex;flex-direction:column">
          <div v-if="currentFile" style="margin-bottom:8px;display:flex;justify-content:space-between;align-items:center">
            <span style="font-weight:600">{{ currentFile }}</span>
            <el-button type="primary" size="small" @click="saveCurrentFile" :loading="fileSaving">保存</el-button>
          </div>
          <el-input v-if="currentFile" v-model="fileContent" type="textarea"
            :autosize="{ minRows: 20, maxRows: 30 }"
            style="font-family:monospace;font-size:13px" />
          <div v-else style="color:#999;text-align:center;padding-top:100px">
            选择左侧文件进行编辑
          </div>
          <!-- 字段映射提示 -->
          <div v-if="currentFile && mappingHints.length" style="margin-top:12px;padding:10px;background:#f5f7fa;border-radius:6px;font-size:12px">
            <div style="font-weight:600;margin-bottom:4px;color:#606266">创建任务时以下字段会被覆盖：</div>
            <div v-for="h in mappingHints" :key="h.field" style="color:#909399">
              <code>{{ h.path }}</code> ← 表单字段 <code>{{ h.field }}</code>
            </div>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { joinBucket } from '../api/obsConfig'

const authStore = useAuthStore()
const configs = ref([])
const loading = ref(false)
const typeFilter = ref('')

const editVisible = ref(false)
const isEditing = ref(false)
const saving = ref(false)
let editingId = null
const editForm = ref({
  harness_type: 'openclaw', version: 'v1', description: '',
  obs_harness_path: '', obs_task_path: '', obs_proxy_path: '',
})

const fileVisible = ref(false)
const fileList = ref([])
const currentFile = ref('')
const fileContent = ref('')
const fileSaving = ref(false)
let fileConfigId = null

const fieldMappings = ref({})

const HARNESS_FILE_LABELS = {
  openclaw: 'Harness (openclaw.json)',
  hermes: 'Harness (hermes_config)',
  'claude-code': 'Harness (cc_settings)',
  openjiuwen: 'Harness (openjiuwen.json)',
  opencode: 'Harness (opencode.json)',
  codex: 'Harness (config.toml)',
  pi: 'Harness (models.json)',
  common: 'Harness 配置',
}

function harnessFileLabel(t) {
  return HARNESS_FILE_LABELS[t] || 'Harness 配置'
}

function tagType(t) {
  return { openclaw: 'primary', hermes: 'warning', 'claude-code': 'success', openjiuwen: 'danger', opencode: 'info', codex: 'warning', pi: 'success', common: 'info' }[t] || ''
}
// harness 类型 -> 显示色(hex)。后续加 harness 只需在这里追加一行。
const HARNESS_COLORS = {
  openclaw: '#409eff', hermes: '#e6a23c', 'claude-code': '#67c23a',
  openjiuwen: '#f56c6c', opencode: '#909399',
  codex: '#8e44ad', pi: '#17a2b8',
  common: '#c0c4cc',
}
function harnessColor(t) { return HARNESS_COLORS[t] || '#909399' }
function typeLabel(t) {
  return { openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', common: '通用' }[t] || t
}
function fileCount(row) {
  try { return JSON.parse(row.config_files_json || '[]').length } catch { return 0 }
}

const mappingHints = ref([])

function computeMappingHints() {
  if (!currentFile.value || !fieldMappings.value) { mappingHints.value = []; return }
  const hints = []
  const fname = currentFile.value
  let section = null
  if (fname === 'config.yaml') section = fieldMappings.value['config.yaml']
  else if (fname === 'openjiuwen.json') section = fieldMappings.value['openjiuwen']
  else if (fname === 'openclaw.json') section = fieldMappings.value['openclaw']
  else if (fname === 'opencode.json') section = fieldMappings.value['opencode']
  else if (fname === 'config.toml') section = fieldMappings.value['codex']
  else if (fname === 'models.json') section = fieldMappings.value['pi']
  else if (fname.includes('hermes')) section = fieldMappings.value['hermes']
  else if (fname.includes('cc_settings')) section = fieldMappings.value['claude-code']
  else if (fname.includes('user_proxy')) section = fieldMappings.value['user_proxy_model']

  if (section) {
    for (const [field, paths] of Object.entries(section)) {
      if (field.startsWith('_')) continue
      if (typeof paths === 'string') {
        hints.push({ field, path: paths })
      } else if (Array.isArray(paths)) {
        for (const p of paths) {
          hints.push({ field, path: typeof p === 'string' ? p : p.path })
        }
      } else if (paths && paths.path) {
        hints.push({ field, path: paths.path })
      }
    }
  }
  mappingHints.value = hints
}

async function loadList() {
  loading.value = true
  try {
    const params = typeFilter.value ? { harness_type: typeFilter.value } : {}
    configs.value = await api.get('/harness-configs', { params })
  } finally { loading.value = false }
}

async function loadMappings() {
  try { fieldMappings.value = await api.get('/harness-configs/field-mappings') } catch { fieldMappings.value = {} }
}

function openCreateDialog() {
  isEditing.value = false
  editingId = null
  editForm.value = {
    harness_type: 'openclaw', version: 'v1', description: '',
    obs_harness_path: '', obs_task_path: '', obs_proxy_path: '',
  }
  editVisible.value = true
}

function openEditDialog(row) {
  isEditing.value = true
  editingId = row.id
  editForm.value = {
    harness_type: row.harness_type,
    version: row.version || 'v1',
    description: row.description || '',
    obs_harness_path: row.obs_harness_path || '',
    obs_task_path: row.obs_task_path || '',
    obs_proxy_path: row.obs_proxy_path || '',
  }
  editVisible.value = true
}

async function saveConfig() {
  if (!editForm.value.version) { ElMessage.warning('请输入版本号'); return }
  saving.value = true
  try {
    if (isEditing.value) {
      await api.put(`/harness-configs/${editingId}`, editForm.value)
      ElMessage.success('已更新')
    } else {
      await api.post('/harness-configs', editForm.value)
      ElMessage.success('已创建')
    }
    editVisible.value = false
    loadList()
  } finally { saving.value = false }
}

async function setDefault(row) {
  await api.put(`/harness-configs/${row.id}/set-default`)
  ElMessage.success('已设为默认')
  loadList()
}

async function deleteConfig(row) {
  await ElMessageBox.confirm(`确认删除配置「${row.name}」？同时删除本地配置文件`, '提示', { type: 'warning' })
  await api.delete(`/harness-configs/${row.id}`)
  ElMessage.success('已删除')
  loadList()
}

function handleMore(cmd, row) {
  if (cmd === 'edit') openEditDialog(row)
  else if (cmd === 'copy') copyConfig(row)
  else if (cmd === 'setDefault') setDefault(row)
  else if (cmd === 'delete') deleteConfig(row)
}

async function copyConfig(row) {
  let version
  try {
    const { value } = await ElMessageBox.prompt('请输入新版本号', '复制配置', {
      inputValue: row.version + '_copy',
      inputPattern: /\S+/,
      inputErrorMessage: '版本号不能为空',
      confirmButtonText: '复制',
    })
    version = value
  } catch { return }
  try {
    await api.post(`/harness-configs/${row.id}/copy`, { version })
    ElMessage.success('已复制')
    loadList()
  } catch { /* api interceptor shows error */ }
}

async function openFileManager(row) {
  fileConfigId = row.id
  currentFile.value = ''
  fileContent.value = ''
  mappingHints.value = []
  fileVisible.value = true
  try {
    const res = await api.get(`/harness-configs/${row.id}/files`)
    fileList.value = res.files || []
  } catch { fileList.value = [] }
}

async function loadFile(fname) {
  currentFile.value = fname
  try {
    const res = await api.get(`/harness-configs/${fileConfigId}/files/${fname}`)
    fileContent.value = res.content || ''
  } catch {
    fileContent.value = ''
    ElMessage.warning('加载文件失败')
  }
  computeMappingHints()
}

async function saveCurrentFile() {
  if (!currentFile.value) return
  fileSaving.value = true
  try {
    await api.put(`/harness-configs/${fileConfigId}/files/${currentFile.value}`, fileContent.value, {
      headers: { 'Content-Type': 'text/plain' },
    })
    ElMessage.success('已保存')
    await refreshFileList()
  } finally { fileSaving.value = false }
}

async function initFile(fname) {
  const existing = fileList.value.find(f => f.name === fname)
  if (existing && existing.exists) {
    try {
      await ElMessageBox.confirm(`文件 ${fname} 已存在，从模板重新初始化将覆盖当前内容，是否继续？`, '确认', { type: 'warning' })
    } catch { return }
  }
  try {
    await api.post(`/harness-configs/${fileConfigId}/init-file`, { filename: fname })
    ElMessage.success(`已从模板初始化 ${fname}`)
    await refreshFileList()
    loadFile(fname)
  } catch { /* api interceptor shows error */ }
}

async function pullObsFile(fileType) {
  try {
    await ElMessageBox.confirm('从 OBS 拉取将覆盖本地文件，是否继续？', 'OBS 拉取', { type: 'warning' })
  } catch { return }
  try {
    await api.post(`/harness-configs/${fileConfigId}/pull-obs`, { file_type: fileType })
    ElMessage.success('已从 OBS 拉取')
    await refreshFileList()
    if (currentFile.value) {
      const cur = fileList.value.find(f => f.name === currentFile.value)
      if (cur && cur.exists) loadFile(currentFile.value)
    }
  } catch { /* api interceptor shows error */ }
}

async function refreshFileList() {
  try {
    const res = await api.get(`/harness-configs/${fileConfigId}/files`)
    fileList.value = res.files || []
  } catch { /* ignore */ }
}

onMounted(() => { loadList(); loadMappings() })
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
.file-item-wrap {
  margin-bottom: 8px;
  border-bottom: 1px solid #f0f0f0;
  padding-bottom: 6px;
}
.file-item {
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.file-item:hover { background: #f5f7fa; }
.file-item.active { background: #ecf5ff; }
.file-item.disabled { cursor: default; }
.file-item.disabled:hover { background: transparent; }
.file-actions {
  display: flex;
  gap: 0;
  padding: 0 4px;
}
</style>
