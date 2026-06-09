<template>
  <div>
    <h2 style="margin-bottom:20px">新建任务实例</h2>

    <el-form :model="form" label-width="140px" style="max-width:800px">
      <el-form-item label="实例名称" required>
        <el-input v-model="form.name" placeholder="例如：web_skill_test_0608" />
      </el-form-item>

      <el-form-item label="任务标识 (user_id)" required>
        <el-input v-model="form.task_name" placeholder="用于Pod命名和OBS路径，例如：zx0608_webtest" />
      </el-form-item>

      <el-form-item label="并发数">
        <el-input-number v-model="form.concurrent_num" :min="1" :max="500" />
      </el-form-item>

      <el-divider>OBS 目录配置（点击输入框右侧按钮从OBS选择）</el-divider>

      <el-form-item label="技能目录">
        <el-input v-model="form.skill_dir" placeholder="skills/260325/skill_localize/skills_library">
          <template #append>
            <el-button @click="openObsBrowser('skill_dir')"><el-icon><FolderOpened /></el-icon></el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="Agent 目录">
        <el-input v-model="form.agent_dir" placeholder="task_data/260413_noenv/noenv_configs/agents">
          <template #append>
            <el-button @click="openObsBrowser('agent_dir')"><el-icon><FolderOpened /></el-icon></el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="用户Config目录" required>
        <el-input v-model="form.user_config_dir" placeholder="task_data/260520/web_configs">
          <template #append>
            <el-button @click="openObsBrowser('user_config_dir')"><el-icon><FolderOpened /></el-icon></el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="用户Profile目录">
        <el-input v-model="form.user_profile_dir" placeholder="可选">
          <template #append>
            <el-button @click="openObsBrowser('user_profile_dir')"><el-icon><FolderOpened /></el-icon></el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="轨迹保存路径">
        <el-input v-model="form.traj_save_path" placeholder="自动生成：openclaw_trajs/traj_{task_name}" />
      </el-form-item>

      <el-divider>模型配置 (openclaw.json)</el-divider>

      <el-form-item label="模型 API Key">
        <el-input v-model="form.model_api_key" placeholder="留空使用模板默认值" show-password />
      </el-form-item>

      <el-form-item label="模型 Base URL">
        <el-input v-model="form.model_base_url" placeholder="例如：http://192.168.30.95:8084" />
      </el-form-item>

      <el-form-item label="模型 ID">
        <el-input v-model="form.model_id" placeholder="例如：claude-opus-4-7-thinking">
          <template #prepend>local/</template>
        </el-input>
        <div style="font-size:12px;color:#999;margin-top:4px">同时更新 agents.defaults.model.primary 和 models[0].id/name</div>
      </el-form-item>

      <el-divider>User Proxy 模型配置 (user_proxy_model.json)</el-divider>

      <el-form-item label="模型名称">
        <el-input v-model="form.user_proxy_model_name" placeholder="例如：gemini-3-flash-preview" />
      </el-form-item>

      <el-form-item label="API Key">
        <el-input v-model="form.user_proxy_api_key" placeholder="留空使用模板默认值" show-password />
      </el-form-item>

      <el-form-item label="Base URL">
        <el-input v-model="form.user_proxy_base_url" placeholder="例如：http://192.168.30.95:8084" />
      </el-form-item>

      <el-divider>高级配置</el-divider>

      <el-form-item label="起始索引">
        <el-input-number v-model="form.start_index" :min="0" />
      </el-form-item>

      <el-form-item label="任务总数">
        <el-input-number v-model="form.total_num" :min="0" />
        <span style="margin-left:8px;color:#999;font-size:12px">0 表示不限制</span>
      </el-form-item>

      <el-form-item label="镜像名称">
        <el-input v-model="form.image_name" placeholder="使用模板默认镜像" />
      </el-form-item>

      <el-form-item>
        <el-button type="primary" @click="handleCreate" :loading="creating">创建实例</el-button>
        <el-button @click="$router.push('/dashboard')">取消</el-button>
      </el-form-item>
    </el-form>

    <!-- OBS Browser Dialog -->
    <el-dialog v-model="obsVisible" title="选择OBS目录" width="700px" destroy-on-close>
      <div style="margin-bottom:12px">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item v-for="(seg, idx) in pathSegments" :key="idx"
            @click="navigateTo(idx)" style="cursor:pointer">
            {{ seg || 'obs://rl-agentdata' }}
          </el-breadcrumb-item>
        </el-breadcrumb>
      </div>
      <el-table :data="obsItems" v-loading="obsLoading" max-height="400" @row-click="handleObsClick" style="cursor:pointer">
        <el-table-column label="名称" min-width="300">
          <template #default="{row}">
            <el-icon v-if="row.is_dir" style="color:#e6a23c"><Folder /></el-icon>
            <el-icon v-else style="color:#909399"><Document /></el-icon>
            <span style="margin-left:8px">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="size" label="大小" width="100" />
      </el-table>
      <template #footer>
        <el-button @click="obsVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmObsSelect">选择当前目录</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'

const router = useRouter()
const creating = ref(false)

const form = ref({
  name: '', task_name: '', concurrent_num: 100,
  skill_dir: '', agent_dir: '', user_config_dir: '', user_profile_dir: '',
  traj_save_path: '', start_index: 0, total_num: 0, image_name: '',
  model_api_key: '', model_base_url: '', model_id: '',
  user_proxy_model_name: '', user_proxy_api_key: '', user_proxy_base_url: '',
})

async function handleCreate() {
  if (!form.value.name || !form.value.task_name || !form.value.user_config_dir) {
    ElMessage.warning('请填写实例名称、任务标识和用户Config目录')
    return
  }
  creating.value = true
  try {
    await api.post('/instances', form.value)
    ElMessage.success('创建成功')
    router.push('/dashboard')
  } finally { creating.value = false }
}

// OBS Browser
const obsVisible = ref(false)
const obsLoading = ref(false)
const obsItems = ref([])
const obsCurrentPath = ref('obs://rl-agentdata/')
let obsTargetField = ''

const pathSegments = computed(() => {
  const p = obsCurrentPath.value.replace('obs://rl-agentdata/', '')
  return ['', ...p.split('/').filter(Boolean)]
})

function openObsBrowser(field) {
  obsTargetField = field
  obsCurrentPath.value = 'obs://rl-agentdata/'
  obsVisible.value = true
  loadObsDir()
}

async function loadObsDir() {
  obsLoading.value = true
  try {
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value } })
    obsItems.value = res.items
  } finally { obsLoading.value = false }
}

function handleObsClick(row) {
  if (row.is_dir) {
    obsCurrentPath.value = row.path
    if (!obsCurrentPath.value.endsWith('/')) obsCurrentPath.value += '/'
    loadObsDir()
  }
}

function navigateTo(idx) {
  const segs = pathSegments.value.slice(1, idx + 1)
  obsCurrentPath.value = 'obs://rl-agentdata/' + (segs.length ? segs.join('/') + '/' : '')
  loadObsDir()
}

function confirmObsSelect() {
  const relative = obsCurrentPath.value.replace('obs://rl-agentdata/', '').replace(/\/$/, '')
  form.value[obsTargetField] = relative
  obsVisible.value = false
}
</script>
