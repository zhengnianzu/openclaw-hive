<template>
  <div v-loading="loading">
    <!-- 头部 -->
    <div class="head">
      <div class="head-left">
        <el-button text @click="router.push('/batch')"><el-icon><Back /></el-icon></el-button>
        <div>
          <div class="title-row">
            <h2 style="margin:0">{{ exp.name || '—' }}</h2>
            <el-tag :type="statusType(exp.status)" size="small">{{ statusLabel(exp.status) }}</el-tag>
          </div>
          <div class="sub">
            {{ exp.id }} ｜ 测试集：{{ exp.test_set_name || '—' }}（{{ exp.sample_total }} 条）
            ｜ {{ exp.arm_count }} 个子任务 ｜ 共 {{ exp.task_total }} 个任务
          </div>
        </div>
      </div>
      <div v-if="authStore.isOperator" style="display:flex;gap:8px;align-items:center">
        <!-- 所有动作统一收进「更多」：这一页的按钮不该有一排，主次不分容易误点。
             「更多」上带角标提示有多少个失败任务可重跑。 -->
        <el-dropdown trigger="click" @command="cmd => handleMore(cmd)">
          <el-button>
            更多<el-icon style="margin-left:4px"><ArrowDown /></el-icon>
            <span v-if="exp.failed_arms" class="btn-badge">{{ exp.failed_arms }}</span>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="start"
                :disabled="!exp.queue_id || (!exp.startable_arms && exp.status !== 'paused')">
                {{ exp.status === 'paused' ? '继续执行' : '启动队列' }}
              </el-dropdown-item>
              <el-dropdown-item command="rerun" :disabled="!exp.failed_arms">
                重跑失败<span v-if="exp.failed_arms" class="menu-count">（{{ exp.failed_arms }}）</span>
              </el-dropdown-item>
              <el-dropdown-item command="copy" divided>复制实验</el-dropdown-item>
              <el-dropdown-item command="pause" :disabled="exp.status === 'paused'" divided>
                暂停调度
              </el-dropdown-item>
              <el-dropdown-item command="resume" :disabled="exp.status !== 'paused'">
                恢复调度
              </el-dropdown-item>
              <el-dropdown-item command="cancel" divided>
                取消实验（停止全部任务）
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <el-tabs v-model="tab" class="glass-card tabs-card" @tab-change="onTabChange">
      <!-- ============ 执行概览 ============ -->
      <el-tab-pane label="执行概览" name="overview">
        <div class="stat-grid">
          <div class="stat"><div class="stat-k">已完成</div><div class="stat-v ok">{{ exp.completed }}</div></div>
          <div class="stat"><div class="stat-k">运行中</div><div class="stat-v run">{{ exp.running }}</div></div>
          <div class="stat"><div class="stat-k">排队中</div><div class="stat-v">{{ exp.queued }}</div></div>
          <div class="stat">
            <div class="stat-k">
              成功率
              <el-tooltip placement="top">
                <template #content>
                  DONE 口径：agent 自己打的【Task_Done】标记数 ÷ 已结算任务数。<br>
                  与「完成数」不同 —— 后者只代表流水线没抛异常。
                </template>
                <el-icon class="q"><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
            <div class="stat-v" :style="{color: rateColor(exp.success_rate)}">{{ exp.success_rate }}%</div>
          </div>
        </div>

        <div class="progress-block">
          <div class="progress-head">
            <span>整体进度</span>
            <span class="muted">{{ exp.progress_percent }}% ({{ settled }} / {{ exp.task_total }})</span>
          </div>
          <el-progress :percentage="exp.progress_percent" :stroke-width="14"
            :status="exp.progress_percent >= 100 ? 'success' : undefined" />
        </div>

        <!-- 任务列表并入执行概览：一屏看完「这批跑得怎么样 + 每个子任务怎么样」 -->
        <el-table :data="items" size="small" border style="width:100%;margin-top:20px">
          <el-table-column label="任务名称" min-width="230">
            <template #default="{row}">
              <el-link v-if="row.instance_id" type="primary"
                @click="router.push(`/instance/${row.instance_id}`)">
                {{ row.instance_name || row.instance_id }}
              </el-link>
              <span v-else class="err">创建失败</span>
              <div v-if="row.create_error" class="err-detail">{{ row.create_error }}</div>
              <div v-else-if="row.repeat_idx > 1" class="muted" style="font-size:12px">
                第{{ row.repeat_idx }}轮
              </div>
            </template>
          </el-table-column>

          <el-table-column label="Harness 类型" width="140">
            <template #default="{row}">
              <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                {{ harnessStore.label(row.harness_type) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="Harness 配置" min-width="170" show-overflow-tooltip>
            <template #default="{row}">
              <div>{{ row.arm_label || '—' }}</div>
              <div v-if="row.sim_label" class="muted" style="font-size:12px">
                模拟：{{ row.sim_label }}
              </div>
            </template>
          </el-table-column>

          <el-table-column label="任务进度" min-width="190">
            <template #default="{row}">
              <div v-if="row.create_error" class="err">创建失败</div>
              <template v-else>
                <div class="muted">
                  {{ row.completed_tasks + row.failed_tasks }} / {{ row.total_tasks }}
                  <span v-if="row.failed_tasks" style="color:#f56c6c">（失败 {{ row.failed_tasks }}）</span>
                </div>
                <el-progress :percentage="row.progress_percent" :stroke-width="8" :show-text="false" />
              </template>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="100" align="center">
            <template #default="{row}">
              <el-tag v-if="row.status" size="small" :type="instStatusType(row.status)">{{ row.status }}</el-tag>
              <span v-else>—</span>
            </template>
          </el-table-column>

          <el-table-column label="DONE" width="80" align="center" prop="done_tasks" />

          <el-table-column label="操作" width="250" fixed="right">
            <template #default="{row}">
              <template v-if="row.instance_id">
                <!-- 三个按钮必须 nowrap 且左对齐：默认 el-button 之间有 margin-left，
                     宽度不足时会被挤成两行。「更多」按钮还带图标，更容易撑破。 -->
                <div class="row-ops">
                <el-button size="small"
                  @click="router.push(`/instance/${row.instance_id}?tab=logs`)">日志</el-button>
                <el-button size="small"
                  @click="router.push(`/instance/${row.instance_id}`)">详情</el-button>
                <el-dropdown trigger="click" @command="cmd => onRowCommand(cmd, row)"
                  v-if="authStore.isOperator">
                  <el-button size="small">更多<el-icon style="margin-left:2px"><ArrowDown /></el-icon></el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="start" :disabled="!['created','stopped'].includes(row.status)">
                        启动任务
                      </el-dropdown-item>
                      <el-dropdown-item command="stop" :disabled="!['running','preparing'].includes(row.status)">
                        停止任务
                      </el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
                </div>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ============ 结果对比 ============ -->
      <el-tab-pane label="结果对比" name="compare">
        <div class="compare-grid">
          <div>
            <div class="block-head">
              <h4 style="margin:0">总体评测结果</h4>
              <el-radio-group v-model="rateMode" size="small">
                <el-radio-button value="done">DONE 率</el-radio-button>
                <el-radio-button value="completed">完成率</el-radio-button>
              </el-radio-group>
            </div>
            <el-table :data="compare" size="small" border style="width:100%">
              <el-table-column label="Harness" min-width="170">
                <template #default="{row}">
                  <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                    {{ row.arm_label }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="rateMode === 'done' ? 'DONE 率' : '完成率'"
                width="110" align="center" sortable :sort-by="r => rateOf(r)">
                <template #default="{row}">
                  <span :style="{color: rateColor(rateOf(row))}">{{ rateOf(row) }}%</span>
                </template>
              </el-table-column>
              <el-table-column label="平均耗时" width="110" align="center"
                sortable :sort-by="r => r.avg_task_seconds ?? Infinity">
                <template #default="{row}">{{ fmtDuration(row.avg_task_seconds) }}</template>
              </el-table-column>
              <el-table-column label="平均 Token" width="110" align="center">
                <template #default="{row}">
                  <el-tooltip content="Token 统计暂未开放" placement="top">
                    <span class="muted">{{ row.total_tokens ?? '—' }}</span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="评分" width="130" align="center"
                sortable :sort-by="r => r.avg_score ?? -1">
                <template #default="{row}">
                  <span v-if="row.avg_score != null" class="score">{{ row.avg_score }}</span>
                  <span v-else class="muted">—</span>
                  <div v-if="row.unscored_count" class="unscored">{{ row.unscored_count }} 条未评估</div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div>
            <h4 style="margin:0 0 12px">关键指标对比</h4>
            <EChart v-if="compare.length" :option="chartOption" height="300px" />
            <el-empty v-else description="暂无数据" />
          </div>
        </div>

        <!-- 逐样本矩阵 -->
        <h4 style="margin:24px 0 12px">
          逐样本横向对比
          <span class="muted">（共 {{ matrix.total }} 条样本，点击格子进入任务详情）</span>
        </h4>
        <el-table :data="matrix.rows" size="small" border style="width:100%" v-loading="matrixLoading">
          <el-table-column label="样本" min-width="220" fixed show-overflow-tooltip>
            <template #default="{row}">{{ row.config_name }}</template>
          </el-table-column>
          <el-table-column v-for="col in matrix.columns" :key="col.instance_id"
            :label="col.arm_label" align="center" min-width="130">
            <template #default="{row}">
              <div v-if="row.cells[col.instance_id]" class="cell-click"
                @click="gotoCell(col.instance_id, row.config_name)">
                <el-icon v-if="row.cells[col.instance_id].task_done" color="#67c23a"><CircleCheck /></el-icon>
                <el-icon v-else color="#f56c6c"><CircleClose /></el-icon>
                <span v-if="row.cells[col.instance_id].traj_level" class="lvl">
                  {{ row.cells[col.instance_id].traj_level }}
                </span>
                <span v-if="row.cells[col.instance_id].eval_score != null" class="lvl">
                  {{ row.cells[col.instance_id].eval_score }}
                </span>
              </div>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无样本数据（任务还没跑出结果）" /></template>
        </el-table>
        <el-pagination v-if="matrix.total > matrix.page_size" style="margin-top:12px;justify-content:flex-end"
          layout="total, prev, pager, next" :total="matrix.total"
          :page-size="matrix.page_size" :current-page="matrix.page" @current-change="loadMatrix" />
      </el-tab-pane>

      <!-- ============ 详情 ============ -->
      <el-tab-pane label="详情" name="config">
        <!-- 顶部四个数据块：一眼看清规模与健康度 -->
        <div class="detail-stats">
          <div class="dstat">
            <div class="dstat-icon purple"><el-icon><Files /></el-icon></div>
            <div>
              <div class="dstat-k">子任务数</div>
              <div class="dstat-v">{{ exp.arm_count }}</div>
            </div>
          </div>
          <div class="dstat">
            <div class="dstat-icon green"><el-icon><CircleCheck /></el-icon></div>
            <div>
              <div class="dstat-k">已完成</div>
              <div class="dstat-v">{{ completedArms }}</div>
            </div>
          </div>
          <div class="dstat">
            <div class="dstat-icon red"><el-icon><CircleClose /></el-icon></div>
            <div>
              <div class="dstat-k">失败数</div>
              <div class="dstat-v">{{ exp.failed_arms || 0 }}</div>
            </div>
          </div>
          <div class="dstat">
            <div class="dstat-icon blue"><el-icon><Timer /></el-icon></div>
            <div>
              <div class="dstat-k">队列并发</div>
              <div class="dstat-v">{{ exp.max_concurrent }}</div>
            </div>
          </div>
        </div>

        <!-- 基础信息 -->
        <section class="dsec">
          <div class="dsec-head"><el-icon><Document /></el-icon><h4>基础信息</h4></div>
          <el-descriptions :column="2" border size="small" class="dinfo">
            <el-descriptions-item label="实验 ID">{{ exp.id }}</el-descriptions-item>
            <el-descriptions-item label="创建人">{{ exp.created_by || '—' }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ exp.created_at }}</el-descriptions-item>
            <el-descriptions-item label="测试集">{{ exp.test_set_name || '—' }}</el-descriptions-item>
            <el-descriptions-item label="样本数">{{ exp.sample_total }}</el-descriptions-item>
            <el-descriptions-item label="重复次数">{{ exp.repeat_times }}</el-descriptions-item>
            <el-descriptions-item label="队列名称">{{ exp.queue_name || exp.queue_id || '—' }}</el-descriptions-item>
            <el-descriptions-item label="队列并发">{{ exp.max_concurrent }}</el-descriptions-item>
            <el-descriptions-item label="长尾跳过">
              {{ exp.tail_skip_enabled ? `开启（${exp.tail_skip_percent}%）` : '关闭' }}
            </el-descriptions-item>
            <el-descriptions-item label="描述">{{ exp.description || '—' }}</el-descriptions-item>
          </el-descriptions>
        </section>

        <!-- 配置摘要 -->
        <section class="dsec">
          <div class="dsec-head">
            <el-icon><Files /></el-icon><h4>配置摘要</h4>
            <span class="muted">本次批量实验的核心配置概览</span>
          </div>
          <div class="summary-grid">
            <div class="sm-item">
              <div class="sm-icon"><el-icon><Box /></el-icon></div>
              <div class="sm-body">
                <div class="sm-k">Harness 组合</div>
                <div class="sm-v">{{ harnessSummary.text }}</div>
              </div>
              <span class="sm-count">{{ harnessSummary.count }} 个</span>
            </div>
            <div class="sm-item">
              <div class="sm-icon"><el-icon><Cpu /></el-icon></div>
              <div class="sm-body">
                <div class="sm-k">主力模型</div>
                <div class="sm-v">{{ modelSummary.text }}</div>
              </div>
              <span class="sm-count">{{ modelSummary.count }} 个</span>
            </div>
            <div class="sm-item">
              <div class="sm-icon"><el-icon><User /></el-icon></div>
              <div class="sm-body">
                <div class="sm-k">用户模拟</div>
                <div class="sm-v">{{ simSummary }}</div>
              </div>
            </div>
            <div class="sm-item">
              <div class="sm-icon"><el-icon><RefreshRight /></el-icon></div>
              <div class="sm-body">
                <div class="sm-k">重复次数</div>
                <div class="sm-v">{{ exp.repeat_times }}</div>
              </div>
            </div>
            <div class="sm-item">
              <div class="sm-icon"><el-icon><VideoPlay /></el-icon></div>
              <div class="sm-body">
                <div class="sm-k">跳过策略</div>
                <div class="sm-v">{{ exp.tail_skip_enabled ? `长尾跳过（${exp.tail_skip_percent}%）` : '不跳过' }}</div>
              </div>
            </div>
          </div>
        </section>

        <!-- 公共任务参数 -->
        <section class="dsec">
          <div class="dsec-head">
            <el-icon><Setting /></el-icon><h4>公共任务参数</h4>
            <span class="muted">任务级公共配置，包含 Harness、模型、沙箱等参数</span>
          </div>
          <div class="param-wrap">
            <div class="param-nav">
              <div v-for="g in paramGroups" :key="g.key" class="pnav-item"
                :class="{ active: activeGroup === g.key }" @click="activeGroup = g.key">
                <div class="pnav-icon"><el-icon><component :is="g.icon" /></el-icon></div>
                <div class="pnav-body">
                  <div class="pnav-t">{{ g.title }}</div>
                  <div class="pnav-s">{{ g.desc }}</div>
                </div>
                <el-icon class="pnav-arrow"><ArrowRight /></el-icon>
              </div>
            </div>
            <div class="param-body">
              <div class="param-body-head">
                <span class="code-mark">{{ activeGroupMeta.title }}</span>
                <el-button size="small" @click="copyParams">
                  <el-icon style="margin-right:4px"><CopyDocument /></el-icon>复制
                </el-button>
              </div>
              <el-table :data="activeGroupMeta.rows" size="small" border class="param-table"
                :empty-text="'暂无数据'">
                <el-table-column v-for="c in activeGroupMeta.columns" :key="c.prop"
                  :prop="c.prop" :label="c.label" :width="c.width"
                  :min-width="c.minWidth || 120" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span :class="{ 'cell-empty': isEmpty(row[c.prop]) }">
                      {{ formatParam(row[c.prop]) }}
                    </span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </div>
        </section>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Back, VideoPlay, VideoPause, CircleClose, CircleCheck, QuestionFilled, ArrowDown,
  RefreshRight, Files, Timer, Document, Box, Cpu, User, Setting, ArrowRight,
  CopyDocument, ArrowUp, Tickets, MagicStick,
} from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import EChart from '../components/EChart.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()

const batchId = route.params.id
const loading = ref(false)
const matrixLoading = ref(false)
const tab = ref(route.query.tab || 'overview')
const rateMode = ref('done')

const exp = ref({
  completed: 0, running: 0, queued: 0, failed: 0, done: 0,
  task_total: 0, arm_count: 0, sample_total: 0,
  success_rate: 0, progress_percent: 0, params_json: '{}',
})
const items = ref([])
const compare = ref([])
const matrix = ref({ total: 0, page: 1, page_size: 50, columns: [], rows: [] })
let timer = null

const settled = computed(() => (exp.value.completed || 0) + (exp.value.failed || 0))

// params_json 里存的是创建时的完整参数（密钥已脱敏），此前端只做展示分组。
const parsedParams = computed(() => {
  try { return JSON.parse(exp.value.params_json || '{}') } catch { return {} }
})

const prettyParams = computed(() => {
  try { return JSON.stringify(JSON.parse(exp.value.params_json || '{}'), null, 2) }
  catch { return exp.value.params_json || '{}' }
})

// ---- 详情 tab：配置摘要 + 参数分组 ----
const activeGroup = ref('harness')

// harness_config_id / version_id → 可读名称。
// params_json 里存的是 ID，直接展示用户看不懂，所以进页面时拉一次映射表。
const cfgNames = ref({})      // { [config_id]: '方案名' }
const verNames = ref({})      // { [version_id]: 'v1.2' }
const sandboxList = ref([])   // 沙箱配置列表，用于把 sandbox_config_id 显示成名称

// 已完成子任务数 = 总数 - 失败 - 运行中 - 待启动（负值兜底为 0）
const completedArms = computed(() => Math.max(
  0,
  (exp.value.arm_count || 0)
    - (exp.value.failed_arms || 0)
    - (exp.value.active_arms || 0)
    - (exp.value.startable_arms || 0),
))

const arms = computed(() => parsedParams.value.arms || [])

// Harness 组合：按 harness_type 去重，保留出现顺序
const harnessSummary = computed(() => {
  const seen = []
  for (const a of arms.value) {
    const t = a.harness_type || ''
    if (t && !seen.includes(t)) seen.push(t)
  }
  return {
    count: seen.length,
    text: seen.map(t => harnessStore.label(t)).join('、') || '—',
  }
})

// 主力模型：跨臂按 model_id 去重（同一模型配在不同 harness 下只算一个）
const modelSummary = computed(() => {
  const seen = []
  for (const a of arms.value) {
    const m = (a.model_id || '').trim()
    if (m && !seen.includes(m)) seen.push(m)
  }
  // 臂上没配模型时回落到 common 的兜底值
  if (!seen.length) {
    const fallback = (parsedParams.value.model_id || '').trim()
    if (fallback) seen.push(fallback)
  }
  return { count: seen.length, text: seen.join('、') || '—' }
})

const simSummary = computed(() => {
  const sims = parsedParams.value.sim_arms || []
  const labels = sims.map(s => s.label).filter(Boolean)
  if (labels.length) return labels.join('、')
  const agents = sims.flatMap(s => s.agents || [])
  const names = agents.map(a => a.name || a.model).filter(Boolean)
  if (names.length) return [...new Set(names)].join('、')
  return '模板默认'
})

// 参数分组与创建页的五个 Tab 一一对应：Harness 类型 / 模型配置 /
// 用户模拟配置 / 批量任务配置 / 高级配置。
// 数据源优先 params_json.harnesses + params_json.models —— 矩阵的两条正交轴，
// 不是摊平后的 arms。展开成子任务配置是后端在建任务时才做的，
// 这里必须还原原始配置，否则「2 个 Harness × 2 个模型」会显示成 4 个重复项。
const paramGroups = computed(() => {
  const p = parsedParams.value
  const harnessList = Array.isArray(p.harnesses) ? p.harnesses : []
  const modelList = Array.isArray(p.models) ? p.models : []
  const blocks = Array.isArray(p.blocks) ? p.blocks : []

  // 老实验没有 blocks（只存了摊平后的 arms），按 harness 三件套归组还原成块。
  // 不做归组的话「GLM 配在 Claude Code 和 Codex 下」会显示成两个独立块，
  // 看起来就像重复配置了两次。
  const blockList = blocks.length ? blocks : (() => {
    const byKey = new Map()
    for (const a of arms.value) {
      const key = `${a.harness_type}|${a.harness_config_id}|${a.harness_config_version_id}`
      if (!byKey.has(key)) {
        byKey.set(key, {
          harness_type: a.harness_type,
          harness_config_id: a.harness_config_id,
          harness_config_version_id: a.harness_config_version_id,
          label: a.harness_config_id ? '' : (a.label || ''),
          image_name: a.image_name,
          sandbox_config_id: a.sandbox_config_id,
          models: [],
        })
      }
      if (a.model_id || a.model_base_url) {
        byKey.get(key).models.push({
          model_id: a.model_id,
          model_base_url: a.model_base_url,
          model_api_key: a.model_api_key,
          model_api_type: a.model_api_type,
        })
      }
    }
    return [...byKey.values()]
  })()

  // ---- Harness 类型：一行一个 Harness 块（不按模型展开） ----
  const harnessCols = [
    { prop: 'harness_type', label: 'Harness 类型', minWidth: 150 },
    { prop: 'config_name', label: '配置方案', minWidth: 180 },
    { prop: 'version_name', label: '版本', minWidth: 120 },
    { prop: 'image_name', label: '镜像名称', minWidth: 200 },
    { prop: 'sandbox_name', label: '沙箱配置', minWidth: 160 },
  ]
  // ID 一律映射成实际名称：用户看不懂数字 ID。
  // 映射表拉不到时（接口失败/已删除的方案）退回显示 ID，至少不至于是空白。
  const cfgName = id => cfgNames.value[id] || (id != null ? `#${id}` : null)
  const verName = id => verNames.value[id] || (id != null ? `#${id}` : null)
  const sandboxName = id => {
    const s = (sandboxList.value || []).find(x => x.id === id)
    return s ? (s.name || s.pool_name || `#${id}`) : (id != null ? `#${id}` : null)
  }

  // Harness 轴的行来源：新形状直接用 harnesses；老形状用上面还原的块
  const harnessSource = harnessList.length ? harnessList : blockList

  const harnessRows = harnessSource.map(b => ({
    harness_type: harnessStore.label(b.harness_type),
    config_name: cfgName(b.harness_config_id),
    version_name: verName(b.harness_config_version_id),
    image_name: b.image_name ?? p.image_name ?? null,
    sandbox_name: sandboxName(b.sandbox_config_id ?? p.sandbox_config_id),
  }))

  // ---- 模型配置：一行一个模型（模型轴对新形状是独立的，对所有 Harness 生效） ----
  const modelCols = [
    { prop: 'model_id', label: '模型 ID', minWidth: 180 },
    { prop: 'model_api_type', label: 'API 类型', minWidth: 130 },
    { prop: 'model_base_url', label: 'Base URL', minWidth: 240 },
    { prop: 'model_api_key', label: 'API Key', minWidth: 150 },
  ]
  // 新形状直接用 params.models；老形状从块内模型里按 model_id 去重凑出来。
  // 这张表回答的是「这次实验用了哪几个模型」。
  const modelSeen = new Map()
  const rawModels = modelList.length
    ? modelList
    : blockList.flatMap(b => ((b.models || []).length
        ? b.models
        : [{ model_id: p.model_id || '模板默认', model_api_type: p.model_api_type,
             model_base_url: p.model_base_url, model_api_key: p.model_api_key }]))
  for (const m of rawModels) {
    const mid = (m.model_id || '').trim()
    if (mid && !modelSeen.has(mid)) {
      modelSeen.set(mid, {
        model_id: mid,
        model_api_type: m.model_api_type,
        model_base_url: m.model_base_url,
        model_api_key: m.model_api_key,
      })
    }
  }
  const modelRows = [...modelSeen.values()]

  // 用户模拟：sim_arms 里的 agents 摊平成行；没有分轴时回落到 common.agents
  const simList = (p.sim_arms && p.sim_arms.length)
    ? p.sim_arms.flatMap(s => s.agents || [])
    : (p.agents || [])
  const agentCols = [
    { prop: 'name', label: 'Agent 名称', minWidth: 130 },
    { prop: 'model', label: '模型 ID', minWidth: 160 },
    { prop: 'api', label: 'API 类型', minWidth: 120 },
    { prop: 'base_url', label: 'Base URL', minWidth: 220 },
    { prop: 'provider', label: 'Provider', minWidth: 110 },
    { prop: 'api_key', label: 'API Key', minWidth: 130 },
  ]
  const agentRows = simList.map((a, i) => ({
    name: a.name || `agent${i + 1}`,
    model: a.model,
    api: a.api,
    base_url: a.base_url,
    provider: a.provider,
    api_key: a.api_key,
  }))

  const kvCols = [
    { prop: 'k', label: '参数', minWidth: 190 },
    { prop: 'v', label: '值', minWidth: 260 },
  ]

  return [
    {
      key: 'harness', title: 'Harness 类型',
      icon: Box,
      columns: harnessCols,
      rows: harnessRows,
      data: blockList.map(b => ({
        harness_type: b.harness_type,
        harness_config_id: b.harness_config_id,
        harness_config_version_id: b.harness_config_version_id,
        label: b.label,
        image_name: b.image_name ?? p.image_name ?? null,
        sandbox_config_id: b.sandbox_config_id ?? p.sandbox_config_id ?? null,
      })),
    },
    {
      key: 'hconfig', title: '模型配置',
      icon: Cpu,
      columns: modelCols,
      rows: modelRows,
      data: blockList.map(b => ({
        label: b.label,
        harness_type: b.harness_type,
        models: (b.models || []).map(m => ({
          model_id: m.model_id,
          model_base_url: m.model_base_url,
          model_api_key: m.model_api_key,
          model_api_type: m.model_api_type,
        })),
      })),
    },
    {
      key: 'sim', title: '用户模拟配置',
      icon: User,
      columns: agentCols,
      rows: agentRows,
      data: simList.map(a => ({
        name: a.name, model: a.model, base_url: a.base_url,
        api_key: a.api_key, api: a.api, provider: a.provider,
      })),
    },
    {
      key: 'batch', title: '批量任务配置',
      icon: Tickets,
      columns: kvCols,
      rows: [
        { k: '队列名称', v: exp.value.queue_name || exp.value.queue_id || null },
        { k: '队列并发', v: exp.value.max_concurrent },
        { k: '长尾跳过', v: exp.value.tail_skip_enabled ? `开启（${exp.value.tail_skip_percent}%）` : '关闭' },
        { k: '重复次数', v: exp.value.repeat_times },
        { k: '独立并发数', v: p.concurrent_num },
      ],
      data: {
        queue_name: exp.value.queue_name || exp.value.queue_id,
        max_concurrent: exp.value.max_concurrent,
        tail_skip_enabled: exp.value.tail_skip_enabled,
        tail_skip_percent: exp.value.tail_skip_percent,
        repeat_times: exp.value.repeat_times,
        concurrent_num: p.concurrent_num,
      },
    },
    {
      key: 'adv', title: '高级配置',
      icon: MagicStick,
      columns: kvCols,
      rows: [
        { k: '沙箱最大存活（秒）', v: p.max_ttl_second },
        { k: '起始索引', v: p.start_index },
        { k: 'API Key 加沙箱后缀', v: p.suffix_api_keys },
        { k: '启动本地代理', v: p.proxy_enable },
        { k: '代码仓 ID', v: p.code_repo_id },
        { k: '轨迹保存路径', v: p.traj_save_path },
      ],
      data: {
        max_ttl_second: p.max_ttl_second,
        start_index: p.start_index,
        suffix_api_keys: p.suffix_api_keys,
        proxy_enable: p.proxy_enable,
        code_repo_id: p.code_repo_id,
        traj_save_path: p.traj_save_path,
      },
    },
  ]
})

const activeGroupMeta = computed(
  () => paramGroups.value.find(g => g.key === activeGroup.value) || paramGroups.value[0],
)

const activeJson = computed(() => {
  const g = activeGroupMeta.value
  if (!g) return prettyParams.value
  try { return JSON.stringify(g.data, null, 2) } catch { return '{}' }
})

const isEmpty = v => v === null || v === undefined || v === ''

function formatParam(v) {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'boolean') return v ? '开启' : '关闭'
  return v
}

