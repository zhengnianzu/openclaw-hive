<template>
  <div class="login-container">
    <div class="login-card">
      <h2>OpenClaw Hive</h2>
      <p class="subtitle">Smart Task Management Platform</p>
      <el-form :model="form" @submit.prevent="handleSubmit" label-width="0">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" size="large" style="width:100%;height:44px;font-size:15px" :loading="loading" @click="handleSubmit">
            登录
          </el-button>
        </el-form-item>
      </el-form>
      <div class="toggle" @click="router.push('/register')">没有账号？去注册</div>
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
const form = ref({ username: '', password: '' })
const loading = ref(false)

async function handleSubmit() {
  if (!form.value.username || !form.value.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const res = await api.post('/auth/login', form.value)
    authStore.setAuth(res)
    router.push('/dashboard')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  position: relative;
}
.login-card {
  background: #fff;
  padding: 48px 40px;
  border-radius: 20px;
  width: 420px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.15);
}
.login-card h2 {
  text-align: center;
  margin-bottom: 4px;
  font-size: 28px;
  font-weight: 700;
  color: var(--text-primary);
}
.subtitle {
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
  margin-bottom: 32px;
}
.toggle {
  text-align: center;
  color: var(--accent-purple);
  cursor: pointer;
  font-size: 14px;
  transition: color 0.2s;
}
.toggle:hover {
  color: #4f46e5;
}
</style>
