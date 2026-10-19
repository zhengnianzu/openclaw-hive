import { defineStore } from 'pinia'
import { ref } from 'vue'
import api from '../api'

const POLL_INTERVAL = 60_000

export const useNotificationStore = defineStore('notification', () => {
  const unread = ref(0)
  const items = ref([])
  const total = ref(0)
  const loading = ref(false)

  let timer = null
  let onVisibility = null

  async function fetchUnread() {
    try {
      // silent：登出瞬间的 401 不应弹全局错误、也不该触发跳转抖动
      const res = await api.get('/notifications/unread-count', { silent: true })
      unread.value = res.unread || 0
    } catch {
      unread.value = 0
    }
  }

  async function fetchList({ unreadOnly = false, page = 1, pageSize = 20 } = {}) {
    loading.value = true
    try {
      const res = await api.get('/notifications', {
        params: { unread_only: unreadOnly, page, page_size: pageSize },
        silent: true,
      })
      items.value = res.items || []
      total.value = res.total || 0
      unread.value = res.unread || 0
      return res
    } finally {
      loading.value = false
    }
  }

  async function markRead(id) {
    const res = await api.post(`/notifications/${id}/read`)
    unread.value = res.unread || 0
    const row = items.value.find((n) => n.id === id)
    if (row) row.is_read = 1
  }

  async function markAllRead() {
    const res = await api.post('/notifications/read-all')
    unread.value = res.unread || 0
    items.value.forEach((n) => { n.is_read = 1 })
    return res
  }

  function startPolling() {
    if (timer) return
    fetchUnread()
    timer = setInterval(fetchUnread, POLL_INTERVAL)
    // 标签页从后台切回来时立即刷新一次，不用等下一个 60s
    onVisibility = () => {
      if (document.visibilityState === 'visible') fetchUnread()
    }
    document.addEventListener('visibilitychange', onVisibility)
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    if (onVisibility) {
      document.removeEventListener('visibilitychange', onVisibility)
      onVisibility = null
    }
  }

  return {
    unread, items, total, loading,
    fetchUnread, fetchList, markRead, markAllRead, startPolling, stopPolling,
  }
})
