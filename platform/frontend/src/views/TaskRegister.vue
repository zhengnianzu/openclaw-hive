<template>
  <div>
    <el-page-header @back="$router.push('/registrations')" style="margin-bottom:24px">
      <template #content>{{ isCopy ? '复制数据集' : '新建数据集' }}</template>
    </el-page-header>

    <div class="glass-card" style="max-width:700px">
    <el-form :model="form" label-width="160px">
      <el-form-item label="任务名称" required>
        <el-input v-model="form.task_name" placeholder="请输入任务名称" />
      </el-form-item>
      <el-divider content-position="left">OBS 配置</el-divider>

      <el-form-item label="技能目录">
        <el-input v-model="form.skill_dir_obs"
          placeholder="skills/260325/skill_localize/skills_library">
          <template #append>
            <el-button @click="openObsBrowser('skill_dir_obs')">
              <el-icon><FolderOpened /></el-icon>
            </el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="默认技能">
        <el-input v-model="form.default_skills"
          placeholder="find-skills,skill-scope（逗号分隔，留空使用模板默认值）" />
      </el-form-item>

      <el-form-item label="Agent 目录">
        <el-input v-model="form.agent_dir_obs"
          placeholder="task_data/260413_noenv/noenv_configs/agents">
          <template #append>
            <el-button @click="openObsBrowser('agent_dir_obs')">
              <el-icon><FolderOpened /></el-icon>
            </el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="用户Config目录" required>
        <el-input v-model="form.task_path_obs" placeholder="task_data/260520/web_configs">
          <template #append>
            <el-button @click="openObsBrowser('task_path_obs')">
              <el-icon><FolderOpened /></el-icon>
            </el-button>
          </template>
        </el-input>
        <div style="font-size:12px;color:#999;margin-top:4px">
          样本所在目录，批量实验按它作为测试集
        </div>
      </el-form-item>

      <el-form-item label="用户Profile目录">
        <el-input v-model="form.user_folder_obs" placeholder="可选">
          <template #append>
            <el-button @click="openObsBrowser('user_folder_obs')">
              <el-icon><FolderOpened /></el-icon>
            </el-button>
          </template>
        </el-input>
      </el-form-item>

      <el-form-item label="轨迹保存路径">
        <el-input v-model="form.traj_save_path"
          placeholder="自动生成：{harness}_trajs/traj_{task_name}" />
      </el-form-item>

      <el-form-item>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">提交</el-button>
      </el-form-item>
    </el-form>
    </div>

    <!-- OBS 浏览对话框：与「新建任务实例」的 OBS配置 同款交互
         （单击进目录、面包屑回跳、目录/文件图标、非法路径退回桶根）。 -->
    <el-dialog v-model="obsDialogVisible" title="选择OBS目录" width="700px" destroy-on-close>
      <!-- 路径输入框：可手工改、可全选复制（面包屑是按钮，选不中文本）。
           回车或点「加载」即跳转。 -->
      <div style="margin-bottom:8px;display:flex;gap:8px;align-items:center">
        <el-input v-model="obsCurrentPath" placeholder="OBS 路径" size="small"
          @keyup.enter="loadObsItems" spellcheck="false">
          <template #prepend>路径</template>
        </el-input>
        <el-button size="small" @click="loadObsItems" :loading="obsLoading">加载</el-button>
      </div>
      <div style="margin-bottom:12px">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item v-for="(seg, idx) in pathSegments" :key="idx"
            @click="navigateTo(idx)" style="cursor:pointer">
            {{ seg || obsBucket().replace(/\/$/, '') }}
          </el-breadcrumb-item>
        </el-breadcrumb>
      </div>
      <el-table :data="obsItems" v-loading="obsLoading" max-height="400"
        @row-click="handleObsClick" style="cursor:pointer">
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
        <el-button @click="obsDialogVisible = false">取消</el-button>
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
import { Folder, Document, FolderOpened } from '@element-plus/icons-vue'
import { obsBucket, stripBucket, joinBucket } from '../api/obsConfig'

const router = useRouter()
const route = useRoute()
const submitting = ref(false)
const isCopy = computed(() => !!route.query.copy_from)

