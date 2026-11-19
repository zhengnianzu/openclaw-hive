<template>
  <div>
    <div class="header-row">
      <div style="display:flex;align-items:center;gap:10px">
        <el-button text @click="router.push('/batch')"><el-icon><Back /></el-icon></el-button>
        <h2 style="margin:0">新建批量实验</h2>
      </div>
    </div>

    <div class="layout">
      <!-- 左：步骤导航 -->
      <div class="glass-card steps-card">
        <el-steps direction="vertical" :active="step" finish-status="success">
          <el-step title="基本配置" />
          <el-step title="选择 Harness" />
          <el-step title="执行参数" />
          <el-step title="确认创建" />
        </el-steps>
      </div>

      <!-- 中：表单主体 -->
      <div class="glass-card form-card">
        <!-- ================= 1. 基本配置 ================= -->
        <template v-if="step === 0">
          <h3 class="section-title">基本配置</h3>
          <el-form label-width="100px" @submit.prevent>
            <el-form-item label="实验名称" required>
              <el-input v-model="form.name" placeholder="例如：科研能力横向评测" />
            </el-form-item>

            <el-form-item label="测试集" required>
              <el-select v-model="form.registration_id" filterable placeholder="选择任务登记作为测试集"
                style="width:100%" @change="onRegChange">
                <el-option v-for="r in regList" :key="r.id"
                  :label="`${r.task_name}（${r.data_total || 0} 条）`" :value="r.id" />
                <template #footer>
                  <el-button text type="primary" style="width:100%" @click="router.push('/task-register')">
                    <el-icon><Plus /></el-icon> 新建任务登记
                  </el-button>
                </template>
              </el-select>
              <div v-if="selectedReg" class="hint">
                OBS 路径：{{ selectedReg.task_path_obs || '（未填写，无法作为测试集）' }}
              </div>
              <div v-if="selectedReg && !selectedReg.task_path_obs" class="hint danger">
                该登记没有任务路径，请先补全后再用于批量实验。
              </div>
            </el-form-item>

            <el-form-item label="实验描述">
              <el-input v-model="form.description" type="textarea" :rows="3"
                placeholder="可选，说明本次实验的目的" />
            </el-form-item>
          </el-form>
        </template>

        <!-- ================= 2. 选择 Harness ================= -->
        <template v-if="step === 1">
          <div class="section-head">
            <h3 class="section-title" style="margin:0">
              选择 Harness <span class="muted">（已选 {{ form.arms.length }} 个）</span>
            </h3>
            <el-button text type="primary" @click="router.push('/harness-configs')">
              <el-icon><Plus /></el-icon> 新建 Harness
            </el-button>
          </div>

          <div v-loading="armsLoading" class="arm-grid">
            <div v-for="opt in armOptions" :key="opt.key"
              class="arm-card" :class="{ active: isPicked(opt) }" @click="toggleArm(opt)">
              <div class="arm-top">
                <el-checkbox :model-value="isPicked(opt)" @click.stop @change="toggleArm(opt)" />
                <el-tag :style="harnessStore.tagStyle(opt.harness_type)" size="small">
                  {{ harnessStore.label(opt.harness_type) }}
                </el-tag>
              </div>
              <div class="arm-name">{{ opt.config_name }}</div>
              <el-select v-if="isPicked(opt)" :model-value="pickedVersion(opt)" size="small"
                placeholder="选择版本" style="width:100%;margin-top:8px"
                @click.stop @change="v => setVersion(opt, v)">
                <el-option v-for="v in opt.versions" :key="v.id"
                  :label="`${v.version}${v.description ? ' · ' + v.description : ''}`" :value="v.id" />
              </el-select>
              <div v-else class="arm-sub">
                {{ opt.versions.length }} 个已发布版本
              </div>
            </div>
          </div>

          <el-empty v-if="!armsLoading && !armOptions.length"
            description="没有可用的 Harness 配置（需要至少一个已发布版本）" />
        </template>

        <!-- ================= 3. 执行参数 ================= -->
        <template v-if="step === 2">
          <h3 class="section-title">执行参数</h3>
          <el-form label-width="130px" @submit.prevent>
            <el-form-item label="每个样本重复次数">
              <el-input-number v-model="form.repeat_times" :min="1" :max="20" />
              <span class="hint inline">同一 Harness 重复跑几轮（用于观察稳定性）</span>
            </el-form-item>

            <el-form-item label="全局并发上限">
              <div style="display:flex;align-items:center;gap:16px;width:100%">
                <el-slider v-model="form.max_concurrent" :min="1" :max="50" style="flex:1" />
                <el-input-number v-model="form.max_concurrent" :min="1" :max="50" size="small" />
              </div>
            </el-form-item>

            <el-form-item label="调度策略">
              <el-radio-group v-model="form.schedule_mode">
                <el-radio value="shared">共享并发池（推荐）</el-radio>
                <el-radio value="isolated">独立队列</el-radio>
                <el-radio value="serial">串行执行</el-radio>
              </el-radio-group>
              <div class="hint">{{ SCHEDULE_HINT[form.schedule_mode] }}</div>
            </el-form-item>

            <el-divider content-position="left">公共任务参数（所有子任务共用）</el-divider>

            <el-form-item label="沙箱并发量">
              <el-input-number v-model="form.common.concurrent_num" :min="1" />
              <span class="hint inline">单个子任务内部的并发沙箱数</span>
            </el-form-item>

            <el-form-item label="镜像">
              <el-select v-model="form.common.image_name" filterable allow-create clearable
                default-first-option placeholder="选择镜像（留空用模板默认）" style="width:100%">
                <el-option v-for="img in imageList" :key="img.id"
                  :label="`${img.name}（${img.os}）`" :value="img.address" />
              </el-select>
            </el-form-item>

            <el-form-item label="沙箱配置">
              <el-select v-model="form.common.sandbox_config_id" placeholder="选择沙箱运行环境" style="width:100%">
                <el-option label="不替换（用模板原样）" :value="0" />
                <el-option v-for="sb in sandboxList" :key="sb.id"
                  :label="sb.name + (sb.is_default ? ' (默认)' : '')" :value="sb.id" />
              </el-select>
            </el-form-item>

            <el-form-item label="沙箱最大存活">
              <el-input-number v-model="form.common.max_ttl_second" :min="1800" :max="36000" :step="600" />
              <span class="hint inline">秒</span>
            </el-form-item>

            <el-form-item label="模型 Base URL">
              <el-input v-model="form.common.model_base_url" placeholder="例如：http://192.168.30.95:8084" />
            </el-form-item>

            <el-form-item label="模型 API Key">
              <el-input v-model="form.common.model_api_key" type="password" show-password
                placeholder="所有子任务共用" />
            </el-form-item>

            <el-form-item label="模型 ID">
              <el-input v-model="form.common.model_id" placeholder="例如：claude-opus-4-7-thinking" />
            </el-form-item>

            <el-form-item label="Key 沙箱后缀">
              <el-switch v-model="form.common.suffix_api_keys" active-text="开启" inactive-text="关闭" />
              <span class="hint inline">用第三方模型时须关闭</span>
            </el-form-item>

            <el-form-item label="Skill 目录">
              <el-input v-model="form.common.skill_dir" placeholder="留空用 Harness 配置默认" />
            </el-form-item>

            <el-form-item label="Agent 目录">
              <el-input v-model="form.common.agent_dir" placeholder="留空用 Harness 配置默认" />
            </el-form-item>

            <el-form-item label="代码仓">
              <el-select v-model="form.common.code_repo_id" placeholder="选择代码仓（可选）"
                style="width:100%" clearable>
                <el-option v-for="r in codeRepoList" :key="r.id"
                  :label="`${r.name} · ${r.version}`" :value="r.id" />
              </el-select>
            </el-form-item>
          </el-form>
        </template>

        <!-- ================= 4. 确认创建 ================= -->
        <template v-if="step === 3">
          <h3 class="section-title">确认创建</h3>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="实验名称">{{ form.name }}</el-descriptions-item>
            <el-descriptions-item label="测试集">
              {{ selectedReg?.task_name }}（{{ sampleTotal }} 条）
            </el-descriptions-item>
            <el-descriptions-item label="Harness 数">{{ form.arms.length }}</el-descriptions-item>
            <el-descriptions-item label="重复次数">{{ form.repeat_times }}</el-descriptions-item>
            <el-descriptions-item label="调度策略">{{ SCHEDULE_LABEL[form.schedule_mode] }}</el-descriptions-item>
            <el-descriptions-item label="并发上限">{{ form.max_concurrent }}</el-descriptions-item>
          </el-descriptions>

          <h4 style="margin:20px 0 8px">任务矩阵预览（前 {{ previewRows.length }} 条）</h4>
          <el-table :data="previewRows" size="small" border style="width:100%">
            <el-table-column prop="sample" label="样本" width="120" />
            <el-table-column v-for="a in form.arms" :key="a._key" :label="a.label" align="center">
              <template #default>
                <el-icon color="#67c23a"><CircleCheck /></el-icon>
              </template>
            </el-table-column>
          </el-table>
          <div class="hint" style="margin-top:8px">
            实际样本由 OBS 目录下的配置文件决定，这里按登记的「数据总量」估算。
          </div>
        </template>

        <!-- 底部按钮 -->
        <div class="actions">
          <el-button @click="step > 0 ? step-- : router.push('/batch')">
            {{ step > 0 ? '上一步' : '取消' }}
          </el-button>
          <el-button v-if="step < 3" type="primary" :disabled="!canNext" @click="step++">下一步</el-button>
          <el-button v-else type="primary" :loading="creating" @click="submit">创建实验</el-button>
        </div>
      </div>

      <!-- 右：规模预估 -->
      <div class="glass-card est-card">
        <h4 class="est-title">任务规模预估</h4>
        <div class="est-row"><span>Harness 数</span><b>{{ form.arms.length }}</b></div>
        <div class="est-row"><span>测试样本数</span><b>{{ sampleTotal }}</b></div>
        <div class="est-row"><span>重复次数</span><b>{{ form.repeat_times }}</b></div>
        <el-divider style="margin:12px 0" />
        <div class="est-total">
          <span>总任务数</span>
          <b :class="{ warn: totalTasks > BIG_THRESHOLD }">{{ totalTasks }}</b>
        </div>
        <el-alert v-if="totalTasks > BIG_THRESHOLD" type="warning" :closable="false"
          show-icon style="margin-top:12px"
          :title="`任务量较大（${totalTasks}），请确认资源池能承载`" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Back, Plus, CircleCheck } from '@element-plus/icons-vue'
