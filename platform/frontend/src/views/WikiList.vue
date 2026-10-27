<template>
  <div>
    <div class="header-row">
      <h2>文档中心</h2>
      <!-- 新建开放给 admin + operator：运行者需要沉淀自己的经验。
           分类管理 / 排序 / 发布仍是 admin（会跨用户批量写或全站广播）。 -->
      <el-button v-if="authStore.isOperator" type="primary" @click="$router.push('/wiki/new')">
        <el-icon><Plus /></el-icon> 新建文档
      </el-button>
    </div>

    <div class="filter-bar">
      <el-input
        v-model="filters.q"
        placeholder="搜索标题 / 摘要 / 标签"
        clearable
        style="width:280px"
        @keyup.enter="reload"
        @clear="reload"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-select v-model="filters.category_id" placeholder="全部分类" clearable style="width:160px" @change="reload">
        <el-option v-for="c in categories" :key="c.id" :label="`${c.name} (${c.doc_count})`" :value="c.id" />
      </el-select>
      <el-select v-model="filters.tag" placeholder="全部标签" clearable filterable allow-create style="width:160px" @change="reload">
        <el-option v-for="t in tagOptions" :key="t" :label="t" :value="t" />
      </el-select>
      <el-checkbox v-model="filters.pinned" @change="reload">只看置顶</el-checkbox>
      <el-button v-if="authStore.isAdmin" @click="categoryDialog = true">管理分类</el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="docs" v-loading="loading" stripe style="width:100%" border>
        <el-table-column label="标题" min-width="260">
          <template #default="{ row }">
            <div class="title-cell">
              <el-tag v-if="row.is_pinned" type="danger" size="small" effect="plain">置顶</el-tag>
              <el-link type="primary" :underline="false" @click="$router.push(`/wiki/${row.slug}`)">{{ row.title }}</el-link>
            </div>
            <div v-if="row.summary" class="summary">{{ row.summary }}</div>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="120">
          <template #default="{ row }">
            <el-tag v-if="categoryName(row.category_id)" type="info" size="small">{{ categoryName(row.category_id) }}</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="t in splitTags(row.tags)" :key="t" size="small" class="tag-item" @click="filterByTag(t)">{{ t }}</el-tag>
            <span v-if="!row.tags" class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="关联版本" min-width="180">
          <template #default="{ row }">
            <span v-if="row.linked_entity_type" class="linked">
              {{ entityLabel(row.linked_entity_type) }} · {{ row.linked_entity_key }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="updated_by" label="更新人" width="110" />
        <el-table-column prop="updated_at" label="更新时间" width="180" />
        <!-- 操作列对 admin+operator 可见，但每行按钮按归属判断：
               上移/下移会调 reorder 批量重写所有文档的 frontmatter，必须 admin；
               编辑/删除只给「自己的文档」，与后端 _load_owned 的口径一致。 -->
        <el-table-column label="操作" width="240" fixed="right" v-if="authStore.isOperator">
          <template #default="{ row, $index }">
            <div class="op-cell">
              <template v-if="authStore.isAdmin">
                <el-button size="small" text :disabled="$index === 0" title="上移"
                  @click="moveDoc($index, -1)"><el-icon><Top /></el-icon></el-button>
                <el-button size="small" text :disabled="$index === docs.length - 1" title="下移"
                  @click="moveDoc($index, 1)"><el-icon><Bottom /></el-icon></el-button>
              </template>
              <template v-if="canEdit(row)">
                <el-button size="small" @click="$router.push(`/wiki/${row.slug}/edit`)">编辑</el-button>
                <el-button size="small" type="danger" text @click="removeDoc(row)">删除</el-button>
              </template>
              <span v-else class="op-readonly">仅作者可改</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="loadDocs"
      />
    </div>

    <!-- 分类管理 -->
    <el-dialog v-model="categoryDialog" title="分类管理" width="520px" destroy-on-close>
      <div v-for="c in categories" :key="c.id" class="cat-row">
        <el-input v-model="c.name" size="small" style="width:200px" />
        <el-input-number v-model="c.sort_order" size="small" :min="0" :controls="false" style="width:80px" />
        <el-button size="small" @click="saveCategory(c)">保存</el-button>
        <el-button size="small" type="danger" text @click="removeCategory(c)">删除</el-button>
      </div>
      <div class="cat-row" style="margin-top:12px;border-top:1px solid var(--border-color);padding-top:12px">
        <el-input v-model="newCategory.name" size="small" placeholder="新分类名称" style="width:200px" />
        <el-input-number v-model="newCategory.sort_order" size="small" :min="0" :controls="false" style="width:80px" />
        <el-button size="small" type="primary" @click="createCategory">新增</el-button>
      </div>
      <div class="hint">排序值越小越靠前；删除分类不会删除文档，只会解除归类。</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Top, Bottom } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

