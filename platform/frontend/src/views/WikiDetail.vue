<template>
  <div v-loading="loading">
    <el-page-header @back="$router.push('/wiki')">
      <template #content>
        <span class="doc-title">{{ doc.title || '文档' }}</span>
      </template>
      <template #extra>
        <div style="display:flex;gap:8px">
          <!-- 编辑只给作者本人或 admin（后端 _load_owned 同样口径）；
               发布为全站通知会推送给所有用户，仍然只有 admin 能点。 -->
          <el-button v-if="canEdit" @click="$router.push(`/wiki/${doc.slug}/edit`)">
            <el-icon><EditPen /></el-icon> 编辑
          </el-button>
          <el-button v-if="authStore.isAdmin" type="primary" @click="openPublish">
            发布为通知
          </el-button>
        </div>
      </template>
    </el-page-header>

    <div class="meta-row" v-if="doc.slug">
      <span v-if="categoryName" class="meta-item">
        <el-tag type="info" size="small">{{ categoryName }}</el-tag>
      </span>
      <el-tag v-for="t in tags" :key="t" size="small" class="tag-item">{{ t }}</el-tag>
      <span v-if="doc.linked_entity_type" class="meta-item">
        关联：<strong>{{ entityLabel(doc.linked_entity_type) }}</strong> · {{ doc.linked_entity_key }}
      </span>
      <span class="meta-item">作者 {{ doc.author }}<template v-if="doc.updated_by"> · 最后由 {{ doc.updated_by }} 更新</template></span>
      <span class="meta-item">{{ doc.updated_at }}</span>
      <el-tag v-if="doc.last_notification_id" type="success" size="small" effect="plain">
        已发布通知 #{{ doc.last_notification_id }}
      </el-tag>
    </div>

    <div class="glass-card">
      <MarkdownView v-if="doc.content_md" :content="doc.content_md" />
      <div v-else class="empty">（本文档暂无正文）</div>
    </div>

    <!-- 发布为通知 -->
    <el-dialog v-model="publishDialog" title="发布为通知" width="680px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="推送">
          <el-checkbox v-model="publishForm.push">推送给所有用户</el-checkbox>
          <div class="hint">取消勾选时仅标记为「已发布」，不产生通知。</div>
        </el-form-item>
        <template v-if="publishForm.push">
          <el-form-item label="通知标题">
            <el-input v-model="publishForm.title" :placeholder="doc.title" />
          </el-form-item>
          <el-form-item label="级别">
            <el-select v-model="publishForm.level" style="width:160px">
              <el-option label="普通 (info)" value="info" />
              <el-option label="成功 (success)" value="success" />
              <el-option label="提醒 (warning)" value="warning" />
              <el-option label="重要 (danger)" value="danger" />
            </el-select>
          </el-form-item>
          <el-form-item label="正文快照">
            <el-alert type="info" :closable="false" show-icon
              title="通知正文取自文档当前内容，发布后不再随文档编辑变化。" style="margin-bottom:8px" />
            <el-input v-model="publishForm.content_md" type="textarea" :rows="10" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="publishDialog = false">取消</el-button>
        <el-button type="primary" :loading="publishing" @click="doPublish">
          {{ publishForm.push ? '发布并推送' : '仅标记发布' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import MarkdownView from '../components/MarkdownView.vue'
import { useAuthStore } from '../stores/auth'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const authStore = useAuthStore()
const notificationStore = useNotificationStore()

const doc = ref({})
const categories = ref([])
const loading = ref(false)

// 作者本人或 admin 才能编辑，与后端 _load_owned 口径一致。
const canEdit = computed(
  () => authStore.isAdmin || (!!doc.value.author && doc.value.author === authStore.username))

const publishDialog = ref(false)
const publishing = ref(false)
const publishForm = ref({ push: true, title: '', level: 'info', content_md: '' })

const ENTITY_LABELS = {
  image: '镜像',
  code_repo: '代码仓',
  harness_config: 'Harness',
  sandbox_config: '沙箱',
  resource_pool: '资源池',
  config_template: '配置模板',
}
const entityLabel = (t) => ENTITY_LABELS[t] || t
const tags = computed(() => (doc.value.tags ? doc.value.tags.split(',').map((t) => t.trim()).filter(Boolean) : []))
const categoryName = computed(
  () => categories.value.find((c) => c.id === doc.value.category_id)?.name || '',
)

async function load() {
  loading.value = true
  try {
    doc.value = await api.get(`/wiki/documents/${route.params.slug}`)
  } finally {
    loading.value = false
  }
}

async function loadCategories() {
  try { categories.value = await api.get('/wiki/categories') } catch { categories.value = [] }
}

function openPublish() {
  publishForm.value = {
    push: true,
    title: doc.value.title,
    level: 'info',
    content_md: doc.value.content_md,
  }
  publishDialog.value = true
}

async function doPublish() {
  publishing.value = true
  try {
    const res = await api.post(`/wiki/documents/${doc.value.slug}/publish`, publishForm.value)
    publishDialog.value = false
    if (res.notification_id) {
      ElMessage.success(`已发布并推送通知 #${res.notification_id}`)
      doc.value.last_notification_id = res.notification_id
      notificationStore.fetchUnread()
    } else {
      ElMessage.success('已标记发布（未推送）')
    }
  } finally {
    publishing.value = false
  }
}

onMounted(() => {
  load()
  loadCategories()
})

// 同一组件内从一个文档跳到另一个文档时不会重新挂载，必须监听参数
watch(() => route.params.slug, (s) => { if (s) load() })
</script>

<style scoped>
.doc-title {
  font-size: 20px;
  font-weight: 700;
}
.meta-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 16px 0;
  font-size: 12px;
  color: var(--text-muted);
}
.tag-item {
  margin-right: -4px;
}
.empty {
  color: var(--text-muted);
  font-style: italic;
}
.hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-left: 8px;
}
</style>
