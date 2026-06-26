<template>
  <div>
    <h2 style="margin-bottom:20px">用户管理</h2>

    <el-table :data="users" v-loading="loading" stripe style="width:100%">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="username" label="用户名" min-width="150" />
      <el-table-column label="角色" width="180">
        <template #default="{row}">
          <el-select
            :model-value="row.role"
            :disabled="row.username === authStore.username"
            size="small"
            @change="val => changeRole(row, val)"
          >
            <el-option label="浏览者 (viewer)" value="viewer" />
            <el-option label="运行者 (operator)" value="operator" />
            <el-option label="管理员 (admin)" value="admin" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column prop="is_active" label="状态" width="80">
        <template #default="{row}">
          <el-tag :type="row.is_active ? 'success' : 'danger'" size="small">
            {{ row.is_active ? '正常' : '禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="160" />
      <el-table-column label="操作" width="100">
        <template #default="{row}">
          <el-button
            v-if="row.username !== authStore.username"
            size="small"
            type="danger"
            @click="deleteUser(row)"
          >删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const users = ref([])
const loading = ref(false)

async function loadUsers() {
  loading.value = true
  try {
    users.value = await api.get('/auth/users')
  } finally {
    loading.value = false
  }
}

async function changeRole(user, newRole) {
  try {
    await api.put(`/auth/users/${user.id}/role`, { role: newRole })
    ElMessage.success(`${user.username} 角色已更新为 ${newRole}`)
    loadUsers()
  } catch { /* shown by interceptor */ }
}

async function deleteUser(user) {
  await ElMessageBox.confirm(`确认删除用户 "${user.username}"？`, '提示', { type: 'warning' })
  await api.delete(`/auth/users/${user.id}`)
  ElMessage.success('已删除')
  loadUsers()
}

onMounted(loadUsers)
</script>
