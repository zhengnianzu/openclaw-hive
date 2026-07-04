<template>
  <div>
    <div class="header-row">
      <h2>镜像管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="dialogVisible = true">
        <el-icon><Plus /></el-icon> 注册镜像
      </el-button>
    </div>

    <div class="filter-bar">
      <el-select v-model="typeFilter" placeholder="Harness 类型" clearable size="default" style="width:160px">
        <el-option label="OpenClaw" value="openclaw" />
        <el-option label="Hermes" value="hermes" />
      </el-select>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="filteredImages" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="name" label="镜像名称" min-width="160" />
        <el-table-column prop="address" label="镜像地址" min-width="360" show-overflow-tooltip />
        <el-table-column prop="harness_type" label="Harness 类型" width="130" align="center">
          <template #default="{ row }">
            <el-tag :type="row.harness_type === 'hermes' ? 'warning' : 'primary'" size="small">
              {{ row.harness_type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="注册人" width="120" />
        <el-table-column prop="created_at" label="注册时间" width="180" />
        <el-table-column label="操作" width="100" align="center" v-if="authStore.isOperator">
          <template #default="{ row }">
            <el-button type="danger" text size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialogVisible" title="注册镜像" width="520px" destroy-on-close>
      <el-form :model="form" label-width="100px">
        <el-form-item label="镜像名称" required>
          <el-input v-model="form.name" placeholder="例如：hermes-0.16" />
        </el-form-item>
        <el-form-item label="镜像地址" required>
          <el-input v-model="form.address" placeholder="例如：swr.cn-southwest-2.myhuaweicloud.com/rl_team/hermes:0.16.0" />
        </el-form-item>
        <el-form-item label="Harness 类型" required>
          <el-radio-group v-model="form.harness_type">
            <el-radio-button value="openclaw">OpenClaw</el-radio-button>
            <el-radio-button value="hermes">Hermes</el-radio-button>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleCreate" :loading="creating">注册</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import api from '../api'

const authStore = useAuthStore()
const loading = ref(false)
const creating = ref(false)
const dialogVisible = ref(false)
const typeFilter = ref('')
const images = ref([])

const form = ref({ name: '', address: '', harness_type: 'openclaw' })

const filteredImages = computed(() => {
  if (!typeFilter.value) return images.value
  return images.value.filter(i => i.harness_type === typeFilter.value)
})

async function loadImages() {
  loading.value = true
  try {
    images.value = await api.get('/images')
  } finally { loading.value = false }
}

async function handleCreate() {
  if (!form.value.name || !form.value.address) {
    ElMessage.warning('请填写镜像名称和地址')
    return
  }
  creating.value = true
  try {
    await api.post('/images', form.value)
    ElMessage.success('镜像注册成功')
    dialogVisible.value = false
    form.value = { name: '', address: '', harness_type: 'openclaw' }
    loadImages()
  } finally { creating.value = false }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除镜像 "${row.name}"？`, '确认删除', { type: 'warning' })
    await api.delete(`/images/${row.id}`)
    ElMessage.success('已删除')
    loadImages()
  } catch { /* cancelled */ }
}

onMounted(loadImages)
</script>

<style scoped>
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}
.header-row h2 {
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 700;
  margin: 0;
}
.filter-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
</style>
