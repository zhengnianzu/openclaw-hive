<template>
  <div>
    <h2 class="page-title">新建任务实例</h2>

    <div class="glass-card" style="max-width:800px">
    <el-form :model="form" label-width="160px">
      <el-form-item label="实例名称" required>
        <el-input v-model="form.name" placeholder="例如：web_skill_test_0608" />
      </el-form-item>

      <el-form-item label="任务标识(sandbox-id)" required>
        <el-input v-model="form.task_name" placeholder="用于Pod命名和OBS路径，例如：zx26070812300001" />
      </el-form-item>

      <el-form-item label="Harness 类型">
        <el-select v-model="form.harness_type" @change="onHarnessChange" style="width:200px">
          <el-option v-for="t in harnessStore.types" :key="t" :label="harnessStore.label(t)" :value="t" />
          <el-option label="通用" value="common" />
        </el-select>
      </el-form-item>

      <el-form-item label="配置方案">
        <el-select v-model="form.harness_config_id" style="width:240px" @change="onSchemeChange">
          <el-option v-for="hc in harnessConfigList" :key="hc.id"
            :label="hc.name + (hc.is_default ? ' (默认)' : '')"
            :value="hc.id" />
        </el-select>
      </el-form-item>

      <el-form-item label="配置版本">
        <el-select v-model="form.harness_config_version_id" style="width:240px"
          :placeholder="versionList.length ? '选择已发布版本' : '该方案暂无已发布版本'"
          :loading="versionsLoading">
          <el-option v-for="v in versionList" :key="v.id"
            :label="`v${v.version}${v.published_at ? ' · ' + formatShortTime(v.published_at) + ' 发布' : ''}`"
            :value="v.id" />
        </el-select>
        <div v-if="harnessConfigList.length && !versionList.length && !versionsLoading"
          style="font-size:12px;color:#f59e0b;margin-top:4px">
          该方案还没有已发布版本，请到「配置工作台」创建并发布版本
        </div>
      </el-form-item>

      <el-form-item label="并发数">
        <el-input-number v-model="form.concurrent_num" :min="1" />
      </el-form-item>

      <el-tabs v-model="activeTab" style="margin-top:8px">
        <el-tab-pane label="OBS配置" name="obs">
          <el-form-item label="默认技能源目录">
            <el-input v-model="form.default_skill_dir" placeholder="qianmin/skill_test">
              <template #append>
                <el-button @click="openObsBrowser('default_skill_dir')"><el-icon><FolderOpened /></el-icon></el-button>
              </template>
            </el-input>
          </el-form-item>

          <el-form-item label="默认技能">
            <el-input v-model="form.default_skills" placeholder="image-search（逗号分隔，留空使用模板默认值）" />
          </el-form-item>

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
            <el-input v-model="form.traj_save_path" placeholder="自动生成：{harness}_trajs/traj_{task_name}" />
          </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="Harness配置" name="model">
          <el-form-item label="模型 Base URL">
            <el-input v-model="form.model_base_url" :placeholder="['opencode','pi','codex','grok'].includes(form.harness_type) ? '例如：http://192.168.30.95:8084（自动补 /v1）' : '例如：http://192.168.30.95:8084'" />
          </el-form-item>

          <el-form-item :label="form.harness_type === 'claude-code' ? '模型 Token' : '模型 API Key'">
            <div style="display:flex;gap:8px;width:100%">
              <el-input v-model="form.model_api_key" placeholder="留空使用模板默认值" style="flex:1" />
              <el-button type="primary" @click="openKeyDialog('harness')">新建KEY</el-button>
            </div>
            <div v-if="form.model_api_key" style="font-size:12px;color:#999;margin-top:4px">
              {{ form.model_api_key.length > 8 ? form.model_api_key.slice(0, 4) + '****' + form.model_api_key.slice(-4) : '' }}
            </div>
          </el-form-item>

          <el-form-item v-if="['openclaw','openclaw-tui','opencode','pi','dsh','jiuwenswarm-dev','jiuwenswarm-xiaoyi'].includes(form.harness_type)" label="API 类型">
            <el-select v-model="form.model_api_type" placeholder="留空使用模板默认值" clearable style="width:100%">
              <el-option label="Anthropic Messages" value="anthropic-messages" />
              <el-option label="OpenAI Completions" value="openai-completions" />
            </el-select>
            <div v-if="form.harness_type === 'opencode'" style="font-size:12px;color:#999;margin-top:4px">决定 provider 的 npm 包：anthropic 用 @ai-sdk/anthropic，openai 用 @ai-sdk/openai-compatible</div>
            <div v-if="form.harness_type === 'pi'" style="font-size:12px;color:#999;margin-top:4px">写入 models.json 的 providers.custom.api 字段</div>
            <div v-if="['jiuwenswarm-dev','jiuwenswarm-xiaoyi'].includes(form.harness_type)" style="font-size:12px;color:#999;margin-top:4px">写入 harness 配置文件（dev= jw_dev_config.yaml / xiaoyi= jw_xiaoyi_config.yaml）的 client_provider（Anthropic/OpenAI）；base_url 的 /v1 由后端按此自动补齐或去除</div>
          </el-form-item>

          <el-form-item v-if="form.harness_type === 'openjiuwen'" label="Provider">
            <el-select v-model="form.model_api_type" placeholder="留空使用模板默认值 (Anthropic)" clearable style="width:100%">
              <el-option label="Anthropic" value="Anthropic" />
              <el-option label="OpenAI" value="OpenAI" />
            </el-select>
            <div style="font-size:12px;color:#999;margin-top:4px">tokenfly 网关走 anthropic-messages 协议，一般选 Anthropic</div>
          </el-form-item>

          <el-form-item label="模型 ID">
            <el-input v-model="form.model_id" :placeholder="form.harness_type === 'claude-code' ? '例如：claude-opus-4-7-thinking' : '例如：claude-opus-4-7-thinking'">
              <template v-if="['openclaw','openclaw-tui'].includes(form.harness_type)" #prepend>local/</template>
            </el-input>
            <div v-if="['openclaw','openclaw-tui'].includes(form.harness_type)" style="font-size:12px;color:#999;margin-top:4px">同时更新 agents.defaults.model.primary 和 models[0].id/name</div>
          </el-form-item>
        </el-tab-pane>

        <el-tab-pane label="用户模拟配置" name="proxy">
          <div class="agent-tabs-header">
            <el-button size="small" text type="primary" @click="addAgent"><el-icon><Plus /></el-icon></el-button>
          </div>
          <el-tabs v-model="activeAgentTab" type="card" @tab-remove="removeAgentTab">
            <el-tab-pane v-for="(ag, idx) in form.agents" :key="idx" :label="ag.name || '未命名'" :name="String(idx)" :closable="idx !== 0">
              <el-form-item label="Agent 名称">
                <el-input v-model="ag.name" placeholder="例如：user_simulator" />
              </el-form-item>

              <el-form-item label="模型 Base URL">
                <el-input v-model="ag.base_url" placeholder="例如：http://192.168.30.95:8084/v1（user_simulator 需带 /v1）" />
                <div style="font-size:12px;color:#999;margin-top:4px">user_simulator 的 Base URL 需带 /v1 后缀</div>
              </el-form-item>

              <el-form-item label="模型 API Key">
                <div style="display:flex;gap:8px;width:100%">
                  <el-input v-model="ag.api_key" placeholder="留空使用模板默认值" style="flex:1" show-password />
                  <el-button type="primary" @click="openKeyDialog('agent', idx)">新建KEY</el-button>
                </div>
                <div v-if="ag.api_key" style="font-size:12px;color:#999;margin-top:4px">
                  {{ ag.api_key.length > 8 ? ag.api_key.slice(0, 4) + '****' + ag.api_key.slice(-4) : '' }}
                </div>
              </el-form-item>

              <el-form-item label="Provider">
                <el-input v-model="ag.provider" placeholder="例如：local-evaluator" />
                <div style="font-size:12px;color:#999;margin-top:4px">留空时该 agent 不会被注入到 openclaw.json</div>
              </el-form-item>

              <el-form-item label="API 类型">
                <el-select v-model="ag.api" placeholder="可选" clearable style="width:100%">
                  <el-option label="OpenAI Completions" value="openai-completions" />
                  <el-option label="Anthropic Messages" value="anthropic-messages" />
                </el-select>
              </el-form-item>

              <el-form-item label="模型 ID">
                <el-input v-model="ag.model" placeholder="例如：gemini-3-flash-preview" />
              </el-form-item>
            </el-tab-pane>
          </el-tabs>
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

          <el-form-item label="沙箱配置">
            <el-select v-model="form.sandbox_config_id" placeholder="选择沙箱运行环境" style="width:100%">
              <el-option label="默认（不替换，用模板原样）" :value="0" />
              <el-option v-for="sb in sandboxList" :key="sb.id"
                :label="`${sb.name}${sb.is_default ? ' (默认)' : ''}`" :value="sb.id">
                <span>{{ sb.name }}<el-tag v-if="sb.is_default" size="small" type="info" style="margin-left:6px">默认</el-tag></span>
                <span style="float:right;color:#999;font-size:12px">
                  {{ sb.sandbox_type === 'windows' ? 'Windows' : 'Linux' }} · {{ sb.cpu_request }}/{{ sb.cpu_limit }} 核
                  <template v-if="sb.sandbox_namespace"> · {{ sb.sandbox_namespace }}</template>
                </span>
              </el-option>
            </el-select>
            <div style="font-size:12px;color:#999;margin-top:4px">
              决定沙箱的资源规格、超时、环境变量与镜像；选「默认」则不替换，用 harness 模板原样
            </div>
          </el-form-item>

          <el-form-item label="Key 追加沙箱后缀">
            <el-switch v-model="form.suffix_api_keys" active-text="开启" inactive-text="关闭" />
            <div style="font-size:12px;color:#999;margin-top:4px">
              仅自部署模型（网关按 user_id 计费）需要：上传前给模型 Key 追加 <code>_&lt;沙箱标识&gt;</code>。第三方模型请关闭，否则鉴权失败
            </div>
          </el-form-item>

          <el-form-item label="代码仓">
            <el-select v-model="form.code_repo_id" placeholder="选择代码仓（可选）" style="width:100%" clearable>
              <el-option v-for="repo in codeRepoList" :key="repo.id" :label="`${repo.name} / ${repo.version}`" :value="repo.id">
                <span>{{ repo.name }} / {{ repo.version }}</span>
                <span style="float:right;color:#999;font-size:12px">{{ repo.obs_path.length > 40 ? '...' + repo.obs_path.slice(-40) : repo.obs_path }}</span>
              </el-option>
            </el-select>
            <div style="font-size:12px;color:#999;margin-top:4px">不选则使用默认uploads中的tar文件；选择后自动下载并打包，config.yaml 的 main_code_tar 将指向打包后的tar文件</div>
          </el-form-item>

          <el-form-item label="任务队列">
            <div style="display:flex;gap:8px;width:100%">
              <el-select v-model="form.queue_id" placeholder="选择队列（不选则直接启动，不参与排队）"
                style="flex:1" clearable>
                <el-option v-for="queue in queueList" :key="queue.id" :label="queue.id" :value="queue.id">
                  <span>{{ queue.id }}</span>
                  <span style="float:right;color:#999;font-size:12px">
                    运行 {{ queue.active_tasks }}/{{ queue.max_concurrent }} · 排队 {{ queue.queued_tasks }}
                  </span>
                </el-option>
              </el-select>
              <el-button type="primary" @click="queuePickerVisible = true">新建队列</el-button>
            </div>
            <div style="font-size:12px;color:#999;margin-top:4px">任务加入队列后，将按照队列顺序执行</div>
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
        <el-form-item label="密钥名称">
          <el-input v-model="keyForm.name" placeholder="例如: 250826-任务名称" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="keyDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="generateApiKey" :loading="keyGenerating">生成</el-button>
      </template>
    </el-dialog>

    <QueuePicker v-model="queuePickerVisible" :selected-id="form.queue_id" @picked="onQueuePicked" />

    <!-- OBS Browser Dialog -->
    <el-dialog v-model="obsVisible" title="选择OBS目录" width="700px" destroy-on-close>
      <div style="margin-bottom:12px">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item v-for="(seg, idx) in pathSegments" :key="idx"
            @click="navigateTo(idx)" style="cursor:pointer">
            {{ seg || obsBucket().replace(/\/$/, '') }}
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
import QueuePicker from '../components/QueuePicker.vue'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import { obsBucket, stripBucket, joinBucket } from '../api/obsConfig'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const creating = ref(false)
const activeTab = ref('obs')

