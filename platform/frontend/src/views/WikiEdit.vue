<template>
  <div v-loading="loading">
    <div class="header-row">
      <h2>{{ isEdit ? '编辑文档' : '新建文档' }}</h2>
      <div>
        <el-button @click="cancel">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </div>
    </div>

    <div class="glass-card">
      <el-form :model="form" label-width="100px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" placeholder="例如：openclaw_v3 发布说明" maxlength="200" show-word-limit />
        </el-form-item>

        <el-form-item label="文件名">
          <el-input
            v-if="!isEdit"
            v-model="form.slug"
            placeholder="留空则由标题自动生成，如 openclaw-v3-release"
          >
            <template #prepend>wiki/md/</template>
            <template #append>.md</template>
          </el-input>
          <el-input v-else :model-value="route.params.slug" disabled>
            <template #prepend>wiki/md/</template>
            <template #append>.md</template>
          </el-input>
          <div class="hint">
            正文保存在 Git 管理的 Markdown 文件里，可用 IDE 直接编辑或用 git 查看历史/diff；
            文件名创建后不可更改。
          </div>
        </el-form-item>

        <div class="row-2">
          <el-form-item label="分类">
            <el-select v-model="form.category_id" placeholder="不归类" clearable style="width:100%">
              <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="标签">
            <el-select
              v-model="tagList"
              multiple filterable allow-create default-first-option
              placeholder="回车创建标签" style="width:100%"
            >
              <el-option v-for="t in tagOptions" :key="t" :label="t" :value="t" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="摘要">
          <el-input v-model="form.summary" placeholder="一句话说明这篇文档讲什么（列表页展示）" maxlength="200" show-word-limit />
        </el-form-item>

        <el-form-item label="置顶">
          <el-switch v-model="pinned" />
        </el-form-item>

        <!-- 关联到某个配置实体的具体版本 -->
        <div class="row-2">
          <el-form-item label="关联类型">
            <el-select v-model="form.linked_entity_type" clearable placeholder="不关联" style="width:100%" @change="onEntityTypeChange">
              <el-option v-for="(label, key) in ENTITY_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
          </el-form-item>
          <el-form-item label="关联对象">
            <el-select
              v-model="form.linked_entity_key"
              clearable filterable
              :disabled="!form.linked_entity_type"
              :loading="entityLoading"
              placeholder="先选择关联类型"
              style="width:100%"
            >
              <el-option v-for="o in entityOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="正文">
          <MarkdownEditor
            v-model="form.content_md"
            placeholder="开始写文档…支持标题、列表、表格、代码块；可拖拽或粘贴图片"
            style="width:100%"
          />
          <div class="hint">
            所见即所得编辑，底层仍是标准 Markdown。图片上传后自动压缩为 WebP，
            存放在 wiki/assets/（不随 Git 同步）；拖拽图片右下角手柄可调整显示比例。
          </div>
        </el-form-item>

        <el-form-item v-if="isEdit" label="发布">
          <el-button @click="publishDialog = true">发布为通知</el-button>
          <el-tag v-if="form.last_notification_id" type="success" size="small" effect="plain" style="margin-left:8px">
            已发布通知 #{{ form.last_notification_id }}
          </el-tag>
          <span class="hint">先保存，再发布；通知正文取保存后的内容。</span>
        </el-form-item>
      </el-form>
    </div>

    <el-dialog v-model="publishDialog" title="发布为通知" width="680px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="推送">
          <el-checkbox v-model="publishForm.push">推送给所有用户</el-checkbox>
          <span class="hint">取消勾选时仅标记为「已发布」，不产生通知。</span>
        </el-form-item>
        <template v-if="publishForm.push">
          <el-form-item label="通知标题">
            <el-input v-model="publishForm.title" :placeholder="form.title" />
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
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'
import MarkdownEditor from '../components/MarkdownEditor.vue'
import { useNotificationStore } from '../stores/notification'

const route = useRoute()
const router = useRouter()
const notificationStore = useNotificationStore()

const isEdit = computed(() => !!route.params.slug)
const loading = ref(false)
const saving = ref(false)

const ENTITY_LABELS = {
  image: '镜像',
  code_repo: '代码仓',
  harness_config: 'Harness 配置',
  sandbox_config: '沙箱配置',
  resource_pool: '资源池',
  config_template: '配置模板',
}

const form = ref({
  title: '',
  slug: '',
  category_id: null,
  tags: '',
  summary: '',
  content_md: '',
  linked_entity_type: '',
  linked_entity_key: '',
  is_pinned: 0,
  last_notification_id: null,
})

const categories = ref([])
const tagList = ref([])
const tagOptions = ref([])
const entityOptions = ref([])
const entityLoading = ref(false)

const pinned = computed({
  get: () => form.value.is_pinned === 1,
  set: (v) => { form.value.is_pinned = v ? 1 : 0 },
})

const publishDialog = ref(false)
const publishing = ref(false)
const publishForm = ref({ push: true, title: '', level: 'info', content_md: '' })

async function loadCategories() {
  try { categories.value = await api.get('/wiki/categories') } catch { categories.value = [] }
}

