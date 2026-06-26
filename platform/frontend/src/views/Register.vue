<template>
  <div class="login-container">
    <div class="login-card">
      <h2>注册账号</h2>
      <el-form :model="form" @submit.prevent="handleSubmit" label-width="0">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名（至少2个字符）" prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码（至少4个字符）" prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.confirmPassword" type="password" placeholder="确认密码" prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" style="width:100%" :loading="loading" @click="handleSubmit">
            注册
          </el-button>
        </el-form-item>
      </el-form>
      <div class="toggle" @click="router.push('/login')">已有账号？去登录</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const form = ref({ username: '', password: '', confirmPassword: '' })
const loading = ref(false)

async function handleSubmit() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请填写完整信息')
    return
  }
  if (form.value.password !== form.value.confirmPassword) {
    ElMessage.warning('两次密码不一致')
    return
  }
  loading.value = true
  try {
    const res = await api.post('/auth/register', {
      username: form.value.username,
      password: form.value.password,
    })
    authStore.setAuth(res)
    ElMessage.success('注册成功')
    router.push('/dashboard')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  background: #fff; padding: 40px; border-radius: 12px; width: 400px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.3);
}
.login-card h2 { text-align: center; margin-bottom: 30px; color: #333; }
.toggle { text-align: center; color: #409eff; cursor: pointer; font-size: 14px; }
</style>
