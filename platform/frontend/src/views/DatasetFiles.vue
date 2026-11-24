<template>
  <div class="df-page">
    <div class="df-header">
      <div class="df-title-row">
        <el-button link @click="$router.push('/registrations')">
          <el-icon><ArrowLeft /></el-icon>&nbsp;数据集管理
        </el-button>
        <h2>{{ dataset?.task_name || '数据集文件' }}</h2>
        <el-tag v-if="fileStatus === 'modified'" size="small" type="warning" effect="plain">
          已修改，待同步
        </el-tag>
      </div>
      <div class="df-path" :title="localPath">
        <el-icon><FolderOpened /></el-icon>&nbsp;{{ localPath || '—' }}
      </div>
    </div>

    <div class="df-layout">
      <!-- 左：本地目录树 -->
      <aside class="df-tree" v-loading="treeLoading">
        <div class="tree-nav">
          <el-button size="small" :disabled="!rel" @click="goUp">上一级</el-button>
          <el-button size="small" :disabled="!rel" @click="loadTree('')">根目录</el-button>
        </div>
        <div class="tree-crumbs" v-if="rel">
          <span class="crumb" @click="loadTree('')">/</span>
          <template v-for="(seg, i) in rel.split('/')" :key="i">
            <span class="crumb" @click="loadTree(rel.split('/').slice(0, i + 1).join('/'))">{{ seg }}/</span>
          </template>
        </div>
        <div v-for="it in items" :key="it.rel"
          :class="['tree-item', { active: current?.rel === it.rel }]"
          @click="onItemClick(it)">
          <el-icon :class="it.is_dir ? 'dir-icon' : 'file-icon'">
            <Folder v-if="it.is_dir" /><Document v-else />
          </el-icon>
          <span class="tree-name">{{ it.name }}</span>
          <span v-if="it.size" class="tree-size">{{ formatBytes(it.size) }}</span>
        </div>
        <div v-if="!items.length && !treeLoading" class="empty-hint">该目录为空</div>
      </aside>

      <!-- 右：内容 / 编辑 -->
      <section class="df-main" v-loading="fileLoading">
        <template v-if="current && !current.is_dir">
          <div class="df-main-head">
            <span class="file-name">{{ current.name }}</span>
            <span class="file-meta">{{ formatBytes(current.size) }}</span>
            <span class="file-meta" v-if="isJson">JSON · {{ lineCount }} 行</span>
            <div class="head-actions">
              <template v-if="!editing">
                <el-button size="small" :disabled="!!fileError" @click="startEdit">编辑</el-button>
              </template>
              <template v-else>
                <el-button size="small" :disabled="!isJson" @click="formatEditor">格式化</el-button>
                <el-button size="small" @click="cancelEdit">取消</el-button>
                <el-button size="small" type="primary" :loading="saving"
                  :disabled="!dirty" @click="save">保存</el-button>
              </template>
            </div>
          </div>

          <el-alert v-if="fileError" :title="fileError" type="error" :closable="false" show-icon />
          <template v-else-if="editing">
            <el-input v-model="editContent" type="textarea" class="df-editor" spellcheck="false" />
            <div class="df-hint">保存只写本地副本；云端由 sync_worker 按哈希异步回传。</div>
          </template>
          <div v-else class="df-code-scroll">
            <div class="df-code-inner">
              <pre class="df-gutter">{{ gutter }}</pre>
              <pre class="df-code">{{ prettyContent }}</pre>
            </div>
          </div>
        </template>
        <div v-else class="empty-hint center">从左侧选择一个文件</div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, FolderOpened, Folder, Document } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const datasetId = computed(() => Number(route.params.id))
const dataset = ref(null)
const rel = ref('')
const localPath = ref('')
const items = ref([])
const treeLoading = ref(false)
const current = ref(null)
const fileContent = ref('')
const fileLoading = ref(false)
const fileError = ref('')
const fileStatus = ref('clean')

const editing = ref(false)
const editContent = ref('')
const saving = ref(false)
const dirty = computed(() => editing.value && editContent.value !== fileContent.value)

const isJson = computed(() => (current.value?.name || '').toLowerCase().endsWith('.json'))

// 尽量格式化：能解析就 2 空格缩进输出，解析不了原样展示。
// 只读态永远走格式化，编辑态保持用户原文，避免一进编辑就被改动。
const prettyContent = computed(() => {
  if (!isJson.value) return fileContent.value
  try {
    return JSON.stringify(JSON.parse(fileContent.value), null, 2)
  } catch {
    return fileContent.value   // JSON 坏了就原样显示，别把用户挡在门外
  }
})
const lineCount = computed(() => prettyContent.value ? prettyContent.value.split('\n').length : 0)
const gutter = computed(() => {
  const n = lineCount.value
  if (!n) return ''
  return Array.from({ length: n }, (_, i) => i + 1).join('\n')
})

function formatBytes(size) {
  if (!size) return ''
  if (size < 1024) return `${size}B`
  if (size < 1048576) return `${(size / 1024).toFixed(1)}KB`
  return `${(size / 1048576).toFixed(1)}MB`
}

async function loadDataset() {
  try {
    dataset.value = await api.get(`/registrations/${datasetId.value}`)
  } catch {
    ElMessage.error('数据集不存在')
    router.push('/registrations')
  }
}