async function loadTags() {
  try {
    const res = await api.get('/wiki/documents', { params: { page: 1, page_size: 200 } })
    const set = new Set()
    res.items.forEach((d) => (d.tags || '').split(',').map((s) => s.trim()).filter(Boolean).forEach((t) => set.add(t)))
    tagOptions.value = [...set]
  } catch { tagOptions.value = [] }
}

/** 关联类型 → 候选对象。每个请求独立降级，一个接口挂了不影响其他类型。 */
async function onEntityTypeChange(type) {
  form.value.linked_entity_key = ''
  entityOptions.value = []
  if (!type) return
  entityLoading.value = true
  try {
    if (type === 'image') {
      const rows = await api.get('/images')
      entityOptions.value = rows.map((r) => ({ value: String(r.id), label: `${r.name} · ${r.address}` }))
    } else if (type === 'code_repo') {
      const rows = await api.get('/code-repos')
      entityOptions.value = rows.map((r) => ({ value: String(r.id), label: `${r.name} · ${r.version}` }))
    } else if (type === 'harness_config') {
      const rows = await api.get('/harness-configs')
      entityOptions.value = rows.map((r) => ({ value: r.version, label: `${r.harness_type} · ${r.version}` }))
    } else if (type === 'sandbox_config') {
      const rows = await api.get('/sandbox-configs')
      entityOptions.value = rows.map((r) => ({ value: String(r.id), label: `${r.name} (${r.sandbox_type})` }))
    } else if (type === 'resource_pool') {
      const res = await api.get('/resource-pools')
      entityOptions.value = res.pools.map((p) => ({ value: p.namespace, label: p.namespace }))
    } else if (type === 'config_template') {
      const rows = await api.get('/config-templates')
      entityOptions.value = rows.map((r) => ({ value: String(r.id), label: r.name }))
    }
  } catch {
    entityOptions.value = []
  } finally {
    entityLoading.value = false
  }
}

async function loadDoc() {
  loading.value = true
  try {
    const doc = await api.get(`/wiki/documents/${route.params.slug}`)
    form.value = { ...doc }
    tagList.value = doc.tags ? doc.tags.split(',').map((s) => s.trim()).filter(Boolean) : []
    if (doc.linked_entity_type) await onEntityTypeChange(doc.linked_entity_type)
    form.value.linked_entity_key = doc.linked_entity_key
  } catch (e) {
    // 越权（改别人的文档）由保存时的 PUT 返回 403；这里先拦住的是「路由直接跳进来」的情况。
    // 提示后退回列表，避免用户对着一个保存必失败的编辑页填半天。
    if (e?.response?.status === 403 || e?.response?.status === 404) {
      ElMessage.error('文档不存在，或你没有修改权限（只能编辑自己创建的文档）')
      router.push('/wiki')
    }
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!form.value.title.trim()) {
    ElMessage.warning('请填写标题')
    return
  }
  const payload = { ...form.value, tags: tagList.value.join(',') }
  // 文件名只在新建时提交（创建后不可改），空串交给后端按标题推导
  if (isEdit.value || !String(payload.slug || '').trim()) delete payload.slug
  delete payload.author
  delete payload.updated_by
  delete payload.created_at
  delete payload.updated_at
  delete payload.last_notification_id

  saving.value = true
  try {
    if (isEdit.value) {
      await api.put(`/wiki/documents/${route.params.slug}`, payload)
      ElMessage.success('已保存')
      router.push(`/wiki/${route.params.slug}`)
    } else {
      const doc = await api.post('/wiki/documents', payload)
      ElMessage.success('已创建')
      router.push(`/wiki/${doc.slug}`)
    }
  } finally {
    saving.value = false
  }
}

function cancel() {
  router.push(isEdit.value ? `/wiki/${route.params.slug}` : '/wiki')
}

function openPublish() {
  publishForm.value = { push: true, title: form.value.title, level: 'info', content_md: form.value.content_md }
  publishDialog.value = true
}

async function doPublish() {
  publishing.value = true
  try {
    const res = await api.post(`/wiki/documents/${route.params.slug}/publish`, publishForm.value)
    publishDialog.value = false
    if (res.notification_id) {
      ElMessage.success(`已发布并推送通知 #${res.notification_id}`)
      form.value.last_notification_id = res.notification_id
      notificationStore.fetchUnread()
    } else {
      ElMessage.success('已标记发布（未推送）')
    }
  } finally {
    publishing.value = false
  }
}

onMounted(() => {
  loadCategories()
  loadTags()
  if (isEdit.value) loadDoc()
})

// 从 /wiki/a/edit 跳到 /wiki/b/edit 时组件复用，需要重新加载
watch(() => route.params.slug, (s) => {
  if (s) loadDoc()
  else resetForm()
})

function resetForm() {
  form.value = {
    title: '', slug: '', category_id: null, tags: '', summary: '', content_md: '',
    linked_entity_type: '', linked_entity_key: '', is_pinned: 0, last_notification_id: null,
  }
  tagList.value = []
  entityOptions.value = []
}
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
.row-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 6px;
}
</style>