async function copyParams() {
  try {
    await navigator.clipboard.writeText(activeJson.value)
    ElMessage.success('已复制到剪贴板')
  } catch { ElMessage.warning('复制失败，请手动选择文本') }
}

const STATUS_LABEL = {
  created: '待开始', running: '运行中', paused: '已暂停',
  completed: '已完成', cancelled: '已取消',
  // failed 是由子任务状态推导出来的（见后端 _derive_status），
  // 库里原本没有这个取值，漏登记就会裸露成英文。
  failed: '有失败',
}
const STATUS_TYPE = {
  created: 'info', running: 'success', paused: 'warning',
  completed: '', cancelled: 'danger', failed: 'danger',
}
const statusLabel = s => STATUS_LABEL[s] || s || '—'
const statusType = s => STATUS_TYPE[s] ?? 'info'

function instStatusType(s) {
  if (s === 'running') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'queued' || s === 'preparing') return 'warning'
  return 'info'
}

function rateColor(v) {
  if (v >= 80) return '#67c23a'
  if (v >= 50) return '#e6a23c'
  return '#f56c6c'
}

const rateOf = r => (rateMode.value === 'done' ? r.done_rate : r.completed_rate)

function fmtDuration(sec) {
  if (sec == null) return '—'
  const m = Math.floor(sec / 60)
  const s = Math.round(sec % 60)
  return m ? `${m}m ${s}s` : `${s}s`
}