async function loadTree(target) {
  treeLoading.value = true
  try {
    const res = await api.get(`/registrations/${datasetId.value}/tree`, {
      params: { rel: target || '' }, silent: true,
    })
    rel.value = res.rel || ''
    localPath.value = res.local_path || ''
    items.value = res.items || []
  } catch (e) {
    items.value = []
    localPath.value = ''
    ElMessage.error(e.response?.data?.detail || '列目录失败')
  } finally {
    treeLoading.value = false
  }
}

function goUp() {
  const parts = rel.value.split('/')
  parts.pop()
  loadTree(parts.join('/'))
}

function onItemClick(it) {
  if (it.is_dir) {
    loadTree(it.rel)
    return
  }
  openFile(it)
}

async function openFile(it) {
  current.value = it
  fileContent.value = ''
  fileError.value = ''
  fileStatus.value = 'clean'
  editing.value = false
  editContent.value = ''
  fileLoading.value = true
  try {
    const res = await api.get(`/registrations/${datasetId.value}/file`, {
      params: { rel: it.rel }, silent: true,
    })
    fileContent.value = res.content || ''
    fileStatus.value = res.status || 'clean'
  } catch (e) {
    fileError.value = e.response?.data?.detail || '读取文件失败'
  } finally {
    fileLoading.value = false
  }
}

function startEdit() {
  editContent.value = fileContent.value
  editing.value = true
}

function cancelEdit() {
  editing.value = false
  editContent.value = ''
}

function formatEditor() {
  try {
    editContent.value = JSON.stringify(JSON.parse(editContent.value), null, 2)
  } catch (e) {
    ElMessage.warning(`JSON 无法解析：${e.message}`)
  }
}

async function save() {
  if (!dirty.value) return
  saving.value = true
  try {
    const res = await api.put(`/registrations/${datasetId.value}/file`, {
      rel: current.value.rel,
      content: editContent.value,
      validate_json: true,
    })
    fileContent.value = editContent.value
    fileStatus.value = res.status || 'clean'
    editing.value = false
    editContent.value = ''
    current.value = { ...current.value, size: res.size }
    ElMessage.success(res.detail || '已保存')
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

// 支持从列表页带 ?rel= 直接打开某个文件
async function openFromQuery() {
  const target = route.query.rel
  if (!target) return
  const dir = target.split('/').slice(0, -1).join('/')
  await loadTree(dir)
  const hit = items.value.find(i => i.rel === target && !i.is_dir)
  if (hit) openFile(hit)
}

watch(() => route.query.rel, () => { if (route.query.rel) openFromQuery() })

onMounted(async () => {
  await loadDataset()
  await loadTree('')
  await openFromQuery()
})
</script>

<style scoped>
.df-page { display: flex; flex-direction: column; height: calc(100vh - 130px); }
.df-header { flex-shrink: 0; margin-bottom: 12px; }
.df-title-row { display: flex; align-items: center; gap: 12px; }
.df-title-row h2 { margin: 0; font-size: 20px; font-weight: 700; color: var(--text-primary); }
.df-path {
  margin-top: 6px; font-size: 12px; color: var(--text-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  display: flex; align-items: center; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}

.df-layout { flex: 1; min-height: 0; display: flex; gap: 12px; }
.df-tree {
  width: 300px; flex-shrink: 0; overflow-y: auto;
  border: 1px solid var(--border-color); border-radius: 8px; padding: 8px;
}
.tree-nav { display: flex; gap: 6px; margin-bottom: 6px; }
.tree-crumbs { font-size: 12px; color: var(--text-muted); margin-bottom: 6px; word-break: break-all; }
.crumb { cursor: pointer; }
.crumb:hover { color: var(--el-color-primary); }
.tree-item {
  display: flex; align-items: center; gap: 6px; padding: 5px 8px;
  border-radius: 6px; cursor: pointer; font-size: 13px;
}
.tree-item:hover { background: var(--el-fill-color-light, #f5f7fa); }
.tree-item.active { background: var(--el-color-primary-light-9, #ecf5ff); }
.tree-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tree-size { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.dir-icon { color: #e6a23c; }
.file-icon { color: #909399; }

.df-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  border: 1px solid var(--border-color); border-radius: 8px; padding: 10px 12px;
}
.df-main-head {
  display: flex; align-items: baseline; gap: 10px; flex-shrink: 0;
  padding-bottom: 8px; margin-bottom: 8px; border-bottom: 1px solid var(--border-color);
}
.file-name { font-weight: 600; font-size: 13px; color: var(--text-primary); }
.file-meta { font-size: 11px; color: var(--text-muted); }
.head-actions { margin-left: auto; display: flex; gap: 6px; }

.df-code-scroll { flex: 1; min-height: 0; overflow: auto; }
.df-code-inner { display: flex; min-width: max-content; }
.df-gutter {
  margin: 0; padding: 0 10px 0 4px; text-align: right; user-select: none;
  color: var(--text-muted); font-size: 12px; line-height: 1.7;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  position: sticky; left: 0; background: var(--bg-card, #fff);
  border-right: 1px solid var(--border-color);
}
.df-code {
  margin: 0; padding: 0 8px; white-space: pre; color: var(--text-secondary);
  font-size: 12px; line-height: 1.7;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.df-editor { flex: 1; min-height: 0; display: flex; }
.df-editor :deep(.el-textarea) { flex: 1; display: flex; }
.df-editor :deep(textarea) {
  flex: 1; height: 100% !important; resize: none;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.7;
}
.df-hint { font-size: 11px; color: var(--text-muted); margin-top: 8px; flex-shrink: 0; }

.empty-hint { font-size: 12px; color: var(--text-muted); padding: 8px; }
.empty-hint.center { margin: auto; }
</style>
