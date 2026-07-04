<template>
  <div>
    <el-page-header @back="$router.push('/registrations')" style="margin-bottom:24px">
      <template #content>{{ isCopy ? '复制任务登记' : '提交任务登记' }}</template>
    </el-page-header>

    <div class="glass-card" style="max-width:700px">
    <el-form :model="form" label-width="160px">
      <el-form-item label="任务名称" required>
        <el-input v-model="form.task_name" placeholder="请输入任务名称" />
      </el-form-item>
      <el-form-item label="需求方">
        <el-input v-model="form.requester" placeholder="需求方名称" />
      </el-form-item>
      <el-form-item label="Harness类型" required>
        <el-select v-model="form.harness_type" style="width:100%">
          <el-option label="Openclaw" value="openclaw" />
          <el-option label="Hermes" value="hermes" />
        </el-select>
      </el-form-item>
      <el-form-item label="Harness模型" required>
        <el-input v-model="form.model_name" placeholder="例如：claude-opus-4-7-thinking" />
      </el-form-item>
      <el-form-item label="用户模拟模型">
        <el-input v-model="form.eval_model_name" placeholder="对应模型ID" />
      </el-form-item>
      <el-form-item label="任务路径OBS">
        <el-input v-model="form.task_path_obs" placeholder="OBS路径，如 obs://rl-agentdata/path/">
          <template #append>
            <el-button @click="openObsBrowser('task_path_obs')">浏览</el-button>
          </template>
        </el-input>
      </el-form-item>
      <el-form-item label="数据总量">
        <el-input-number v-model="form.data_total" :min="0" :step="100" />
      </el-form-item>
      <el-form-item label="技能目录OBS">
        <el-input v-model="form.skill_dir_obs" placeholder="OBS路径">
          <template #append>
            <el-button @click="openObsBrowser('skill_dir_obs')">浏览</el-button>
          </template>
        </el-input>
      </el-form-item>
      <el-form-item label="Agent目录OBS">
        <el-input v-model="form.agent_dir_obs" placeholder="OBS路径">
          <template #append>
            <el-button @click="openObsBrowser('agent_dir_obs')">浏览</el-button>
          </template>
        </el-input>
      </el-form-item>
      <el-form-item label="用户文件夹OBS">
        <el-input v-model="form.user_folder_obs" placeholder="OBS路径">
          <template #append>
            <el-button @click="openObsBrowser('user_folder_obs')">浏览</el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-collapse style="margin-bottom:20px">
        <el-collapse-item title="高级配置" name="advanced">
          <el-form-item label="任务供应商URL">
            <el-input v-model="form.base_url" placeholder="选填，例如：http://192.168.30.95:8084" />
          </el-form-item>
          <el-form-item label="任务供应商KEY">
            <el-input v-model="form.api_key" placeholder="选填，仅管理员和登记人可查看完整KEY" show-password />
          </el-form-item>
        </el-collapse-item>
      </el-collapse>

      <el-form-item>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">提交登记</el-button>
      </el-form-item>
    </el-form>
    </div>

    <el-dialog v-model="obsDialogVisible" title="OBS 文件浏览" width="700px">
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
        <el-input v-model="obsCurrentPath" placeholder="OBS 路径" size="small" @keyup.enter="loadObsItems">
          <template #prepend>路径</template>
        </el-input>
        <el-button size="small" @click="loadObsItems" :loading="obsLoading">加载</el-button>
      </div>
      <el-table :data="obsItems" v-loading="obsLoading" max-height="400" @row-dblclick="handleObsDblClick">
        <el-table-column prop="name" label="名称" />
        <el-table-column prop="size" label="大小" width="100" />
        <el-table-column label="类型" width="80">
          <template #default="{row}">{{ row.is_dir ? '目录' : '文件' }}</template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="obsDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmObsSelect">选择当前路径</el-button>
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
const submitting = ref(false)
const isCopy = computed(() => !!route.query.copy_from)

const OBS_DEFAULT_PATH = 'obs://rl-agentdata/'

const form = ref({
  task_name: '',
  requester: '',
  task_path_obs: '',
  data_total: 0,
  skill_dir_obs: '',
  agent_dir_obs: '',
  user_folder_obs: '',
  model_name: '',
  eval_model_name: '',
  user_proxy_model_name: '',
  harness_type: 'openclaw',
  base_url: '',
  api_key: '',
})

async function handleSubmit() {
  if (!form.value.task_name) {
    ElMessage.warning('请输入任务名称')
    return
  }
  if (!form.value.model_name) {
    ElMessage.warning('请填写Harness模型名称')
    return
  }
  submitting.value = true
  try {
    await api.post('/registrations', form.value)
    ElMessage.success('登记成功')
    router.push('/registrations')
  } finally {
    submitting.value = false
  }
}

const obsDialogVisible = ref(false)
const obsCurrentPath = ref('')
const obsItems = ref([])
const obsLoading = ref(false)
let obsTargetField = ''

function openObsBrowser(field) {
  obsTargetField = field
  obsCurrentPath.value = form.value[field] || OBS_DEFAULT_PATH
  obsItems.value = []
  obsDialogVisible.value = true
  loadObsItems()
}

async function loadObsItems() {
  obsLoading.value = true
  try {
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value } })
    obsItems.value = res.items || []
  } finally {
    obsLoading.value = false
  }
}

function handleObsDblClick(row) {
  if (row.is_dir) {
    obsCurrentPath.value = row.path
    loadObsItems()
  }
}

function confirmObsSelect() {
  form.value[obsTargetField] = obsCurrentPath.value
  obsDialogVisible.value = false
}

onMounted(async () => {
  const copyFrom = route.query.copy_from
  if (copyFrom) {
    try {
      const reg = await api.get(`/registrations/${copyFrom}`)
      form.value = {
        task_name: reg.task_name + '-copy',
        requester: reg.requester || '',
        task_path_obs: reg.task_path_obs || '',
        data_total: reg.data_total || 0,
        skill_dir_obs: reg.skill_dir_obs || '',
        agent_dir_obs: reg.agent_dir_obs || '',
        user_folder_obs: reg.user_folder_obs || '',
        model_name: reg.model_name || '',
        eval_model_name: reg.eval_model_name || '',
        user_proxy_model_name: reg.user_proxy_model_name || '',
        harness_type: reg.harness_type || 'openclaw',
        base_url: reg.base_url || '',
        api_key: '',
      }
      ElMessage.info('已从已有登记复制，请修改后提交')
    } catch {
      ElMessage.warning('无法加载源登记信息')
    }
  }
})
</script>
