<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>输出文件 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <div class="toolbar">
      <el-button @click="loadTree" :loading="loading">刷新</el-button>
      <el-breadcrumb separator="/" style="margin-left:12px">
        <el-breadcrumb-item>{{ obsBasePath || '...' }}</el-breadcrumb-item>
      </el-breadcrumb>
    </div>

    <div class="split-layout">
      <!-- 左侧：文件树 -->
      <div class="file-list">
        <el-tree
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="path"
          :load="loadNode"
          lazy
          highlight-current
          @node-click="handleNodeClick"
          v-loading="loading"
        >
          <template #default="{ node, data }">
            <span class="tree-node">
              <el-icon v-if="data.is_dir" style="color:#e6a23c"><Folder /></el-icon>
              <el-icon v-else style="color:#909399"><Document /></el-icon>
              <span style="margin-left:4px">{{ data.name }}</span>
              <el-button v-if="!data.is_dir" size="small" text type="primary"
                style="margin-left:8px" @click.stop="downloadFile(data)">下载</el-button>
            </span>
          </template>
        </el-tree>
      </div>

      <!-- 右侧：文件预览 -->
      <div class="file-preview">
        <div v-if="previewLoading" style="padding:40px;text-align:center">
          <el-icon class="is-loading" :size="24"><Loading /></el-icon>
          <p style="color:#999;margin-top:8px">加载中...</p>
        </div>
        <div v-else-if="previewContent !== null">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#1e293b;border-radius:var(--radius-sm) var(--radius-sm) 0 0">
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
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const route = useRoute()
const id = route.params.id
const inst = ref({})
const loading = ref(false)
const treeData = ref([])
const treeRef = ref(null)
const selectedFile = ref(null)
const previewContent = ref(null)
const previewTotalLines = ref(0)
const previewLoading = ref(false)
const obsBasePath = ref('')

const treeProps = {
  label: 'name',
  children: 'children',
  isLeaf: (data) => !data.is_dir,
}

async function loadTree() {
  loading.value = true
  try {
    const res = await api.get(`/logs/${id}/obs-logs`)
    obsBasePath.value = res.obs_path
    treeData.value = buildFirstLevel(res.items || [])
  } finally { loading.value = false }
}

function buildFirstLevel(items) {
  const dirs = new Map()
  const files = []

  for (const item of items) {
    const name = item.name.replace(/\/$/, '')
    const parts = name.split('/')
    if (parts.length > 1) {
      const topDir = parts[0]
      if (!dirs.has(topDir)) {
        dirs.set(topDir, {
          name: topDir,
          path: obsBasePath.value + topDir + '/',
          is_dir: true,
        })
      }
    } else {
      if (item.is_dir) {
        dirs.set(name, { name, path: item.path, is_dir: true })
      } else {
        files.push({ name, path: item.path, is_dir: false })
      }
    }
  }

  return [...Array.from(dirs.values()), ...files]
}

async function loadNode(node, resolve) {
  if (node.level === 0) {
    resolve(treeData.value)
    return
  }
  const data = node.data
  if (!data.is_dir) { resolve([]); return }

  try {
    const res = await api.get('/obs/list', { params: { path: data.path, show_files: true } })
    const children = (res.items || []).map(item => ({
      name: item.name.replace(/\/$/, ''),
      path: item.path,
      is_dir: item.is_dir,
    }))
    resolve(children)
  } catch {
    resolve([])
  }
}

function handleNodeClick(data) {
  if (!data.is_dir) {
    previewFile(data)
  }
}

async function previewFile(file) {
  selectedFile.value = file
  previewLoading.value = true
  previewContent.value = null
  try {
    const res = await api.get(`/logs/${id}/obs-view`, { params: { file_path: file.path, tail: 1000 } })
    previewContent.value = (res.lines || []).join('\n')
    previewTotalLines.value = res.total_lines || 0
  } catch {
    previewContent.value = '文件加载失败'
  } finally { previewLoading.value = false }
}

function downloadFile(file) {
  if (!file) return
  const token = localStorage.getItem('token')
  window.open(`/api/logs/${id}/obs-download?file_path=${encodeURIComponent(file.path)}&token=${token}`, '_blank')
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTree()
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; margin-bottom: 12px; }
.split-layout { display: flex; gap: 16px; height: calc(100vh - 220px); }
.file-list {
  width: 360px; flex-shrink: 0; overflow: auto;
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 8px; background: #fff;
}
.file-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.tree-node { display: flex; align-items: center; font-size: 13px; }
.preview-content {
  background: #1e293b; color: #e2e8f0; padding: 12px; margin: 0;
  border-radius: 0 0 var(--radius-sm) var(--radius-sm); flex: 1; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}
</style>