// 只有作者本人或 admin 能改/删某篇文档，与后端 _load_owned 的判断保持一致。
// author 是服务端写入的可信字段（创建时取当前登录用户，请求体无法覆盖）。
function canEdit(row) {
  return authStore.isAdmin || row.author === authStore.username
}

const docs = ref([])
const categories = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = 20

const filters = ref({ q: '', category_id: null, tag: '', pinned: false })

const categoryDialog = ref(false)
const newCategory = ref({ name: '', sort_order: 0 })

const ENTITY_LABELS = {
  image: '镜像',
  code_repo: '代码仓',
  harness_config: 'Harness',
  sandbox_config: '沙箱',
  resource_pool: '资源池',
  config_template: '配置模板',
}
const entityLabel = (t) => ENTITY_LABELS[t] || t
const splitTags = (s) => (s ? s.split(',').map((x) => x.trim()).filter(Boolean) : [])
const categoryName = (id) => categories.value.find((c) => c.id === id)?.name || ''

// 标签下拉候选：从已加载的文档里汇总（量小，不值得单开接口）
const tagOptions = computed(() => {
  const set = new Set()
  docs.value.forEach((d) => splitTags(d.tags).forEach((t) => set.add(t)))
  return [...set]
})

async function loadDocs() {
  loading.value = true
  try {
    const params = {
      page: page.value,
      page_size: pageSize,
      q: filters.value.q || undefined,
      category_id: filters.value.category_id || undefined,
      tag: filters.value.tag || undefined,
    }
    if (filters.value.pinned) params.pinned = true
    const res = await api.get('/wiki/documents', { params })
    docs.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function loadCategories() {
  try { categories.value = await api.get('/wiki/categories') } catch { categories.value = [] }
}

function reload() {
  page.value = 1
  loadDocs()
}

function filterByTag(tag) {
  filters.value.tag = tag
  reload()
}

// 上下移动：按当前页顺序重排并提交
async function moveDoc(index, delta) {
  const target = index + delta
  if (target < 0 || target >= docs.value.length) return
  const list = docs.value.slice()
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
  docs.value = list
  try {
    await api.post('/wiki/documents/reorder', { slugs: list.map(d => d.slug) })
    await loadDocs()
  } catch { await loadDocs() }
}

async function removeDoc(row) {
  try {
    await ElMessageBox.confirm(`确定删除《${row.title}》？已发出的通知不会一并撤回。`, '删除文档', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  await api.delete(`/wiki/documents/${row.slug}`)
  ElMessage.success('已删除')
  loadDocs()
}

async function createCategory() {
  if (!newCategory.value.name.trim()) return
  await api.post('/wiki/categories', {
    name: newCategory.value.name.trim(),
    sort_order: newCategory.value.sort_order,
  })
  newCategory.value = { name: '', sort_order: 0 }
  ElMessage.success('分类已创建')
  loadCategories()
}

async function saveCategory(c) {
  await api.put(`/wiki/categories/${c.id}`, { name: c.name, sort_order: c.sort_order })
  ElMessage.success('已保存')
  loadCategories()
}

async function removeCategory(c) {
  try {
    await ElMessageBox.confirm(`删除分类「${c.name}」？文档会保留，但解除归类。`, '删除分类', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
  } catch { return }
  await api.delete(`/wiki/categories/${c.id}`)
  ElMessage.success('已删除')
  loadCategories()
  loadDocs()
}

onMounted(() => {
  loadDocs()
  loadCategories()
})
</script>

<style scoped>
.op-cell { display: flex; align-items: center; gap: 4px; flex-wrap: nowrap; white-space: nowrap; }

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
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
}
.summary {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 4px;
  line-height: 1.5;
}
.tag-item {
  margin-right: 4px;
  cursor: pointer;
}
.linked {
  font-size: 12px;
  color: var(--text-secondary);
}
.muted {
  color: var(--text-muted);
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
.cat-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.hint {
  margin-top: 12px;
  font-size: 12px;
  color: var(--text-muted);
}
</style>
