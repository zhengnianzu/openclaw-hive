<template>
  <div>
    <el-page-header @back="$router.push('/registrations')" style="margin-bottom:24px">
      <template #content>提交任务登记</template>
    </el-page-header>

    <el-form :model="form" label-width="140px" style="max-width:700px">
      <el-form-item label="任务名称" required>
        <el-input v-model="form.task_name" placeholder="请输入任务名称" />
      </el-form-item>
      <el-form-item label="需求方">
        <el-input v-model="form.requester" placeholder="需求方名称" />
      </el-form-item>
      <el-form-item label="任务路径OBS">
        <el-input v-model="form.task_path_obs" placeholder="OBS路径，如 obs://bucket/path/">
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
      <el-form-item>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">提交登记</el-button>
      </el-form-item>
    </el-form>

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
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import api from '../api'

const router = useRouter()
const submitting = ref(false)
const form = ref({
  task_name: '',
  requester: '',
  task_path_obs: '',
  data_total: 0,
  skill_dir_obs: '',
  agent_dir_obs: '',
  user_folder_obs: '',
})

async function handleSubmit() {
  if (!form.value.task_name) {
    ElMessage.warning('请输入任务名称')
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
  obsCurrentPath.value = form.value[field] || ''
  obsItems.value = []
  obsDialogVisible.value = true
  if (obsCurrentPath.value) loadObsItems()
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
</script>