function gotoCell(instanceId, configName) {
  router.push(`/instance/${instanceId}?tab=outputs&config=${encodeURIComponent(configName)}`)
}

const chartOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { data: ['成功率', '评分'] },
  grid: { left: 40, right: 16, top: 40, bottom: 40 },
  xAxis: {
    type: 'category',
    data: compare.value.map(r => r.arm_label),
    axisLabel: { interval: 0, fontSize: 11 },
  },
  yAxis: { type: 'value' },
  series: [
    {
      name: '成功率', type: 'bar',
      data: compare.value.map(r => rateOf(r)),
      itemStyle: { color: '#6366f1' }, barMaxWidth: 36,
    },
    {
      name: '评分', type: 'bar',
      // 未评估的臂不画柱子（null 在 echarts 里就是空缺），不要填 0
      data: compare.value.map(r => r.avg_score),
      itemStyle: { color: '#22d3a7' }, barMaxWidth: 36,
    },
  ],
}))

async function loadMain() {
  try {
    const [e, it] = await Promise.all([
      api.get(`/batch-experiments/${batchId}`, { silent: true }),
      api.get(`/batch-experiments/${batchId}/items`, { silent: true }),
    ])
    exp.value = e
    items.value = it
  } catch { /* interceptor */ }
}

async function loadCompare() {
  try {
    compare.value = await api.get(`/batch-experiments/${batchId}/compare`, { silent: true })
  } catch { compare.value = [] }
}