import api from '../api'
import { useHarnessStore } from '../stores/harness'

const router = useRouter()
const harnessStore = useHarnessStore()

const BIG_THRESHOLD = 2000
const PREVIEW_ROWS = 3

const SCHEDULE_LABEL = {
  shared: '共享并发池', isolated: '独立队列', serial: '串行执行',
}
const SCHEDULE_HINT = {
  shared: '所有子任务放进同一个队列，按并发上限一起放行 —— 资源利用率最高。',
  isolated: '每个 Harness 一条独立队列，彼此不互相阻塞，各自按上限并发。',
  serial: '同一队列内严格一个接一个跑，耗时最长但资源占用最稳。',
}

const step = ref(0)
const creating = ref(false)
const armsLoading = ref(false)

const regList = ref([])
const imageList = ref([])
const sandboxList = ref([])
const codeRepoList = ref([])
const armOptions = ref([])

const form = ref({
  name: '',
  description: '',
  registration_id: null,
  arms: [],
  repeat_times: 1,
  schedule_mode: 'shared',
  max_concurrent: 20,
  common: {
    concurrent_num: 100,
    image_name: '',
    max_ttl_second: 36000,
    start_index: 0,
    skill_dir: '',
    default_skills: '',
    agent_dir: '',
    user_profile_dir: '',
    traj_save_path: '',
    model_api_key: '',
    model_base_url: '',
    model_id: '',
    model_api_type: '',
    suffix_api_keys: true,
    proxy_enable: false,
    agents: [],
    sandbox_config_id: 0,
    code_repo_id: null,
    invite_code: '',
  },
})

