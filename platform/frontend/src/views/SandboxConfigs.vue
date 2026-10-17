<template>
  <div>
    <div class="header-row">
      <div>
        <h2>沙箱配置</h2>
        <p class="subtitle">统一管理任务运行所需的沙箱模板</p>
      </div>
      <el-button type="primary" v-if="authStore.isOperator" @click="openCreate">
        <el-icon><Plus /></el-icon> 新建配置
      </el-button>
    </div>

    <!-- 筛选条 -->
    <div class="filter-bar">
      <el-radio-group v-model="typeFilter" size="default">
        <el-radio-button label="">全部 ({{ allCounts.total }})</el-radio-button>
        <el-radio-button label="linux">Linux ({{ allCounts.linux }})</el-radio-button>
        <el-radio-button label="windows">Windows ({{ allCounts.windows }})</el-radio-button>
      </el-radio-group>
      <el-input v-model="search" placeholder="搜索配置名称" clearable class="search-input">
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
    </div>

    <div class="glass-card" style="padding:0;overflow:hidden">
      <el-table :data="pagedList" v-loading="loading" stripe style="width:100%">
        <el-table-column prop="name" label="配置名称" min-width="280">
          <template #default="{row}">
            <span style="font-weight:600">{{ row.name }}</span>
            <el-tag v-if="row.is_default" size="small" type="info" style="margin-left:8px">默认</el-tag>
            <el-tag v-if="!row.enabled" size="small" type="info" effect="plain" style="margin-left:8px">已停用</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" min-width="110" align="left">
          <template #default="{row}">
            <span class="sb-type">
              <span class="sb-icon" :class="row.sandbox_type"></span>
              {{ row.sandbox_type === 'windows' ? 'Windows' : 'Linux' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="resource_pool" label="资源池" min-width="120" show-overflow-tooltip />
        <el-table-column label="CPU 请求/限制" min-width="130">
          <template #default="{row}">{{ row.cpu_request }} / {{ row.cpu_limit }} 核</template>
        </el-table-column>
        <el-table-column label="内存请求/限制" min-width="130">
          <template #default="{row}">{{ row.memory_request }} / {{ row.memory_limit }}</template>
        </el-table-column>
        <el-table-column label="状态" min-width="100">
          <template #default="{row}">
            <span class="status-dot" :class="row.enabled ? 'on' : 'off'"></span>
            {{ row.enabled ? '启用' : '停用' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right" v-if="authStore.isOperator">
          <template #default="{row}">
            <div class="op-cell">
              <el-button size="small" @click="openView(row)">查看</el-button>
              <el-button size="small" type="primary" @click="openEdit(row)">编辑</el-button>
              <el-dropdown trigger="click" @command="cmd => handleMore(cmd, row)">
                <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="copy">复制</el-dropdown-item>
                    <el-dropdown-item v-if="!row.is_default" command="setDefault">设为默认</el-dropdown-item>
                    <el-dropdown-item v-if="row.enabled" command="disable">停用</el-dropdown-item>
                    <el-dropdown-item v-else command="enable">启用</el-dropdown-item>
                    <el-dropdown-item v-if="!row.is_default" command="delete" divided style="color:#f56c6c">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="table-footer">
      <span class="total-text">共 {{ filteredList.length }} 条</span>
      <el-pagination v-model:current-page="page" :page-size="pageSize" :total="filteredList.length"
        layout="prev, pager, next" background small />
    </div>

    <!-- 新建/编辑弹窗：左侧锚点 + 右侧五面板 -->
    <el-dialog v-model="editVisible" width="880px" top="6vh" destroy-on-close
      :close-on-click-modal="false" class="sandbox-dialog">
      <template #header>
        <div class="drawer-header">
          <div>
            <div class="drawer-title">
              {{ readOnly ? '查看沙箱配置' : (isEditing ? '编辑沙箱配置' : '新建沙箱配置') }}
              <el-tag v-if="isEditing && form.is_default" size="small" type="info" style="margin-left:8px">默认</el-tag>
              <el-tag v-if="isEditing && !form.enabled" size="small" type="info" effect="plain" style="margin-left:4px">已停用</el-tag>
            </div>
            <div class="drawer-sub">设置任务实例的沙箱运行环境</div>
          </div>
        </div>
      </template>

      <div class="editor-layout">
        <el-menu :default-active="activePanel" class="panel-nav">
          <el-menu-item index="basic" @click="activePanel='basic'">
            <el-icon><Document /></el-icon><span>基本信息</span>
          </el-menu-item>
          <el-menu-item index="resource" @click="activePanel='resource'">
            <el-icon><Grid /></el-icon><span>资源配置</span>
          </el-menu-item>
          <el-menu-item index="env" @click="activePanel='env'">
            <el-icon><Setting /></el-icon><span>环境变量</span>
          </el-menu-item>
          <el-menu-item index="timeout" @click="activePanel='timeout'">
            <el-icon><Timer /></el-icon><span>超时设置</span>
          </el-menu-item>
          <el-menu-item index="s3" @click="activePanel='s3'">
            <el-icon><Files /></el-icon><span>S3 上传配置</span>
          </el-menu-item>
          <el-menu-item v-if="isEditing" index="file" @click="activePanel='file'">
            <el-icon><EditPen /></el-icon><span>源文件</span>
          </el-menu-item>
        </el-menu>

        <div class="panel-body">
          <!-- 基本信息 -->
          <div v-show="activePanel === 'basic'">
            <h3>基本信息</h3>
            <el-form :model="form" :rules="rules" ref="formRef" label-position="top">
              <el-form-item label="配置名称" prop="name">
                <el-input v-model="form.name" placeholder="输入唯一的配置名称" />
              </el-form-item>
              <el-form-item label="沙箱类型">
                <el-radio-group v-model="form.sandbox_type" :disabled="isEditing">
                  <el-radio label="linux">
                    <span class="sb-type"><span class="sb-icon linux"></span> Linux</span>
                  </el-radio>
                  <el-radio label="windows">
                    <span class="sb-type"><span class="sb-icon windows"></span> Windows</span>
                  </el-radio>
                </el-radio-group>
                <div class="hint">类型决定沙箱块的结构，创建后不可修改</div>
              </el-form-item>
              <el-form-item label="状态" v-if="isEditing">
                <div style="display:flex;align-items:center;gap:8px">
                  <el-tag :type="form.enabled ? 'success' : 'info'" size="small">
                    {{ form.enabled ? '启用' : '停用' }}
                  </el-tag>
                  <el-tag v-if="form.is_default" type="info" size="small">默认配置</el-tag>
                  <span class="hint" style="margin:0">
                    {{ form.is_default
                      ? '默认配置不可删除或停用；新建任务时默认选中'
                      : '可编辑资源、超时与环境变量' }}
                  </span>
                </div>
              </el-form-item>

              <el-divider content-position="left">
                <span class="divider-text">控制面与命名空间</span>
              </el-divider>
              <el-form-item label="controlplane_base_url">
                <el-input v-model="form.controlplane_base_url" placeholder="如 http://192.168.11.87:30181" />
              </el-form-item>
              <el-form-item label="controlplane api_key">
                <el-input v-model="form.controlplane_api_key" type="password" show-password />
              </el-form-item>
              <el-form-item label="sandbox_namespace">
                <el-input v-model="form.sandbox_namespace" />
              </el-form-item>
              <el-form-item label="默认镜像">
                <el-input v-model="form.image" placeholder="留空则新建任务时从镜像下拉选择" />
              </el-form-item>
            </el-form>
          </div>

          <!-- 资源配置 -->
          <div v-show="activePanel === 'resource'">
            <h3>资源配置</h3>
            <p class="panel-sub">设置沙箱使用的资源池以及 CPU、内存规格</p>
            <el-form :model="form" label-position="top">
              <el-form-item label="资源池">
                <el-select v-model="form.resource_pool" filterable allow-create default-first-option
                  placeholder="选择或输入资源池" style="width:100%" @change="onPoolChange">
                  <el-option v-for="p in poolOptions" :key="p.name" :label="p.name" :value="p.name" />
                </el-select>
              </el-form-item>
              <div class="grid-2">
                <el-form-item label="CPU 请求（核）">
                  <el-input v-model="form.cpu_request" />
                </el-form-item>
                <el-form-item label="CPU 限制（核）">
                  <el-input v-model="form.cpu_limit" />
                </el-form-item>
                <el-form-item label="内存请求">
                  <el-input v-model="form.memory_request" />
                </el-form-item>
                <el-form-item label="内存限制">
                  <el-input v-model="form.memory_limit" />
                </el-form-item>
              </div>
              <div class="field-note">
                <div class="field-note-title">关于 Request 与 Limit</div>
                <ul>
                  <li><b>Request</b> 是保底用量，K8s 调度器据此判断节点放不放得下这个沙箱。</li>
                  <li><b>Limit</b> 是运行时上限，超过后 CPU 会被限流、内存会被杀。</li>
                  <li>两者都需大于 0，且 <b>Request 不能大于 Limit</b>（K8s 的硬性约束）。</li>
                </ul>
              </div>
            </el-form>
          </div>

          <!-- 环境变量 -->
          <div v-show="activePanel === 'env'">
            <h3>环境变量</h3>
            <p class="panel-sub">以 Key / Value 形式注入沙箱环境。</p>
            <el-table :data="form.env" size="small" border style="width:100%">
              <el-table-column label="变量名（Key）" min-width="200">
                <template #default="{row}"><el-input v-model="row.name" /></template>
              </el-table-column>
              <el-table-column label="变量值（Value）" min-width="260">
                <template #default="{row}"><el-input v-model="row.value" /></template>
              </el-table-column>
              <el-table-column label="操作" width="80" align="center">
                <template #default="{$index}">
                  <el-button link type="danger" @click="form.env.splice($index,1)">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
            <el-button style="margin-top:12px" @click="form.env.push({name:'',value:''})">
              <el-icon><Plus /></el-icon> 添加环境变量
            </el-button>
          </div>

          <!-- 超时设置 -->
          <div v-show="activePanel === 'timeout'">
            <h3>超时设置</h3>
            <p class="panel-sub">设置沙箱的生存时间和等待超时。</p>
            <div class="grid-3">
              <el-form-item label="TTL（秒）" required>
                <el-input v-model.number="form.ttl_seconds" />
                <div class="hint">沙箱最长生存时间，超时后自动回收</div>
              </el-form-item>
              <el-form-item label="Lease TTL（秒）" required>
                <el-input v-model.number="form.lease_ttl_seconds" />
                <div class="hint">沙箱租约有效期</div>
              </el-form-item>
              <el-form-item label="Wait Timeout（秒）" required>
                <el-input v-model.number="form.wait_timeout_seconds" />
                <div class="hint">等待沙箱就绪的最大时长</div>
              </el-form-item>
            </div>
          </div>

          <!-- S3 -->
          <div v-show="activePanel === 's3'">
            <h3>S3 上传配置</h3>
            <p class="panel-sub">配置用于上传文件到 S3 的脚本或脚本引用。</p>
            <el-form-item label="s3_uploader_script" required>
              <el-input v-model="form.s3_uploader_script" type="textarea" :autosize="{minRows:4}"
                placeholder="# 这里填写脚本内容或脚本引用路径&#10;#!/bin/bash&#10;echo &quot;upload to s3...&quot;" />
            </el-form-item>
            <el-form-item label="s3_download_script">
              <el-input v-model="form.s3_download_script" placeholder="留空则与上传脚本相同" />
            </el-form-item>
            <div class="hint">建议采用脚本引用或版本 ID，避免在模板中直接保存敏感凭据。</div>
          </div>

          <!-- 源文件（文件权威：直接编辑 settings/sandbox/<file>） -->
          <div v-show="activePanel === 'file'">
            <h3>源文件</h3>
            <p class="panel-sub">
              权威内容在
              <code>settings/sandbox/{{ fileInfo.filename }}</code>
              （由模板 <code>{{ fileInfo.template }}</code> 派生）。
              上面的表单字段会写回此文件；也可直接编辑原文。
            </p>
            <div style="margin-bottom:12px;display:flex;gap:8px">
              <el-button size="small" @click="loadFile" :loading="fileLoading">刷新</el-button>
              <el-button size="small" @click="saveFile" :loading="fileSaving" v-if="!readOnly">保存文件</el-button>
              <el-popconfirm title="将丢弃文件上的手改，从模板重新派生？" @confirm="resetFile">
                <template #reference>
                  <el-button size="small" type="warning" plain v-if="!readOnly">从模板重置</el-button>
                </template>
              </el-popconfirm>
              <span v-if="fileInfo.path" class="hint" style="margin:0;line-height:32px">{{ fileInfo.path }}</span>
            </div>
            <el-input v-model="fileContent" type="textarea" :autosize="{minRows:22}" class="json-area" />
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveConfig" :loading="saving" v-if="!readOnly">保存</el-button>
      </template>
    </el-dialog>

    <!-- 复制弹窗 -->
    <el-dialog v-model="copyVisible" title="复制沙箱配置" width="440px">
      <el-form label-width="90px">
        <el-form-item label="新名称">
          <el-input v-model="copyName" placeholder="新配置名称" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="copyVisible = false">取消</el-button>
        <el-button type="primary" :loading="copying" @click="doCopy">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search, Delete, Document, Grid, Setting, Timer, Files, EditPen, ArrowDown } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()

const list = ref([])
const loading = ref(false)
const typeFilter = ref('')
const search = ref('')
const page = ref(1)
const pageSize = ref(10)

// 资源池预设：选中时联动填充控制面与命名空间
const poolOptions = ref([
  { name: 'windows-pool', controlplane_base_url: '', controlplane_api_key: '', sandbox_namespace: 'omni-env-default-worker' },
  { name: 'linux-pool', controlplane_base_url: '', controlplane_api_key: '', sandbox_namespace: 'omni-env-default-worker' },
])

const filteredList = computed(() => {
  let arr = list.value
  if (typeFilter.value) arr = arr.filter(x => x.sandbox_type === typeFilter.value)
  if (search.value) arr = arr.filter(x => x.name.toLowerCase().includes(search.value.toLowerCase()))
  return arr
})
const pagedList = computed(() => filteredList.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const allCounts = computed(() => ({
  total: list.value.length,
  linux: list.value.filter(x => x.sandbox_type === 'linux').length,
  windows: list.value.filter(x => x.sandbox_type === 'windows').length,
}))

async function loadList() {
  loading.value = true
  try {
    list.value = await api.get('/sandbox-configs')
  } catch { list.value = [] } finally { loading.value = false }
}

// ---- 编辑抽屉 ----
const editVisible = ref(false)
const isEditing = ref(false)
const readOnly = ref(false)
const saving = ref(false)
const activePanel = ref('basic')
const formRef = ref(null)
let editingId = null

const emptyForm = () => ({
  name: '', sandbox_type: 'linux', resource_pool: 'linux-pool',
  controlplane_base_url: '', controlplane_api_key: '',
  sandbox_namespace: 'omni-env-default-worker',
  cpu_request: '2', cpu_limit: '3', memory_request: '6Gi', memory_limit: '8Gi',
  env: [], ttl_seconds: 8000, lease_ttl_seconds: 3600, wait_timeout_seconds: 3600,
  image: '', s3_uploader_script: '', s3_download_script: '',
  is_default: 0, enabled: 1, file: '',
})
const form = ref(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入配置名称', trigger: 'blur' },
         { pattern: /^[A-Za-z0-9][A-Za-z0-9._-]*$/, message: '只能包含字母、数字、点、下划线、连字符', trigger: 'blur' }],
}

function openCreate() {
  form.value = emptyForm()
  isEditing.value = false
  readOnly.value = false
  editingId = null
  activePanel.value = 'basic'
  editVisible.value = true
}

function openEdit(row) {
  fillForm(row); isEditing.value = true; readOnly.value = false; editingId = row.id
  activePanel.value = 'basic'; editVisible.value = true; loadFile()
}
function openView(row) {
  fillForm(row); isEditing.value = true; readOnly.value = true; editingId = row.id
  activePanel.value = 'basic'; editVisible.value = true; loadFile()
}

function fillForm(row) {
  let env = []
  try { env = JSON.parse(row.env_json || '[]') } catch {}
  form.value = {
    name: row.name, sandbox_type: row.sandbox_type, resource_pool: row.resource_pool,
    controlplane_base_url: row.controlplane_base_url, controlplane_api_key: row.controlplane_api_key,
    sandbox_namespace: row.sandbox_namespace,
    cpu_request: row.cpu_request, cpu_limit: row.cpu_limit,
    memory_request: row.memory_request, memory_limit: row.memory_limit,
    env, ttl_seconds: row.ttl_seconds, lease_ttl_seconds: row.lease_ttl_seconds,
    wait_timeout_seconds: row.wait_timeout_seconds, image: row.image,
    s3_uploader_script: row.s3_uploader_script, s3_download_script: row.s3_download_script,
    is_default: row.is_default, enabled: row.enabled, file: row.file || '',
  }
}

function onPoolChange(name) {
  const p = poolOptions.value.find(x => x.name === name)
  if (p) {
    form.value.controlplane_base_url = p.controlplane_base_url || form.value.controlplane_base_url
    form.value.controlplane_api_key = p.controlplane_api_key || form.value.controlplane_api_key
    form.value.sandbox_namespace = p.sandbox_namespace || form.value.sandbox_namespace
  }
}

function validateForm() {
  const f = form.value
  if (!f.name) { ElMessage.warning('请输入配置名称'); return false }
  for (const [reqF, limF, label] of [['cpu_request', 'cpu_limit', 'CPU'], ['memory_request', 'memory_limit', '内存']]) {
    if (!f[reqF] || !f[limF]) { ElMessage.warning(`请填写${label}请求与限制`); return false }
    if (qty(f[reqF]) > qty(f[limF])) { ElMessage.warning(`${label} 请求不得大于限制`); return false }
  }
  for (const k of ['ttl_seconds', 'lease_ttl_seconds', 'wait_timeout_seconds']) {
    if (!(f[k] > 0)) { ElMessage.warning('超时值必须为正整数'); return false }
  }
  return true
}

function qty(v) {
  const m = String(v ?? '').trim().match(/^([\d.]+)\s*([A-Za-z]*)$/)
  if (!m) return 0
  const num = parseFloat(m[1])
  const unit = m[2].toLowerCase()
  const factor = { gi: 1024, mi: 1, g: 1000, m: 1, '': 1 }[unit] ?? 1
  return num * factor
}

async function saveConfig() {
  if (!validateForm()) return
  saving.value = true
  try {
    const payload = { ...form.value }
    payload.env_json = JSON.stringify(payload.env || [])
    delete payload.env
    // 索引/状态字段走各自的端点或由后端维护，表单不上传
    delete payload.is_default
    delete payload.enabled
    delete payload.file
    delete payload.file_exists
    if (isEditing.value) {
      // sandbox_type / sandbox_key 编辑态不可改
      delete payload.sandbox_type
      delete payload.sandbox_key
      await api.put(`/sandbox-configs/${editingId}`, payload)
    } else {
      await api.post('/sandbox-configs', payload)
    }
    ElMessage.success('保存成功')
    editVisible.value = false
    await loadList()
  } catch { /* 拦截器已提示 */ } finally { saving.value = false }
}

// ---- 源文件编辑（文件权威） ----
const fileContent = ref('')
const fileLoading = ref(false)
const fileSaving = ref(false)
const fileInfo = ref({ filename: '', path: '', template: '' })

async function loadFile() {
  if (!editingId) return
  fileLoading.value = true
  try {
    const res = await api.get(`/sandbox-configs/${editingId}/file`)
    fileContent.value = res.content || ''
    fileInfo.value = { filename: res.filename, path: res.path, template: res.template }
  } catch { fileContent.value = '' } finally { fileLoading.value = false }
}

async function saveFile() {
  fileSaving.value = true
  try {
    await api.put(`/sandbox-configs/${editingId}/file`, fileContent.value, {
      headers: { 'Content-Type': 'text/plain' },
    })
    ElMessage.success('文件已保存')
    await loadFile()
    await loadList()
  } catch { /* 已提示 */ } finally { fileSaving.value = false }
}

async function resetFile() {
  try {
    await api.post(`/sandbox-configs/${editingId}/reset`)
    ElMessage.success('已从模板重置')
    await loadFile()
    await loadList()
  } catch { /* 已提示 */ }
}

// ---- 行操作 ----
const copyVisible = ref(false)
const copyName = ref('')
const copying = ref(false)
let copySourceId = null

function copyConfig(row) {
  copySourceId = row.id
  copyName.value = `${row.name}-copy`
  copyVisible.value = true
}
async function doCopy() {
  if (!copyName.value.trim()) { ElMessage.warning('请输入新名称'); return }
  copying.value = true
  try {
    await api.post(`/sandbox-configs/${copySourceId}/copy`, { name: copyName.value.trim() })
    ElMessage.success('复制成功')
    copyVisible.value = false
    await loadList()
  } catch { /* 已提示 */ } finally { copying.value = false }
}

function handleMore(cmd, row) {
  if (cmd === 'copy') copyConfig(row)
  else if (cmd === 'setDefault') setDefault(row)
  else if (cmd === 'delete') deleteConfig(row)
  else if (cmd === 'enable') setEnabled(row, true)
  else if (cmd === 'disable') setEnabled(row, false)
}

async function setDefault(row) {
  await api.put(`/sandbox-configs/${row.id}/set-default`)
  ElMessage.success('已设为默认')
  await loadList()
}
async function setEnabled(row, enabled) {
  await api.put(`/sandbox-configs/${row.id}/enabled`, { enabled })
  ElMessage.success(enabled ? '已启用' : '已停用')
  await loadList()
}
async function deleteConfig(row) {
  try {
    await ElMessageBox.confirm(`确认删除沙箱配置「${row.name}」？`, '删除确认', { type: 'warning' })
  } catch { return }
  await api.delete(`/sandbox-configs/${row.id}`)
  ElMessage.success('已删除')
  await loadList()
}

onMounted(loadList)
</script>

<style scoped>
.header-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 20px;
}
.header-row h2 {
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 700;
  margin: 0;
}
.subtitle {
  color: var(--text-muted);
  font-size: 13px;
  margin: 4px 0 0;
}
.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
}
.search-input { width: 260px; }
.table-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
}
.total-text { color: var(--text-muted); font-size: 13px; }