async function loadMatrix(page = 1) {
  matrixLoading.value = true
  try {
    matrix.value = await api.get(`/batch-experiments/${batchId}/matrix`,
      { params: { page, page_size: matrix.value.page_size }, silent: true })
  } catch { /* interceptor */ } finally {
    matrixLoading.value = false
  }
}

function onTabChange(name) {
  router.replace({ query: { ...route.query, tab: name } })
  // 对比数据只在进入该 tab 时拉，避免概览页每 10s 顺带算一遍聚合
  if (name === 'compare') {
    loadCompare()
    loadMatrix(1)
  }
}

// 终态实验不再轮询：数据不会再变，白耗请求
const isActive = computed(() => ['created', 'running', 'paused'].includes(exp.value.status))

// 子任务行内操作（启动/停止单个任务），与任务详情页的按钮同义
async function onRowCommand(cmd, row) {
  if (!row.instance_id) return
  const label = cmd === 'start' ? '启动' : '停止'
  try {
    await ElMessageBox.confirm(`${label}任务「${row.instance_name || row.instance_id}」？`,
      label + '确认', { type: 'warning' })
  } catch { return }
  try {
    if (cmd === 'start') await api.post(`/instances/${row.instance_id}/start`)
    else await api.post(`/instances/${row.instance_id}/stop`)
    ElMessage.success(`已${label}`)
    loadMain()
  } catch { /* interceptor */ }
}