// 数据集只描述「数据在哪」，Harness / 模型 / 配置模板都归任务实例决定，
// 所以这里只保留名称和 OBS 目录这几项。
const form = ref({
  task_name: '',
  skill_dir_obs: '',
  default_skills: '',
  agent_dir_obs: '',
  task_path_obs: '',
  user_folder_obs: '',
  traj_save_path: '',
})

async function handleSubmit() {
  if (!form.value.task_name) {
    ElMessage.warning('请输入任务名称')
    return
  }
  if (!form.value.task_path_obs) {
    ElMessage.warning('请选择用户Config目录')
    return
  }
  submitting.value = true
  try {
    await api.post('/registrations', form.value)
    ElMessage.success('数据集创建成功')
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

// 面包屑分段。首段是空串，渲染成桶名本身（见模板）。
const pathSegments = computed(() => {
  const p = stripBucket(obsCurrentPath.value)
  return ['', ...p.split('/').filter(Boolean)]
})

// 以字段里已填的路径为浏览起点。本页字段存的是带桶前缀的全路径
// （历史格式，消费方 CreateInstance/BatchCreate 都会再 stripBucket 一次），
// 经 stripBucket→joinBucket 归一化后交给 /obs/list。
function openObsBrowser(field) {
  obsTargetField = field
  const current = String(form.value[field] || '').trim()
  obsCurrentPath.value = current ? joinBucket(current) : obsBucket()
  obsItems.value = []
  obsDialogVisible.value = true
  loadObsItems()
}

// 起点可能是用户手打的、OBS 上并不存在的路径 —— obsutil 会非零退出，
// 后端转成 500（api/routers/obs.py）。这时退回桶根重列，而不是让对话框空着。
async function loadObsItems() {
  // 输入框可编辑，手打的值可能缺桶前缀/缺尾斜杠/带多余空格。
  // 统一归一化，顺带让面包屑能正确分段。
  obsCurrentPath.value = joinBucket(stripBucket(String(obsCurrentPath.value || '').trim()))
  obsLoading.value = true
  try {
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value }, silent: true })
    obsItems.value = res.items || []
  } catch (e) {
    const atRoot = obsCurrentPath.value === obsBucket()
    if (!atRoot) {
      ElMessage.warning(`路径 ${stripBucket(obsCurrentPath.value) || '/'} 无法浏览，已回到桶根`)
      obsCurrentPath.value = obsBucket()
      obsLoading.value = false
      return loadObsItems()
    }
    obsItems.value = []
    ElMessage.error(e.response?.data?.detail || e.message || 'OBS 列目录失败')
  } finally {
    obsLoading.value = false
  }
}

function handleObsClick(row) {
  if (row.is_dir) {
    obsCurrentPath.value = row.path
    if (!obsCurrentPath.value.endsWith('/')) obsCurrentPath.value += '/'
    loadObsItems()
  }
}

function navigateTo(idx) {
  const segs = pathSegments.value.slice(1, idx + 1)
  obsCurrentPath.value = joinBucket(segs.join('/'))
  loadObsItems()
}

// 存回带桶前缀的全路径，保持与表中既有数据同一格式。
function confirmObsSelect() {
  form.value[obsTargetField] = joinBucket(stripBucket(obsCurrentPath.value))
  obsDialogVisible.value = false
}

onMounted(async () => {
  const copyFrom = route.query.copy_from
  if (copyFrom) {
    try {
      const reg = await api.get(`/registrations/${copyFrom}`)
      form.value = {
        task_name: reg.task_name + '-copy',
        skill_dir_obs: reg.skill_dir_obs || '',
        default_skills: reg.default_skills || '',
        agent_dir_obs: reg.agent_dir_obs || '',
        task_path_obs: reg.task_path_obs || '',
        user_folder_obs: reg.user_folder_obs || '',
        traj_save_path: reg.traj_save_path || '',
      }
      ElMessage.info('已从已有数据集复制，请修改后提交')
    } catch {
      ElMessage.warning('无法加载源数据集信息')
    }
  }
})
</script>
