<template>
  <div>
    <el-page-header @back="$router.push('/dashboard')" style="margin-bottom:20px">
      <template #content>输出文件 - {{ inst.name || route.params.id }}</template>
    </el-page-header>

    <!-- 统计面板 -->
    <el-card style="margin-bottom:16px" v-loading="evalLoading">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span>评估统计</span>
          <div>
            <el-button size="small" @click="loadEvalStats(false)" :loading="evalLoading">增量刷新</el-button>
            <el-button size="small" type="warning" @click="loadEvalStats(true)" :loading="evalLoading">全量刷新</el-button>
          </div>
        </div>
      </template>
      <div v-if="evalAvailable">
        <div class="eval-two-col">
          <!-- 左列: 5个指标 (flex:7) -->
          <div class="eval-stat-grid">
            <div class="eval-stat-item">
              <span class="eval-stat-num">{{ evalTotalSamples }}</span>
              <span class="eval-stat-label">总样本量</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#6366f1">{{ evalUploadedTrajs }}</span>
              <span class="eval-stat-label">上传轨迹</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#10b981">{{ taskCompletedDisplay }}</span>
              <span class="eval-stat-label">任务完成</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#10b981">{{ evalCount }}</span>
              <span class="eval-stat-label">已评估</span>
            </div>
            <div class="eval-stat-item">
              <span class="eval-stat-num" style="color:#f59e0b">{{ avgScoreDisplay }}</span>
              <span class="eval-stat-label">平均分</span>
            </div>
          </div>
          <!-- 右列: 柱状图 (flex:3) -->
          <div v-if="evalCount > 0" class="score-chart">
            <div class="score-chart-title">分数分布</div>
            <div class="score-chart-body">
              <div v-for="bucket in distributionBuckets" :key="bucket.label" class="score-chart-col">
                <span class="score-chart-count">{{ bucket.count || '' }}</span>
                <div class="score-chart-bar-wrap">
                  <div class="score-chart-bar" :style="{height: bucket.pct + '%'}" />
                </div>
                <span class="score-chart-label">{{ bucket.label }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <el-empty v-else description="暂无评估数据" :image-size="40" />
    </el-card>

    <div class="toolbar">
      <el-button @click="loadTopDirs(true)" :loading="loading">刷新</el-button>
      <el-checkbox v-model="showHidden" style="margin-left:12px" @change="onFilterChange">显示隐藏文件</el-checkbox>
      <el-breadcrumb separator="/" style="margin-left:12px">
        <el-breadcrumb-item>{{ obsBasePath || '...' }}</el-breadcrumb-item>
      </el-breadcrumb>
      <div style="flex:1" />
      <el-input v-model="searchKeyword" placeholder="搜索文件..." clearable size="small"
        style="width:220px;margin-right:8px" @input="onSearchInput" @clear="onSearchClear">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-dropdown trigger="click" @command="handleShortcut">
        <el-button size="small" type="primary">快捷查看<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="run">任务日志</el-dropdown-item>
            <el-dropdown-item command="gateway">harness 日志</el-dropdown-item>
            <el-dropdown-item command="evaluator">评估日志</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>

    <div class="split-layout">
      <!-- 左侧：文件树 -->
      <div class="file-list">
        <el-tree
          v-if="!isSearchMode"
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="path"
          highlight-current
          @node-click="handleNodeClick"
          @node-expand="handleNodeExpand"
          v-loading="loading"
        >
          <template #default="{ node, data }">
            <span class="tree-node">
              <el-icon v-if="data.is_dir" style="color:#e6a23c"><Folder /></el-icon>
              <el-icon v-else style="color:#909399"><Document /></el-icon>
              <span style="margin-left:4px">{{ data.label }}</span>
              <span v-if="data.size && !data.is_dir" class="tree-size">{{ data.size }}</span>
              <el-button v-if="!data.is_dir" size="small" text type="primary"
                style="margin-left:8px" @click.stop="downloadFile(data)">下载</el-button>
            </span>
          </template>
        </el-tree>
        <!-- 搜索模式：静态树 -->
        <el-tree
          v-else
          :data="searchTreeData"
          :props="treeProps"
          node-key="path"
          default-expand-all
          highlight-current
          @node-click="handleNodeClick"
          v-loading="searchLoading"
        >
          <template #default="{ node, data }">
            <span class="tree-node">
              <el-icon v-if="data.is_dir" style="color:#e6a23c"><Folder /></el-icon>
              <el-icon v-else style="color:#909399"><Document /></el-icon>
              <span style="margin-left:4px">{{ data.label }}</span>
              <span v-if="data.size && !data.is_dir" class="tree-size">{{ data.size }}</span>
              <el-button v-if="!data.is_dir" size="small" text type="primary"
                style="margin-left:8px" @click.stop="downloadFile(data)">下载</el-button>
            </span>
          </template>
        </el-tree>
        <el-empty v-if="isSearchMode && !searchLoading && searchTreeData.length === 0"
          description="无匹配文件" :image-size="40" />
      </div>

      <!-- 右侧：文件预览 -->
      <div class="file-preview">
        <div v-if="previewLoading" style="padding:40px;text-align:center">
          <el-icon class="is-loading" :size="24"><Loading /></el-icon>
          <p style="color:#999;margin-top:8px">加载中...</p>
        </div>
        <div v-else-if="previewContent !== null">
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#1e293b;border-radius:var(--radius-sm) var(--radius-sm) 0 0">
            <span style="color:#ccc;font-size:13px">{{ selectedFile?.label || selectedFile?.name }} ({{ previewTotalLines }} 行)</span>
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
import { ElMessage } from 'element-plus'
import { ArrowDown, Search } from '@element-plus/icons-vue'
import api from '../api'

const HIDDEN_RE = /^\./

function isHidden(name) {
  return HIDDEN_RE.test(name) || name === '__pycache__'
}

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
const showHidden = ref(false)
const searchKeyword = ref('')
const searchLoading = ref(false)
const searchTreeData = ref([])

// 一级目录名列表
const topDirs = ref([])
// 前端子树缓存: subdir -> items[]
const subtreeCache = {}

const isSearchMode = computed(() => searchKeyword.value.trim().length > 0)

// ============ 评估统计 ============

const evalLoading = ref(false)
const evalAvailable = ref(false)
const evalTotalSamples = ref(0)
const evalUploadedTrajs = ref(0)
const taskScores = ref({})
const taskCompleted = ref({})

const evalCount = computed(() => Object.keys(taskScores.value).length)
const taskCompletedCount = computed(() => Object.values(taskCompleted.value).filter(Boolean).length)
const taskCompletedDisplay = computed(() => {
  const total = Object.keys(taskCompleted.value).length
  if (!total) return '-'
  const count = taskCompletedCount.value
  const pct = ((count / total) * 100).toFixed(0)
  return `${count}/${total} (${pct}%)`
})
const avgScoreDisplay = computed(() => {
  const scores = Object.values(taskScores.value)
  if (!scores.length) return '-'
  const avg = scores.reduce((a, b) => a + b, 0) / scores.length
  return (avg * 100).toFixed(1) + '%'
})

const scoreDistribution = computed(() => {
  const dist = {}
  for (const s of Object.values(taskScores.value)) {
    const idx = Math.min(Math.floor(s * 10), 10)
    const label = idx * 10 + '%'
    dist[label] = (dist[label] || 0) + 1
  }
  return dist
})

const distributionBuckets = computed(() => {
  const dist = scoreDistribution.value
  const total = Object.values(taskScores.value).length || 1
  const maxCount = Math.max(1, ...Object.values(dist))
  const buckets = []
  for (let i = 0; i <= 10; i++) {
    const label = i * 10 + '%'
    const count = dist[label] || 0
    buckets.push({
      label,
      count,
      pct: (count / maxCount) * 100,
      ratio: total > 0 ? ((count / total) * 100).toFixed(0) : '0',
    })
  }
  return buckets
})

async function loadEvalStats(refresh = false) {
  evalLoading.value = true
  try {
    const res = await api.get(`/logs/${id}/eval-stats`, { params: { refresh } })
    evalAvailable.value = res.available
    evalTotalSamples.value = res.total_samples || 0
    evalUploadedTrajs.value = res.uploaded_trajs || 0
    taskScores.value = res.task_scores || {}
    taskCompleted.value = res.task_completed || {}
  } catch {
    evalAvailable.value = false
  } finally {
    evalLoading.value = false
  }
}

// ============ 文件树（两级加载） ============

const treeProps = {
  label: 'label',
  children: 'children',
  isLeaf: (data) => !data.is_dir,
}

/** 加载一级目录列表 */
async function loadTopDirs(refresh = false) {
  loading.value = true
  try {
    const res = await api.get(`/logs/${id}/obs-tree`, { params: { refresh } })
    obsBasePath.value = res.obs_path
    topDirs.value = res.dirs || []
    const dirs = topDirs.value
      .filter(d => showHidden.value || !isHidden(d))
      .sort((a, b) => a.localeCompare(b))
      .map(d => ({
        label: d,
        path: obsBasePath.value + d + '/',
        is_dir: true,
        subdir: d,
        children: [{ label: '加载中...', path: '_placeholder_' + d, is_dir: false, _placeholder: true }],
      }))
    treeData.value = dirs
  } finally { loading.value = false }
}

/** 加载子目录的文件列表，返回扁平 items */
async function loadSubtreeItems(subdir) {
  if (subtreeCache[subdir]) return subtreeCache[subdir]
  const res = await api.get(`/logs/${id}/obs-subtree`, { params: { subdir } })
  const items = res.items || []
  subtreeCache[subdir] = items
  return items
}

/** 展开一级目录时加载子树 */
async function handleNodeExpand(data) {
  if (!data.subdir) return
  // 已加载过（children 不是 placeholder）
  if (data.children && data.children.length > 0 && !data.children[0]._placeholder) return

  try {
    const items = await loadSubtreeItems(data.subdir)
    const parentPath = obsBasePath.value + data.subdir + '/'
    const children = buildPathTree(items, parentPath)
    data.children = children.length > 0 ? children : []
  } catch {
    data.children = []
  }
}

/**
 * 从扁平路径列表构建嵌套树结构
 * items: [{name: "logs/evaluator_use.log", path: "obs://...", is_dir, size}, ...]
 * parentPath: OBS base path for the parent directory
 */
function buildPathTree(items, parentPath) {
  const root = { children: new Map() }

  for (const item of items) {
    const relativePath = item.name
    if (!relativePath) continue

    const segments = relativePath.split('/')
    if (!showHidden.value && segments.some(seg => isHidden(seg))) continue

    let current = root
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      const isLast = i === segments.length - 1

      if (!current.children.has(seg)) {
        current.children.set(seg, {
          label: seg,
          path: isLast ? item.path : parentPath + segments.slice(0, i + 1).join('/') + '/',
          is_dir: isLast ? item.is_dir : true,
          size: isLast ? item.size : null,
          children: new Map(),
        })
      }
      current = current.children.get(seg)
    }
  }

  function toArray(node) {
    if (!node.children || node.children.size === 0) {
      return { label: node.label, path: node.path, is_dir: node.is_dir, size: node.size }
    }
    const dirs = []
    const files = []
    for (const child of node.children.values()) {
      const converted = toArray(child)
      if (converted.is_dir) dirs.push(converted)
      else files.push(converted)
    }
    dirs.sort((a, b) => a.label.localeCompare(b.label))
    files.sort((a, b) => a.label.localeCompare(b.label))
    return {
      label: node.label, path: node.path, is_dir: node.is_dir, size: node.size,
      children: [...dirs, ...files],
    }
  }

  const dirs = []
  const files = []
  for (const child of root.children.values()) {
    const converted = toArray(child)
    if (converted.is_dir) dirs.push(converted)
    else files.push(converted)
  }
  dirs.sort((a, b) => a.label.localeCompare(b.label))
  files.sort((a, b) => a.label.localeCompare(b.label))
  return [...dirs, ...files]
}

