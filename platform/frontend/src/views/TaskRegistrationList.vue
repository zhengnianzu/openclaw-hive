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
      <el-table-column prop="created_at" label="创建时间" width="160" resizable />
      <el-table-column prop="task_name" label="任务名称" min-width="160" show-overflow-tooltip resizable />
      <el-table-column prop="created_by" label="创建人" width="100" resizable />
      <el-table-column prop="task_path_obs" label="用户Config目录" min-width="220"
        show-overflow-tooltip resizable />
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
    <el-dialog v-model="detailVisible" title="数据集详情" width="650px">
      <el-descriptions :column="1" border v-if="currentReg">
        <el-descriptions-item label="任务名称">{{ currentReg.task_name }}</el-descriptions-item>
        <el-descriptions-item label="创建人">{{ currentReg.created_by }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ currentReg.created_at }}</el-descriptions-item>
        <el-descriptions-item label="技能目录">{{ currentReg.skill_dir_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="默认技能">{{ currentReg.default_skills || '-' }}</el-descriptions-item>
        <el-descriptions-item label="Agent 目录">{{ currentReg.agent_dir_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="用户Config目录">{{ currentReg.task_path_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="用户Profile目录">{{ currentReg.user_folder_obs || '-' }}</el-descriptions-item>
        <el-descriptions-item label="轨迹保存路径">{{ currentReg.traj_save_path || '-' }}</el-descriptions-item>
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
import { useHarnessStore } from '../stores/harness'

const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const registrations = ref([])
const loading = ref(false)

const detailVisible = ref(false)
const currentReg = ref(null)

const editVisible = ref(false)
const editForm = ref({ task_name: '', skill_dir_obs: '', default_skills: '', agent_dir_obs: '', task_path_obs: '', user_folder_obs: '', traj_save_path: '' })
const editLoading = ref(false)
let editingId = null

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
    task_name: row.task_name || '',
    skill_dir_obs: row.skill_dir_obs || '',
    default_skills: row.default_skills || '',
    agent_dir_obs: row.agent_dir_obs || '',
    task_path_obs: row.task_path_obs || '',
    user_folder_obs: row.user_folder_obs || '',
    traj_save_path: row.traj_save_path || '',
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

async function deleteReg(row) {
  await ElMessageBox.confirm('确认删除该数据集？', '提示', { type: 'warning' })
  await api.delete(`/registrations/${row.id}`)
  ElMessage.success('已删除')
  loadRegistrations()
}

function handleCommand(cmd, row) {
  const actions = {
    copy: () => router.push(`/task-register?copy_from=${row.id}`),
    execute: () => {
      router.push(`/create?from_registration=${row.id}`)
    },
    delete: () => deleteReg(row),
  }
  actions[cmd]?.()
}

onMounted(() => { harnessStore.load(); loadRegistrations() })
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
</style>
