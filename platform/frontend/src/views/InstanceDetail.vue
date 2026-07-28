<template>
  <div v-loading="loading">
    <div class="header-row">
      <el-page-header @back="$router.push('/dashboard')">
        <template #content>{{ inst.name || '实例详情' }}</template>
      </el-page-header>
      <div style="display:flex;gap:8px">
        <el-button type="success" v-if="authStore.isOperator && !['running','preparing'].includes(inst.status)" @click="startInstance">启动</el-button>
        <el-button type="danger" v-if="authStore.isOperator && inst.status === 'running'" @click="stopInstance">停止</el-button>
        <el-button type="warning" v-if="authStore.isOperator && inst.failed_tasks > 0 && !['running','preparing'].includes(inst.status)" @click="retryFailed">重跑失败</el-button>
      </div>
    </div>

    <div v-if="inst.status === 'running'" class="glass-card" style="padding:16px 20px;margin-bottom:16px">
      <el-progress :percentage="progressPct" :stroke-width="16" :status="progressStatus"
        :color="[{color:'#6366f1',percentage:50},{color:'#8b5cf6',percentage:80},{color:'#10b981',percentage:100}]" />
    </div>

    <el-tabs v-model="activeTab" class="detail-tabs">
      <!-- ================= 配置信息 ================= -->
      <el-tab-pane label="配置信息" name="config">
        <div class="stat-grid">
          <div class="stat-card"><span class="stat-num">{{ overview.total }}</span><span class="stat-label">总任务</span></div>
          <div class="stat-card"><span class="stat-num" style="color:#10b981">{{ overview.completed }}</span><span class="stat-label">已完成</span></div>
          <div class="stat-card"><span class="stat-num" :style="{color: overview.failed > 0 ? '#ef4444' : ''}">{{ overview.failed }}</span><span class="stat-label">失败</span></div>
          <div class="stat-card"><span class="stat-num" style="color:#f59e0b">{{ overview.running }}</span><span class="stat-label">运行中</span></div>
          <div class="stat-card"><span class="stat-num">{{ overview.pending }}</span><span class="stat-label">队列中</span></div>
          <div class="stat-card"><span class="stat-num" style="color:#6366f1">{{ overview.success_rate }}%</span><span class="stat-label">成功率</span></div>
        </div>

        <div class="time-info glass-card" style="padding:12px 20px;margin-bottom:20px"
          v-if="overview.elapsed_seconds != null || overview.avg_task_seconds || overview.estimated_remaining_seconds != null">
          <span v-if="overview.elapsed_seconds != null">已用时间: <strong>{{ formatDuration(overview.elapsed_seconds) }}</strong></span>
          <span v-if="overview.avg_task_seconds">平均耗时: <strong>{{ formatDuration(overview.avg_task_seconds) }}</strong></span>
          <span v-if="overview.estimated_remaining_seconds != null && inst.status === 'running'">预计剩余: <strong>{{ formatDuration(overview.estimated_remaining_seconds) }}</strong></span>
          <span v-if="overview.estimated_finish_time && inst.status === 'running'" style="color:var(--text-muted)">
            (预计完成: {{ overview.estimated_finish_time?.replace('T', ' ') }})
          </span>
        </div>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-card header="实例信息">
              <el-descriptions :column="1" border>
                <el-descriptions-item label="实例ID">{{ inst.id }}</el-descriptions-item>
                <el-descriptions-item label="状态"><el-tag :type="statusColor(inst.status)">{{ statusText(inst.status) }}</el-tag></el-descriptions-item>
                <el-descriptions-item label="PID">{{ inst.pid || '-' }}</el-descriptions-item>
                <el-descriptions-item label="配置文件">{{ inst.config_path }}</el-descriptions-item>
                <el-descriptions-item label="并发数">{{ inst.concurrent_num }}</el-descriptions-item>
                <el-descriptions-item label="创建时间">{{ inst.created_at }}</el-descriptions-item>
                <el-descriptions-item label="启动时间">{{ inst.started_at || '-' }}</el-descriptions-item>
                <el-descriptions-item label="创建者">{{ inst.created_by }}</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card header="任务执行情况">
              <div v-if="Object.keys(overview.error_breakdown || {}).length">
                <div v-for="(count, category) in overview.error_breakdown" :key="category"
                  :class="['breakdown-row', category.startsWith('└') ? 'breakdown-sub' : '']">
                  <span>{{ category }}</span>
                  <el-tag :type="taskTagType(category)" size="small">{{ count }}</el-tag>
                </div>
              </div>
              <el-empty v-else description="暂无数据" :image-size="60" />
            </el-card>
          </el-col>
        </el-row>
        <el-card header="创建参数" style="margin-top:20px" v-if="Object.keys(createParams).length">
          <el-collapse v-model="expandedPanels">
            <el-collapse-item title="基本配置" name="basic">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="任务标识">{{ createParams.task_name || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Harness类型">
                  <el-tag size="small" :type="createParams.harness_type === 'hermes' ? 'warning' : createParams.harness_type === 'claude-code' ? 'success' : 'primary'">
                    {{ createParams.harness_type || 'openclaw' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="Invite Code">{{ createParams.invite_code || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Harness配置ID">{{ createParams.harness_config_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item title="OBS配置" name="obs">
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="技能目录">{{ createParams.skill_dir || '-' }}</el-descriptions-item>
                <el-descriptions-item label="默认技能">{{ createParams.default_skills || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Agent目录">{{ createParams.agent_dir || '-' }}</el-descriptions-item>
                <el-descriptions-item label="用户Config目录">{{ createParams.user_config_dir || '-' }}</el-descriptions-item>
                <el-descriptions-item label="用户Profile目录">{{ createParams.user_profile_dir || '-' }}</el-descriptions-item>
                <el-descriptions-item label="轨迹保存路径">{{ createParams.traj_save_path || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item title="模型配置" name="model">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="Base URL">{{ createParams.model_base_url || '-' }}</el-descriptions-item>
                <el-descriptions-item label="API Key">{{ maskKey(createParams.model_api_key) }}</el-descriptions-item>
                <el-descriptions-item label="API类型">{{ createParams.model_api_type || '-' }}</el-descriptions-item>
                <el-descriptions-item label="模型ID">{{ createParams.model_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item title="用户模拟配置" name="agents">
              <template v-if="createParams.agents && createParams.agents.length">
                <div v-for="(ag, idx) in createParams.agents" :key="idx" :style="idx > 0 ? 'margin-top:12px' : ''">
                  <div style="font-size:13px;font-weight:600;margin-bottom:6px;color:var(--text-secondary)">Agent {{ idx + 1 }}: {{ ag.name || '-' }}</div>
                  <el-descriptions :column="2" border size="small">
                    <el-descriptions-item label="Provider">{{ ag.provider || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="Base URL">{{ ag.base_url || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="API Key">{{ maskKey(ag.api_key) }}</el-descriptions-item>
                    <el-descriptions-item label="API类型">{{ ag.api || '-' }}</el-descriptions-item>
                    <el-descriptions-item label="模型ID">{{ ag.model || '-' }}</el-descriptions-item>
                  </el-descriptions>
                </div>
              </template>
              <template v-else-if="createParams.user_proxy_model_name || createParams.user_proxy_base_url">
                <el-descriptions :column="2" border size="small">
                  <el-descriptions-item label="模型名称">{{ createParams.user_proxy_model_name || '-' }}</el-descriptions-item>
                  <el-descriptions-item label="Base URL">{{ createParams.user_proxy_base_url || '-' }}</el-descriptions-item>
                  <el-descriptions-item label="API Key">{{ maskKey(createParams.user_proxy_api_key) }}</el-descriptions-item>
                </el-descriptions>
              </template>
              <el-empty v-else description="未配置" :image-size="40" />
            </el-collapse-item>

            <el-collapse-item title="高级配置" name="advanced">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="起始索引">{{ createParams.start_index ?? 0 }}</el-descriptions-item>
                <el-descriptions-item label="任务总数">{{ createParams.total_num ? createParams.total_num : '不限' }}</el-descriptions-item>
                <el-descriptions-item label="镜像名称">{{ createParams.image_name || '-' }}</el-descriptions-item>
                <el-descriptions-item label="代码仓ID">{{ createParams.code_repo_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <el-card header="配置文件" style="margin-top:20px">
          <el-tabs v-model="activeConfigTab" @tab-change="loadConfigContent">
            <el-tab-pane v-for="cf in configFiles" :key="cf.name" :label="cf.name" :name="cf.name" />
          </el-tabs>
          <el-button size="small" style="margin-bottom:12px" @click="loadConfigFiles" :loading="configLoading">刷新</el-button>
          <pre class="config-preview" v-if="configContent">{{ configContent }}</pre>
          <el-empty v-else-if="!configLoading" description="选择配置文件查看" :image-size="40" />
        </el-card>
      </el-tab-pane>
      <!-- ================= 日志 ================= -->
      <el-tab-pane label="日志" name="logs">
        <div class="toolbar">
          <el-select-v2 v-model="logSource" placeholder="日志源" style="width:260px"
            :options="taskSelectOptions" filterable @change="onLogSourceChange">
            <template #default="{ item }">
              <span class="task-opt">
                <span class="task-dot" :style="{ background: dotColor(item.st) }"></span>
                <span class="task-opt-name">{{ item.label }}</span>
                <el-tag v-if="markerText(item.st)" size="small" :type="markerType(item.st)"
                  disable-transitions class="task-opt-tag">{{ markerText(item.st) }}</el-tag>
              </span>
            </template>
          </el-select-v2>

          <el-select v-model="categoryFilter" placeholder="错误类型筛选" clearable style="width:150px">
            <el-option label="成功" value="success" />
            <el-option label="服务侧失败 S" value="S" />
            <el-option label="任务侧失败 T" value="T" />
            <el-option label="客户端失败 C" value="C" />
            <el-option label="未分类 X" value="X" />
          </el-select>

          <el-input v-model="filterKeyword" placeholder="过滤关键词..." clearable style="width:200px" />

          <el-button @click="scrollToBottom">滚到底部</el-button>
          <el-button @click="loadLogs" :loading="logLoading">刷新</el-button>
          <el-button @click="clearLogs">清屏</el-button>
        </div>

        <div ref="logContainer" class="log-container" @scroll="onScroll">
          <div v-if="rawText !== null" class="log-more-hint">
            <span v-if="loadingMore">加载中…</span>
            <span v-else-if="logHasMore">上滑加载更早的日志</span>
            <span v-else>已到日志开头</span>
          </div>
          <pre v-if="rawText !== null" class="log-raw">{{ displayRawText }}</pre>
          <template v-else>
            <div v-for="(line, idx) in filteredLines" :key="idx"
              :class="['log-line', lineClass(line)]">{{ line }}</div>
            <div v-if="!filteredLines.length" style="color:#666;padding:20px;text-align:center">
              暂无日志
            </div>
          </template>
        </div>
      </el-tab-pane>
      <!-- ================= 输出 ================= -->
      <el-tab-pane label="输出" name="outputs">
        <!-- 统计面板 -->
        <el-card style="margin-bottom:16px" v-loading="evalLoading">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>评估统计</span>
              <div>
                <el-button size="small" @click="loadEvalStats(false)" :loading="evalLoading">增量刷新</el-button>
                <el-button size="small" type="warning" @click="loadEvalStats(true)" :loading="evalLoading">全量刷新</el-button>
              </div>
            </div>
          </template>
          <div v-if="evalAvailable">
            <!-- 顶部: 指标 -->
            <div class="eval-stat-grid">
              <div class="eval-stat-item">
                <span class="eval-stat-num">{{ evalTotalSamples }}</span>
                <span class="eval-stat-label">总样本量</span>
              </div>
              <div class="eval-stat-item">
                <span class="eval-stat-num" style="color:#6366f1">{{ evalUploadedTrajs }}</span>
                <span class="eval-stat-label">上传轨迹</span>
              </div>
              <div class="eval-stat-item">
                <span class="eval-stat-num" style="color:#10b981">{{ taskCompletedCountDisplay }}</span>
                <span class="eval-stat-label">任务完成数</span>
              </div>
              <div class="eval-stat-item">
                <span class="eval-stat-num" style="color:#10b981">{{ taskCompletedRateDisplay }}</span>
                <span class="eval-stat-label">任务完成率</span>
              </div>
              <div class="eval-stat-item">
                <span class="eval-stat-num" style="color:#10b981">{{ evalCount }}</span>
                <span class="eval-stat-label">已评估</span>
              </div>
              <div class="eval-stat-item" style="cursor:pointer" @click="scoreDetailVisible = true">
                <span class="eval-stat-num avg-score-link">{{ avgScoreDisplay }}</span>
                <span class="eval-stat-label">平均分</span>
              </div>
            </div>

            <!-- 底部: 两图并排 -->
            <div class="eval-charts-row">
              <!-- 分数分布 -->
              <div class="chart-box">
                <div class="chart-title">分数分布</div>
                <div v-if="evalCount > 0" class="score-chart-body">
                  <div v-for="bucket in distributionBuckets" :key="bucket.label" class="score-chart-col">
                    <span class="score-chart-count">{{ bucket.count || '' }}</span>
                    <div class="score-chart-bar-wrap">
                      <div class="score-chart-bar" :style="{height: bucket.pct + '%'}" />
                    </div>
                    <span class="score-chart-label">{{ bucket.label }}</span>
                  </div>
                </div>
                <el-empty v-else description="暂无分数" :image-size="30" />
              </div>

              <!-- 轨迹分级 -->
              <div class="chart-box" v-loading="trajLoading">
                <div class="chart-title-row">
                  <span class="chart-title">
                    轨迹分级<span v-if="trajGradedTrajs" style="color:var(--text-muted)"> · 已分级 {{ trajGradedTrajs }}</span>
                  </span>
                  <span>
                    <el-button size="small" @click="loadTrajStats(false)" :loading="trajLoading">增量刷新</el-button>
                    <el-button size="small" type="warning" @click="loadTrajStats(true)" :loading="trajLoading">全量刷新</el-button>
                  </span>
                </div>
                <div v-if="trajAvailable" class="score-chart-body">
                  <div v-for="bucket in trajBuckets" :key="bucket.label" class="score-chart-col">
                    <span class="score-chart-count">{{ bucket.count || '' }}</span>
                    <div class="score-chart-bar-wrap">
                      <div class="score-chart-bar traj-bar" :style="{height: bucket.pct + '%', background: bucket.color}" />
                    </div>
                    <span class="score-chart-label">{{ bucket.label }}</span>
                  </div>
                </div>
                <el-empty v-else description="暂无轨迹分级数据（点刷新抓取）" :image-size="30" />
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无评估数据" :image-size="40" />
        </el-card>

        <div class="toolbar">
          <el-button @click="loadTopDirs(true)" :loading="fileLoading">刷新</el-button>
          <el-checkbox v-model="showHidden" style="margin-left:12px" @change="onFilterChange">显示隐藏文件</el-checkbox>
          <el-breadcrumb separator="/" style="margin-left:12px">
            <el-breadcrumb-item>{{ obsBasePath || '...' }}</el-breadcrumb-item>
          </el-breadcrumb>
          <div style="flex:1" />
          <el-input v-model="searchKeyword" placeholder="搜索文件..." clearable size="small"
            style="width:220px;margin-right:8px" @input="onSearchInput" @clear="onSearchClear">
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-dropdown trigger="click" @command="handleShortcut">
            <el-button size="small" type="primary">快捷查看<el-icon style="margin-left:4px"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="run">任务日志</el-dropdown-item>
                <el-dropdown-item command="gateway">harness 日志</el-dropdown-item>
                <el-dropdown-item command="evaluator">评估日志</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <div class="split-layout">
          <!-- 左侧：文件树 -->
          <div class="file-list">
            <el-tree
              v-if="!isSearchMode"
              ref="treeRef"
              :data="treeData"
              :props="treeProps"
              node-key="path"
              highlight-current
              @node-click="handleNodeClick"
              @node-expand="handleNodeExpand"
              v-loading="fileLoading"
            >
              <template #default="{ node, data }">
                <span class="tree-node">
                  <el-icon v-if="data.is_dir" style="color:#e6a23c"><Folder /></el-icon>
                  <el-icon v-else style="color:#909399"><Document /></el-icon>
                  <span style="margin-left:4px">{{ data.label }}</span>
                  <span v-if="data.size && !data.is_dir" class="tree-size">{{ data.size }}</span>
                  <el-button v-if="!data.is_dir" size="small" text type="primary"
                    style="margin-left:8px" @click.stop="downloadFile(data)">下载</el-button>
                </span>
              </template>
            </el-tree>
            <!-- 搜索模式：静态树 -->
            <el-tree
              v-else
              :data="searchTreeData"
              :props="treeProps"
              node-key="path"
              default-expand-all
              highlight-current
              @node-click="handleNodeClick"
              v-loading="searchLoading"
            >
              <template #default="{ node, data }">
                <span class="tree-node">
                  <el-icon v-if="data.is_dir" style="color:#e6a23c"><Folder /></el-icon>
                  <el-icon v-else style="color:#909399"><Document /></el-icon>
                  <span style="margin-left:4px">{{ data.label }}</span>
                  <span v-if="data.size && !data.is_dir" class="tree-size">{{ data.size }}</span>
                  <el-button v-if="!data.is_dir" size="small" text type="primary"
                    style="margin-left:8px" @click.stop="downloadFile(data)">下载</el-button>
                </span>
              </template>
            </el-tree>
            <el-empty v-if="isSearchMode && !searchLoading && searchTreeData.length === 0"
              description="无匹配文件" :image-size="40" />
          </div>

          <!-- 右侧：文件预览 -->
          <div class="file-preview">
            <div v-if="previewLoading" style="padding:40px;text-align:center">
              <el-icon class="is-loading" :size="24"><Loading /></el-icon>
              <p style="color:#999;margin-top:8px">加载中...</p>
            </div>
            <div v-else-if="previewContent !== null">
              <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:#1e293b;border-radius:var(--radius-sm) var(--radius-sm) 0 0">
                <span style="color:#ccc;font-size:13px">{{ selectedFile?.label || selectedFile?.name }} ({{ previewTotalLines }} 行)</span>
                <el-button size="small" text style="color:#ccc" @click="downloadFile(selectedFile)">下载</el-button>
              </div>
              <pre class="preview-content">{{ previewContent }}</pre>
            </div>
            <el-empty v-else description="点击左侧文件预览内容" :image-size="80" />
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 分数明细弹窗 -->
    <el-dialog v-model="scoreDetailVisible" title="评估分数明细" width="900px" destroy-on-close>
      <div style="margin-bottom:12px;color:#666;font-size:13px">
        计算公式：score = Π(gate) × Σ(norm_weight × passed/total)
      </div>
      <el-table :data="scoreDetailRows" stripe border max-height="500" style="width:100%"
        :default-sort="{ prop: 'task', order: 'ascending' }">
        <el-table-column prop="task" label="任务" min-width="180" show-overflow-tooltip sortable />
        <el-table-column label="Gate" width="110" align="center" sortable :sort-by="row => row.gate">
          <template #default="{ row }">
            <div :style="{ color: row.gate === 0 ? '#f56c6c' : '#67c23a', fontWeight: 700 }">{{ row.gate }}</div>
            <div style="font-size:11px;color:#999;margin-top:2px">{{ row.gateExpr }}</div>
          </template>
        </el-table-column>
        <el-table-column label="Bucket 得分" min-width="160">
          <template #default="{ row }">
            <div>{{ row.bucketSum != null ? row.bucketSum.toFixed(4) : '-' }}</div>
            <div style="font-size:11px;color:#999;margin-top:2px">{{ row.bucketExpr }}</div>
          </template>
        </el-table-column>
        <el-table-column label="计算分" width="90" align="center" sortable :sort-by="row => row.score">
          <template #default="{ row }">
            <span :style="{ fontWeight: 700, color: row.score === 0 ? '#f56c6c' : row.score >= 1 ? '#67c23a' : '#e6a23c' }">
              {{ row.score != null ? row.score.toFixed(4) : '-' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="Completion" width="110" align="center" sortable :sort-by="row => row.completion ?? -1">
          <template #default="{ row }">
            <span style="color:#999">{{ row.completion != null ? row.completion.toFixed(4) : '-' }}</span>
          </template>
        </el-table-column>
      </el-table>
      <div style="margin-top:12px;font-size:13px;color:#666">
        合计: {{ scoreDetailRows.length }} 个任务 ·
        平均分(计算): {{ avgScoreDisplay }} ·
        平均分(completion): {{ completionAvgDisplay }}
      </div>
    </el-dialog>
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, Search, Folder, Document, Loading } from '@element-plus/icons-vue'
import api from '../api'
import { useAuthStore } from '../stores/auth'

const authStore = useAuthStore()
const route = useRoute()
const router = useRouter()
const id = route.params.id

// ============ 共享 / tab 管理 ============
const VALID_TABS = ['config', 'logs', 'outputs']
const activeTab = ref(VALID_TABS.includes(route.query.tab) ? route.query.tab : 'config')
const loadedTabs = new Set()   // 记录已懒加载过的 tab，避免重复拉取
const loading = ref(false)
const inst = ref({})

// tab 切换：同步 URL query + 懒加载 + 日志滚动监听绑定/解绑
watch(activeTab, (tab) => {
  if (route.query.tab !== tab) router.replace({ query: { ...route.query, tab } })
  ensureTabLoaded(tab)
  if (tab === 'logs') {
    nextTick(() => { if (logContainer.value) logContainer.value.addEventListener('scroll', onScroll) })
  } else if (logContainer.value) {
    logContainer.value.removeEventListener('scroll', onScroll)
  }
})

function ensureTabLoaded(tab) {
  if (loadedTabs.has(tab)) return
  loadedTabs.add(tab)
  if (tab === 'config') { loadData(); loadCreateParams(); loadConfigFiles() }
  else if (tab === 'logs') { loadTaskLogFiles(); loadTaskStatusMap(); loadLogs() }
  else if (tab === 'outputs') { loadTopDirs(); loadEvalStats(); loadTrajStats(false, true) }
}

// ============ 配置信息 tab ============
const overview = ref({ total: 0, completed: 0, failed: 0, running: 0, pending: 0, success_rate: 0, error_breakdown: {} })
const createParams = ref({})
const expandedPanels = ref([])
let timer = null

const progressPct = computed(() => {
  if (!overview.value.total) return 0
  return Math.round(((overview.value.completed + overview.value.failed) / overview.value.total) * 100)
})
const progressStatus = computed(() => {
  if (overview.value.failed > 0) return 'warning'
  if (progressPct.value === 100) return 'success'
  return ''
})

function statusColor(s) { return { running: 'success', preparing: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', created: '' }[s] || '' }
function statusText(s) { return { running: '运行中', preparing: '准备中', completed: '已完成', finished: '已结束', stopped: '已停止', created: '待启动' }[s] || s }
function taskTagType(category) {
  const map = {
    '任务成功': 'success',
    '任务失败': 'danger',
    '任务异常': 'warning',
    '└ C 客户端': 'warning',
    '└ S 服务端': 'warning',
    '└ T 任务侧': 'warning',
    '└ X 未分类': 'warning',
    '未执行': 'info',
  }
  return map[category] || ''
}
function formatDuration(seconds) {
  if (seconds == null) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h${m}m`
  return `${m}min`
}
function maskKey(key) {
  if (!key || key.length <= 8) return key || '-'
  return key.slice(0, 4) + '***' + key.slice(-4)
}

async function loadData() {
  try { const [i, o] = await Promise.all([api.get(`/instances/${id}`), api.get(`/instances/${id}/overview`)]); inst.value = i; overview.value = o } catch {}
}
async function loadCreateParams() { try { createParams.value = await api.get(`/instances/${id}/create-params`) } catch {} }
async function startInstance() { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadData() }
async function stopInstance() { await ElMessageBox.confirm('确认停止？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadData() }
async function retryFailed() { await api.post(`/instances/${id}/retry-failed`); ElMessage.success('重跑已启动'); loadData() }

const configFiles = ref([]); const configLoading = ref(false); const activeConfigTab = ref(''); const configContent = ref('')
async function loadConfigFiles() {
  configLoading.value = true
  try { const res = await api.get(`/instances/${id}/configs`); configFiles.value = res.files || []; if (configFiles.value.length && !activeConfigTab.value) { activeConfigTab.value = configFiles.value[0].name; loadConfigContent(activeConfigTab.value) } } finally { configLoading.value = false }
}
async function loadConfigContent(filename) { if (!filename) return; try { const res = await api.get(`/instances/${id}/configs/${filename}`); configContent.value = res.content } catch { configContent.value = '' } }

// ============ 日志 tab ============
const filterKeyword = ref('')
const logLines = ref([])
const logContainer = ref(null)
const logLoading = ref(false)
const logSource = ref('main.log')
const taskLogFiles = ref([])
const rawText = ref(null)
const taskStatusMap = ref({})   // { task_idx: {status, category, code} }
const categoryFilter = ref('')
const logStartOffset = ref(null)  // 当前已加载窗口首字节偏移（load-more 游标）
const logHasMore = ref(false)     // 上方是否还有更早日志
const loadingMore = ref(false)

// task-N.log -> 该 task 的状态对象（从 task_idx 映射）
function taskStatusOf(fname) {
  const m = /task-(\d+)\.log/.exec(fname)
  if (!m) return null
  return taskStatusMap.value[Number(m[1])] || null
}

// 彩色圆点：成功=绿 / S=红 / T=琥珀 / C=紫 / X=灰 / 未知=透明
function dotColor(st) {
  if (!st) return 'transparent'
  if (st.status === '任务成功') return '#10b981'
  switch (st.category) {
    case 'S': return '#ef4444'
    case 'T': return '#f59e0b'
    case 'C': return '#9254de'
    case 'X': return '#909399'
    default: return '#909399'
  }
}
// el-tag 语义类型（紫色 C 无对应语义色，退回 info 灰底，仅圆点区分）
function markerType(st) {
  if (!st) return 'info'
  if (st.status === '任务成功') return 'success'
  return { S: 'danger', T: 'warning', C: 'info', X: 'info' }[st.category] || 'info'
}
function markerText(st) {
  if (!st) return ''
  if (st.status === '任务成功') return '✓'
  return st.category || (st.status === '任务异常' ? 'X' : '')
}

// 按错误类型筛选下拉里的 task 列表
const visibleTaskLogFiles = computed(() => {
  if (!categoryFilter.value) return taskLogFiles.value
  return taskLogFiles.value.filter(f => {
    const st = taskStatusOf(f)
    if (!st) return false
    if (categoryFilter.value === 'success') return st.status === '任务成功'
    return st.category === categoryFilter.value
  })
})

// el-select-v2 的扁平 options：两个固定源 + 全部 task
const taskSelectOptions = computed(() => {
  const base = [
    { label: '主进程日志', value: 'main.log', st: null },
    { label: '完整日志(nohup)', value: 'nohup.log', st: null },
  ]
  for (const f of visibleTaskLogFiles.value) {
    base.push({ label: f.replace('.log', ''), value: f, st: taskStatusOf(f) })
  }
  return base
})

const filteredLines = computed(() => {
  let lines = logLines.value
  if (filterKeyword.value) {
    const kw = filterKeyword.value.toLowerCase()
    lines = lines.filter(l => l.toLowerCase().includes(kw))
  }
  return lines
})

// raw 模式下按关键字过滤保留原换行
const displayRawText = computed(() => {
  if (rawText.value === null) return ''
  if (!filterKeyword.value) return rawText.value
  const kw = filterKeyword.value.toLowerCase()
  return rawText.value
    .split('\n')
    .filter(l => l.toLowerCase().includes(kw))
    .join('\n')
})

function onLogSourceChange() {
  loadLogs()
}

function lineClass(line) {
  if (/^\[STDERR\]/i.test(line)) return 'log-error'
  if (/error|failed|exception|traceback/i.test(line)) return 'log-error'
  if (/warning|warn/i.test(line)) return 'log-warn'
  if (/success|completed|done/i.test(line)) return 'log-success'
  return ''
}

function scrollToBottom() {
  nextTick(() => {
    if (logContainer.value) logContainer.value.scrollTop = logContainer.value.scrollHeight
  })
}

function clearLogs() { logLines.value = []; rawText.value = '' }

async function loadLogs() {
  logLoading.value = true
  try {
    // 首屏只取最新 50 行（raw 模式，1:1 展示），后续上滑用 end 游标向上翻页
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: 50, raw: true }
    })
    rawText.value = res.text ?? ''
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    logLines.value = []
    scrollToBottom()
  } catch { /* handled by interceptor */ }
  finally { logLoading.value = false }
}

// 上滑到顶时向上加载更早的 50 行，前置到 rawText，并保持视觉滚动位置不跳
async function loadMore() {
  if (!logHasMore.value || loadingMore.value || logStartOffset.value == null) return
  loadingMore.value = true
  const el = logContainer.value
  const prevHeight = el ? el.scrollHeight : 0
  const prevTop = el ? el.scrollTop : 0
  try {
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: 50, raw: true, end: logStartOffset.value }
    })
    const chunk = res.text ?? ''
    if (chunk) rawText.value = chunk + rawText.value
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    await nextTick()
    if (el) el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
  } catch { /* ignore */ }
  finally { loadingMore.value = false }
}

function onScroll() {
  const el = logContainer.value
  if (el && el.scrollTop < 40 && logHasMore.value && !loadingMore.value) loadMore()
}

async function loadTaskLogFiles() {
  try {
    const res = await api.get(`/logs/${id}/task-log-list`)
    taskLogFiles.value = res.files || []
  } catch { /* ignore */ }
}

async function loadTaskStatusMap() {
  try {
    const res = await api.get(`/instances/${id}/task-status-map`)
    taskStatusMap.value = res.map || {}
  } catch { /* ignore */ }
}

// ============ 输出 tab: 评估统计 ============
const HIDDEN_RE = /^\./
function isHidden(name) { return HIDDEN_RE.test(name) || name === '__pycache__' }

const evalLoading = ref(false)
const evalAvailable = ref(false)
const evalTotalSamples = ref(0)
const evalUploadedTrajs = ref(0)
const taskScores = ref({})
const taskCompleted = ref({})
const taskEvalDetails = ref({})
const scoreDetailVisible = ref(false)

const evalCount = computed(() => Object.keys(taskScores.value).length)
const taskCompletedCount = computed(() => Object.values(taskCompleted.value).filter(Boolean).length)
const taskCompletedCountDisplay = computed(() => {
  const total = Object.keys(taskCompleted.value).length
  if (!total) return '-'
  return taskCompletedCount.value
})
const taskCompletedRateDisplay = computed(() => {
  const total = Object.keys(taskCompleted.value).length
  if (!total) return '-'
  const pct = ((taskCompletedCount.value / total) * 100).toFixed(1)
  return `${pct}%`
})
const avgScoreDisplay = computed(() => {
  const scores = Object.values(taskScores.value)
  if (!scores.length) return '-'
  const avg = scores.reduce((a, b) => a + b, 0) / scores.length
  return (avg * 100).toFixed(1) + '%'
})

const scoreDetailRows = computed(() => {
  const details = taskEvalDetails.value
  if (!details || !Object.keys(details).length) return []
  return Object.entries(details)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([task, d]) => ({
      task,
      gate: d.gate,
      gateExpr: d.gate_expr || '-',
      gateStatus: d.gate_status || {},
      bucketExpr: d.bucket_expr || '-',
      bucketSum: d.bucket_sum,
      score: d.score,
      completion: d.completion,
    }))
})

const completionAvgDisplay = computed(() => {
  const rows = scoreDetailRows.value.filter(r => r.completion != null)
  if (!rows.length) return '-'
  const avg = rows.reduce((a, r) => a + r.completion, 0) / rows.length
  return (avg * 100).toFixed(1) + '%'
})

const scoreDistribution = computed(() => {
  const dist = {}
  for (const s of Object.values(taskScores.value)) {
    const idx = Math.min(Math.floor(s * 10), 10)
    const label = idx * 10 + '%'
    dist[label] = (dist[label] || 0) + 1
  }
  return dist
})

const distributionBuckets = computed(() => {
  const dist = scoreDistribution.value
  const total = Object.values(taskScores.value).length || 1
  const maxCount = Math.max(1, ...Object.values(dist))
  const buckets = []
  for (let i = 0; i <= 10; i++) {
    const label = i * 10 + '%'
    const count = dist[label] || 0
    buckets.push({
      label,
      count,
      pct: (count / maxCount) * 100,
      ratio: total > 0 ? ((count / total) * 100).toFixed(0) : '0',
    })
  }
  return buckets
})

async function loadEvalStats(refresh = false) {
  evalLoading.value = true
  try {
    const res = await api.get(`/logs/${id}/eval-stats`, { params: { refresh } })
    evalAvailable.value = res.available
    evalTotalSamples.value = res.total_samples || 0
    evalUploadedTrajs.value = res.uploaded_trajs || 0
    taskScores.value = res.task_scores || {}
    taskCompleted.value = res.task_completed || {}
    taskEvalDetails.value = res.task_eval_details || {}
  } catch {
    evalAvailable.value = false
  } finally {
    evalLoading.value = false
  }
}

// ============ 输出 tab: 轨迹分级 (L0–L3) ============
const trajLoading = ref(false)
const trajAvailable = ref(false)
const trajGradedTrajs = ref(0)
const trajLevelDist = ref({})

const TRAJ_LEVEL_ORDER = ['none', 'L0', 'L1', 'L1.5', 'L2', 'L3']
const TRAJ_LEVEL_COLOR = {
  none: '#909399',
  L0: '#94a3b8',
  L1: '#38bdf8',
  'L1.5': '#818cf8',
  L2: '#a78bfa',
  L3: '#10b981',
}

const trajBuckets = computed(() => {
  const dist = trajLevelDist.value
  const maxCount = Math.max(1, ...TRAJ_LEVEL_ORDER.map(l => dist[l] || 0))
  return TRAJ_LEVEL_ORDER.map(label => {
    const count = dist[label] || 0
    return {
      label,
      count,
      pct: (count / maxCount) * 100,
      color: TRAJ_LEVEL_COLOR[label] || '#909399',
    }
  })
})

async function loadTrajStats(refresh = false, cacheOnly = false) {
  trajLoading.value = true
  try {
    const res = await api.get(`/logs/${id}/traj-stats`, { params: { refresh, cache_only: cacheOnly } })
    trajAvailable.value = res.available
    trajGradedTrajs.value = res.graded_trajs || 0
    trajLevelDist.value = res.level_dist || {}
  } catch {
    trajAvailable.value = false
  } finally {
    trajLoading.value = false
  }
}

// ============ 输出 tab: 文件树（两级加载） ============
const fileLoading = ref(false)
const treeData = ref([])
const treeRef = ref(null)
const selectedFile = ref(null)
const previewContent = ref(null)
const previewTotalLines = ref(0)
const previewLoading = ref(false)
const obsBasePath = ref('')
const showHidden = ref(false)
const searchKeyword = ref('')
const searchLoading = ref(false)
const searchTreeData = ref([])
const topDirs = ref([])
const subtreeCache = {}

const isSearchMode = computed(() => searchKeyword.value.trim().length > 0)

const treeProps = {
  label: 'label',
  children: 'children',
  isLeaf: (data) => !data.is_dir,
}

/** 加载一级目录列表 */
async function loadTopDirs(refresh = false) {
  fileLoading.value = true
  try {
    const res = await api.get(`/logs/${id}/obs-tree`, { params: { refresh } })
    obsBasePath.value = res.obs_path
    topDirs.value = res.dirs || []
    const dirs = topDirs.value
      .filter(d => showHidden.value || !isHidden(d))
      .sort((a, b) => a.localeCompare(b))
      .map(d => ({
        label: d,
        path: obsBasePath.value + d + '/',
        is_dir: true,
        subdir: d,
        children: [{ label: '加载中...', path: '_placeholder_' + d, is_dir: false, _placeholder: true }],
      }))
    treeData.value = dirs
  } finally { fileLoading.value = false }
}

/** 加载子目录的文件列表，返回扁平 items */
async function loadSubtreeItems(subdir) {
  if (subtreeCache[subdir]) return subtreeCache[subdir]
  const res = await api.get(`/logs/${id}/obs-subtree`, { params: { subdir } })
  const items = res.items || []
  subtreeCache[subdir] = items
  return items
}

/** 展开一级目录时加载子树 */
async function handleNodeExpand(data) {
  if (!data.subdir) return
  if (data.children && data.children.length > 0 && !data.children[0]._placeholder) return
  try {
    const items = await loadSubtreeItems(data.subdir)
    const parentPath = obsBasePath.value + data.subdir + '/'
    const children = buildPathTree(items, parentPath)
    data.children = children.length > 0 ? children : []
  } catch {
    data.children = []
  }
}

/**
 * 从扁平路径列表构建嵌套树结构
 */
function buildPathTree(items, parentPath) {
  const root = { children: new Map() }

  for (const item of items) {
    const relativePath = item.name
    if (!relativePath) continue

    const segments = relativePath.split('/')
    if (!showHidden.value && segments.some(seg => isHidden(seg))) continue

    let current = root
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i]
      const isLast = i === segments.length - 1

      if (!current.children.has(seg)) {
        current.children.set(seg, {
          label: seg,
          path: isLast ? item.path : parentPath + segments.slice(0, i + 1).join('/') + '/',
          is_dir: isLast ? item.is_dir : true,
          size: isLast ? item.size : null,
          children: new Map(),
        })
      }
      current = current.children.get(seg)
    }
  }

  function toArray(node) {
    if (!node.children || node.children.size === 0) {
      return { label: node.label, path: node.path, is_dir: node.is_dir, size: node.size }
    }
    const dirs = []
    const files = []
    for (const child of node.children.values()) {
      const converted = toArray(child)
      if (converted.is_dir) dirs.push(converted)
      else files.push(converted)
    }
    dirs.sort((a, b) => a.label.localeCompare(b.label))
    files.sort((a, b) => a.label.localeCompare(b.label))
    return {
      label: node.label, path: node.path, is_dir: node.is_dir, size: node.size,
      children: [...dirs, ...files],
    }
  }

  const dirs = []
  const files = []
  for (const child of root.children.values()) {
    const converted = toArray(child)
    if (converted.is_dir) dirs.push(converted)
    else files.push(converted)
  }
  dirs.sort((a, b) => a.label.localeCompare(b.label))
  files.sort((a, b) => a.label.localeCompare(b.label))
  return [...dirs, ...files]
}

// ============ 输出 tab: 搜索/过滤 ============
let searchTimer = null

function onSearchInput() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => { doSearch() }, 300)
}

function onSearchClear() {
  searchTreeData.value = []
}

function onFilterChange() {
  if (isSearchMode.value) doSearch()
  else loadTopDirs(false)
}

async function doSearch() {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) {
    searchTreeData.value = []
    return
  }

  searchLoading.value = true
  try {
    const dirsToLoad = topDirs.value.filter(d => !subtreeCache[d])
    if (dirsToLoad.length > 0) {
      await Promise.all(dirsToLoad.map(d => loadSubtreeItems(d)))
    }

    const resultNodes = []
    for (const subdir of topDirs.value) {
      if (!showHidden.value && isHidden(subdir)) continue
      const items = subtreeCache[subdir] || []
      const filtered = items.filter(item => item.name.toLowerCase().includes(kw))
      if (filtered.length === 0) continue

      const parentPath = obsBasePath.value + subdir + '/'
      const children = buildPathTree(filtered, parentPath)
      resultNodes.push({
        label: subdir,
        path: parentPath,
        is_dir: true,
        children,
      })
    }
    searchTreeData.value = resultNodes
  } finally {
    searchLoading.value = false
  }
}

// ============ 输出 tab: 文件操作 ============
function handleNodeClick(data) {
  if (!data.is_dir) previewFile(data)
}

async function previewFile(file) {
  selectedFile.value = file
  previewLoading.value = true
  previewContent.value = null
  try {
    const res = await api.get(`/logs/${id}/obs-view`, { params: { file_path: file.path, tail: 1000 } })
    previewContent.value = (res.lines || []).join('\n')
    previewTotalLines.value = res.total_lines || 0
  } catch {
    previewContent.value = '文件加载失败'
  } finally { previewLoading.value = false }
}

function downloadFile(file) {
  if (!file) return
  const token = localStorage.getItem('token')
  window.open(`/api/logs/${id}/obs-download?file_path=${encodeURIComponent(file.path)}&token=${token}`, '_blank')
}

function handleShortcut(cmd) {
  const keywords = {
    gateway: 'gateway.log',
    run: 'run.log',
    evaluator: 'evaluator_use.log',
  }
  searchKeyword.value = keywords[cmd] || ''
  doSearch()
}

// ============ 生命周期 ============
onMounted(async () => {
  loading.value = true
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loading.value = false
  // config tab 常驻 10s 轮询（无论当前在哪个 tab，保持顶部进度条/状态最新）
  timer = setInterval(loadData, 10000)
  ensureTabLoaded(activeTab.value)
  if (activeTab.value === 'logs') {
    nextTick(() => { if (logContainer.value) logContainer.value.addEventListener('scroll', onScroll) })
  }
})
onUnmounted(() => {
  clearInterval(timer)
  if (logContainer.value) logContainer.value.removeEventListener('scroll', onScroll)
})
</script>

<style scoped>
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.detail-tabs { margin-top: 4px; }

/* ---- 配置信息 tab ---- */
.stat-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px; }
.stat-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 16px 20px; display: flex; flex-direction: column; align-items: center;
  box-shadow: var(--shadow-sm);
}
.stat-num { font-size: 32px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.2; }
.stat-label { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
.time-info { display: flex; gap: 24px; font-size: 13px; color: var(--text-secondary); flex-wrap: wrap; }
.time-info strong { color: var(--text-primary); }
.breakdown-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); }
.breakdown-row:last-child { border-bottom: none; }
.breakdown-sub {
  padding: 4px 0 4px 16px;
  border-bottom: none;
  font-size: 12px;
  color: var(--text-muted);
}
.config-preview {
  background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: var(--radius-sm);
  max-height: 500px; overflow: auto; font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}

/* ---- 日志 tab ---- */
.toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.log-container {
  background: #1e293b; color: #e2e8f0; padding: 12px; border-radius: var(--radius-md);
  height: calc(100vh - 340px); overflow-y: auto; overflow-x: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
}
.log-line { padding: 1px 0; white-space: pre-wrap; word-break: break-all; }
.log-raw {
  margin: 0; padding: 0;
  color: inherit; background: transparent;
  font: inherit;
  white-space: pre;
  tab-size: 4;
}
.log-error { color: #f87171; }
.log-warn { color: #fbbf24; }
.log-success { color: #34d399; }
.log-more-hint {
  text-align: center; color: #94a3b8; font-size: 12px;
  padding: 4px 0 8px; user-select: none;
}
.task-opt { display: flex; align-items: center; gap: 8px; }
.task-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.task-opt-name { flex: 1; }
.task-opt-tag { margin-left: auto; }

/* ---- 输出 tab: 文件树/预览 ---- */
.split-layout { display: flex; gap: 16px; height: calc(100vh - 300px); }
.file-list {
  width: 360px; flex-shrink: 0; overflow: auto;
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 8px; background: #fff;
}
.file-preview { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.tree-node { display: flex; align-items: center; font-size: 13px; }
.tree-size { margin-left: 6px; font-size: 11px; color: #999; }
.preview-content {
  background: #1e293b; color: #e2e8f0; padding: 12px; margin: 0;
  border-radius: 0 0 var(--radius-sm) var(--radius-sm); flex: 1; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px;
  white-space: pre-wrap; word-break: break-all;
}

/* ---- 输出 tab: 评估统计 ---- */
.eval-stat-grid {
  display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
  align-content: center;
  padding-bottom: 16px; margin-bottom: 16px;
  border-bottom: 1px solid var(--border-color, #e5e7eb);
}
.eval-charts-row { display: flex; gap: 32px; align-items: stretch; }
.chart-box { flex: 1; min-width: 0; }
.chart-title { font-size: 13px; color: var(--text-muted); }
.chart-title-row {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 8px; min-height: 24px;
}
.chart-box > .chart-title { display: block; margin-bottom: 8px; }
.traj-bar { max-width: 40px; }
.eval-stat-item { display: flex; flex-direction: column; align-items: center; padding: 8px 0; }
.eval-stat-num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.2; }
.avg-score-link {
  color: #f59e0b !important;
  text-decoration: underline;
  text-decoration-style: dashed;
  text-underline-offset: 4px;
}
.avg-score-link:hover { text-decoration-style: solid; }
.eval-stat-label { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

/* 柱状图 */
.score-chart-body {
  display: flex; align-items: flex-end; gap: 2px;
  height: 120px; padding-bottom: 20px; position: relative;
  border-bottom: 1px solid #e5e7eb;
}
.score-chart-col {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  height: 100%; position: relative;
}
.score-chart-count {
  font-size: 10px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; top: 0; white-space: nowrap;
}
.score-chart-bar-wrap {
  flex: 1; width: 100%; display: flex; align-items: flex-end;
  padding-top: 16px;
}
.score-chart-bar {
  width: 100%; max-width: 32px; margin: 0 auto;
  background: linear-gradient(180deg, #6366f1, #8b5cf6);
  border-radius: 3px 3px 0 0; transition: height 0.3s ease;
  min-height: 2px;
}
.score-chart-label {
  font-size: 10px; color: var(--text-muted); font-variant-numeric: tabular-nums;
  position: absolute; bottom: -18px; white-space: nowrap;
}
</style>