const selectedReg = computed(() =>
  regList.value.find(r => r.id === form.value.registration_id) || null)
const sampleTotal = computed(() => selectedReg.value?.data_total || 0)
const totalTasks = computed(() =>
  form.value.arms.length * form.value.repeat_times * sampleTotal.value)

const previewRows = computed(() =>
  Array.from({ length: Math.min(PREVIEW_ROWS, sampleTotal.value || PREVIEW_ROWS) },
    (_, i) => ({ sample: `Case-${String(i + 1).padStart(3, '0')}` })))

const canNext = computed(() => {
  if (step.value === 0) {
    return !!form.value.name && !!form.value.registration_id && !!selectedReg.value?.task_path_obs
  }
  if (step.value === 1) return form.value.arms.length > 0
  return true
})

// --- Harness 选择 ---
const optKey = o => `${o.harness_type}:${o.config_id}`
const isPicked = o => form.value.arms.some(a => a._key === optKey(o))
const pickedVersion = o =>
  form.value.arms.find(a => a._key === optKey(o))?.harness_config_version_id ?? null

function toggleArm(opt) {
  const key = optKey(opt)
  const idx = form.value.arms.findIndex(a => a._key === key)
  if (idx >= 0) {
    form.value.arms.splice(idx, 1)
    return
  }
  // 默认取最新已发布版本（接口按 created_at DESC 返回，[0] 最新）
  const v = opt.versions[0]
  form.value.arms.push({
    _key: key,
    harness_type: opt.harness_type,
    harness_config_id: opt.config_id,
    harness_config_version_id: v?.id ?? null,
    label: `${opt.config_name}${v ? ' ' + v.version : ''}`,
  })
}