// ============ 搜索/过滤 ============

let searchTimer = null

function onSearchInput() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    doSearch()
  }, 300)
}

function onSearchClear() {
  searchTreeData.value = []
}

function onFilterChange() {
  if (isSearchMode.value) {
    doSearch()
  } else {
    loadTopDirs(false)
  }
}

async function doSearch() {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) {
    searchTreeData.value = []
    return
  }

  searchLoading.value = true
  try {
    // 确保所有一级目录都已加载子树
    const dirsToLoad = topDirs.value.filter(d => !subtreeCache[d])
    if (dirsToLoad.length > 0) {
      await Promise.all(dirsToLoad.map(d => loadSubtreeItems(d)))
    }

    // 遍历所有缓存的子树，过滤匹配项，按 parentDir 分组构建树
    const resultNodes = []
    for (const subdir of topDirs.value) {
      if (!showHidden.value && isHidden(subdir)) continue
      const items = subtreeCache[subdir] || []
      const filtered = items.filter(item => item.name.toLowerCase().includes(kw))
      if (filtered.length === 0) continue

      const parentPath = obsBasePath.value + subdir + '/'
      const children = buildPathTree(filtered, parentPath)
      resultNodes.push({
        label: subdir,
        path: parentPath,
        is_dir: true,
        children,
      })
    }
    searchTreeData.value = resultNodes
  } finally {
    searchLoading.value = false
  }
}

