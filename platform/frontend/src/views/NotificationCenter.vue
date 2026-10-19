<template>
  <div>
    <div class="header-row">
      <h2>通知中心</h2>
      <el-button v-if="unread > 0" @click="markAll">全部标为已读</el-button>
    </div>

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <el-tab-pane name="inbox">
        <template #label>
          <span>我的通知</span>
          <el-badge v-if="unread" :value="unread" :max="99" style="margin-left:6px" />
        </template>

        <div class="filter-bar">
          <el-radio-group v-model="unreadOnly" @change="loadInbox">
            <el-radio-button :value="false">全部</el-radio-button>
            <el-radio-button :value="true">未读</el-radio-button>
          </el-radio-group>
        </div>

        <div v-loading="store.loading">
          <el-empty v-if="!store.items.length" description="暂无通知" />
          <div
            v-for="n in store.items"
            :key="n.id"
            class="notif-card glass-card"
            :class="['level-' + n.level, { unread: !n.is_read }]"
            @click="openNotification(n)"
          >
            <div class="notif-head">
              <el-tag size="small" :type="levelTagType(n.level)" effect="plain">{{ levelLabel(n.level) }}</el-tag>
              <span class="notif-title">{{ n.title }}</span>
              <el-tag v-if="!n.is_read" type="danger" size="small" effect="dark" class="unread-dot">未读</el-tag>
              <span class="notif-time">{{ n.created_at }}</span>
            </div>
            <div v-if="n.content_md" class="notif-body">
              <MarkdownView :content="preview(n.content_md)" />
            </div>
            <div class="notif-foot">
              <span class="muted">发布人 {{ n.created_by }}</span>
              <el-link v-if="n.doc_slug" type="primary" :underline="false" @click.stop="$router.push(`/wiki/${n.doc_slug}`)">
                查看文档 →
              </el-link>
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="authStore.isAdmin" label="我发出的" name="sent">
        <div v-loading="sentLoading">
          <el-empty v-if="!sentItems.length" description="还没有发布过通知" />
          <el-table v-else :data="sentItems" stripe border style="width:100%">
            <el-table-column prop="title" label="标题" min-width="240" />
            <el-table-column label="级别" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="levelTagType(row.level)" effect="plain">{{ levelLabel(row.level) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="已读" width="120" align="center">
              <template #default="{ row }">
                {{ row.read_count }} / {{ row.total_users }}
              </template>
            </el-table-column>
            <el-table-column label="文档" width="100" align="center">
              <template #default="{ row }">
                <el-link v-if="row.doc_slug" type="primary" :underline="false" @click="$router.push(`/wiki/${row.doc_slug}`)">#{{ row.id }}</el-link>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="发布时间" width="180" />
            <el-table-column label="操作" width="120" align="center">
              <template #default="{ row }">
                <el-button size="small" type="danger" text @click="revoke(row)">撤回</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '../api'
import MarkdownView from '../components/MarkdownView.vue'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'

const router = useRouter()
const authStore = useAuthStore()
const store = useNotificationStore()

const activeTab = ref('inbox')
const unreadOnly = ref(false)
const unread = ref(0)

const sentItems = ref([])
const sentLoading = ref(false)

const LEVEL_LABELS = { info: '公告', success: '更新', warning: '提醒', danger: '重要' }
const levelLabel = (l) => LEVEL_LABELS[l] || '公告'
const levelTagType = (l) => ({ info: 'info', success: 'success', warning: 'warning', danger: 'danger' }[l] || 'info')

// 列表里只给开头一段，点文档看全文
function preview(md) {
  const s = (md || '').trim()
  return s.length > 600 ? s.slice(0, 600) + '\n\n…' : s
}

async function loadInbox() {
  const res = await store.fetchList({ unreadOnly: unreadOnly.value, page: 1, pageSize: 100 })
  unread.value = res?.unread ?? store.unread
}

async function onTabChange(name) {
  if (name === 'sent' && authStore.isAdmin) await loadSent()
  else await loadInbox()
}

async function openNotification(n) {
  if (!n.is_read) {
    try {
      await store.markRead(n.id)
      unread.value = store.unread
    } catch { /* 已读失败不影响查看 */ }
  }
  if (n.doc_slug) router.push(`/wiki/${n.doc_slug}`)
}

async function markAll() {
  try {
    await store.markAllRead()
    unread.value = 0
    ElMessage.success('已全部标为已读')
    if (activeTab.value === 'inbox') loadInbox()
  } catch { /* 全局拦截器已提示 */ }
}

async function loadSent() {
  sentLoading.value = true
  try {
    sentItems.value = await api.get('/notifications/sent', { params: { page: 1, page_size: 200 } })
  } finally {
    sentLoading.value = false
  }
}

async function revoke(row) {
  try {
    await ElMessageBox.confirm(`撤回「${row.title}」？所有用户的列表里都会消失。`, '撤回通知', {
      type: 'warning', confirmButtonText: '撤回', cancelButtonText: '取消',
    })
  } catch { return }
  await api.delete(`/notifications/${row.id}`)
  ElMessage.success('已撤回')
  loadSent()
  if (authStore.username) store.fetchUnread()
}

onMounted(async () => {
  await loadInbox()
  unread.value = store.unread
})
</script>

<style scoped>
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}
.header-row h2 {
  font-size: 24px;
  font-weight: 700;
}
.filter-bar {
  margin-bottom: 16px;
}
.notif-card {
  margin-bottom: 12px;
  padding: 16px 20px;
  cursor: pointer;
  transition: box-shadow 0.2s ease, border-color 0.2s ease;
  border-left: 3px solid var(--border-color);
}
.notif-card:hover {
  box-shadow: var(--shadow-md);
}
.notif-card.unread {
  background: #fafbff;
}
.notif-card.level-success { border-left-color: var(--success); }
.notif-card.level-warning { border-left-color: var(--warning); }
.notif-card.level-danger  { border-left-color: var(--danger); }
.notif-card.level-info    { border-left-color: var(--accent-purple); }

.notif-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.notif-title {
  font-size: 15px;
  font-weight: 600;
  flex: 1;
}
.notif-time {
  font-size: 12px;
  color: var(--text-muted);
}
.notif-body {
  font-size: 13px;
  max-height: 220px;
  overflow: hidden;
  position: relative;
}
.notif-body::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 32px;
  background: linear-gradient(transparent, var(--bg-card));
}
.notif-card.unread .notif-body::after {
  background: linear-gradient(transparent, #fafbff);
}
.notif-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 10px;
  font-size: 12px;
}
.muted {
  color: var(--text-muted);
}
</style>
