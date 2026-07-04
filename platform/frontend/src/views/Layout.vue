<template>
  <el-container style="min-height: 100vh">
    <el-aside width="240px" class="sidebar">
      <div class="logo">
        <span class="logo-text">Hive Platform</span>
      </div>
      <el-menu :default-active="route.path" router class="sidebar-menu">
        <el-menu-item index="/dashboard">
          <el-icon><Monitor /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/registrations">
          <el-icon><Document /></el-icon>
          <span>任务登记</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/images">
          <el-icon><Box /></el-icon>
          <span>镜像管理</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/code-repos">
          <el-icon><FolderOpened /></el-icon>
          <span>代码管理</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isAdmin" index="/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <div></div>
        <div style="display:flex;align-items:center;gap:12px">
          <span class="role-badge" :class="'role-' + authStore.role">{{ roleLabel }}</span>
          <span class="username">{{ authStore.username }}</span>
          <el-button text class="logout-btn" @click="logout">退出</el-button>
        </div>
      </el-header>
      <el-main class="main-content">
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

onMounted(() => {
  if (authStore.token) authStore.fetchMe()
})

function logout() {
  authStore.clearAuth()
  router.push('/login')
}
</script>

<style scoped>
.sidebar {
  background: var(--bg-sidebar);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}

.logo {
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid rgba(255,255,255,0.08);
}

.logo-text {
  font-size: 20px;
  font-weight: 700;
  color: #fff;
  letter-spacing: -0.02em;
}

.sidebar-menu {
  border-right: none !important;
  background: transparent !important;
  padding: 12px 8px;
}

.sidebar-menu .el-menu-item {
  height: 44px;
  line-height: 44px;
  border-radius: var(--radius-sm);
  margin-bottom: 4px;
  color: rgba(255,255,255,0.5);
  font-weight: 500;
  font-size: 14px;
  transition: all 0.2s ease;
}

.sidebar-menu .el-menu-item:hover {
  background: rgba(255, 255, 255, 0.08) !important;
  color: rgba(255,255,255,0.85);
}

.sidebar-menu .el-menu-item.is-active {
  background: rgba(99, 102, 241, 0.2) !important;
  color: #fff !important;
}

.topbar {
  background: #fff;
  border-bottom: 1px solid var(--border-color);
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 60px;
  padding: 0 24px;
}

.role-badge {
  font-size: 12px;
  padding: 3px 10px;
  border-radius: 20px;
  font-weight: 600;
}
.role-admin {
  background: #fef2f2;
  color: #dc2626;
}
.role-operator {
  background: #fffbeb;
  color: #d97706;
}
.role-viewer {
  background: #eef2ff;
  color: #4f46e5;
}

.username {
  color: var(--text-secondary);
  font-size: 14px;
  font-weight: 500;
}

.logout-btn {
  color: var(--text-muted) !important;
  font-size: 13px;
}
.logout-btn:hover {
  color: var(--danger) !important;
}

.main-content {
  background: var(--bg-primary);
  padding: 24px;
  overflow-x: hidden;
}
</style>