.sb-type { display: inline-flex; align-items: center; gap: 6px; }
.sb-icon { width: 16px; height: 16px; border-radius: 3px; display: inline-block; }
.sb-icon.windows { background: #2563eb; }
.sb-icon.linux { background: #f59e0b; border-radius: 50%; }

.status-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 6px; vertical-align: middle;
}
.status-dot.on { background: #22c55e; }
.status-dot.off { background: #cbd5e1; }
/* 操作区：与队列管理一致，强制不换行 */
.op-cell { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; white-space: nowrap; }

.editor-layout { display: flex; gap: 20px; min-height: 460px; }
.panel-nav {
  width: 160px;
  flex-shrink: 0;
  border-right: none;
}
.panel-nav .el-menu-item {
  height: 42px; line-height: 42px; border-radius: 6px; margin-bottom: 2px;
}
.panel-body {
  flex: 1;
  min-width: 0;
  max-height: 62vh;
  overflow-y: auto;
  padding-right: 8px;
}
/* 弹窗内导航菜单不占满高度，避免切换面板时底部留白 */
.sandbox-dialog :deep(.el-dialog__body) { padding-top: 8px; }
.panel-body h3 { font-size: 16px; margin: 0 0 4px; color: var(--text-primary); }
.panel-sub { color: var(--text-muted); font-size: 13px; margin: 0 0 16px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 16px; }
.grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0 16px; }
.hint { font-size: 12px; color: #999; margin-top: 4px; }
/* 说明性文字（非报错）：中性灰底，避免被误读为校验错误 */
.field-note {
  background: #f8fafc;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  padding: 12px 14px;
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.7;
}
.field-note-title { font-weight: 600; margin-bottom: 4px; color: var(--text-primary); }
.field-note ul { margin: 0; padding-left: 18px; }
.field-note li { margin: 2px 0; }
.field-note b { color: var(--text-primary); }
.divider-text { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.drawer-title { font-size: 18px; font-weight: 600; }
.drawer-sub { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
.json-area :deep(textarea) { font-family: ui-monospace, monospace; font-size: 12px; }
</style>
