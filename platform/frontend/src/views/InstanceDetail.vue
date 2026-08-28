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

    <div v-if="inst.status === 'preparing'" class="glass-card" style="padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;gap:10px">
      <el-icon class="is-loading" :size="18" style="color:#e6a23c"><Loading /></el-icon>
      <span style="color:#e6a23c">准备中：正在下载配置</span>
      <span class="mono-num" style="color:#e6a23c">已下载 {{ prepareInfo.downloaded_configs ?? 0 }} 个 config</span>
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
                <!-- 顶层汇总：成功/失败/任务异常总数/未执行（不含 └ 前缀子行） -->
                <div v-for="(count, category) in summaryBreakdown" :key="category" class="breakdown-row">
                  <span>{{ category }}</span>
                  <el-tag :type="taskTagType(category)" size="small">{{ count }}</el-tag>
                </div>
                <!-- 异常明细：一级分类默认折叠，展开看具体错误码 -->
                <el-collapse v-if="overview.error_tree && overview.error_tree.length"
                  v-model="expandedErrorGroups" class="error-tree">
                  <el-collapse-item v-for="grp in overview.error_tree" :key="grp.prefix" :name="grp.prefix">
                    <template #title>
                      <span class="err-grp-title">
                        <el-tag size="small" type="warning" effect="plain">{{ grp.prefix }}</el-tag>
                        <span class="err-grp-name">{{ grp.label }}</span>
                        <span class="err-grp-count">{{ grp.count }}</span>
                      </span>
                    </template>
                    <div v-for="c in grp.codes" :key="c.code" class="breakdown-row breakdown-sub">
                      <span>
                        <code class="err-code">{{ c.code }}</code>
                        <span class="err-desc" v-if="c.desc">{{ c.desc }}</span>
                      </span>
                      <el-tag type="info" size="small">{{ c.count }}</el-tag>
                    </div>
                  </el-collapse-item>
                </el-collapse>
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
                  <el-tag size="small" :color="harnessColor(createParams.harness_type)" :style="{borderColor: harnessColor(createParams.harness_type)}" effect="dark">
                    {{ harnessLabel(createParams.harness_type) }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="Invite Code">{{ createParams.invite_code || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Harness配置ID">{{ createParams.harness_config_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item title="OBS配置" name="obs">
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="技能目录">{{ obsPath(createParams.skill_dir) }}</el-descriptions-item>
                <el-descriptions-item label="默认技能">{{ createParams.default_skills || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Agent目录">{{ obsPath(createParams.agent_dir) }}</el-descriptions-item>
                <el-descriptions-item label="用户Config目录">{{ obsPath(createParams.user_config_dir) }}</el-descriptions-item>
                <el-descriptions-item label="用户Profile目录">{{ obsPath(createParams.user_profile_dir) }}</el-descriptions-item>
                <el-descriptions-item label="轨迹保存路径">{{ obsPath(createParams.traj_save_path) }}</el-descriptions-item>
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
        <!-- 统计面板（浅层漏斗） -->
        <el-card style="margin-bottom:16px" v-loading="shallowLoading">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>会话漏斗</span>
              <div>
                <el-button size="small" @click="loadShallow(false)">刷新</el-button>
              </div>
            </div>
          </template>
          <!-- 浅层分析进度条：finished 实例还有未分级行时显示；全部分完隐藏 -->
          <div v-if="shallowProgress && shallowProgress.undone > 0" style="margin-bottom:12px">
            <el-progress :percentage="shallowProgressPct" :stroke-width="10"
              :color="shallowProgressColor" :format="shallowProgressText" />
            <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:12px;color:var(--text-muted)">
              <span>浅层分析{{ shallowProgressStateLabel }}</span>
              <span>已定级 {{ shallowProgress.approved }} / {{ shallowProgress.total }} 个会话</span>
            </div>
          </div>
          <div v-if="shallowSummary.total > 0">
            <!-- 漏斗柱状图: L0→L3 -->
            <div class="funnel-bar-row">
              <div v-for="b in funnelBuckets" :key="b.label" class="funnel-bar-col">
                <span class="funnel-bar-count">{{ b.count || '' }}</span>
                <div class="funnel-bar-track">
                  <div class="funnel-bar" :style="{ height: b.pct + '%', background: b.color }" />
                </div>
                <span class="funnel-bar-label">{{ b.label }}</span>
              </div>
              <!-- 未评估(排队中) -->
              <div class="funnel-bar-col">
                <span class="funnel-bar-count">{{ shallowSummary.unevaluated }}</span>
                <div class="funnel-bar-track">
                  <div class="funnel-bar uneval-bar"
                    :style="{ height: unevaluatedPct + '%' }" />
                </div>
                <span class="funnel-bar-label">未评估</span>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无会话数据（运行中会自动采集）" :image-size="40" />
        </el-card>

        <!-- 状态分析：浅层→深层 流水线阶段（含手动提交浅层） -->
        <el-card style="margin-bottom:16px">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>状态分析</span>
              <el-tag :type="stageMeta.type" size="small">{{ stageMeta.label }}</el-tag>
            </div>
          </template>
          <div style="display:flex;flex-wrap:wrap;gap:16px;align-items:center">
            <!-- 浅层 → 深层 状态步骤 -->
            <div style="display:flex;align-items:center;gap:0;flex-wrap:wrap">
              <!-- 浅层步骤：非空即已在分析 -->
              <div :style="{ padding: '6px 14px', borderRadius: '6px', background: shallowActive ? 'var(--el-color-primary-light-9)' : 'transparent', border: '1px solid ' + (shallowActive ? 'var(--el-color-primary-light-5)' : 'var(--el-border-color-lighter)') }">
                <div :style="{ fontSize: '12px', color: 'var(--text-muted)' }">浅层分析</div>
                <div :style="{ fontSize: '14px', fontWeight: '600', color: shallowActive ? 'var(--el-color-primary)' : 'var(--text-muted)' }">{{ shallowStepLabel }}</div>
              </div>
              <div style="color:var(--text-muted);margin:0 8px">→</div>
              <!-- 深层步骤：浅层已完成才可进入 -->
              <div :style="{ padding: '6px 14px', borderRadius: '6px', background: deepActive ? 'var(--el-color-primary-light-9)' : 'transparent', border: '1px solid ' + (deepActive ? 'var(--el-color-primary-light-5)' : 'var(--el-border-color-lighter)') }">
                <div :style="{ fontSize: '12px', color: 'var(--text-muted)' }">深层分析</div>
                <div :style="{ fontSize: '14px', fontWeight: '600', color: deepActive ? 'var(--el-color-primary)' : 'var(--text-muted)' }">{{ deepStepLabel }}</div>
              </div>
            </div>
            <div style="flex:1;min-width:200px;font-size:12px;color:var(--text-muted)">
              {{ stageMeta.desc }}
              <div v-if="shallowProgress && shallowProgress.total > 0" style="margin-top:4px">
                已定级 <b>{{ shallowProgress.approved }}</b> / {{ shallowProgress.total }} 个会话
                <span v-if="shallowProgress.undone > 0">，剩余 <b>{{ shallowProgress.undone }}</b> 未分析</span>
              </div>
            </div>
            <!-- 手动提交浅层：ended 实例且仍有缺口时显示 -->
            <el-button v-if="authStore.isOperator && pipelineStage === 'ungraded'" type="primary" size="small"
              :loading="submittingShallow" @click="submitShallow">提交浅层分析</el-button>
            <el-button v-else-if="pipelineStage === 'queued' || pipelineStage === 'grading'" size="small" disabled>浅层分析处理中…</el-button>
          </div>
        </el-card>

        <!-- 下载队列（深层: 批量入队 + 状态显示） -->
        <el-card v-if="deepQueueVisible" style="margin-bottom:16px" v-loading="deepQueueLoading">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>下载队列 <span style="color:var(--text-muted);font-weight:400;font-size:12px">深层轨迹/日志按需下载</span></span>
              <div style="display:flex;gap:8px;align-items:center">
                <el-button v-if="authStore.isOperator" size="small" type="primary"
                  :loading="enqueuing" @click="enqueueAllDeep">批量下载</el-button>
                <el-button size="small" @click="loadDeepQueue(false)">刷新</el-button>
              </div>
            </div>
          </template>
          <!-- 统计条 -->
          <div class="deep-queue-stats">
            <span class="deep-queue-stat" :class="{ 'is-active': deepQueueSummary.pending > 0 }">
              待下载 <b>{{ deepQueueSummary.pending }}</b>
            </span>
            <span class="deep-queue-stat" :class="{ 'is-active': deepQueueSummary.downloading > 0 }">
              下载中 <b>{{ deepQueueSummary.downloading }}</b>
            </span>
            <span class="deep-queue-stat done">已完成 <b>{{ deepQueueSummary.done }}</b></span>
            <span class="deep-queue-stat fail" v-if="deepQueueSummary.failed > 0">
              失败 <b>{{ deepQueueSummary.failed }}</b>
            </span>
            <span class="deep-queue-stat muted">共 <b>{{ deepQueueSummary.total }}</b></span>
          </div>
          <!-- 列表 -->
          <el-table :data="deepQueueRows" size="small" style="width:100%"
            :max-height="260" empty-text="暂无下载任务">
            <el-table-column prop="traj_name" label="会话" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span style="cursor:pointer;color:var(--text-primary)"
                  @click="openTaskDetail(row)">{{ row.traj_name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="等级" min-width="80" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="deepLevelTagType(row.level)">{{ row.level || '—' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" min-width="110" align="center">
              <template #default="{ row }">
                <span v-if="row.status === 'downloading'" class="deep-status downloading">
                  <el-icon class="is-loading" :size="13"><Loading /></el-icon> 下载中
                </span>
                <el-tag v-else size="small" :type="deepStatusTagType(row.status)">{{ deepStatusLabel(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="更新时间" min-width="150" align="right">
              <template #default="{ row }">
                <span style="color:var(--text-muted);font-size:12px">{{ fmtDeepTime(row.updated_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="错误" min-width="160" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.error" style="color:#f56c6c;font-size:12px">{{ row.error }}</span>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="table-footer" style="margin-top:8px">
            <el-pagination layout="total, prev, pager, next" :total="deepQueueTotal"
              :page-size="deepQueuePageSize" :current-page="deepQueuePage"
              @current-change="onDeepQueuePage" small />
          </div>
        </el-card>

        <!-- 会话表格 -->
        <el-card>
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>会话列表 <span style="color:var(--text-muted);font-weight:400;font-size:12px">共 {{ shallowTotal }} 个</span></span>
              <div style="display:flex;gap:8px">
                <el-select v-model="levelFilter" size="small" placeholder="等级" clearable style="width:120px"
                  @change="onTaskFilterChange">
                  <el-option label="L0" value="L0" /><el-option label="L1" value="L1" />
                  <el-option label="L1.5" value="L1.5" /><el-option label="L2" value="L2" />
                  <el-option label="L3" value="L3" />
                  <el-option label="未评估" value="unevaluated" />
                  <el-option label="失败" value="fail" />
                </el-select>
                <el-input v-model="taskKeyword" size="small" placeholder="搜索会话..." clearable
                  style="width:200px" @input="onTaskFilterChange">
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
              </div>
            </div>
          </template>
          <el-table :data="pagedTasks" stripe size="small" style="width:100%"
            @sort-change="onTaskSort">
            <el-table-column prop="task_title" label="任务名称" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">
                <span style="cursor:pointer;color:var(--text-primary)"
                  :title="row.traj_name"
                  @click="openTaskDetail(row)">{{ row.task_title || row.traj_name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="等级" min-width="90" align="center">
              <template #default="{ row }">
                <el-tag size="small" :color="levelTagBg(row.traj_level)" style="color:#fff;border:none"
                  v-if="row.traj_level && row.traj_level !== 'failed'">
                  {{ row.traj_level }}
                </el-tag>
                <el-tag v-else-if="row.traj_level === 'failed'" size="small" type="danger" effect="plain">失败</el-tag>
                <el-tag v-else size="small" type="info" effect="plain">未评估</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="eval_completion" label="Completion" min-width="110" align="right" sortable="custom">
              <template #default="{ row }">
                <span :style="{ color: completionColor(row.eval_completion) }">
                  {{ fmtPct(row.eval_completion) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="task_done" label="Task-DONE" min-width="100" align="center">
              <template #default="{ row }">
                <span v-if="row.task_done" class="task-done-badge">✓</span>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
            <el-table-column label="工具失败" min-width="90" align="center">
              <template #default="{ row }">
                <span v-if="row.tool_fail" style="color:#f56c6c;font-weight:600">✗</span>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
            <el-table-column label="Eval-OC-Trace" min-width="130" align="center">
              <template #default="{ row }">
                <span v-if="row.has_eval" class="eval-trace-badge">有</span>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="eval_score" label="评分" min-width="110" align="right" sortable="custom">
              <template #default="{ row }">
                <span :style="{ color: scoreColor(row.eval_score) }">{{ fmtScore(row.eval_score) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="table-footer">
            <el-pagination layout="total, prev, pager, next" :total="shallowTotal"
              :page-size="taskPageSize" :current-page="taskPage" @current-change="onTaskPage" small />
            <span class="poll-hint" v-if="instanceStatus === 'running'">每 10s 自动刷新</span>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 会话详情弹窗（深层加载：轨迹 / Log / 评测） -->
    <el-dialog v-model="detailVisible" :title="`会话详情 · ${detailTrajName || ''}`" width="980px"
      top="4vh" destroy-on-close>
      <div v-if="detailLoading" style="padding:60px;text-align:center">
        <el-icon class="is-loading" :size="28" style="color:#6366f1"><Loading /></el-icon>
        <p style="color:#999;margin-top:12px">{{ detailStatusHint }}</p>
      </div>
      <template v-else-if="detailData">
        <el-descriptions :column="3" border size="small" style="margin-bottom:16px">
          <el-descriptions-item label="Harness">
            <el-tag size="small" :type="harnessTagType(detailData.harness)">{{ harnessLabel(detailData.harness) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag size="small" :type="detailData.verdict?.has_eval ? 'success' : 'info'">
              {{ detailData.verdict?.has_eval ? '已评测' : '未评测' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Task-DONE">
            <span v-if="detailData.verdict?.task_done" style="color:#10b981;font-weight:600">✓</span>
            <span v-else style="color:#c0c4cc">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="轨迹">
            <el-tag v-if="detailData.deep_status === 'downloaded'" size="small" type="success">已下载</el-tag>
            <el-tag v-else size="small" type="warning">未下载</el-tag>
            <div v-if="detailData.deep_error" style="color:#f56c6c;font-size:12px;margin-top:2px">
              {{ detailData.deep_error }}
            </div>
          </el-descriptions-item>
          <el-descriptions-item label="Completion">
            <span :style="{ color: completionColor(detailData.verdict?.completion) }">
              {{ fmtPct(detailData.verdict?.completion) }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="工具调用" :span="2">
            {{ detailData.assistant_stats?.tool_calls ?? '-' }} 次
            <span style="color:var(--text-muted)">· 普通轮 {{ detailData.assistant_stats?.plain_rounds ?? '-' }}</span>
            <span style="color:var(--text-muted)">· assistant {{ detailData.assistant_stats?.assistant_rounds ?? '-' }} 轮</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-tabs v-model="detailTab" class="detail-sub-tabs">
          <!-- 轨迹 -->
          <el-tab-pane label="轨迹" name="traj">
            <div v-if="assistantBlocks.length" class="traj-viewer">
              <div v-for="(b, i) in assistantBlocks" :key="i" class="traj-block" :class="`traj-${b.role || 'meta'}`">
                <div class="traj-block-head">
                  <span class="traj-role">{{ roleLabel(b) }}</span>
                  <span v-if="b.part_type === 'thinking'" class="traj-type">thinking</span>
                  <span v-if="b.tool_name" class="traj-tool">{{ b.tool_name }}</span>
                  <span v-if="b.part_type === 'toolCall'" class="traj-type">toolCall</span>
                  <span v-if="b.isError || b.exitCode" class="traj-err">exit {{ b.exitCode ?? '?' }}</span>
                  <el-icon v-if="i < assistantBlocks.length - 1" class="traj-fold" :size="14"
                    @click="toggleBlock(i)">
                    <ArrowDown v-if="!collapsedBlocks[i]" /><ArrowRight v-else />
                  </el-icon>
                </div>
                <div v-if="!collapsedBlocks[i] && (b.content || b.args)" class="traj-body">
                  <pre v-if="b.content" class="traj-content">{{ b.content }}</pre>
                  <pre v-if="b.args" class="traj-args">{{ b.args }}</pre>
                </div>
              </div>
            </div>
            <el-empty v-else description="无 assistant 轨迹" :image-size="40" />
          </el-tab-pane>

          <!-- Log -->
          <el-tab-pane label="Log" name="log">
            <el-tabs v-model="logTab" class="log-sub-tabs">
              <el-tab-pane label="主日志" name="task">
                <div class="log-panel-head">
                  <span style="color:var(--text-muted)">{{ detailData.log?.path || '无主日志' }}</span>
                  <el-button size="small" text type="primary" @click="detailLogTailFull = !detailLogTailFull">
                    {{ detailLogTailFull ? '仅看尾部' : '查看全部' }}
                  </el-button>
                </div>
                <pre class="log-panel" v-if="detailData.log?.tail">{{ logDisplayText(detailData.log.tail) }}</pre>
                <el-empty v-else description="无主日志" :image-size="30" />
              </el-tab-pane>
              <el-tab-pane label="Gateway" name="gateway">
                <div class="log-panel-head">
                  <span style="color:var(--text-muted)">{{ detailData.gateway?.path || '无 gateway 日志' }}</span>
                </div>
                <pre class="log-panel" v-if="detailData.gateway?.tail">{{ detailData.gateway.tail }}</pre>
                <el-empty v-else description="无 gateway 日志" :image-size="30" />
              </el-tab-pane>
              <el-tab-pane label="Eval 日志" name="eval">
                <div class="log-panel-head">
                  <span style="color:var(--text-muted)">{{ detailData.eval_use_log?.path || '无 eval 日志' }}</span>
                </div>
                <pre class="log-panel" v-if="detailData.eval_use_log?.tail">{{ detailData.eval_use_log.tail }}</pre>
                <el-empty v-else description="无 eval 日志" :image-size="30" />
              </el-tab-pane>
            </el-tabs>
          </el-tab-pane>

          <!-- 评测 -->
          <el-tab-pane label="评测" name="eval">
            <div class="verdict-panel">
              <div class="verdict-item"><span class="verdict-label">已评测</span>
                <el-tag size="small" :type="detailData.verdict?.has_eval ? 'success' : 'info'">
                  {{ detailData.verdict?.has_eval ? '是' : '否' }}
                </el-tag>
              </div>
              <div class="verdict-item"><span class="verdict-label">Completion</span>
                <span :style="{ color: completionColor(detailData.verdict?.completion) }">{{ fmtPct(detailData.verdict?.completion) }}</span>
              </div>
              <div class="verdict-item"><span class="verdict-label">Task-DONE</span>
                <span v-if="detailData.verdict?.task_done" style="color:#10b981;font-weight:600">✓</span>
                <span v-else style="color:#c0c4cc">—</span>
              </div>
            </div>
            <div v-if="evaluatorBlocks.length" class="traj-viewer" style="margin-top:12px">
              <div v-for="(b, i) in evaluatorBlocks" :key="i" class="traj-block" :class="`traj-${b.role || 'meta'}`">
                <div class="traj-block-head">
                  <span class="traj-role">{{ roleLabel(b) }}</span>
                  <span v-if="b.part_type === 'thinking'" class="traj-type">thinking</span>
                  <span v-if="b.tool_name" class="traj-tool">{{ b.tool_name }}</span>
                </div>
                <pre v-if="b.content" class="traj-content">{{ b.content }}</pre>
              </div>
            </div>
            <el-empty v-else description="无 evaluator 轨迹" :image-size="40" style="margin-top:12px" />
          </el-tab-pane>
        </el-tabs>
      </template>
      <template v-else>
        <el-empty description="详情不可用" :image-size="40" />
      </template>
    </el-dialog>
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowDown, ArrowRight, Search, Folder, Document, Loading } from '@element-plus/icons-vue'
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
  else if (tab === 'outputs') { loadShallow(false); loadDeepQueue(false) }
}

// ============ 配置信息 tab ============
const overview = ref({ total: 0, completed: 0, failed: 0, running: 0, pending: 0, success_rate: 0, error_breakdown: {}, error_tree: [] })
const prepareInfo = ref({ downloaded_configs: 0 })
const createParams = ref({})
const expandedPanels = ref([])
const expandedErrorGroups = ref([])
let timer = null

// 顶层汇总：过滤掉后端返回的 └ 前缀子行（这些已移入折叠树展示）
const summaryBreakdown = computed(() => {
  const out = {}
  for (const [k, v] of Object.entries(overview.value.error_breakdown || {})) {
    if (!k.startsWith('└')) out[k] = v
  }
  return out
})

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
function harnessTagType(t) {
  return { openclaw: 'primary', hermes: 'warning', 'claude-code': 'success', openjiuwen: 'danger', opencode: 'info', codex: 'warning', pi: 'success', grok: 'success', common: 'info' }[t] || ''
}
const HARNESS_COLORS = {
  openclaw: '#409eff', hermes: '#e6a23c', 'claude-code': '#67c23a',
  openjiuwen: '#f56c6c', opencode: '#909399',
  codex: '#8e44ad', pi: '#17a2b8', grok: '#00d084',
  common: '#c0c4cc',
}
function harnessColor(t) { return HARNESS_COLORS[t] || '#909399' }
function harnessLabel(t) {
  return { openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', grok: 'Grok', common: '通用' }[t] || t || 'openclaw'
}
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

// 把 OBS 目录（相对桶路径）拼上桶名展示；已是 obs:// 绝对路径或缺桶名则原样返回。
function obsPath(dir) {
  if (!dir) return '-'
  if (dir.startsWith('obs://')) return dir
  const bucket = createParams.value.obs_bucket
  if (!bucket) return dir
  return `${bucket.replace(/\/$/, '')}/${dir.replace(/^\//, '')}`
}

async function loadData() {
  try { const [i, o] = await Promise.all([api.get(`/instances/${id}`), api.get(`/instances/${id}/overview`)]); inst.value = i; overview.value = o } catch {}
  // 准备中：拉下载进度（已下载多少个 config）
  if (inst.value.status === 'preparing') {
    try { prepareInfo.value = await api.get(`/instances/${id}/prepare-progress`) } catch {}
  }
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

// ============ 输出 tab: 浅层漏斗（task_records 会话分级） ============
// 数据源: GET /instances/{id}/shallow（L0-L3 计数 + 全部行），10s 轮询。
const shallowLoading = ref(false)
const shallowSummary = ref({ L0: 0, L1: 0, 'L1.5': 0, L2: 0, L3: 0, graded: 0, total: 0, task_done: 0 })
const shallowRows = ref([])
const instanceStatus = ref('')

const LEVEL_COLORS = {
  L0: '#94a3b8',
  L1: '#38bdf8',
  'L1.5': '#818cf8',
  L2: '#a78bfa',
  L3: '#10b981',
}
const LEVEL_ORDER = ['L0', 'L1', 'L1.5', 'L2', 'L3']
// 从 traj_name/config stem 抽取任务名称（仅展示用）：
//   <序号>_<角色>_<任务标题>_<hash8>_q1  → 取中间 title 段（标题本身可含 _）
//   user_profile_* / task_<hash>.json 等无规律命名 → 原样回退（保留完整 traj_name）
function _extractTaskTitle(name) {
  const leaf = name.includes('/') ? name.split('/').pop() : name
  const s = leaf.replace(/\.json$/, '').replace(/_q\d+$/, '')
  const parts = s.split('_')
  if (parts.length >= 4 && /^\d{2,}$/.test(parts[0]) && /^[0-9a-f]{8}$/.test(parts[parts.length - 1])) {
    return parts.slice(2, -1).join('_') || s
  }
  return s || leaf
}

// 会话行: task_records 行 ∪ traj_name(leaf) ∪ tool_fail 列
const taskRows = computed(() => {
  return shallowRows.value.map(r => {
    const name = r.config_name || ''
    const leaf = name.includes('/') ? name.split('/').pop() : name
    const stripped = leaf.replace(/\.json$/, '')
    return {
      ...r,
      traj_name: stripped || leaf,
      task_title: _extractTaskTitle(name), // 展示用简短任务名；traj_name 仍是 API key
      tool_fail: typeof r.tool_fail === 'number' ? r.tool_fail > 0 : !!r.tool_fail,
    }
  })
})
const unevaluatedCount = computed(() => taskRows.value.filter(r => !r.traj_level).length)

// 浅层进度（后端 GET /shallow 返回 progress 字段）
const shallowProgress = ref(null)
const submittingShallow = ref(false)
const shallowProgressPct = computed(() => {
  const p = shallowProgress.value
  if (!p || !p.total) return 0
  const done = p.approved + p.failed
  return Math.round((done / p.total) * 100)
})
const shallowProgressText = computed(() => `${shallowProgressPct.value}%`)
const shallowProgressColor = computed(() => {
  if (!shallowProgress.value) return '#409eff'
  return shallowProgress.value.undone > 0 ? '#e6a23c' : '#10b981'
})
const shallowProgressStateLabel = computed(() => {
  const p = shallowProgress.value
  if (!p) return ''
  if (p.queued) return '处理中…'
  return '待处理'
})

// ---- 状态分析：浅层→深层 流水线当前所处阶段（供顶部状态卡展示） ----
// empty    = 无 task_records（无会话数据）
// ungraded = ended 实例且仍有 NULL/failed 行，尚未登记浅层（可手动提交）
// queued   = 已登记 shallow_requests（worker 排队/处理中）
// grading  = running/preparing 自动分级中
// done     = 浅层全部分级完成
const pipelineStage = computed(() => {
  const st = inst.value?.status
  const p = shallowProgress.value
  const total = shallowSummary.value.total || 0
  if (!total) return 'empty'
  const undone = p ? p.undone : total
  if (undone === 0) return 'done'
  if (st === 'running' || st === 'preparing') return 'grading'
  if (p && p.queued) return 'queued'
  return 'ungraded'
})
const stageMeta = computed(() => {
  const map = {
    empty:    { label: '暂无会话数据', type: 'info',  desc: '该实例还没有 task_records，先确认任务是否已产出会话' },
    ungraded: { label: '未分析',       type: 'warning', desc: '有会话未做浅层分级，需提交浅层分析' },
    queued:   { label: '浅层分析中',   type: 'primary', desc: '已提交浅层分析，worker 正在排队/处理' },
    grading:  { label: '自动分级中',   type: 'primary', desc: 'running/preparing 实例由 worker 持续采集分级，无需手动提交' },
    done:     { label: '浅层已完成',   type: 'success', desc: '所有会话均已定级，可进行深层下载/查看详情' },
  }
  return map[pipelineStage.value] || { label: pipelineStage.value, type: 'info', desc: '' }
})

// 状态卡步骤的两步标签：浅层已分级数 / 深层下载进度
const shallowStepLabel = computed(() => {
  const p = shallowProgress.value
  if (!p || !p.total) return '未分析'
  if (p.undone === 0) return `已完成 ${p.approved}`
  if (p.queued) return '处理中…'
  return `${p.approved}/${p.total}`
})
const deepStepLabel = computed(() => {
  const s = deepQueueSummary.value
  if (!s || !s.total) return '未下载'
  if (s.downloading > 0) return `下载中 ${s.downloading}`
  if (s.pending > 0) return `待下载 ${s.pending}`
  if (s.done > 0) return `已完成 ${s.done}`
  return '未下载'
})
// 步骤高亮：浅层非空即激活；深层需浅层已完成且有下载动作/产物才激活
const shallowActive = computed(() => pipelineStage.value !== 'empty' && pipelineStage.value !== 'ungraded')
const deepActive = computed(() => {
  if (pipelineStage.value !== 'done') return false
  const s = deepQueueSummary.value
  return !!(s && s.total > 0)
})

async function submitShallow() {
  submittingShallow.value = true
  try {
    const res = await api.post(`/instances/${id}/shallow`)
    shallowProgress.value = { ...(shallowProgress.value || {}), queued: true }
    ElMessage.success(res.hint || '已提交浅层分析')
    loadShallow(false)
  } catch { /* interceptor 已提示 */ }
  finally { submittingShallow.value = false }
}

// 漏斗柱状图: 高度相对已分级总数（未评估单独一柱，按总数比例）
const funnelBuckets = computed(() => {
  const graded = shallowSummary.value.graded || 1
  return LEVEL_ORDER.map(label => {
    const count = shallowSummary.value[label] || 0
    return { label, count, pct: (count / graded) * 100, color: LEVEL_COLORS[label] }
  })
})
const unevaluatedPct = computed(() => {
  const total = shallowSummary.value.total
  if (!total) return 0
  return ((unevaluatedCount.value / total) * 100)
})

// ---- 表格分页 / 过滤 / 排序（本地，浅层一次性返回全部行） ----
const levelFilter = ref('')
const taskKeyword = ref('')
const taskPage = ref(1)
const taskPageSize = ref(50)
const taskSort = ref({})

const filteredTaskRows = computed(() => {
  let rows = taskRows.value
  if (levelFilter.value) {
    if (levelFilter.value === 'unevaluated') rows = rows.filter(r => !r.traj_level)
    else if (levelFilter.value === 'fail') rows = rows.filter(r => r.traj_level === 'failed')
    else rows = rows.filter(r => r.traj_level === levelFilter.value)
  }
  const kw = taskKeyword.value.trim().toLowerCase()
  if (kw) rows = rows.filter(r => (r.traj_name || '').toLowerCase().includes(kw)
    || (r.task_title || '').toLowerCase().includes(kw))
  const s = taskSort.value
  if (s.prop) {
    const dir = s.order === 'descending' ? -1 : 1
    const cmp = (a, b) => {
      let x = a[s.prop], y = b[s.prop]
      if (x == null) x = -Infinity
      if (y == null) y = -Infinity
      if (typeof x === 'string') return x.localeCompare(String(y)) * dir
      return (x - y) * dir
    }
    rows = [...rows].sort(cmp)
  }
  return rows
})
const pagedTasks = computed(() => {
  const start = (taskPage.value - 1) * taskPageSize.value
  return filteredTaskRows.value.slice(start, start + taskPageSize.value)
})
const shallowTotal = computed(() => filteredTaskRows.value.length)

function onTaskFilterChange() { taskPage.value = 1 }
function onTaskPage(p) { taskPage.value = p }
function onTaskSort({ prop, order }) {
  taskSort.value = { prop, order }
  taskPage.value = 1
}

async function loadShallow(refresh = false) {
  shallowLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/shallow`)
    shallowSummary.value = res.summary || shallowSummary.value
    shallowRows.value = res.rows || []
    instanceStatus.value = res.instance_status || ''
    shallowProgress.value = res.progress || null
    // 点击驱动：看到输出数据后，若该实例已结束且仍有缺口，登记一次浅层请求
    maybeTriggerShallow()
  } catch { /* interceptor 已提示 */ }
  finally { shallowLoading.value = false }
}

// ---- 浅层标签 / 颜色辅助 ----
function levelTagBg(level) {
  return LEVEL_COLORS[level] || ''
}
function completionColor(v) {
  if (v == null) return '#909399'
  if (v >= 0.5) return '#10b981'
  if (v > 0) return '#e6a23c'
  return '#f56c6c'
}
function scoreColor(v) {
  if (v == null) return '#909399'
  if (v >= 0.5) return '#10b981'
  if (v > 0) return '#e6a23c'
  return '#f56c6c'
}
function fmtPct(v) {
  if (v == null) return '-'
  return (v * 100).toFixed(1) + '%'
}
function fmtScore(v) {
  if (v == null) return '-'
  return Number(v).toFixed(4)
}

// ============ 输出 tab: 深层下载队列（批量入队 + 状态显示） ============
// 数据源: GET /deep/queue（summary 计数 + 按「进行中优先」排序的行）。
// 轮询: 有 pending/downloading 时 3s（工作在动，紧看），全终态时放 10s（省请求）。
const deepQueueVisible = ref(false)
const deepQueueLoading = ref(false)
const deepQueueSummary = ref({ pending: 0, downloading: 0, done: 0, failed: 0, total: 0 })
const deepQueueRows = ref([])
const deepQueuePage = ref(1)
const deepQueuePageSize = ref(50)
const deepQueueTotal = ref(0)
const enqueuing = ref(false)
let deepQueueTimer = null

const deepQueuePollInterval = computed(() => {
  const s = deepQueueSummary.value
  return (s.pending > 0 || s.downloading > 0) ? 3000 : 10000
})
watch(deepQueuePollInterval, (ms) => {
  if (!deepQueueVisible.value) return
  if (deepQueueTimer) clearInterval(deepQueueTimer)
  deepQueueTimer = setInterval(() => loadDeepQueue(false), ms)
})

async function loadDeepQueue(showLoading = true) {
  if (showLoading) deepQueueLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/deep/queue`, {
      params: { page: deepQueuePage.value, page_size: deepQueuePageSize.value },
    })
    deepQueueSummary.value = res.summary || deepQueueSummary.value
    deepQueueRows.value = res.rows || []
    deepQueueTotal.value = res.total || deepQueueSummary.value.total || 0
    deepQueueVisible.value = true
  } catch { /* 404/网络等由 interceptor 提示，静默保持上次状态 */ }
  finally { if (showLoading) deepQueueLoading.value = false }
}
function onDeepQueuePage(p) { deepQueuePage.value = p }

function startDeepQueuePolling() {
  if (!deepQueueTimer) deepQueueTimer = setInterval(() => loadDeepQueue(false), deepQueuePollInterval.value)
}
function stopDeepQueuePolling() {
  if (deepQueueTimer) { clearInterval(deepQueueTimer); deepQueueTimer = null }
}

async function enqueueAllDeep() {
  try {
    await ElMessageBox.confirm(
      '把该实例所有已分级（L0-L3）且尚未下载的会话批量入队，worker 逐个下载？',
      '批量下载', { type: 'warning' })
  } catch { return }  // 取消
  enqueuing.value = true
  try {
    const res = await api.post(`/instances/${id}/deep/enqueue_all`)
    deepQueueVisible.value = true
    startDeepQueuePolling()
    const msg = res.queued
      ? `已入队 ${res.queued} 个会话` +
        (res.skipped_done ? `，${res.skipped_done} 个已下载跳过` : '') +
        (res.skipped_ungraded ? `，${res.skipped_ungraded} 个未分级/失败跳过` : '')
      : '没有可入队的会话（都已分级并下载，或均未分级）'
    ElMessage.success(msg)
    await loadDeepQueue(false)
  } catch (e) {
    ElMessage.error('批量入队失败: ' + (e?.message || '未知错误'))
  } finally { enqueuing.value = false }
}

function deepStatusLabel(s) {
  return { pending: '待下载', downloading: '下载中', done: '已完成', failed: '失败' }[s] || s || '—'
}
function deepStatusTagType(s) {
  return { pending: 'warning', downloading: 'primary', done: 'success', failed: 'danger' }[s] || 'info'
}
function deepLevelTagType(l) {
  // 复用等级色系: L3 紫 / L2 红 / L1.5 橙 / L1 蓝 / L0 灰 / failed 红
  return { L0: 'info', L1: 'primary', 'L1.5': 'warning', L2: 'danger', L3: 'warning' }[l] || 'info'
}
function fmtDeepTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 19)
}

// ============ 输出 tab: 深层详情（task_traj_records 本地缓存） ============
// 触发 POST /deep/{traj_name} → worker 下载到 output_cache → 3s 轮询 status → 拉 detail。
const detailVisible = ref(false)
const detailTrajName = ref('')
const detailLoading = ref(false)
const detailStatusHint = ref('')
const detailData = ref(null)
const detailTab = ref('traj')
const logTab = ref('task')
const collapsedBlocks = ref({})
const detailLogTailFull = ref(false)
let deepPollTimer = null

watch(detailVisible, (v) => {
  if (!v && deepPollTimer) {
    clearInterval(deepPollTimer)
    deepPollTimer = null
  }
})

const assistantBlocks = computed(() => detailData.value?.assistant_trajectory || [])
const evaluatorBlocks = computed(() => detailData.value?.evaluator_trajectory || [])

async function openTaskDetail(row) {
  const name = row.traj_name
  detailTrajName.value = name
  detailVisible.value = true
  detailData.value = null
  detailLoading.value = true
  detailTab.value = 'traj'
  logTab.value = 'task'
  collapsedBlocks.value = {}
  detailLogTailFull.value = false
  // 先直接尝试读本地缓存详情（浅层 tsr+log 产物可直接展示，无需触发下载）。
  // 409 = 本地轨迹未下载 → 静默（silent 标记跳过全局弹错），转触发下载分支。
  // deep_status='not_downloaded' = 深层未下载（只有浅层 tsr+log，无轨迹文件）；
  //   harness/stats 仍可读，但轨迹文件在 OBS，需触发下载后重拉。
  try {
    const d = await api.get(`/instances/${id}/deep/${name}/detail`, { silent: true })
    detailData.value = d
    detailLoading.value = false
    // 未下载 → 继续走触发下载流程（不显示轨迹，下载完成后重拉显示）
    if (d.deep_status === 'not_downloaded') {
      detailStatusHint.value = '轨迹未下载，正在触发深层加载…'
    } else {
      return
    }
  } catch { /* 本地无轨迹缓存(409) → 走触发下载流程 */ }
  // 幂等触发下载；已 downloading/pending 时后端返回 in_progress，不打断
  let trigger = 'queued'
  try { trigger = (await api.post(`/instances/${id}/deep/${name}`)).status || 'queued' } catch { /* 404 等由 interceptor 提示 */ }
  if (trigger === 'in_progress') detailStatusHint.value = '正在后台加载详情…'
  else if (!detailData.value) detailStatusHint.value = '正在加载详情…'
  clearInterval(deepPollTimer)
  deepPollTimer = setInterval(pollDeep, 3000)
  await pollDeep()
}

async function pollDeep() {
  if (!detailVisible.value || !detailTrajName.value) return
  let st = null
  try { st = await api.get(`/instances/${id}/deep/${detailTrajName.value}/status`) } catch { return }
  if (st.status === 'downloading') { detailStatusHint.value = '正在下载缓存…' }
  if (st.status === 'failed') {
    // 下载失败：若本地已有浅层 tsr+log 产物，回退直接读详情展示
    detailStatusHint.value = `下载失败: ${st.error || '未知错误'}，回退本地缓存…`
    try {
      const d = await api.get(`/instances/${id}/deep/${detailTrajName.value}/detail`)
      detailData.value = d
      detailLoading.value = false
      return
    } catch { /* 本地也无缓存 → 展示失败 */ }
    detailStatusHint.value = `加载失败: ${st.error || '未知错误'}`
    clearInterval(deepPollTimer)
    deepPollTimer = null
    detailLoading.value = false
    return
  }
  if (st.status !== 'done') return
  clearInterval(deepPollTimer)
  deepPollTimer = null
  try {
    const d = await api.get(`/instances/${id}/deep/${detailTrajName.value}/detail`)
    detailData.value = d
    detailLoading.value = false
  } catch {
    detailStatusHint.value = '详情解析中…'
  }
}

// 轨迹 viewer 辅助
function roleLabel(b) {
  if (!b) return '?'
  const map = {
    assistant: '助手',
    user: '用户',
    toolResult: '工具结果',
    thinking: '思考',
    toolCall: '工具调用',
    meta: '系统',
  }
  return map[b.role] || map[b.part_type] || b.role || b.part_type || '系统'
}
function toggleBlock(i) {
  collapsedBlocks.value = { ...collapsedBlocks.value, [i]: !collapsedBlocks.value[i] }
}
function logDisplayText(text) {
  if (detailLogTailFull.value) return text
  return (text || '').slice(-20000)
}

// ============ 生命周期 ============
let shallowTimer = null
let shallowTriggered = false

onMounted(async () => {
  loading.value = true
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loading.value = false
  // 点击驱动：打开详情页即登记浅层请求（仅 ended 实例；running 由 worker 持续分级，无需登记）。
  // 幂等登记，worker 处理完删登记出队；flag 防止同一会话重复 POST。漏斗进度由 10s 轮询反映。
  maybeTriggerShallow()
  // config tab 常驻 10s 轮询（无论当前在哪个 tab，保持顶部进度条/状态最新）
  timer = setInterval(loadData, 10000)
  ensureTabLoaded(activeTab.value)
  if (activeTab.value === 'logs') {
    nextTick(() => { if (logContainer.value) logContainer.value.addEventListener('scroll', onScroll) })
  }
  if (activeTab.value === 'outputs') {
    shallowTimer = setInterval(() => loadShallow(false), 10000)
    startDeepQueuePolling()
  }
})

async function maybeTriggerShallow() {
  if (shallowTriggered) return
  const status = inst.value?.status
  // finished/completed/stopped：worker 的 _pick_running_instances 消费这类登记 + running/preparing。
  // stopped 实例轨迹可能仍在 OBS（中止已上传的部分 task 目录），同样可分级查看会话详情；
  // worker 处理完（无缺口）删登记出队，不会永久残留。
  if (status !== 'finished' && status !== 'completed' && status !== 'stopped') return
  // 用 progress.undone（剩余未分级会话数）判定缺口，而不是依赖 shallowRows 已填充：
  //   loadShallow 在 mount 时可能尚未返回，shallowRows 为空会导致永远跳过（既不登记也无提示）。
  //   只要 ended 实例还有未分级会话就应登记；0 或全部完成则无需登记。
  const p = shallowProgress.value
  const undone = p ? p.undone : undefined
  if (undone === undefined) return      // progress 未加载，等 loadShallow 返回后再触发
  if (undone === 0) return              // 全部定级，无需浅层
  shallowTriggered = true
  try {
    const res = await api.post(`/instances/${id}/shallow`)
    // 立即可见：切到「浅层分析中」，无需等下一轮轮询
    if (shallowProgress.value) shallowProgress.value = { ...shallowProgress.value, queued: true }
    console.debug('浅层已登记', res)
  } catch { /* interceptor 已提示；登记失败不阻塞页面 */ }
}

// outputs tab 懒加载时启动浅层 + 深层队列轮询；离开时停止
watch(activeTab, (tab) => {
  if (tab === 'outputs') {
    if (!shallowTimer) shallowTimer = setInterval(() => loadShallow(false), 10000)
    if (!deepQueueTimer) startDeepQueuePolling()
  } else {
    if (shallowTimer) { clearInterval(shallowTimer); shallowTimer = null }
    stopDeepQueuePolling()
  }
}, { flush: 'sync' })

onUnmounted(() => {
  clearInterval(timer)
  clearInterval(shallowTimer)
  clearInterval(deepPollTimer)
  stopDeepQueuePolling()
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
/* 异常明细折叠树 */
.error-tree { margin-top: 6px; border-top: 1px solid var(--border-color); }
.error-tree :deep(.el-collapse-item__header) { height: 40px; line-height: 40px; font-size: 14px; }
.error-tree :deep(.el-collapse-item__content) { padding-bottom: 6px; }
.err-grp-title { display: flex; align-items: center; gap: 8px; width: 100%; }
.err-grp-name { flex: 1; color: var(--text-secondary); }
.err-grp-count { color: var(--text-muted); font-size: 13px; margin-right: 8px; }
.err-code {
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  background: var(--fill-color, #f4f4f5); padding: 1px 6px; border-radius: 3px;
  font-size: 12px; color: var(--text-secondary);
}
.err-desc { color: var(--text-muted); font-size: 12px; margin-left: 8px; }
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

/* ---- 输出 tab: 漏斗柱状图 ---- */
.funnel-bar-row {
  display: flex; align-items: flex-end; justify-content: center; gap: 16px;
  height: 170px; padding: 8px 24px 26px;
  border-bottom: 1px solid #e5e7eb; margin-bottom: 8px;
}
.funnel-bar-col {
  flex: 1; display: flex; flex-direction: column; align-items: center;
  height: 100%; position: relative; max-width: 90px;
}
.funnel-bar-count {
  font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; top: 0; white-space: nowrap;
}
.funnel-bar-track {
  flex: 1; width: 100%; display: flex; align-items: flex-end; padding-top: 18px;
}
.funnel-bar {
  width: 100%; max-width: 44px; margin: 0 auto;
  border-radius: 3px 3px 0 0; transition: height 0.3s ease;
  min-height: 2px;
}
.uneval-bar {
  background: repeating-linear-gradient(135deg, #d8d8dc 0 6px, #c2c2c8 6px 12px);
}
.funnel-bar-label {
  font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; bottom: -20px; white-space: nowrap;
}

/* ---- 输出 tab: 下载队列 ---- */
.deep-queue-stats {
  display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px;
}
.deep-queue-stat {
  padding: 4px 12px; border-radius: 14px; background: #f4f4f5; color: #909399;
  font-size: 12px; display: inline-flex; align-items: center; gap: 4px;
}
.deep-queue-stat b { font-size: 14px; font-weight: 700; }
.deep-queue-stat.is-active { background: rgba(230, 162, 60, .14); color: #e6a23c; }
.deep-queue-stat.done { background: rgba(16, 185, 129, .12); color: #10b981; }
.deep-queue-stat.fail { background: rgba(245, 108, 108, .12); color: #f56c6c; }
.deep-queue-stat.muted { background: transparent; color: var(--text-muted); padding: 4px 0; }
.deep-status.downloading {
  display: inline-flex; align-items: center; gap: 4px;
  color: #e6a23c; font-size: 12px;
}

/* ---- 输出 tab: 会话表格 ---- */
.table-footer {
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 12px;
}
.poll-hint { font-size: 12px; color: var(--text-muted); }
.task-done-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  background: #10b981; color: #fff; font-size: 11px; font-weight: 700;
}
.eval-trace-badge {
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  background: rgba(99, 102, 241, .12); color: #6366f1;
  font-size: 12px;
}

/* ---- 输出 tab: 会话详情弹窗 ---- */
.detail-sub-tabs { margin-top: 4px; }
.log-sub-tabs { margin-top: 4px; }
.traj-viewer {
  max-height: 520px; overflow-y: auto; border: 1px solid var(--border-color);
  border-radius: var(--radius-md); padding: 8px;
}
.traj-block { border-radius: 6px; margin-bottom: 6px; overflow: hidden; }
.traj-block:last-child { margin-bottom: 0; }
.traj-block-head {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 10px; font-size: 12px; min-height: 26px;
}
.traj-role { font-weight: 600; }
.traj-type { color: #909399; font-size: 11px; }
.traj-tool {
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  color: #6366f1; font-size: 11px;
}
.traj-err { color: #f56c6c; font-size: 11px; font-weight: 600; }
.traj-fold { margin-left: auto; cursor: pointer; color: #909399; }
.traj-fold:hover { color: var(--text-primary); }
.traj-body { padding: 2px 10px 8px; }
.traj-content, .traj-args {
  margin: 0; padding: 0; font-size: 12px; line-height: 1.55;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  white-space: pre-wrap; word-break: break-all;
}
.traj-args { color: var(--text-muted); margin-top: 4px; }
.traj-assistant .traj-block-head { background: rgba(99, 102, 241, .10); }
.traj-assistant .traj-role { color: #6366f1; }
.traj-user .traj-block-head { background: rgba(16, 185, 129, .10); }
.traj-user .traj-role { color: #10b981; }
.traj-toolResult .traj-block-head { background: #f5f7fa; }
.traj-toolResult .traj-role { color: #606266; }
.traj-meta .traj-block-head { background: rgba(230, 162, 60, .10); }
.traj-meta .traj-role { color: #e6a23c; }
.log-panel-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 6px;
}
.log-panel {
  background: #1e293b; color: #e2e8f0; padding: 12px;
  border-radius: var(--radius-sm); max-height: 420px; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px;
  white-space: pre-wrap; word-break: break-all; margin: 0;
}
.verdict-panel {
  display: flex; gap: 32px; padding: 12px 16px;
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
  background: #fafafa;
}
.verdict-item { display: flex; flex-direction: column; gap: 4px; }
.verdict-label { font-size: 12px; color: var(--text-muted); }
</style>






