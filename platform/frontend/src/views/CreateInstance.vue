<template>
  <div>
    <h2 class="page-title">新建任务实例</h2>

    <div class="harness-switcher">
      <el-radio-group v-model="form.harness_type" @change="onHarnessChange" size="large">
        <el-radio-button value="openclaw">OpenClaw</el-radio-button>
        <el-radio-button value="hermes">Hermes</el-radio-button>
      </el-radio-group>
    </div>

    <div class="glass-card" style="max-width:800px">
    <el-form :model="form" label-width="140px">
      <el-form-item label="实例名称" required>
        <el-input v-model="form.name" placeholder="例如：web_skill_test_0608" />
      </el-form-item>

      <el-form-item label="任务标识 (user_id)" required>
        <el-input v-model="form.task_name" placeholder="用于Pod命名和OBS路径，例如：zx0608_webtest" />
      </el-form-item>

      <el-form-item label="并发数">
        <el-input-number v-model="form.concurrent_num" :min="1" :max="500" />
      </el-form-item>

      <el-tabs v-model="activeTab" style="margin-top:8px">
        <el-tab-pane label="OBS配置" name="obs">
          <el-form-item label="技能目录">
            <el-input v-model="form.skill_dir" placeholder="skills/260325/skill_localize/skills_library">
              <template #append>
                <el-button @click="openObsBrowser('skill_dir')"><el-icon><FolderOpened /></el-icon></el-button>
              </template>
            </el-input>
          </el-form-item>

          <el-form-item label="默认技能">
            <el-input v-model="form.default_skills" placeholder="find-skills,skill-scope（逗号分隔，留空使用模板默认值）" />
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
        </el-tab-pane>

        <el-tab-pane label="Harness配置" name="model">
          <el-form-item label="模型 Base URL">
            <el-input v-model="form.model_base_url" placeholder="例如：http://192.168.30.95:8084" />
          </el-form-item>

          <el-form-item label="模型 API Key">
            <div style="display:flex;gap:8px;width:100%">
              <el-input v-model="form.model_api_key" placeholder="留空使用模板默认值" style="flex:1" />
              <el-button type="primary" @click="openKeyDialog">新建KEY</el-button>
            </div>
            <div v-if="form.model_api_key" style="font-size:12px;color:#999;margin-top:4px">
              {{ form.model_api_key.length > 8 ? form.model_api_key.slice(0, 4) + '****' + form.model_api_key.slice(-4) : '' }}
            </div>
          </el-form-item>

          <el-form-item v-if="form.harness_type !== 'hermes'" label="API 类型">
            <el-select v-model="form.model_api_type" placeholder="留空使用模板默认值" clearable style="width:100%">
              <el-option label="Anthropic Messages" value="anthropic-messages" />
              <el-option label="OpenAI Completions" value="openai-completions" />
            </el-select>
          </el-form-item>

          <el-form-item label="模型 ID">
            <el-input v-model="form.model_id" placeholder="例如：claude-opus-4-7-thinking">
              <template v-if="form.harness_type !== 'hermes'" #prepend>local/</template>
            </el-input>
            <div v-if="form.harness_type !== 'hermes'" style="font-size:12px;color:#999;margin-top:4px">同时更新 agents.defaults.model.primary 和 models[0].id/name</div>
          </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="用户模拟配置" name="proxy">
          <el-form-item label="模型名称">
            <el-input v-model="form.user_proxy_model_name" placeholder="例如：gemini-3-flash-preview" />
          </el-form-item>

          <el-form-item label="API Key">
            <el-input v-model="form.user_proxy_api_key" placeholder="留空使用模板默认值" show-password />
          </el-form-item>

          <el-form-item label="Base URL">
            <el-input v-model="form.user_proxy_base_url" placeholder="例如：http://192.168.30.95:8084" />
          </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="高级配置" name="advanced">
          <el-form-item label="起始索引">
            <el-input-number v-model="form.start_index" :min="0" />
          </el-form-item>

          <el-form-item label="任务总数">
            <el-input-number v-model="form.total_num" :min="0" />
            <span style="margin-left:8px;color:#999;font-size:12px">0 表示不限制</span>
          </el-form-item>

          <el-form-item label="镜像名称">
            <el-select v-model="form.image_name" filterable allow-create default-first-option
              placeholder="选择或输入镜像地址" style="width:100%" clearable>
              <el-option v-for="img in imageList" :key="img.id" :label="img.name" :value="img.address">
                <span>{{ img.name }}</span>
                <span style="float:right;color:#999;font-size:12px">{{ img.address.length > 40 ? '...' + img.address.slice(-40) : img.address }}</span>
              </el-option>
            </el-select>
          </el-form-item>
        </el-tab-pane>
      </el-tabs>

      <el-form-item style="margin-top:20px">
        <el-button type="primary" @click="handleCreate" :loading="creating">创建实例</el-button>
        <el-button @click="$router.push('/dashboard')">取消</el-button>
      </el-form-item>
    </el-form>
    </div>
    <el-dialog v-model="keyDialogVisible" title="新建 API Key" width="480px" destroy-on-close>
      <el-form label-width="100px">
        <el-form-item label="Invite Code">
          <el-input v-model="keyForm.invite_code" placeholder="pangu" />
        </el-form-item>
        <el-form-item label="Name">
          <el-input v-model="keyForm.name" placeholder="mtime-任务名称" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="generateApiKey" :loading="keyGenerating">生成</el-button>
      </template>
    </el-dialog>

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
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'