const keyDialogVisible = ref(false)
const keyGenerating = ref(false)
const keyForm = ref({
  invite_code: localStorage.getItem('last_invite_code') || 'pangu',
  name: '',
})
let keyTarget = { type: 'harness', agentIdx: -1 }

function openKeyDialog(type = 'harness', agentIdx = -1) {
  keyTarget = { type, agentIdx }
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
    const baseUrl = keyTarget.type === 'agent' && keyTarget.agentIdx >= 0
      ? form.value.agents[keyTarget.agentIdx]?.base_url
      : form.value.model_base_url
    if (baseUrl) {
      params.base_url = baseUrl
    }
    const res = await api.post('/generate-api-key', null, { params })
    if (keyTarget.type === 'agent' && keyTarget.agentIdx >= 0) {
      form.value.agents[keyTarget.agentIdx].api_key = res.api_key
      form.value.agents[keyTarget.agentIdx].invite_code = keyForm.value.invite_code
    } else {
      form.value.model_api_key = res.api_key
      form.value.invite_code = keyForm.value.invite_code
    }
    localStorage.setItem('last_invite_code', keyForm.value.invite_code)
    ElMessage.success(`API Key 已生成: ${res.api_key.slice(0, 4)}****${res.api_key.slice(-4)}`)
    keyDialogVisible.value = false
  } catch {
    // error already shown by api interceptor
  } finally {
    keyGenerating.value = false
  }
}

