<template>
  <el-container style="min-height: 100vh">
    <el-aside width="240px" class="sidebar">
      <div class="logo">
        <span class="logo-text">Hive Platform</span>
      </div>
      <el-menu :default-active="activeMenu" router class="sidebar-menu">
        <el-menu-item index="/dashboard">
          <el-icon><Monitor /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/analytics">
          <el-icon><DataAnalysis /></el-icon>
          <span>资源看板</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/queues">
          <el-icon><List /></el-icon>
          <span>队列管理</span>
        </el-menu-item>
        <el-menu-item index="/registrations">
          <el-icon><Document /></el-icon>
          <span>任务登记</span>
        </el-menu-item>
        <el-menu-item index="/wiki">
          <el-icon><Reading /></el-icon>
          <span>文档中心</span>
        </el-menu-item>
        <el-menu-item index="/config-templates">
          <el-icon><Setting /></el-icon>
          <span>配置模板</span>
        </el-menu-item>
        <el-menu-item index="/harness-configs">
          <el-icon><Files /></el-icon>
          <span>Harness配置</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/sandbox-configs">
          <el-icon><Grid /></el-icon>
          <span>沙箱配置</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/images">
          <el-icon><Box /></el-icon>
          <span>镜像管理</span>
        </el-menu-item>
        <el-menu-item v-if="authStore.isOperator" index="/code-repos">
          <el-icon><FolderOpened /></el-icon>
          <span>代码管理</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Tools /></el-icon>
          <span>系统设置</span>
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
          <NotificationBell />
          <span class="role-badge" :class="'role-' + authStore.role">{{ roleLabel }}</span>
          <span class="username">{{ authStore.username }}</span>
          <el-button text class="logout-btn" @click="pwdVisible = true">改密码</el-button>
          <el-button text class="logout-btn" @click="logout">退出</el-button>
        </div>
      </el-header>
      <el-main class="main-content">
        <router-view />
      </el-main>
    </el-container>

    <el-dialog v-model="pwdVisible" title="修改密码" width="420px" destroy-on-close>
      <el-form :model="pwdForm" label-width="80px" @submit.prevent>
        <el-form-item label="原密码" required>
          <el-input v-model="pwdForm.old_password" type="password" show-password />
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input v-model="pwdForm.new_password" type="password" placeholder="至少4个字符" show-password />
        </el-form-item>
        <el-form-item label="确认" required>
          <el-input v-model="pwdForm.confirm" type="password" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" @click="submitPwd" :loading="pwdLoading">确定</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import { useNotificationStore } from '../stores/notification'
import NotificationBell from '../components/NotificationBell.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const notificationStore = useNotificationStore()

const roleLabel = computed(() => {
  const map = { admin: '管理员', operator: '运行者', viewer: '浏览者' }
  return map[authStore.role] || '浏览者'
})

// 侧栏高亮：详情页（/wiki/3、/instance/xxx）要让父菜单项亮起来。
// 原先直接用 route.path，子路径时整条菜单都不高亮。
const activeMenu = computed(() => {
  const p = route.path
  if (p.startsWith('/harness')) return '/harness-configs'
  if (p.startsWith('/wiki')) return '/wiki'
  if (p.startsWith('/instance') || p.startsWith('/logs') || p.startsWith('/outputs')) return '/dashboard'
  if (p.startsWith('/task-register')) return '/registrations'
  return p
})

onMounted(() => {
  if (authStore.token) authStore.fetchMe()
  harnessStore.load()
  notificationStore.startPolling()
})

onUnmounted(() => {
  notificationStore.stopPolling()
})

function logout() {
  authStore.clearAuth()
  router.push('/login')
}

const pwdVisible = ref(false)
const pwdLoading = ref(false)
const pwdForm = ref({ old_password: '', new_password: '', confirm: '' })

async function submitPwd() {
  const f = pwdForm.value
  if (!f.old_password || !f.new_password) {
    ElMessage.warning('请填写完整')
    return
  }
  if (f.new_password !== f.confirm) {
    ElMessage.warning('两次新密码不一致')
    return
  }
  pwdLoading.value = true
  try {
    // 后端回新 token：改密码不会让已签发的 token 失效，但换上新的，
    // 当前会话的有效期就从这一刻重新计算。
    const res = await api.put('/auth/me/password', {
      old_password: f.old_password,
      new_password: f.new_password,
    })
    if (res.access_token) authStore.setAuth(res)
    ElMessage.success('密码已修改')
    pwdVisible.value = false
    pwdForm.value = { old_password: '', new_password: '', confirm: '' }
  } catch { /* shown by interceptor */ } finally {
    pwdLoading.value = false
  }
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