const acting = ref('')

// 整批启动：把队列里待启动的子任务全部入队（与列表页同一个后端接口）
async function startQueue() {
  if (!exp.value.queue_id) return
  const paused = exp.value.status === 'paused'
  try {
    await ElMessageBox.confirm(
      paused
        ? `继续执行？将解除暂停并放行排队中的子任务。`
        : `启动队列「${exp.value.queue_name || exp.value.queue_id}」？待启动的子任务会按顺序入队。`,
      paused ? '继续执行' : '启动队列', { type: 'info' })
  } catch { return }
  acting.value = 'start'
  try {
    const res = await api.post(`/queues/${encodeURIComponent(exp.value.queue_id)}/start-all`)
    ElMessage.success(res.message || '已启动')
    loadMain()
  } catch { /* interceptor */ } finally { acting.value = '' }
}

// 重跑失败：只重新拉起 status='failed' 的子任务，成功的和没跑的不动
async function rerunFailed() {
  try {
    await ElMessageBox.confirm(
      `重跑全部失败任务？\n只重新拉起已失败的子任务，成功跑完的不会被重跑。`,
      '重跑失败', { type: 'warning' })
  } catch { return }
  acting.value = 'rerun'
  try {
    const res = await api.post(`/batch-experiments/${batchId}/rerun-failed`)
    if (res.errors && res.errors.length) ElMessage.warning(res.message)
    else ElMessage.success(res.message || '已重跑')
    loadMain()
  } catch { /* interceptor */ } finally { acting.value = '' }
}

