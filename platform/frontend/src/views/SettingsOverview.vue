<template>
  <div>
    <div class="header-row">
      <h2>系统设置</h2>
      <span class="subtitle">当前可配置的全部选项；按你的角色过滤可见入口</span>
    </div>

    <div v-loading="loading">
      <div v-for="group in groups" :key="group.name" class="group">
        <h3 class="group-title">{{ group.name }}</h3>
        <el-row :gutter="16">
          <el-col v-for="item in group.items" :key="item.key" :xs="24" :sm="12" :md="8" :lg="6">
            <div class="setting-card glass-card" @click="go(item)">
              <div class="card-top">
                <span class="card-title">{{ item.title }}</span>
                <el-tag size="small" type="info" effect="plain">{{ item.count }}</el-tag>
              </div>
              <div class="card-desc">{{ item.desc }}</div>
              <div class="card-foot">
                <el-tag size="small" :type="roleTagType(item.role)" effect="plain">{{ roleLabel(item.role) }}</el-tag>
                <span class="go">进入 →</span>
              </div>
            </div>
          </el-col>
        </el-row>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const items = ref([])
const loading = ref(false)
const myRole = ref('viewer')

const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 }
const ROLE_LABELS = { user: '所有用户', operator: '运行者以上', admin: '仅管理员' }
const roleLabel = (r) => ROLE_LABELS[r] || r
const roleTagType = (r) => ({ user: 'success', operator: 'warning', admin: 'danger' }[r] || 'info')

const groups = computed(() => {
  const visible = items.value.filter((i) => {
    if (!i.role || i.role === 'user') return true
    return (ROLE_RANK[myRole.value] ?? 0) >= (ROLE_RANK[i.role] ?? 0)
  })
  const order = []
  const map = new Map()
  visible.forEach((i) => {
    if (!map.has(i.group)) {
      map.set(i.group, [])
      order.push(i.group)
    }
    map.get(i.group).push(i)
  })
  return order.map((name) => ({ name, items: map.get(name) }))
})

function go(item) {
  router.push(item.route)
}

onMounted(async () => {
  loading.value = true
  try {
    const res = await api.get('/settings/overview')
    items.value = res.items || []
    myRole.value = res.role || 'viewer'
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.header-row {
  display: flex;
  align-items: baseline;
  gap: 16px;
  margin-bottom: 24px;
}
.header-row h2 {
  font-size: 24px;
  font-weight: 700;
}
.subtitle {
  font-size: 13px;
  color: var(--text-muted);
}
.group {
  margin-bottom: 28px;
}
.group-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 12px;
  padding-left: 4px;
}
.setting-card {
  padding: 18px 20px;
  margin-bottom: 16px;
  height: 150px;
  display: flex;
  flex-direction: column;
  cursor: pointer;
  transition: box-shadow 0.2s ease, transform 0.2s ease;
}
.setting-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
.card-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.card-title {
  font-size: 15px;
  font-weight: 600;
}
.card-desc {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.6;
  flex: 1;
  overflow: hidden;
}
.card-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 8px;
}
.go {
  font-size: 12px;
  color: var(--accent-purple);
  font-weight: 500;
}
</style>
