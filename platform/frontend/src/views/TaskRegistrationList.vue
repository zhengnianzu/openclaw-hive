<template>
  <div>
    <div class="header-row">
      <h2>任务登记列表</h2>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font-size:13px;color:#606266;white-space:nowrap">高级配置</span>
        <el-select v-model="selectedTemplateId" placeholder="选择配置模板" clearable style="width:200px" size="default">
          <el-option v-for="tpl in templateList" :key="tpl.id"
            :label="tpl.name + (tpl.is_default ? ' (默认)' : '')" :value="tpl.id" />
          <template #footer>
            <el-button text type="primary" style="width:100%" @click="router.push('/config-templates')">
              <el-icon><Plus /></el-icon> 新增模板
            </el-button>
          </template>
        </el-select>
        <el-button type="primary" @click="router.push('/task-register')">
          <el-icon><Plus /></el-icon> 新建
        </el-button>
      </div>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
    <el-table :data="registrations" v-loading="loading" stripe style="width:100%" border>
      <el-table-column prop="created_at" label="登记时间" width="160" resizable />
      <el-table-column prop="task_name" label="任务名称" min-width="160" show-overflow-tooltip resizable />
      <el-table-column prop="requester" label="需求方" width="100" resizable />
      <el-table-column prop="created_by" label="登记人" width="100" resizable />
      <el-table-column prop="harness_type" label="Harness" width="100" resizable>
        <template #default="{row}">
          <el-tag :color="harnessColor(row.harness_type)" :style="{borderColor: harnessColor(row.harness_type)}" effect="dark" size="small">{{ harnessLabel(row.harness_type) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="model_name" label="Harness模型" min-width="140" show-overflow-tooltip resizable />
      <el-table-column prop="data_total" label="数据总量" width="90" align="center" resizable />
      <el-table-column label="配置模板" width="120" resizable>
        <template #default="{row}">
          <span>{{ templateName(row.config_template_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="90" resizable>
        <template #default="{row}">
          <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="completed_tasks" label="完成" width="70" align="center" resizable />
      <el-table-column prop="failed_tasks" label="失败" width="70" align="center" resizable>
        <template #default="{row}">
          <span :style="{color: row.failed_tasks > 0 ? '#f56c6c' : ''}">{{ row.failed_tasks }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="export_path_obs" label="导出路径" min-width="160" show-overflow-tooltip resizable />
      <el-table-column label="操作" width="220" fixed="right">
        <template #default="{row}">
          <div style="display:flex;align-items:center;gap:4px">
            <el-button size="small" @click="showDetail(row)">详情</el-button>
            <el-button v-if="authStore.isAdmin" size="small" type="warning" @click="openEditDialog(row)">编辑</el-button>
            <el-dropdown v-if="authStore.isAdmin" trigger="click" @command="cmd => handleCommand(cmd, row)">
              <el-button size="small">更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="copy">复制</el-dropdown-item>
                  <el-dropdown-item v-if="row.status === 'pending'" command="execute">执行</el-dropdown-item>
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
    <el-dialog v-model="detailVisible" title="登记详情" width="650px">
      <el-descriptions :column="1" border v-if="currentReg">
        <el-descriptions-item label="任务名称">{{ currentReg.task_name }}</el-descriptions-item>
        <el-descriptions-item label="需求方">{{ currentReg.requester }}</el-descriptions-item>
        <el-descriptions-item label="Harness类型">
          <el-tag :color="harnessColor(currentReg.harness_type)" :style="{borderColor: harnessColor(currentReg.harness_type)}" effect="dark" size="small">{{ harnessLabel(currentReg.harness_type) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Harness模型">{{ currentReg.model_name }}</el-descriptions-item>
        <el-descriptions-item label="用户模拟模型">{{ currentReg.eval_model_name }}</el-descriptions-item>
        <el-descriptions-item label="Evaluator模型">{{ currentReg.eval_config_model || '-' }}</el-descriptions-item>
        <el-descriptions-item label="任务供应商URL">{{ currentReg.base_url || '-' }}</el-descriptions-item>
        <el-descriptions-item label="任务供应商KEY">{{ currentReg.api_key || '-' }}</el-descriptions-item>
        <el-descriptions-item label="登记人">{{ currentReg.created_by }}</el-descriptions-item>
        <el-descriptions-item label="登记时间">{{ currentReg.created_at }}</el-descriptions-item>
        <el-descriptions-item label="任务路径OBS">{{ currentReg.task_path_obs }}</el-descriptions-item>
        <el-descriptions-item label="数据总量">{{ currentReg.data_total }}</el-descriptions-item>
        <el-descriptions-item label="技能目录OBS">{{ currentReg.skill_dir_obs }}</el-descriptions-item>
        <el-descriptions-item label="默认技能">{{ currentReg.default_skills || '-' }}</el-descriptions-item>
        <el-descriptions-item label="Agent目录OBS">{{ currentReg.agent_dir_obs }}</el-descriptions-item>
        <el-descriptions-item label="用户文件夹OBS">{{ currentReg.user_folder_obs }}</el-descriptions-item>
        <el-descriptions-item label="导出路径OBS">{{ currentReg.export_path_obs }}</el-descriptions-item>
        <el-descriptions-item label="轨迹路径">{{ currentReg.traj_path }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusType(currentReg.status)">{{ statusLabel(currentReg.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="关联实例">{{ currentReg.linked_instance_id || '-' }}</el-descriptions-item>
        <el-descriptions-item label="配置模板">{{ templateName(currentReg.config_template_id) }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑登记" width="550px">
      <el-form :model="editForm" label-width="140px">
        <el-form-item label="Harness类型">
          <el-select v-model="editForm.harness_type" style="width:100%">
            <el-option label="Openclaw" value="openclaw" />
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
        <el-form-item label="Harness模型">
          <el-input v-model="editForm.model_name" />
        </el-form-item>
        <el-form-item label="用户模拟模型">
          <el-input v-model="editForm.eval_model_name" />
        </el-form-item>
        <el-form-item label="Evaluator模型">
          <el-input v-model="editForm.eval_config_model" placeholder="选填" />
        </el-form-item>
        <el-form-item label="任务路径OBS">
          <el-input v-model="editForm.task_path_obs" />
        </el-form-item>
        <el-form-item label="技能目录OBS">
          <el-input v-model="editForm.skill_dir_obs" />
        </el-form-item>
        <el-form-item label="默认技能">
          <el-input v-model="editForm.default_skills" placeholder="逗号分隔" />
        </el-form-item>
        <el-form-item label="Agent目录OBS">
          <el-input v-model="editForm.agent_dir_obs" />
        </el-form-item>
        <el-form-item label="用户文件夹OBS">
          <el-input v-model="editForm.user_folder_obs" />
        </el-form-item>
        <el-form-item label="任务供应商URL">
          <el-input v-model="editForm.base_url" placeholder="例如：http://192.168.30.95:8084" />
        </el-form-item>
        <el-form-item label="任务供应商KEY">
          <el-input v-model="editForm.api_key" placeholder="留空则不修改" show-password />
        </el-form-item>
        <el-form-item label="导出路径OBS">
          <el-input v-model="editForm.export_path_obs" />
        </el-form-item>
        <el-form-item label="轨迹路径">
          <el-input v-model="editForm.traj_path" />
        </el-form-item>
        <el-form-item label="配置模板">
          <el-select v-model="editForm.config_template_id" placeholder="不绑定模板" clearable style="width:100%">
            <el-option v-for="tpl in templateList" :key="tpl.id" :label="tpl.name + (tpl.is_default ? ' (默认)' : '')" :value="tpl.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, ArrowDown } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const registrations = ref([])
const loading = ref(false)

const detailVisible = ref(false)
const currentReg = ref(null)

const editVisible = ref(false)
const editForm = ref({ export_path_obs: '', traj_path: '', model_name: '', eval_model_name: '', eval_config_model: '', user_proxy_model_name: '', harness_type: 'openclaw', base_url: '', api_key: '', task_path_obs: '', skill_dir_obs: '', agent_dir_obs: '', user_folder_obs: '', default_skills: '', config_template_id: null })
const editLoading = ref(false)
let editingId = null
const templateList = ref([])
const selectedTemplateId = ref(null)

function statusType(s) {
  return { pending: 'warning', executing: '', completed: 'success', cancelled: 'info' }[s] || ''
}
function statusLabel(s) {
  return { pending: '待执行', executing: '执行中', completed: '已完成', cancelled: '已取消' }[s] || s
}
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
  return { openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', grok: 'Grok', dsh: 'DSH', common: '通用' }[type] || type || 'openclaw'
}
function templateName(id) {
  if (!id) return '-'
  const tpl = templateList.value.find(t => t.id === id)
  return tpl ? tpl.name : '-'
}

async function loadRegistrations() {
  loading.value = true
  try {
    registrations.value = await api.get('/registrations')
  } finally {
    loading.value = false
  }
}

function showDetail(row) {
  currentReg.value = row
  detailVisible.value = true
}

function openEditDialog(row) {
  editingId = row.id
  editForm.value = {
    export_path_obs: row.export_path_obs || '',
    traj_path: row.traj_path || '',
    model_name: row.model_name || '',
    eval_model_name: row.eval_model_name || '',
    eval_config_model: row.eval_config_model || '',
    user_proxy_model_name: row.user_proxy_model_name || '',
    harness_type: row.harness_type || 'openclaw',
    base_url: row.base_url || '',
    api_key: '',
    task_path_obs: row.task_path_obs || '',
    skill_dir_obs: row.skill_dir_obs || '',
    agent_dir_obs: row.agent_dir_obs || '',
    user_folder_obs: row.user_folder_obs || '',
    default_skills: row.default_skills || '',
    config_template_id: row.config_template_id || null,
  }
  editVisible.value = true
}

async function saveEdit() {
  editLoading.value = true
  try {
    const payload = { ...editForm.value }
    if (!payload.api_key) delete payload.api_key
    await api.put(`/registrations/${editingId}`, payload)
    ElMessage.success('保存成功')
    editVisible.value = false
    loadRegistrations()
  } finally {
    editLoading.value = false
  }
}

async function deleteReg(row) {
  await ElMessageBox.confirm('确认删除该登记？', '提示', { type: 'warning' })
  await api.delete(`/registrations/${row.id}`)
  ElMessage.success('已删除')
  loadRegistrations()
}

function handleCommand(cmd, row) {
  const actions = {
    copy: () => router.push(`/task-register?copy_from=${row.id}`),
    execute: () => {
      let url = `/create?from_registration=${row.id}`
      const tplId = row.config_template_id || selectedTemplateId.value
      if (tplId) url += `&template_id=${tplId}`
      router.push(url)
    },
    delete: () => deleteReg(row),
  }
  actions[cmd]?.()
}

async function loadTemplates() {
  try {
    templateList.value = await api.get('/config-templates')
    const def = templateList.value.find(t => t.is_default)
    if (def) selectedTemplateId.value = def.id
  } catch { templateList.value = [] }
}

onMounted(() => { loadRegistrations(); loadTemplates() })
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
</style>