function handleMore(cmd) {
  if (cmd === 'start') return startQueue()
  if (cmd === 'rerun') return rerunFailed()
  if (cmd === 'copy') {
    // 与列表页一致：跳新建页预填，让用户补 key 后再建
    return router.push({ path: '/batch/new', query: { copy_from: batchId } })
  }
  return act(cmd)
}

async function act(cmd) {
  const confirms = {
    cancel: '取消实验？将停止所有未完成的子任务。',
  }
  if (confirms[cmd]) {
    try { await ElMessageBox.confirm(confirms[cmd], '确认', { type: 'warning' }) } catch { return }
  }
  try {
    const res = await api.post(`/batch-experiments/${batchId}/${cmd}`)
    ElMessage.success(res.message || '操作成功')
    loadMain()
  } catch { /* interceptor */ }
}

watch(isActive, v => {
  if (!v && timer) { clearInterval(timer); timer = null }
})

// 拉 harness 方案与版本名，供详情页把 ID 显示成名字。失败不阻塞页面。
async function loadHarnessNames() {
  try {
    const configs = await api.get('/harness-configs', { silent: true })
    const map = {}
    for (const cfg of configs || []) map[cfg.id] = cfg.name
    cfgNames.value = map

    const versions = {}
    await Promise.all((configs || []).map(async cfg => {
      try {
        const list = await api.get(`/harness-configs/${cfg.id}/versions`, { silent: true })
        for (const v of list || []) versions[v.id] = v.version
      } catch { /* 单个方案拉不到就跳过后面的映射 */ }
    }))
    verNames.value = versions
  } catch { /* 保持空映射，页面上退回显示 ID */ }

  try {
    const list = await api.get('/sandbox-configs', { silent: true })
    sandboxList.value = list || []
  } catch { sandboxList.value = [] }
}

