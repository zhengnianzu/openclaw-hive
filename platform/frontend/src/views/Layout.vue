<template>
  <el-container style="min-height: 100vh">
    <el-aside width="220px" style="background: #304156">
      <div class="logo">Hive Platform</div>
      <el-menu :default-active="route.path" router background-color="#304156" text-color="#bfcbd9" active-text-color="#409eff">
        <el-menu-item index="/dashboard">
          <el-icon><Monitor /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/registrations">
          <el-icon><Document /></el-icon>
          <span>任务登记</span>
        </el-menu-item>
        <el-menu-item index="/task-register">
          <el-icon><EditPen /></el-icon>
          <span>提交登记</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/create">
          <el-icon><Plus /></el-icon>
          <span>新建任务</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isAdmin" index="/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background:#fff;display:flex;align-items:center;justify-content:flex-end;border-bottom:1px solid #e6e6e6">
        <el-tag :type="roleTagType" size="small" style="margin-right:8px">{{ roleLabel }}</el-tag>
        <span style="margin-right:16px;color:#666">{{ authStore.username }}</span>
        <el-button text @click="logout">退出登录</el-button>
      </el-header>
      <el-main style="padding:20px;overflow-x:hidden">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const roleLabel = computed(() => {
  const map = { admin: '管理员', operator: '运行者', viewer: '浏览者' }
  return map[authStore.role] || '浏览者'
})
const roleTagType = computed(() => {
  const map = { admin: 'danger', operator: 'warning', viewer: 'info' }
  return map[authStore.role] || 'info'
})

onMounted(() => {
  if (authStore.token) authStore.fetchMe()
})

function logout() {
  authStore.clearAuth()
  router.push('/login')
}
</script>

<style scoped>
.logo {
  height: 60px; display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 18px; font-weight: bold; border-bottom: 1px solid rgba(255,255,255,0.1);
}
</style>
