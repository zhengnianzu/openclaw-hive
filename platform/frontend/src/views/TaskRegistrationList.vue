<template>
  <div>
    <div class="header-row">
      <h2>任务登记列表</h2>
    </div>

    <el-table :data="registrations" v-loading="loading" stripe style="width:100%">
      <el-table-column prop="created_at" label="登记时间" width="160" />
      <el-table-column prop="task_name" label="任务名称" min-width="160" show-overflow-tooltip />
      <el-table-column prop="requester" label="需求方" width="100" />
      <el-table-column prop="created_by" label="登记人" width="100" />
      <el-table-column prop="data_total" label="数据总量" width="90" align="center" />
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{row}">
          <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="completed_tasks" label="完成" width="70" align="center" />
      <el-table-column prop="failed_tasks" label="失败" width="70" align="center">
        <template #default="{row}">
          <span :style="{color: row.failed_tasks > 0 ? '#f56c6c' : ''}">{{ row.failed_tasks }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="export_path_obs" label="导出路径" min-width="160" show-overflow-tooltip />
      <el-table-column label="操作" width="200">
        <template #default="{row}">
          <el-button size="small" @click="showDetail(row)">详情</el-button>
          <el-button v-if="authStore.isAdmin && row.status === 'pending'" size="small" type="success" @click="executeReg(row)">执行</el-button>
          <el-button v-if="authStore.isAdmin" size="small" type="warning" @click="openEditDialog(row)">编辑</el-button>
          <el-button v-if="authStore.isAdmin" size="small" type="danger" @click="deleteReg(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 详情弹窗 -->
    <el-dialog v-model="detailVisible" title="登记详情" width="600px">
      <el-descriptions :column="1" border v-if="currentReg">
        <el-descriptions-item label="任务名称">{{ currentReg.task_name }}</el-descriptions-item>
        <el-descriptions-item label="需求方">{{ currentReg.requester }}</el-descriptions-item>
        <el-descriptions-item label="登记人">{{ currentReg.created_by }}</el-descriptions-item>
        <el-descriptions-item label="登记时间">{{ currentReg.created_at }}</el-descriptions-item>
        <el-descriptions-item label="任务路径OBS">{{ currentReg.task_path_obs }}</el-descriptions-item>
        <el-descriptions-item label="数据总量">{{ currentReg.data_total }}</el-descriptions-item>
        <el-descriptions-item label="技能目录OBS">{{ currentReg.skill_dir_obs }}</el-descriptions-item>
        <el-descriptions-item label="Agent目录OBS">{{ currentReg.agent_dir_obs }}</el-descriptions-item>
        <el-descriptions-item label="用户文件夹OBS">{{ currentReg.user_folder_obs }}</el-descriptions-item>
        <el-descriptions-item label="导出路径OBS">{{ currentReg.export_path_obs }}</el-descriptions-item>
        <el-descriptions-item label="轨迹路径">{{ currentReg.traj_path }}</el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="statusType(currentReg.status)">{{ statusLabel(currentReg.status) }}</el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="关联实例">{{ currentReg.linked_instance_id || '-' }}</el-descriptions-item>
      </el-descriptions>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog v-model="editVisible" title="编辑登记" width="500px">
      <el-form :model="editForm" label-width="120px">
        <el-form-item label="导出路径OBS">
          <el-input v-model="editForm.export_path_obs" />
        </el-form-item>
        <el-form-item label="轨迹路径">
          <el-input v-model="editForm.traj_path" />
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
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const registrations = ref([])
const loading = ref(false)

const detailVisible = ref(false)
const currentReg = ref(null)

const editVisible = ref(false)
const editForm = ref({ export_path_obs: '', traj_path: '' })
const editLoading = ref(false)
let editingId = null

function statusType(s) {
  return { pending: 'warning', executing: '', completed: 'success', cancelled: 'info' }[s] || ''
}
function statusLabel(s) {
  return { pending: '待执行', executing: '执行中', completed: '已完成', cancelled: '已取消' }[s] || s
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

function executeReg(row) {
  router.push(`/create?from_registration=${row.id}`)
}

function openEditDialog(row) {
  editingId = row.id
  editForm.value = { export_path_obs: row.export_path_obs || '', traj_path: row.traj_path || '' }
  editVisible.value = true
}

async function saveEdit() {
  editLoading.value = true
  try {
    await api.put(`/registrations/${editingId}`, editForm.value)
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

onMounted(loadRegistrations)
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: #303133; }
</style>