// 任务标识默认值: 用户名 + 本地时间 YYMMDDHHMM (例如 zx2608102022)
function genTaskId() {
  const now = new Date()
  const ts = [
    String(now.getFullYear()).slice(2),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
  ].join('')
  return `${authStore.username || ''}${ts}`
}

const form = ref({
  name: '', task_name: genTaskId(), concurrent_num: 100,
  skill_dir: '', default_skill_dir: 'qianmin/skill_test', default_skills: 'image-search', agent_dir: '', user_config_dir: '', user_profile_dir: '',
  traj_save_path: '', start_index: 0, total_num: 0, image_name: '',
  model_api_key: '', model_base_url: '', model_api_type: '', model_id: '',
  suffix_api_keys: true,
  user_proxy_model_name: '', user_proxy_api_key: '', user_proxy_base_url: '',
  harness_type: 'openclaw',
  code_repo_id: null,
  invite_code: '',
  harness_config_id: null,
  harness_config_version_id: null,
  copy_complete_from: null,
  queue_id: null,
  sandbox_config_id: 0,
  agents: [{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }],
})

const activeAgentTab = ref('0')

function addAgent() {
  form.value.agents.push({ name: '', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' })
  activeAgentTab.value = String(form.value.agents.length - 1)
}

function removeAgentTab(tabName) {
  const idx = parseInt(tabName)
  if (idx === 0) return
  form.value.agents.splice(idx, 1)
  if (parseInt(activeAgentTab.value) >= form.value.agents.length) {
    activeAgentTab.value = String(form.value.agents.length - 1)
  }
}

const imageList = ref([])
const codeRepoList = ref([])
const harnessConfigList = ref([])
const versionList = ref([])
const versionsLoading = ref(false)
const queueList = ref([])
const sandboxList = ref([])
const queuePickerVisible = ref(false)

async function loadSandboxConfigs() {
  try {
    sandboxList.value = await api.get('/sandbox-configs', { params: { enabled_only: true } })
    // 默认保持「不替换，用模板原样」：只有用户主动选过沙箱配置才保留其选择。
    // 不要自动预选 is_default 的那条——那会改变平台既有行为（模板里的沙箱段被整段覆盖）。
    if (form.value.sandbox_config_id == null) {
      form.value.sandbox_config_id = 0
    }
  } catch { sandboxList.value = [] }
}

async function loadQueues() {
  try {
    queueList.value = await api.get('/queues')
  } catch {
    queueList.value = []
  }
}

// 加入队列窗口回填：选中的队列直接写进表单；若窗口里新建了队列，重新拉一次列表
async function onQueuePicked(queueId) {
  form.value.queue_id = queueId
  await loadQueues()
}

// 复制场景（?copy_from=xxx）下保留源实例的方案，不要被「默认方案」覆盖。
// onMounted 里 copy_from 分支先填好 harness_config_id，最后才调本函数；
// 若无条件按 is_default 赋值，复制 id=9 的实例会静默换成 id=63 的方案。
async function loadHarnessConfigs() {
  try {
    harnessConfigList.value = await api.get('/harness-configs', {
      params: { harness_type: form.value.harness_type }
    })
    const inherited = form.value.harness_config_id &&
      harnessConfigList.value.some(h => h.id === form.value.harness_config_id)
    if (!inherited) {
      const def = harnessConfigList.value.find(h => h.is_default)
      if (def) {
        form.value.harness_config_id = def.id
      } else if (harnessConfigList.value.length) {
        form.value.harness_config_id = harnessConfigList.value[0].id
      } else {
        form.value.harness_config_id = null
      }
    }
    await loadVersions({ keepSelection: true })
  } catch {
    harnessConfigList.value = []
    versionList.value = []
  }
}

// 只列已发布版本——实例只能引用发布过的快照。
// keepSelection=true 时保留当前已选版本（复制场景）：源实例用的是哪个版本就继续用哪个，
// 不管该方案后来有没有发布新版本。历史实例没有记录版本号，则退到「初始发布版本」。
async function loadVersions({ keepSelection = false } = {}) {
  const prev = form.value.harness_config_version_id
  versionList.value = []
  if (!form.value.harness_config_id) {
    form.value.harness_config_version_id = null
    return
  }
  versionsLoading.value = true
  try {
    const all = await api.get(`/harness-configs/${form.value.harness_config_id}/versions`)
    versionList.value = (all || []).filter(v => v.status === 'published')
    if (keepSelection && prev && versionList.value.some(v => v.id === prev)) {
      // 源实例记录的版本仍在，原样沿用
      form.value.harness_config_version_id = prev
    } else if (versionList.value.length) {
      // 接口按 created_at DESC 返回，[0] 是最新发布、末尾是初始发布。
      // 复制时取初始发布版本；普通新建仍取最新。
      const isCopy = keepSelection && !!route.query.copy_from
      const idx = isCopy ? versionList.value.length - 1 : 0
      form.value.harness_config_version_id = versionList.value[idx].id
    } else {
      form.value.harness_config_version_id = null
    }
  } catch { versionList.value = [] }
  finally { versionsLoading.value = false }
}

function onSchemeChange() { loadVersions() }

function formatShortTime(t) {
  return String(t || '').replace('T', ' ').slice(5, 16)
}

function applyTemplate(tpl) {
  if (!tpl) return  form.value.model_base_url = tpl.model_base_url || ''
  form.value.invite_code = tpl.invite_code || ''
  form.value.model_api_type = tpl.model_api_type || ''
  if (tpl.model_id) form.value.model_id = tpl.model_id
  form.value.image_name = tpl.image_name || ''
  form.value.code_repo_id = tpl.code_repo_id || null
  form.value.model_api_key = ''
  try {
    const tplAgents = JSON.parse(tpl.agents_json || '[]')
    if (tplAgents.length > 0) {
      form.value.agents = tplAgents.map(a => ({
        name: a.name || 'user_simulator',
        model: a.model || '',
        provider: a.provider || '',
        base_url: a.base_url || '',
        api_key: a.api_key || '',
        api: a.api || '',
        invite_code: a.invite_code || '',
      }))
    }
  } catch {}
  activeAgentTab.value = '0'
}

async function loadImages() {
  try {
    imageList.value = await api.get('/images', { params: { harness_type: form.value.harness_type } })
  } catch { imageList.value = [] }
}

async function loadCodeRepos() {
  try {
    codeRepoList.value = await api.get('/code-repos')
  } catch { codeRepoList.value = [] }
}

function onHarnessChange() {
  form.value.image_name = ''
  form.value.harness_config_id = null
  form.value.harness_config_version_id = null
  versionList.value = []
  loadImages()
  loadHarnessConfigs()
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
const obsCurrentPath = ref(obsBucket())
let obsTargetField = ''

const pathSegments = computed(() => {
  const p = stripBucket(obsCurrentPath.value)
  return ['', ...p.split('/').filter(Boolean)]
})

function openObsBrowser(field) {
  obsTargetField = field
  obsCurrentPath.value = obsBucket()
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
  obsCurrentPath.value = joinBucket(segs.join('/'))
  loadObsDir()
}

function confirmObsSelect() {
  const relative = stripBucket(obsCurrentPath.value)
  form.value[obsTargetField] = relative
  obsVisible.value = false
}

onMounted(async () => {
  harnessStore.load()
  const copyFrom = route.query.copy_from
  const fromRegistration = route.query.from_registration
  if (copyFrom) {
    try {
      const params = await api.get(`/instances/${copyFrom}/create-params`)
      Object.assign(form.value, params)
      // 历史实例的 suffix_api_keys 统一是 false（迁移默认值），直接继承会让
      // 「复制」出来的新任务默认关闭后缀。复制时回到当前默认值，用户仍可手动改。
      form.value.suffix_api_keys = true
      form.value.name = params.name + '-copy'
      form.value.task_name = genTaskId()
      // 重跑失败场景: 透传 copy_complete_from, 后端创建时搬源实例 complete.jsonl
      form.value.copy_complete_from = route.query.copy_complete_from || null
      if (route.query.copy_complete_from) {
        form.value.name = params.name + '-retry'
      }
      if (!form.value.agents || form.value.agents.length === 0) {
        form.value.agents = [{ name: 'user_simulator', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }]
      }
      ElMessage.info('已从已有实例复制配置，请修改任务标识后创建')
    } catch {
      ElMessage.warning('无法加载源实例配置')
    }
  } else if (fromRegistration) {
    try {
      const reg = await api.get(`/registrations/${fromRegistration}`)
      const stripObsPrefix = p => stripBucket(p)
      const rand = String(Math.floor(Math.random() * 10000)).padStart(4, '0')

      // 1. 先加载配置模板（兜底）
      const templateId = reg.config_template_id || route.query.template_id
      if (templateId) {
        try {
          const tpl = await api.get(`/config-templates/${templateId}`)
          if (tpl) applyTemplate(tpl)
        } catch {}
      }

      // 2. 再用登记字段覆盖（登记优先级更高）
      form.value.name = `${reg.task_name}-${reg.requester || 'task'}`
      form.value.task_name = `${genTaskId()}${rand}`
      form.value.harness_type = reg.harness_type || 'openclaw'
      form.value.user_config_dir = stripObsPrefix(reg.task_path_obs)
      form.value.skill_dir = stripObsPrefix(reg.skill_dir_obs)
      form.value.default_skill_dir = stripObsPrefix(reg.default_skill_dir_obs) || 'qianmin/skill_test'
      form.value.agent_dir = stripObsPrefix(reg.agent_dir_obs)
      form.value.user_profile_dir = stripObsPrefix(reg.user_folder_obs)
      form.value.default_skills = reg.default_skills || 'image-search'
      form.value.total_num = reg.data_total || 0
      if (reg.model_name) form.value.model_id = reg.model_name
      if (reg.base_url) form.value.model_base_url = reg.base_url

      // 3. 用登记的 agent 信息覆盖模板中的 agents
      if (reg.eval_model_name && form.value.agents.length > 0) {
        form.value.agents[0].model = reg.eval_model_name
      }
      if (reg.eval_config_model) {
        const evaluator = form.value.agents.find(a => a.name === 'evaluator')
        if (evaluator) {
          evaluator.model = reg.eval_config_model
        } else {
          form.value.agents.push({ name: 'evaluator', model: reg.eval_config_model, base_url: '', api_key: '', api: '', provider: '', invite_code: '' })
        }
      }

      if (!form.value.agents || form.value.agents.length === 0) {
        const agents = [{ name: 'user_simulator', model: reg.eval_model_name || '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }]
        if (reg.eval_config_model) {
          agents.push({ name: 'evaluator', model: reg.eval_config_model, base_url: '', api_key: '', api: '', provider: '', invite_code: '' })
        }
        form.value.agents = agents
      }
      ElMessage.info('已从任务登记预填配置')
    } catch {
      ElMessage.warning('无法加载登记信息')
    }
  }
  loadImages()
  loadCodeRepos()
  loadHarnessConfigs()
  loadQueues()
  loadSandboxConfigs()
})
</script>

<style scoped>
.page-title {
  color: var(--text-primary);
  font-size: 24px;
  font-weight: 700;
  margin-bottom: 24px;
}
.agent-tabs-header {
  float: right;
  margin-top: 2px;
}
/* 任务标识等较长的表单标签不换行, 保持一行显示 */
:deep(.el-form-item__label) {
  white-space: nowrap;
}
</style>
