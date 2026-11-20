<template>
  <div>
    <div class="header-row">
      <h2>镜像管理</h2>
      <el-button v-if="authStore.isOperator" type="primary" @click="dialogVisible = true">
        <el-icon><Plus /></el-icon> 注册镜像
      </el-button>
    </div>

    <el-alert type="info" :closable="false" style="margin-bottom:12px">
      <template #title>镜像命名规范（名称中请携带日期）</template>
      支持三种写法：<code>26.11.4</code>（年.月.日）、<code>20261102</code>（年月日连写）、
      <code>jiuwenswarm:dev-stable-2026.10.23v1</code>（任意位置含日期即可）。<br>
      推荐规则：当前 Harness 有专属镜像时优先推荐专属里日期最新的，否则推荐通用（如 multiharness 主镜像）里日期最新的；
      名称中无日期的镜像不参与日期排序，按注册时间兜底。<b>停用</b>只影响创建页下拉可见性，存量实例不受影响。
    </el-alert>

    <div class="filter-bar">
      <el-select v-model="typeFilter" placeholder="Harness 类型" clearable size="default" style="width:160px">
        <el-option label="通用" value="common" />
        <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
      </el-select>
      <el-select v-model="osFilter" placeholder="操作系统" clearable size="default" style="width:140px">
        <el-option v-for="o in OS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="pagedImages" v-loading="loading" stripe style="width:100%" border>
        <el-table-column prop="id" label="序号" width="80" align="center" />
        <el-table-column prop="name" label="镜像名称" min-width="160" />
        <el-table-column prop="address" label="镜像地址" min-width="360" show-overflow-tooltip />
        <el-table-column prop="harness_type" label="Harness 类型" width="130" align="center">
          <template #default="{ row }">
            <el-tag :style="harnessTagStyle(row.harness_type)" size="small">
              {{ harnessLabel(row.harness_type) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="os" label="操作系统" width="110" align="center">
          <template #default="{ row }">
            <el-tag :type="osTagType(row.os)" size="small">{{ osLabel(row.os) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="remark" label="备注" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.remark">{{ row.remark }}</span>
            <span v-else class="remark-empty">—</span>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90" align="center">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled !== false" @change="v => toggleEnabled(row, v)" />
          </template>
        </el-table-column>
        <el-table-column prop="created_by" label="注册人" width="120" />
        <el-table-column prop="created_at" label="注册时间" width="180" />
        <el-table-column label="操作" width="150" align="center" fixed="right" v-if="authStore.isOperator">
          <template #default="{ row }">
            <div class="op-cell">
              <el-button size="small" type="primary" @click="openEdit(row)">编辑</el-button>
              <el-dropdown trigger="click" @command="cmd => handleMore(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="delete" style="color:#f56c6c">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="pagination-bar">
      <span class="total-text">共 {{ filteredImages.length }} 条</span>
      <el-pagination v-model:current-page="currentPage" v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]" :total="filteredImages.length"
        layout="sizes, prev, pager, next" background />
    </div>

    <el-dialog v-model="dialogVisible" title="注册镜像" width="520px" destroy-on-close>
      <el-form :model="form" label-width="120px">
        <el-form-item label="镜像名称" required>
          <el-input v-model="form.name" placeholder="例如：hermes-0.16" />
        </el-form-item>
        <el-form-item label="镜像地址" required>
          <el-input v-model="form.address" placeholder="例如：swr.cn-southwest-2.myhuaweicloud.com/rl_team/hermes:0.16.0" />
        </el-form-item>
        <el-form-item label="Harness 类型" required>
          <el-select v-model="form.harness_type" style="width:100%">
            <el-option label="通用" value="common" />
            <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
          </el-select>
        </el-form-item>
        <el-form-item label="操作系统" required>
          <el-radio-group v-model="form.os">
            <el-radio v-for="o in OS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="form.remark"
            type="textarea"
            :rows="2"
            placeholder="可选：该镜像相对上一版的改进点"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleCreate" :loading="creating">注册</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑镜像" width="520px" destroy-on-close>
      <el-form :model="editForm" label-width="120px">
        <el-form-item label="镜像名称" required>
          <el-input v-model="editForm.name" placeholder="例如：hermes-0.16" />
        </el-form-item>
        <el-form-item label="镜像地址">
          <!-- 只读：实例与配置模板引用的是地址，改了会对不上历史任务 -->
          <el-input :model-value="editForm.address" disabled />
          <div class="form-hint">镜像地址不可修改</div>
        </el-form-item>
        <el-form-item label="操作系统" required>
          <el-radio-group v-model="editForm.os">
            <el-radio v-for="o in OS_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="editForm.remark"
            type="textarea"
            :rows="3"
            placeholder="该镜像相对上一版的改进点"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="handleUpdate" :loading="updating">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import api from '../api'

const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const loading = ref(false)
const creating = ref(false)
const updating = ref(false)
const dialogVisible = ref(false)
const editVisible = ref(false)
const typeFilter = ref('')
const osFilter = ref('')
const currentPage = ref(1)
const pageSize = ref(10)
const images = ref([])

const OS_OPTIONS = [
  { value: 'linux', label: 'Linux' },
  { value: 'windows', label: 'Windows' },
]

const form = ref({ name: '', address: '', harness_type: 'openclaw', os: 'linux', remark: '' })
// 编辑弹窗的表单。address 只读展示，不随保存提交。
const editForm = ref({ id: null, name: '', address: '', os: 'linux', remark: '' })

function harnessColor(type) { return harnessStore.color(type) }
function harnessTagStyle(type) { return harnessStore.tagStyle(type) }
function harnessLabel(type) { return harnessStore.label(type) }

// 历史行在后端迁移时已回填，这里的兜底只为「字段还没下发」的瞬间
function osLabel(os) {
  return OS_OPTIONS.find(o => o.value === os)?.label || 'Linux'
}
function osTagType(os) {
  return os === 'windows' ? 'warning' : 'success'
}

const filteredImages = computed(() => {
  return images.value.filter(i =>
    (!typeFilter.value || i.harness_type === typeFilter.value) &&
    (!osFilter.value || (i.os || 'linux') === osFilter.value)
  )
})

const pagedImages = computed(() => {
  const start = (currentPage.value - 1) * pageSize.value
  return filteredImages.value.slice(start, start + pageSize.value)
})

// 换筛选条件或每页条数时回到第一页，否则会停在一个空页上
watch([typeFilter, osFilter, pageSize], () => { currentPage.value = 1 })
// 删除导致当前页越界时回退（例如在最后一页删掉唯一一条）
watch(() => filteredImages.value.length, (n) => {
  const maxPage = Math.max(1, Math.ceil(n / pageSize.value))
  if (currentPage.value > maxPage) currentPage.value = maxPage
})

// 启用开关: 只影响创建页下拉可见性; 存量实例引用的是地址快照, 不受影响
async function toggleEnabled(row, v) {
  try {
    await api.patch(`/images/${row.id}`, { enabled: v })
    row.enabled = v
    ElMessage.success(v ? '已启用' : '已停用（创建页不再列出）')
  } catch (e) {
    ElMessage.error('切换失败')
  }
}

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
    form.value = { name: '', address: '', harness_type: 'openclaw', os: 'linux', remark: '' }
    // 后端按 created_at DESC 排序，新镜像在第一页；留在原页会看不到刚注册的那条
    currentPage.value = 1
    loadImages()
  } finally { creating.value = false }
}

function openEdit(row) {
  editForm.value = {
    id: row.id,
    name: row.name,
    address: row.address,
    os: row.os || 'linux',
    remark: row.remark || '',
  }
  editVisible.value = true
}

async function handleUpdate() {
  const f = editForm.value
  if (!f.name.trim()) {
    ElMessage.warning('请填写镜像名称')
    return
  }
  updating.value = true
  try {
    // 不传 address —— 后端也不接受，地址是实例/模板的引用标识
    await api.patch(`/images/${f.id}`, {
      name: f.name.trim(),
      os: f.os,
      remark: f.remark.trim(),
    })
    ElMessage.success('已保存')
    editVisible.value = false
    loadImages()
  } finally { updating.value = false }
}

function handleMore(cmd, row) {
  if (cmd === 'delete') handleDelete(row)
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确认删除镜像 "${row.name}"？`, '确认删除', { type: 'warning' })
    await api.delete(`/images/${row.id}`)
    ElMessage.success('已删除')
    loadImages()
  } catch { /* cancelled */ }
}

onMounted(() => { harnessStore.load(); loadImages() })
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
.pagination-bar { display: flex; align-items: center; justify-content: space-between; margin-top: 16px; }
.total-text { color: var(--text-muted); font-size: 13px; }
/* 操作区：强制不换行，否则「更多」下拉会被挤到第二行、撑高整行 */
.op-cell { display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }
.remark-empty {
  color: #999;
}
.form-hint {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.4;
  margin-top: 4px;
}
</style>
