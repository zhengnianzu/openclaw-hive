<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>输出文件 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <div class="toolbar">
      <el-button @click="loadObsFiles" :loading="loading">刷新</el-button>
      <el-breadcrumb separator="/" style="margin-left:12px">
        <el-breadcrumb-item v-for="(seg, idx) in pathSegments" :key="idx"
          @click="navigateTo(idx)" style="cursor:pointer">
          {{ seg || obsBasePath }}
        </el-breadcrumb-item>
      </el-breadcrumb>
    </div>

    <div class="split-layout">
      <!-- 左侧：文件列表 -->
      <div class="file-list">
        <el-table :data="fileItems" v-loading="loading" max-height="calc(100vh - 240px)"
          @row-click="handleItemClick" highlight-current-row style="cursor:pointer" size="small">
          <el-table-column label="文件名" min-width="200">
            <template #default="{row}">
              <el-icon v-if="row.is_dir" style="color:#e6a23c"><Folder /></el-icon>
              <el-icon v-else style="color:#909399"><Document /></el-icon>
              <span style="margin-left:6px" :style="{fontWeight: row.path === selectedFile?.path ? 'bold' : ''}">{{ row.name }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{row}">
              <el-button v-if="!row.is_dir" size="small" text type="primary" @click.stop="downloadFile(row)">下载</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 右侧：文件预览 -->
      <div class="file-preview">
        <div v-if="previewLoading" style="padding:40px;text-align:center">
          <el-icon class="is-loading" :size="24"><Loading /></el-icon>
          <p style="color:#999;margin-top:8px">加载中...</p>
        </div>
        <div v-else-if="previewContent" class="preview-header">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#2d2d2d;border-radius:8px 8px 0 0">
            <span style="color:#ccc;font-size:13px">{{ selectedFile?.name }} ({{ previewTotalLines }} 行)</span>
            <el-button size="small" text style="color:#ccc" @click="downloadFile(selectedFile)">下载</el-button>
          </div>
          <pre class="preview-content">{{ previewContent }}</pre>
        </div>
        <el-empty v-else description="点击左侧文件预览内容" :image-size="80" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()
const id = route.params.id
const inst = ref({})
const loading = ref(false)
const fileItems = ref([])
const selectedFile = ref(null)
const previewContent = ref('')
const previewTotalLines = ref(0)
const previewLoading = ref(false)

const obsBasePath = ref('')
const currentPath = ref('')

const pathSegments = computed(() => {
  if (!obsBasePath.value || !currentPath.value) return ['']
  const relative = currentPath.value.replace(obsBasePath.value, '')
  return ['', ...relative.split('/').filter(Boolean)]
})

function navigateTo(idx) {
  const segs = pathSegments.value.slice(1, idx + 1)
  currentPath.value = obsBasePath.value + (segs.length ? segs.join('/') + '/' : '')
  loadObsDir()
}

async function loadObsFiles() {
  loading.value = true
  try {
    const res = await api.get(`/logs/${id}/obs-logs`)
    obsBasePath.value = res.obs_path
    currentPath.value = res.obs_path
    fileItems.value = (res.items || []).map(item => ({
      ...item,
      name: item.name.replace(/\/$/, ''),
    }))
  } finally { loading.value = false }
}

async function loadObsDir() {
  loading.value = true
  try {
    const res = await api.get('/obs/list', { params: { path: currentPath.value, show_files: true } })
    fileItems.value = (res.items || []).map(item => ({
      ...item,
      name: item.name.replace(/\/$/, ''),
    }))
  } finally { loading.value = false }
}

function handleItemClick(row) {
  if (row.is_dir) {
    currentPath.value = row.path
    if (!currentPath.value.endsWith('/')) currentPath.value += '/'
    loadObsDir()
  } else {
    previewFile(row)
  }
}

async function previewFile(row) {
  selectedFile.value = row
  previewLoading.value = true
  previewContent.value = ''
  try {
    const res = await api.get(`/logs/${id}/obs-view`, { params: { file_path: row.path, tail: 1000 } })
    previewContent.value = (res.lines || []).join('\n')
    previewTotalLines.value = res.total_lines || 0
  } catch {
    previewContent.value = '文件加载失败'
  } finally { previewLoading.value = false }
}

function downloadFile(row) {
  if (!row) return
  const token = localStorage.getItem('token')
  window.open(`/api/logs/${id}/obs-download?file_path=${encodeURIComponent(row.path)}&token=${token}`, '_blank')
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadObsFiles()
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; margin-bottom: 12px; }
.split-layout { display: flex; gap: 16px; height: calc(100vh - 220px); }
.file-list { width: 360px; flex-shrink: 0; overflow: auto; }
.file-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.preview-content {
  background: #1e1e1e; color: #d4d4d4; padding: 12px; margin: 0;
  border-radius: 0 0 8px 8px; flex: 1; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}
</style>