onMounted(async () => {
  harnessStore.load()
  loadHarnessNames()
  loading.value = true
  await loadMain()
  loading.value = false
  if (tab.value === 'compare') { loadCompare(); loadMatrix(1) }
  timer = setInterval(() => { if (isActive.value) loadMain() }, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
.head-left { display: flex; gap: 10px; align-items: flex-start; }
.title-row { display: flex; align-items: center; gap: 10px; }
.sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.tabs-card { padding: 0 20px 20px; }
.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 8px 0 20px; }
.stat { border: 1px solid var(--border-color); border-radius: 8px; padding: 14px 16px; }
.stat-k { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 4px; }
.stat-v { font-size: 28px; font-weight: 700; margin-top: 4px; }
.stat-v.ok { color: #10b981; }
.stat-v.run { color: #3b82f6; }
.q { cursor: help; }
.progress-block { margin-top: 8px; }
.progress-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 6px; }
.muted { color: var(--text-muted); font-size: 12px; }
.err { color: #f56c6c; font-size: 12px; }
.err-detail { color: var(--text-muted); font-size: 11px; margin-top: 2px; }
.compare-grid { display: grid; grid-template-columns: 1.3fr 1fr; gap: 20px; align-items: start; }
.block-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.score { color: #10b981; font-weight: 600; }
.unscored { font-size: 11px; color: #e6a23c; margin-top: 2px; }
.cell-click { cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 4px; }
.cell-click:hover { opacity: .7; }
.lvl { font-size: 11px; color: var(--text-secondary); }
.params {
  background: var(--bg-primary); border: 1px solid var(--border-color);
  border-radius: 6px; padding: 12px; font-size: 12px;
  max-height: 360px; overflow: auto; margin: 0;
}
.row-ops {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: nowrap;
  white-space: nowrap;
}
.row-ops :deep(.el-button + .el-button) { margin-left: 0; }
.btn-badge {
  display: inline-block;
  margin-left: 6px;
  min-width: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: #f56c6c;
  color: #fff;
  font-size: 11px;
  line-height: 16px;
  text-align: center;
}
.menu-count { color: #f56c6c; }

/* ===== 详情 tab ===== */
.detail-stats {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 14px; margin: 4px 0 18px;
}
.dstat {
  display: flex; align-items: center; gap: 14px;
  border: 1px solid var(--border-color); border-radius: 10px;
  padding: 16px 18px; background: var(--bg-primary);
}
.dstat-icon {
  width: 42px; height: 42px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; flex: 0 0 auto;
}
.dstat-icon.purple { background: rgba(139, 92, 246, .12); color: #8b5cf6; }
.dstat-icon.green  { background: rgba(16, 185, 129, .12); color: #10b981; }
.dstat-icon.red    { background: rgba(245, 108, 108, .12); color: #f56c6c; }
.dstat-icon.blue   { background: rgba(59, 130, 246, .12); color: #3b82f6; }
.dstat-k { font-size: 12px; color: var(--text-muted); }
.dstat-v { font-size: 24px; font-weight: 700; line-height: 1.25; }

.dsec {
  border: 1px solid var(--border-color); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 16px; background: var(--bg-primary);
}
.dsec-head {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 14px; color: var(--text-secondary);
}
.dsec-head h4 { margin: 0; font-size: 14px; color: var(--text-primary); }
.dsec-head .muted { font-weight: 400; }
.dinfo :deep(.el-descriptions__label) { width: 110px; }

.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 12px;
}
.sm-item {
  display: flex; align-items: center; gap: 10px;
  padding: 12px 14px; border-radius: 8px;
  background: rgba(139, 92, 246, .06);
  border: 1px solid rgba(139, 92, 246, .14);
}
.sm-icon { color: #8b5cf6; font-size: 18px; flex: 0 0 auto; }
.sm-body { min-width: 0; flex: 1; }
.sm-k { font-size: 12px; color: var(--text-muted); }
.sm-v {
  font-size: 13px; font-weight: 600; margin-top: 2px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sm-count {
  flex: 0 0 auto; font-size: 12px; font-weight: 600;
  color: #8b5cf6; background: rgba(139, 92, 246, .14);
  border-radius: 10px; padding: 1px 9px;
}

.param-wrap { display: grid; grid-template-columns: 300px 1fr; gap: 16px; align-items: start; }
.param-nav { display: flex; flex-direction: column; gap: 8px; }
.pnav-item {
  display: flex; align-items: center; gap: 10px; cursor: pointer;
  padding: 11px 12px; border-radius: 8px;
  border: 1px solid var(--border-color);
  background: var(--bg-primary);
  transition: border-color .15s, background .15s;
}
.pnav-item:hover { border-color: #8b5cf6; }
.pnav-item.active {
  border-color: #8b5cf6;
  background: rgba(139, 92, 246, .08);
}
.pnav-icon {
  width: 32px; height: 32px; border-radius: 8px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center;
  background: rgba(139, 92, 246, .12); color: #8b5cf6;
}
.pnav-body { min-width: 0; flex: 1; }
.pnav-t { font-size: 13px; font-weight: 600; }
.pnav-s {
  font-size: 11px; color: var(--text-muted); margin-top: 1px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.pnav-arrow { color: var(--text-muted); font-size: 12px; flex: 0 0 auto; }

.param-body { min-width: 0; }
.param-body-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 8px;
}
.code-mark { font-size: 13px; font-weight: 600; color: var(--text-secondary); }
.param-table { margin-top: 2px; }
.cell-empty { color: var(--text-muted); }

@media (max-width: 900px) {
  .detail-stats { grid-template-columns: repeat(2, 1fr); }
  .param-wrap { grid-template-columns: 1fr; }
}
</style>