function setVersion(opt, versionId) {
  const arm = form.value.arms.find(a => a._key === optKey(opt))
  if (!arm) return
  arm.harness_config_version_id = versionId
  const v = opt.versions.find(x => x.id === versionId)
  arm.label = `${opt.config_name}${v ? ' ' + v.version : ''}`
}

function onRegChange() {
  // 实验名留空时用登记名兜一个，用户可改
  if (!form.value.name && selectedReg.value) {
    form.value.name = `${selectedReg.value.task_name} 横向评测`
  }
}

// --- 加载 ---
async function loadArmOptions() {
  armsLoading.value = true
  try {
    const configs = await api.get('/harness-configs')
    // 每个方案拉一次已发布版本。方案数是几十量级，并发拉没问题。
    const withVersions = await Promise.all((configs || []).map(async c => {
      let versions = []
      try {
        const all = await api.get(`/harness-configs/${c.id}/versions`, { silent: true })
        versions = (all || []).filter(v => v.status === 'published')
      } catch { versions = [] }
      return {
        key: `${c.harness_type}:${c.id}`,
        harness_type: c.harness_type,
        config_id: c.id,
        config_name: c.name,
        versions,
      }
    }))
    // 没有已发布版本的方案不能用于实验（create_instance 会拒绝）
    armOptions.value = withVersions.filter(o => o.versions.length > 0)
  } catch {
    armOptions.value = []
  } finally {
    armsLoading.value = false
  }
}

async function submit() {
  if (!form.value.arms.length) {
    ElMessage.warning('请至少选择一个 Harness')
    return
  }
  creating.value = true
  try {
    const payload = {
      ...form.value,
      // _key 只是前端用来做选中态的，后端模型没有这个字段
      arms: form.value.arms.map(({ _key, ...rest }) => rest),
    }
    const res = await api.post('/batch-experiments', payload)
    if (res.failed) ElMessage.warning(res.message)
    else ElMessage.success(res.message || '创建成功')
    router.push(`/batch/${res.experiment.id}`)
  } catch { /* interceptor */ } finally {
    creating.value = false
  }
}

onMounted(async () => {
  harnessStore.load()
  loadArmOptions()
  try { regList.value = await api.get('/registrations') } catch { regList.value = [] }
  try { imageList.value = await api.get('/images') } catch { imageList.value = [] }
  try {
    sandboxList.value = await api.get('/sandbox-configs', { params: { enabled_only: true } })
  } catch { sandboxList.value = [] }
  try { codeRepoList.value = await api.get('/code-repos') } catch { codeRepoList.value = [] }
})
</script>

<style scoped>
.header-row { margin-bottom: 16px; }
.layout {
  display: grid;
  grid-template-columns: 180px 1fr 260px;
  gap: 16px;
  align-items: start;
}
.steps-card { padding: 24px 16px; position: sticky; top: 16px; }
.form-card { padding: 24px; min-height: 420px; }
.est-card { padding: 20px; position: sticky; top: 16px; }
.est-title { margin: 0 0 16px; font-size: 14px; }
.est-row {
  display: flex; justify-content: space-between;
  font-size: 13px; color: var(--text-secondary); margin-bottom: 10px;
}
.est-row b { color: var(--text-primary); }
.est-total { display: flex; justify-content: space-between; align-items: center; }
.est-total span { font-size: 13px; color: var(--text-secondary); }
.est-total b { font-size: 28px; color: #6366f1; }
.est-total b.warn { color: #e6a23c; }
.section-title { margin: 0 0 20px; font-size: 16px; }
.section-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.muted { color: var(--text-muted); font-weight: 400; font-size: 13px; }
.hint { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.hint.inline { margin-left: 12px; }
.hint.danger { color: var(--danger, #f56c6c); }
.arm-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 12px; }
.arm-card {
  border: 1px solid var(--border-color); border-radius: 8px;
  padding: 12px; cursor: pointer; transition: all .2s;
}
.arm-card:hover { border-color: #a5b4fc; }
.arm-card.active { border-color: #6366f1; background: rgba(99,102,241,.05); }
.arm-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.arm-name { font-size: 13px; font-weight: 600; }
.arm-sub { font-size: 12px; color: var(--text-muted); margin-top: 6px; }
.actions {
  display: flex; justify-content: flex-end; gap: 8px;
  margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border-color);
}
</style>
