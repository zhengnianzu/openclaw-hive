<template>
  <div>
    <div class="header-row">
      <h2>代码仓管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="openCreateDialog">
        <el-icon><Plus /></el-icon> 新建代码仓
      </el-button>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="repos" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="name" label="名称" min-width="140" show-overflow-tooltip resizable />
        <el-table-column prop="version" label="版本" width="80" align="center" resizable />
        <el-table-column prop="obs_path" label="OBS路径" min-width="260" show-overflow-tooltip resizable />
        <el-table-column prop="main_python_file" label="入口文件" width="200" show-overflow-tooltip resizable />
        <el-table-column prop="description" label="描述" min-width="140" show-overflow-tooltip resizable />
        <el-table-column prop="created_by" label="创建者" width="100" resizable />
        <el-table-column prop="created_at" label="创建时间" width="180" resizable />
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{row}">
            <div style="display:flex;align-items:center;gap:4px">
              <el-button size="small" type="success" :loading="downloadingId === row.id" @click="downloadRepo(row)">
                下载
              </el-button>
              <el-button size="small" @click="checkStatus(row)">检查状态</el-button>
              <el-button v-if="authStore.isOperator" size="small" type="danger" @click="deleteRepo(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="createVisible" title="新建代码仓" width="600px" destroy-on-close>
      <el-form :model="createForm" label-width="100px">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" placeholder="例如：openclaw-task" />
        </el-form-item>
        <el-form-item label="版本" required>
          <el-input v-model="createForm.version" placeholder="例如：v1" />
        </el-form-item>
        <el-form-item label="OBS路径" required>
          <el-input v-model="createForm.obs_path" placeholder="例如：obs://rl-agentdata/code/openclaw-task/v1/openclaw-task/">
            <template #append>
              <el-button @click="openObsBrowser('obs_path')">浏览</el-button>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="入口文件" required>
          <el-input v-model="createForm.main_python_file" placeholder="例如：harness_automation.py">
            <template #append>
              <el-button @click="openObsBrowser('main_python_file')">选择</el-button>
            </template>
          </el-input>
          <div style="font-size:12px;color:#999;margin-top:4px">从OBS代码目录中选择.py入口文件，只需填文件名</div>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="2" placeholder="可选描述" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="obsVisible" :title="obsMode === 'main_python_file' ? '选择入口文件' : '选择OBS目录'" width="700px" destroy-on-close>
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
        <el-input v-model="obsCurrentPath" placeholder="OBS 路径" size="small" @keyup.enter="loadObsDir">
          <template #prepend>路径</template>
        </el-input>
        <el-button size="small" @click="loadObsDir" :loading="obsLoading">加载</el-button>
      </div>
      <el-table :data="filteredObsItems" v-loading="obsLoading" max-height="400" @row-dblclick="handleObsDblClick">
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="size" label="大小" width="100" />
        <el-table-column label="类型" width="80">
          <template #default="{row}">{{ row.is_dir ? '目录' : '文件' }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="obsVisible = false">取消</el-button>
        <el-button v-if="obsMode !== 'main_python_file'" type="primary" @click="confirmObsSelect">选择当前路径</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const repos = ref([])
const loading = ref(false)
const downloadingId = ref(null)

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', version: 'v1', obs_path: '', description: '', main_python_file: 'openclaw_automation.py' })

async function loadRepos() {
  loading.value = true
  try {
    repos.value = await api.get('/code-repos')
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  createForm.value = { name: '', version: 'v1', obs_path: '', description: '', main_python_file: 'openclaw_automation.py' }
  createVisible.value = true
}

async function handleCreate() {
  if (!createForm.value.name || !createForm.value.obs_path) {
    ElMessage.warning('请填写名称和OBS路径')
    return
  }
  if (!createForm.value.main_python_file) {
    ElMessage.warning('请指定入口文件')
    return
  }
  creating.value = true
  try {
    await api.post('/code-repos', createForm.value)
    ElMessage.success('创建成功')
    createVisible.value = false
    loadRepos()
  } finally {
    creating.value = false
  }
}

async function downloadRepo(row) {
  downloadingId.value = row.id
  try {
    const res = await api.post(`/code-repos/${row.id}/download`)
    if (res.already_exists) {
      ElMessage.info(`已打包: ${res.tar_path}`)
    } else {
      ElMessage.success(`下载并打包成功: ${res.tar_path}`)
    }
  } finally {
    downloadingId.value = null
  }
}

async function checkStatus(row) {
  try {
    const res = await api.get(`/code-repos/${row.id}/status`)
    if (res.packaged) {
      ElMessage.success(`已打包: ${res.tar_path}`)
    } else if (res.downloaded) {
      ElMessage.warning('已下载但未打包，请点击"下载"触发打包')
    } else {
      ElMessage.warning('尚未下载到本地')
    }
  } catch {
    ElMessage.error('检查状态失败')
  }
}

async function deleteRepo(row) {
  await ElMessageBox.confirm('确认删除该代码仓记录？（不会删除本地已下载的文件）', '提示', { type: 'warning' })
  await api.delete(`/code-repos/${row.id}`)
  ElMessage.success('已删除')
  loadRepos()
}

// OBS Browser
const obsVisible = ref(false)
const obsLoading = ref(false)
const obsItems = ref([])
const obsCurrentPath = ref('obs://rl-agentdata/code/')
const obsMode = ref('obs_path')

const filteredObsItems = computed(() => {
  let items = obsItems.value.filter(item => {
    if (!item.is_dir) return true
    return !item.name.startsWith('.') && item.name !== '__pycache__' && item.name !== 'node_modules'
  })
  if (obsMode.value === 'main_python_file') {
    items = items.filter(item => item.is_dir || item.name.endsWith('.py'))
  }
  return items
})

function openObsBrowser(mode) {
  obsMode.value = mode
  if (mode === 'main_python_file') {
    obsCurrentPath.value = createForm.value.obs_path || 'obs://rl-agentdata/code/'
  } else {
    obsCurrentPath.value = createForm.value.obs_path || 'obs://rl-agentdata/code/'
  }
  obsItems.value = []
  obsVisible.value = true
  loadObsDir()
}

async function loadObsDir() {
  obsLoading.value = true
  try {
    const showFiles = obsMode.value === 'main_python_file'
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value, show_files: showFiles } })
    obsItems.value = res.items || []
  } finally {
    obsLoading.value = false
  }
}

function handleObsDblClick(row) {
  if (row.is_dir) {
    obsCurrentPath.value = row.path
    if (!obsCurrentPath.value.endsWith('/')) obsCurrentPath.value += '/'
    loadObsDir()
  } else if (obsMode.value === 'main_python_file' && row.name.endsWith('.py')) {
    createForm.value.main_python_file = row.name
    obsVisible.value = false
    ElMessage.success(`已选择入口文件: ${row.name}`)
  }
}

function confirmObsSelect() {
  createForm.value.obs_path = obsCurrentPath.value
  obsVisible.value = false
}

onMounted(loadRepos)
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
h2 { color: var(--text-primary); font-size: 24px; font-weight: 700; }
</style>