// ============ 文件操作 ============

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

function handleShortcut(cmd) {
  const keywords = {
    gateway: 'gateway.log',
    run: 'run.log',
    evaluator: 'evaluator_use.log',
  }
  searchKeyword.value = keywords[cmd] || ''
  doSearch()
}

onMounted(async () => {
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loadTopDirs()
  loadEvalStats()
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
.tree-size { margin-left: 6px; font-size: 11px; color: #999; }
.preview-content {
  background: #1e293b; color: #e2e8f0; padding: 12px; margin: 0;
  border-radius: 0 0 var(--radius-sm) var(--radius-sm); flex: 1; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}

/* 评估统计: 两列布局 7:3 */
.eval-two-col { display: flex; gap: 24px; align-items: stretch; }
.eval-stat-grid {
  flex: 7;
  display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px;
  align-content: center;
}
.eval-stat-item { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
.eval-stat-num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.2; }
.eval-stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

/* 柱状图 */
.score-chart { flex: 3; min-width: 0; }
.score-chart-title { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; }
.score-chart-body {
  display: flex; align-items: flex-end; gap: 2px;
  height: 120px; padding-bottom: 20px; position: relative;
  border-bottom: 1px solid #e5e7eb;
}
.score-chart-col {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  height: 100%; position: relative;
}
.score-chart-count {
  font-size: 10px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; top: 0; white-space: nowrap;
}
.score-chart-bar-wrap {
  flex: 1; width: 100%; display: flex; align-items: flex-end;
  padding-top: 16px;
}
.score-chart-bar {
  width: 100%; max-width: 32px; margin: 0 auto;
  background: linear-gradient(180deg, #6366f1, #8b5cf6);
  border-radius: 3px 3px 0 0; transition: height 0.3s ease;
  min-height: 2px;
}
.score-chart-label {
  font-size: 10px; color: var(--text-muted); font-variant-numeric: tabular-nums;
  position: absolute; bottom: -18px; white-space: nowrap;
}
</style>
