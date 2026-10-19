<template>
  <el-badge :value="store.unread" :hidden="!store.unread" :max="99" class="notif-badge">
    <el-button text class="notif-btn" @click="$router.push('/notifications')" title="通知中心">
      <el-icon :size="18"><Bell /></el-icon>
    </el-button>
  </el-badge>
</template>

<script setup>
import { onMounted } from 'vue'
import { useNotificationStore } from '../stores/notification'

const store = useNotificationStore()

// 轮询由 Layout 统一启停；这里只在挂载时补一次，避免刷新页面时角标滞后 60s
onMounted(() => store.fetchUnread())
</script>

<style scoped>
.notif-btn {
  padding: 6px;
  color: var(--text-secondary);
}
.notif-btn:hover {
  color: var(--accent-purple);
  background: var(--bg-hover);
}
:deep(.el-badge__content) {
  background: var(--danger);
  border: none;
}
</style>
