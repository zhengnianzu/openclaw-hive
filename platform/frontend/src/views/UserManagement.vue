<template>
  <div>
    <h2 style="margin-bottom:20px;color:var(--text-primary);font-size:24px;font-weight:700">用户管理</h2>

    <div style="margin-bottom:16px">
      <el-button type="primary" @click="dialogVisible = true">
        <el-icon><Plus /></el-icon> 新增用户
      </el-button>
      <span style="margin-left:12px;color:var(--text-muted);font-size:13px">
        自助注册已关闭，新账号在此创建
      </span>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
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
      <el-table-column label="操作" width="180">
        <template #default="{row}">
          <el-button size="small" @click="openReset(row)">重置密码</el-button>
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

    <el-dialog v-model="dialogVisible" title="新增用户" width="480px" destroy-on-close>
      <el-form :model="form" label-width="90px">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" placeholder="至少2个字符" />
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input v-model="form.password" type="password" placeholder="至少4个字符" show-password />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="form.role" style="width:100%">
            <el-option label="浏览者 (viewer)" value="viewer" />
            <el-option label="运行者 (operator)" value="operator" />
            <el-option label="管理员 (admin)" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleCreate" :loading="creating">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resetVisible" :title="`重置 ${resetTarget?.username ?? ''} 的密码`"
               width="420px" destroy-on-close>
      <el-form :model="resetForm" label-width="80px">
        <el-form-item label="新密码" required>
          <el-input v-model="resetForm.new_password" type="password" placeholder="至少4个字符" show-password />
        </el-form-item>
        <el-form-item label="确认" required>
          <el-input v-model="resetForm.confirm" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" @click="submitReset" :loading="resetting">重置</el-button>
      </template>
    </el-dialog>
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
const dialogVisible = ref(false)
const creating = ref(false)
const form = ref({ username: '', password: '', role: 'viewer' })

async function handleCreate() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  creating.value = true
  try {
    await api.post('/auth/users', form.value)
    ElMessage.success(`用户 ${form.value.username} 已创建`)
    dialogVisible.value = false
    form.value = { username: '', password: '', role: 'viewer' }
    loadUsers()
  } catch { /* shown by interceptor */ } finally {
    creating.value = false
  }
}

const resetVisible = ref(false)
const resetting = ref(false)
const resetTarget = ref(null)
const resetForm = ref({ new_password: '', confirm: '' })

function openReset(row) {
  resetTarget.value = row
  resetForm.value = { new_password: '', confirm: '' }
  resetVisible.value = true
}

async function submitReset() {
  const f = resetForm.value
  if (!f.new_password) {
    ElMessage.warning('请填写新密码')
    return
  }
  if (f.new_password !== f.confirm) {
    ElMessage.warning('两次密码不一致')
    return
  }
  resetting.value = true
  try {
    await api.put(`/auth/users/${resetTarget.value.id}/password`, { new_password: f.new_password })
    ElMessage.success(`已重置 ${resetTarget.value.username} 的密码`)
    resetVisible.value = false
  } catch { /* shown by interceptor */ } finally {
    resetting.value = false
  }
}

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
