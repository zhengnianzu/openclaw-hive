<template>
  <el-container style="min-height: 100vh">
    <el-aside width="220px" style="background: #304156">
      <div class="logo">Hive Platform</div>
      <el-menu :default-active="route.path" router background-color="#304156" text-color="#bfcbd9" active-text-color="#409eff">
        <el-menu-item index="/dashboard">
          <el-icon><Monitor /></el-icon>
          <span>任务管理</span>
        </el-menu-item>
        <el-menu-item index="/create">
          <el-icon><Plus /></el-icon>
          <span>新建任务</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background:#fff;display:flex;align-items:center;justify-content:flex-end;border-bottom:1px solid #e6e6e6">
        <span style="margin-right:16px;color:#666">{{ username }}</span>
        <el-button text @click="logout">退出登录</el-button>
      </el-header>
      <el-main style="padding:20px">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { useRoute, useRouter } from 'vue-router'
const route = useRoute()
const router = useRouter()
const username = localStorage.getItem('username') || ''
function logout() {
  localStorage.removeItem('token')
  localStorage.removeItem('username')
  router.push('/login')
}
</script>

<style scoped>
.logo {
  height: 60px; display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 18px; font-weight: bold; border-bottom: 1px solid rgba(255,255,255,0.1);
}
</style>