const router = useRouter()
const route = useRoute()
const creating = ref(false)
const activeTab = ref('obs')

const keyDialogVisible = ref(false)
const keyGenerating = ref(false)
const keyForm = ref({
  invite_code: localStorage.getItem('last_invite_code') || 'pangu',
  name: '',
})

function openKeyDialog() {
  const now = new Date()
  const ts = [
    String(now.getFullYear()).slice(2),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
  ].join('')
  keyForm.value.name = `${ts}-${form.value.name || '任务'}`
  keyDialogVisible.value = true
}

async function generateApiKey() {
  keyGenerating.value = true
  try {
    const params = {
      invite_code: keyForm.value.invite_code,
      name: keyForm.value.name,
    }
    if (form.value.model_base_url) {
      params.base_url = form.value.model_base_url
    }
    const res = await api.post('/generate-api-key', null, { params })
    form.value.model_api_key = res.api_key
    localStorage.setItem('last_invite_code', keyForm.value.invite_code)
    ElMessage.success(`API Key 已生成: ${res.api_key.slice(0, 4)}****${res.api_key.slice(-4)}`)
    keyDialogVisible.value = false
  } catch {
    // error already shown by api interceptor
  } finally {
    keyGenerating.value = false
  }
}

const form = ref({
  name: '', task_name: '', concurrent_num: 100,
  skill_dir: '', default_skills: '', agent_dir: '', user_config_dir: '', user_profile_dir: '',
  traj_save_path: '', start_index: 0, total_num: 0, image_name: '',
  model_api_key: '', model_base_url: '', model_api_type: '', model_id: '',
  user_proxy_model_name: '', user_proxy_api_key: '', user_proxy_base_url: '',
  harness_type: 'openclaw',
})

const imageList = ref([])

async function loadImages() {
  try {
    imageList.value = await api.get('/images', { params: { harness_type: form.value.harness_type } })
  } catch { imageList.value = [] }
}

function onHarnessChange() {
  form.value.image_name = ''
  loadImages()
}

async function handleCreate() {
  if (!form.value.name || !form.value.task_name || !form.value.user_config_dir) {
    ElMessage.warning('请填写实例名称、任务标识和用户Config目录')
    return
  }
  creating.value = true
  try {
    const res = await api.post('/instances', form.value)
    const fromRegistration = route.query.from_registration
    if (fromRegistration && res.id) {
      try {
        await api.put(`/registrations/${fromRegistration}/link?instance_id=${res.id}`)
      } catch { /* non-critical */ }
    }
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

onMounted(async () => {
  const copyFrom = route.query.copy_from
  const fromRegistration = route.query.from_registration
  if (copyFrom) {
    try {
      const params = await api.get(`/instances/${copyFrom}/create-params`)
      Object.assign(form.value, params)
      form.value.name = params.name + '-copy'
      form.value.task_name = ''
      ElMessage.info('已从已有实例复制配置，请修改任务标识后创建')
    } catch {
      ElMessage.warning('无法加载源实例配置')
    }
  } else if (fromRegistration) {
    try {
      const reg = await api.get(`/registrations/${fromRegistration}`)
      form.value.name = `${reg.task_name}-${reg.requester || 'task'}`
      form.value.task_name = reg.task_name
      form.value.user_config_dir = reg.task_path_obs
      form.value.skill_dir = reg.skill_dir_obs
      form.value.agent_dir = reg.agent_dir_obs
      form.value.total_num = reg.data_total || 0
      form.value.harness_type = reg.harness_type || 'openclaw'
      if (reg.model_name) form.value.model_id = reg.model_name
      if (reg.eval_model_name) form.value.user_proxy_model_name = reg.eval_model_name
      ElMessage.info('已从任务登记预填配置')
    } catch {
      ElMessage.warning('无法加载登记信息')
    }
  }
  loadImages()
})
</script>

<style scoped>
.page-title {
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 24px;
}
.harness-switcher {
  margin-bottom: 16px;
  max-width: 800px;
}
</style>
