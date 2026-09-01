<template>
  <div>
    <div class="header-row">
      <h2>配置模板管理</h2>
      <el-button type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon> 新建模板
      </el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="templates" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="name" label="模板名称" min-width="160" />
        <el-table-column prop="harness_type" label="Harness类型" width="120">
          <template #default="{row}">
            <el-tag :color="harnessColor(row.harness_type)" :style="{borderColor: harnessColor(row.harness_type)}" effect="dark" size="small">{{ harnessLabel(row.harness_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="默认" width="80" align="center">
          <template #default="{row}">
            <el-tag v-if="row.is_default" type="success" size="small">默认</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="model_base_url" label="Base URL" min-width="200" show-overflow-tooltip />
        <el-table-column prop="invite_code" label="Invite Code" width="120" />
        <el-table-column prop="updated_at" label="更新时间" width="160" />
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" @click="openEditDialog(row)">编辑</el-button>
              <el-button size="small" type="success" v-if="!row.is_default" @click="setDefault(row)">设默认</el-button>
              <el-button size="small" type="danger" @click="deleteTemplate(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" :title="isEditing ? '编辑模板' : '新建模板'" width="700px" destroy-on-close>
      <el-form :model="form" label-width="130px">
        <el-form-item label="模板名称" required>
          <el-input v-model="form.name" placeholder="例如：默认配置" />
        </el-form-item>
        <el-form-item label="Harness类型">
          <el-select v-model="form.harness_type" style="width:100%">
            <el-option label="OpenClaw" value="openclaw" />
            <el-option label="Hermes" value="hermes" />
            <el-option label="Claude Code" value="claude-code" />
            <el-option label="Jiuwen Claw" value="openjiuwen" />
            <el-option label="OpenCode" value="opencode" />
            <el-option label="Codex" value="codex" />
            <el-option label="Pi" value="pi" />
            <el-option label="Grok" value="grok" />
            <el-option label="DSH" value="dsh" />
            <el-option label="通用" value="common" />
          </el-select>
        </el-form-item>

        <el-tabs v-model="activeTab" style="margin-top:8px">
          <el-tab-pane label="Harness 配置" name="harness">
            <el-form-item label="模型 Base URL">
              <el-input v-model="form.model_base_url" :placeholder="['opencode','pi','codex','grok'].includes(form.harness_type) ? '例如：http://192.168.30.95:8084（自动补 /v1）' : '例如：http://192.168.30.95:8084'" />
            </el-form-item>
            <el-form-item label="Invite Code">
              <el-input v-model="form.invite_code" placeholder="pangu" />
            </el-form-item>
            <el-form-item v-if="['openclaw','opencode','pi','dsh'].includes(form.harness_type)" label="API 类型">
              <el-select v-model="form.model_api_type" clearable style="width:100%">
                <el-option label="Anthropic Messages" value="anthropic-messages" />
                <el-option label="OpenAI Completions" value="openai-completions" />
              </el-select>
            </el-form-item>
            <el-form-item label="模型 ID">
              <el-input v-model="form.model_id" placeholder="例如：claude-opus-4-7-thinking" />
            </el-form-item>
          </el-tab-pane>

          <el-tab-pane label="用户模拟配置" name="agents">
            <div class="agent-tabs-header">
              <el-button size="small" text type="primary" @click="addAgent"><el-icon><Plus /></el-icon></el-button>
            </div>
            <el-tabs v-model="activeAgentTab" type="card" @tab-remove="removeAgentTab">
              <el-tab-pane v-for="(ag, idx) in agents" :key="idx" :label="ag.name || '未命名'" :name="String(idx)" :closable="idx !== 0">
                <el-form-item label="Agent 名称">
                  <el-input v-model="ag.name" placeholder="例如：user_simulator" />
                </el-form-item>
                <el-form-item label="Base URL">
                  <el-input v-model="ag.base_url" placeholder="例如：http://192.168.30.95:8084" />
                </el-form-item>
                <el-form-item label="API Key">
                  <div style="display:flex;gap:8px;width:100%">
                    <el-input v-model="ag.api_key" placeholder="留空则无" style="flex:1" show-password />
                    <el-button type="primary" @click="openKeyDialog(idx)">新建KEY</el-button>
                  </div>
                  <div v-if="ag.api_key" style="font-size:12px;color:#999;margin-top:4px">
                    {{ ag.api_key.length > 8 ? ag.api_key.slice(0, 4) + '****' + ag.api_key.slice(-4) : '' }}
                  </div>
                </el-form-item>
                <el-form-item label="Provider">
                  <el-input v-model="ag.provider" placeholder="例如：local-evaluator" />
                </el-form-item>
                <el-form-item label="API 类型">
                  <el-select v-model="ag.api" clearable style="width:100%">
                    <el-option label="OpenAI Completions" value="openai-completions" />
                    <el-option label="Anthropic Messages" value="anthropic-messages" />
                  </el-select>
                </el-form-item>
                <el-form-item label="模型 ID">
                  <el-input v-model="ag.model" placeholder="例如：gemini-3-flash-preview" />
                </el-form-item>
              </el-tab-pane>
            </el-tabs>
          </el-tab-pane>

          <el-tab-pane label="高级配置" name="advanced">
            <el-form-item label="镜像名称">
              <el-select v-model="form.image_name" filterable allow-create default-first-option
                placeholder="选择或输入镜像地址" style="width:100%" clearable>
                <el-option v-for="img in imageList" :key="img.id" :label="img.name" :value="img.address">
                  <span>{{ img.name }}</span>
                  <span style="float:right;color:#999;font-size:12px">{{ img.address.length > 40 ? '...' + img.address.slice(-40) : img.address }}</span>
                </el-option>
              </el-select>
            </el-form-item>
            <el-form-item label="代码仓">
              <el-select v-model="form.code_repo_id" placeholder="选择代码仓（可选）" style="width:100%" clearable>
                <el-option v-for="repo in codeRepoList" :key="repo.id" :label="`${repo.name} / ${repo.version}`" :value="repo.id" />
              </el-select>
            </el-form-item>
          </el-tab-pane>
        </el-tabs>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存</el-button>
      </template>
    </el-dialog>
    <el-dialog v-model="keyDialogVisible" title="新建 API Key" width="480px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="Invite Code">
          <el-input v-model="keyForm.invite_code" placeholder="pangu" />
        </el-form-item>
        <el-form-item label="Name">
          <el-input v-model="keyForm.name" placeholder="用途标识" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="generateApiKey" :loading="keyGenerating">生成</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '../api'

const templates = ref([])
const loading = ref(false)
const dialogVisible = ref(false)
const isEditing = ref(false)
const saving = ref(false)
const activeTab = ref('harness')
const activeAgentTab = ref('0')
let editingId = null

const imageList = ref([])
const codeRepoList = ref([])

const keyDialogVisible = ref(false)
const keyGenerating = ref(false)
const keyForm = ref({ invite_code: 'pangu', name: '' })
let keyTargetAgentIdx = -1

function openKeyDialog(agentIdx) {
  keyTargetAgentIdx = agentIdx
  const ag = agents.value[agentIdx]
  keyForm.value.invite_code = form.value.invite_code || 'pangu'
  keyForm.value.name = `${ag.name || 'agent'}-${form.value.name || 'template'}`
  keyDialogVisible.value = true
}

async function generateApiKey() {
  keyGenerating.value = true
  try {
    const baseUrl = agents.value[keyTargetAgentIdx]?.base_url || form.value.model_base_url
    const params = {
      invite_code: keyForm.value.invite_code,
      name: keyForm.value.name,
    }
    if (baseUrl) params.base_url = baseUrl
    const res = await api.post('/generate-api-key', null, { params })
    agents.value[keyTargetAgentIdx].api_key = res.api_key
    ElMessage.success(`API Key 已生成: ${res.api_key.slice(0, 4)}****${res.api_key.slice(-4)}`)
    keyDialogVisible.value = false
  } catch {
  } finally {
    keyGenerating.value = false
  }
}

const defaultForm = () => ({
  name: '默认配置',
  harness_type: 'openclaw',
  model_base_url: '',
  invite_code: 'pangu',
  model_api_type: '',
  model_id: '',
  image_name: '',
  code_repo_id: null,
})
const form = ref(defaultForm())
const agents = ref([{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }])

function harnessTagType(type) {
  return { openclaw: 'primary', hermes: 'warning', 'claude-code': 'success', openjiuwen: 'danger', opencode: 'info', codex: 'warning', pi: 'success', grok: 'success', dsh: 'primary', common: 'info' }[type] || ''
}
const HARNESS_COLORS = {
  openclaw: '#409eff', hermes: '#e6a23c', 'claude-code': '#67c23a',
  openjiuwen: '#f56c6c', opencode: '#909399',
  codex: '#8e44ad', pi: '#17a2b8', grok: '#00d084', dsh: '#0A3D91',
  common: '#c0c4cc',
}
function harnessColor(type) { return HARNESS_COLORS[type] || '#909399' }
function harnessLabel(type) {
  return { openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', grok: 'Grok', dsh: 'DSH', common: '通用' }[type] || type
}

function addAgent() {
  agents.value.push({ name: '', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' })
  activeAgentTab.value = String(agents.value.length - 1)
}

function removeAgentTab(tabName) {
  const idx = parseInt(tabName)
  if (idx === 0) return
  agents.value.splice(idx, 1)
  if (parseInt(activeAgentTab.value) >= agents.value.length) {
    activeAgentTab.value = String(agents.value.length - 1)
  }
}

async function loadTemplates() {
  loading.value = true
  try {
    templates.value = await api.get('/config-templates')
  } finally {
    loading.value = false
  }
}

async function loadImages() {
  try { imageList.value = await api.get('/images') } catch { imageList.value = [] }
}
async function loadCodeRepos() {
  try { codeRepoList.value = await api.get('/code-repos') } catch { codeRepoList.value = [] }
}

function openCreateDialog() {
  isEditing.value = false
  editingId = null
  form.value = defaultForm()
  agents.value = [{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }]
  activeTab.value = 'harness'
  activeAgentTab.value = '0'
  dialogVisible.value = true
}

function openEditDialog(row) {
  isEditing.value = true
  editingId = row.id
  form.value = {
    name: row.name,
    harness_type: row.harness_type,
    model_base_url: row.model_base_url || '',
    invite_code: row.invite_code || 'pangu',
    model_api_type: row.model_api_type || '',
    model_id: row.model_id || '',
    image_name: row.image_name || '',
    code_repo_id: row.code_repo_id || null,
  }
  try {
    const parsed = JSON.parse(row.agents_json || '[]')
    agents.value = parsed.length ? parsed : [{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }]
  } catch {
    agents.value = [{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }]
  }
  activeTab.value = 'harness'
  activeAgentTab.value = '0'
  dialogVisible.value = true
}

async function saveTemplate() {
  if (!form.value.name) {
    ElMessage.warning('请输入模板名称')
    return
  }
  saving.value = true
  try {
    const payload = { ...form.value, agents_json: JSON.stringify(agents.value) }
    if (isEditing.value) {
      await api.put(`/config-templates/${editingId}`, payload)
      ElMessage.success('模板已更新')
    } else {
      await api.post('/config-templates', payload)
      ElMessage.success('模板已创建')
    }
    dialogVisible.value = false
    loadTemplates()
  } finally {
    saving.value = false
  }
}

async function setDefault(row) {
  await api.put(`/config-templates/${row.id}/set-default`)
  ElMessage.success('已设为默认模板')
  loadTemplates()
}

async function deleteTemplate(row) {
  await ElMessageBox.confirm('确认删除该模板？', '提示', { type: 'warning' })
  await api.delete(`/config-templates/${row.id}`)
  ElMessage.success('已删除')
  loadTemplates()
}

onMounted(() => {
  loadTemplates()
  loadImages()
  loadCodeRepos()
})
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
.agent-tabs-header { float: right; margin-top: 2px; }
</style>
