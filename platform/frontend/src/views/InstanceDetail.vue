<template>
  <div v-loading="loading">
    <div class="header-row">
      <el-page-header @back="$router.back()">
        <template #content>{{ inst.name || '实例详情' }}</template>
      </el-page-header>
      <div style="display:flex;gap:8px">
        <el-button type="success" v-if="authStore.isOperator && !['running','preparing'].includes(inst.status)" @click="startInstance">
          {{ inst.status === 'queued' ? '启动（重新加入排队）' : '启动' }}
        </el-button>
        <el-button type="danger" v-if="authStore.isOperator && ['running','queued'].includes(inst.status)" @click="stopInstance">
          {{ inst.status === 'queued' ? '停止（退出队列）' : '停止' }}
        </el-button>
        <el-button type="warning" v-if="authStore.isOperator && inst.failed_tasks > 0 && !['running','preparing'].includes(inst.status)" @click="retryFailed">重跑失败</el-button>
      </div>
    </div>

    <div v-if="inst.status === 'preparing'" class="glass-card" style="padding:16px 20px;margin-bottom:16px;display:flex;align-items:center;gap:10px">
      <el-icon class="is-loading" :size="18" style="color:#e6a23c"><Loading /></el-icon>
      <span style="color:#e6a23c">准备中：正在下载配置</span>
      <span class="mono-num" style="color:#e6a23c">已下载 {{ prepareInfo.downloaded_configs ?? 0 }} 个 config</span>
    </div>

    <el-tabs v-model="activeTab" class="detail-tabs">
      <!-- ================= 配置信息 ================= -->
      <el-tab-pane label="配置信息" name="config">
        <!-- 概览卡：左侧图标色块 + 右侧「标签在上、数值在下」。
             图标只是辅助识别，含义由标签文字承担，不靠色相单独区分。 -->
        <div class="stat-grid">
          <div v-for="s in statCards" :key="s.key" class="stat-card">
            <span class="stat-icon" :style="{ background: s.tint, color: s.color }">
              <el-icon :size="20"><component :is="s.icon" /></el-icon>
            </span>
            <span class="stat-body">
              <span class="stat-label">{{ s.label }}</span>
              <span class="stat-num" :style="{ color: s.color }">{{ s.value }}</span>
            </span>
          </div>
        </div>

        <el-row :gutter="20">
          <el-col :span="12">
            <el-card class="sec-card">
              <template #header>
                <span class="sec-head"><el-icon class="sec-head-icon"><Postcard /></el-icon>实例信息</span>
              </template>
              <el-descriptions :column="1" border>
                <el-descriptions-item label="实例ID">{{ inst.id }}</el-descriptions-item>
                <el-descriptions-item label="状态"><el-tag :type="statusColor(inst.status)">{{ statusText(inst.status) }}</el-tag></el-descriptions-item>
                <el-descriptions-item v-if="inst.error_summary" label="失败原因">
                  <span style="color:#ef4444;white-space:pre-wrap;word-break:break-all">{{ inst.error_summary }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="PID">{{ inst.pid || '-' }}</el-descriptions-item>
                <el-descriptions-item label="配置文件">{{ inst.config_path }}</el-descriptions-item>
                <el-descriptions-item label="并发数">{{ inst.concurrent_num }}</el-descriptions-item>
                <el-descriptions-item label="创建时间">{{ inst.created_at }}</el-descriptions-item>
                <el-descriptions-item label="启动时间">{{ inst.started_at || '-' }}</el-descriptions-item>
                <!-- 耗时相关原先是一条横跨整页的浮动条，与「实例信息」同属实例自身属性，
                     挪进来跟启动时间放一起才读得顺。 -->
                <el-descriptions-item v-if="overview.elapsed_seconds != null" label="已用时间">
                  {{ formatDuration(overview.elapsed_seconds) }}
                </el-descriptions-item>
                <el-descriptions-item v-if="overview.avg_task_seconds" label="平均耗时">
                  {{ formatDuration(overview.avg_task_seconds) }}
                </el-descriptions-item>
                <el-descriptions-item v-if="overview.estimated_remaining_seconds != null && inst.status === 'running'" label="预计剩余">
                  {{ formatDuration(overview.estimated_remaining_seconds) }}
                </el-descriptions-item>
                <el-descriptions-item v-if="overview.estimated_finish_time && inst.status === 'running'" label="预计完成">
                  {{ overview.estimated_finish_time?.replace('T', ' ') }}
                </el-descriptions-item>
                <el-descriptions-item label="创建者">{{ inst.created_by }}</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card class="sec-card">
              <template #header>
                <span class="sec-head"><el-icon class="sec-head-icon"><TrendCharts /></el-icon>任务执行情况</span>
              </template>
              <div v-if="Object.keys(overview.error_breakdown || {}).length">
                <!-- 环形图：任务的最终归属（成功/失败/异常/未执行）。
                     颜色是状态语义色，不随大小变；每段都带文字+数值图例，
                     不靠色相单独承担含义。 -->
                <div class="donut-wrap">
                  <div class="donut-chart">
                    <EChart :option="outcomeOption" height="240px" />
                    <!-- 中心数字用 HTML 叠上去：echarts 的 center 标签是逐数据项渲染的，
                         四段就会叠出四层同样的文字。 -->
                    <div class="donut-center">
                      <span class="donut-total">{{ outcomeTotal }}</span>
                      <span class="donut-total-label">任务</span>
                    </div>
                  </div>
                  <div class="donut-legend">
                    <div v-for="o in outcomeItems" :key="o.key" class="donut-item">
                      <i class="dot" :style="{ background: o.color }"></i>
                      <span class="donut-name">{{ o.label }}</span>
                      <span class="donut-count">{{ o.value }}</span>
                      <span class="donut-pct">{{ o.pct }}%</span>
                    </div>
                  </div>
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
        <el-card class="sec-card" style="margin-top:20px" v-if="Object.keys(createParams).length">
          <template #header>
            <span class="sec-head"><el-icon class="sec-head-icon"><Setting /></el-icon>创建参数</span>
          </template>
          <el-collapse v-model="expandedPanels" class="param-collapse">
            <el-collapse-item name="basic">
              <template #title>
                <span class="param-title">
                  <el-icon class="param-icon"><Setting /></el-icon>
                  <span class="param-name">基本配置</span>
                  <span class="param-peek">{{ peek.basic }}</span>
                </span>
              </template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="任务标识">{{ createParams.task_name || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Harness类型">
                  <el-tag size="small" :style="harnessTagStyle(createParams.harness_type)">
                    {{ harnessLabel(createParams.harness_type) }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="Invite Code">{{ createParams.invite_code || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Harness 配置">
                  <template v-if="createParams.harness_config_name">
                    {{ createParams.harness_config_name }}
                    <span v-if="createParams.harness_config_version" class="muted">
                      / v{{ createParams.harness_config_version }}
                    </span>
                  </template>
                  <template v-else-if="createParams.harness_config_label">
                    {{ createParams.harness_config_label }}
                  </template>
                  <span v-else>{{ createParams.harness_config_id || '-' }}</span>
                </el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item name="obs">
              <template #title>
                <span class="param-title">
                  <el-icon class="param-icon"><FolderOpened /></el-icon>
                  <span class="param-name">OBS配置</span>
                  <span class="param-peek">{{ peek.obs }}</span>
                </span>
              </template>
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item label="技能目录">{{ obsPath(createParams.skill_dir) }}</el-descriptions-item>
                <el-descriptions-item label="默认技能">{{ createParams.default_skills || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Agent目录">{{ obsPath(createParams.agent_dir) }}</el-descriptions-item>
                <el-descriptions-item label="用户Config目录">{{ obsPath(createParams.user_config_dir) }}</el-descriptions-item>
                <el-descriptions-item label="用户Profile目录">{{ obsPath(createParams.user_profile_dir) }}</el-descriptions-item>
                <el-descriptions-item label="轨迹保存路径">{{ trajSaveFullPath || obsPath(createParams.traj_save_path) }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item name="model">
              <template #title>
                <span class="param-title">
                  <el-icon class="param-icon"><Cpu /></el-icon>
                  <span class="param-name">模型配置</span>
                  <span class="param-peek">{{ peek.model }}</span>
                </span>
              </template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="Base URL">{{ createParams.model_base_url || '-' }}</el-descriptions-item>
                <el-descriptions-item label="API Key">{{ maskKey(createParams.model_api_key) }}</el-descriptions-item>
                <el-descriptions-item label="API类型">{{ createParams.model_api_type || '-' }}</el-descriptions-item>
                <el-descriptions-item label="模型ID">{{ createParams.model_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>

            <el-collapse-item name="agents">
              <template #title>
                <span class="param-title">
                  <el-icon class="param-icon"><User /></el-icon>
                  <span class="param-name">用户模拟配置</span>
                  <span class="param-peek">{{ peek.agents }}</span>
                </span>
              </template>
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

            <el-collapse-item name="advanced">
              <template #title>
                <span class="param-title">
                  <el-icon class="param-icon"><Box /></el-icon>
                  <span class="param-name">高级配置</span>
                  <span class="param-peek">{{ peek.advanced }}</span>
                </span>
              </template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="起始索引">{{ createParams.start_index ?? 0 }}</el-descriptions-item>
                <el-descriptions-item label="任务总数">{{ createParams.total_num ? createParams.total_num : '不限' }}</el-descriptions-item>
                <el-descriptions-item label="沙箱配置">
                  <template v-if="createParams.sandbox_config_name">
                    {{ createParams.sandbox_config_name }}
                    <el-tag size="small" type="info" style="margin-left:6px">{{ createParams.sandbox_type === 'windows' ? 'Windows' : 'Linux' }}</el-tag>
                  </template>
                  <span v-else-if="createParams.sandbox_config_id">已删除（id={{ createParams.sandbox_config_id }}）</span>
                  <span v-else>未选择（用模板默认）</span>
                </el-descriptions-item>
                <el-descriptions-item label="沙箱标识">
                  <span v-if="createParams.task_name">{{ createParams.task_name }}</span>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item label="沙箱块">
                  <span v-if="createParams.sandbox_active_key">
                    <code>{{ createParams.sandbox_active_key }}</code>
                  </span>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item label="沙箱文件">{{ createParams.sandbox_file || '-' }}</el-descriptions-item>
                <el-descriptions-item label="Key 沙箱后缀">
                  <el-tag size="small" :type="createParams.suffix_api_keys ? 'success' : 'info'">
                    {{ createParams.suffix_api_keys ? '已开启' : '未开启' }}
                  </el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="资源池">
                  <span v-if="inst.sandbox_namespace"><code>{{ inst.sandbox_namespace }}</code></span>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item label="单沙箱资源">
                  <span v-if="inst.cpu_request">
                    {{ inst.cpu_request }}/{{ inst.cpu_limit }} 核 · {{ inst.memory_request }}/{{ inst.memory_limit }} GiB
                  </span>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item label="镜像名称" :span="2">
                  <span style="word-break:break-all">{{ createParams.sandbox_image || createParams.image_name || '-' }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="代码仓ID">{{ createParams.code_repo_id || '-' }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>
          </el-collapse>
        </el-card>

        <!-- 配置文件：卡片上只放摘要（文件数 / 最后更新 / 文件名快捷入口），
             正文很长（几百行 yaml），塞在页面里会把下面的内容推没，
             所以点开走弹窗看，弹窗里再给搜索 / 复制 / 下载 / 全屏。 -->
        <el-card class="sec-card" style="margin-top:20px">
          <template #header>
            <span class="sec-head"><el-icon class="sec-head-icon"><Document /></el-icon>配置文件</span>
          </template>
          <div v-if="configFiles.length" class="cfg-summary">
            <el-icon class="cfg-summary-icon" :size="22"><Document /></el-icon>
            <div class="cfg-summary-main">
              <div class="cfg-summary-title">配置文件（{{ configFiles.length }} 个）</div>
              <div class="cfg-summary-sub">最后更新：{{ configLastModified || '-' }}</div>
              <div class="cfg-chips">
                <el-tag v-for="cf in configFiles" :key="cf.name" size="small"
                  :type="cf.name === activeConfigTab ? 'primary' : 'info'"
                  :effect="cf.name === activeConfigTab ? 'light' : 'plain'"
                  class="cfg-chip" @click="openConfigViewer(cf.name)">{{ cf.name }}</el-tag>
              </div>
              <div class="cfg-actions">
                <el-button size="small" :icon="View" @click="openConfigViewer()">查看配置</el-button>
                <el-button size="small" :icon="CopyDocument" @click="copyConfigPath">复制路径</el-button>
                <el-button size="small" :icon="Download" @click="downloadConfig()">下载</el-button>
                <el-button size="small" text :loading="configLoading" @click="loadConfigFiles">刷新</el-button>
              </div>
            </div>
          </div>
          <el-empty v-else :description="configLoading ? '加载中' : '暂无配置文件'" :image-size="40" />
        </el-card>

        <!-- 配置文件查看弹窗 -->
        <el-dialog v-model="configViewerOpen" :fullscreen="configFullscreen"
          :width="configFullscreen ? undefined : '960px'" top="6vh"
          class="cfg-dialog" append-to-body destroy-on-close>
          <template #header>
            <div class="cfg-dialog-head">
              <span class="sec-head">
                <el-icon class="sec-head-icon"><Document /></el-icon>
                配置文件 · {{ activeConfigTab || '-' }}
              </span>
              <div class="cfg-dialog-ops">
                <el-button size="small" :icon="CopyDocument" @click="copyConfigContent">复制</el-button>
                <el-button size="small" :icon="Download" @click="downloadConfig()">下载</el-button>
                <el-button size="small" :icon="FullScreen" @click="configFullscreen = !configFullscreen">
                  {{ configFullscreen ? '退出全屏' : '全屏' }}
                </el-button>
              </div>
            </div>
          </template>

          <el-tabs v-model="activeConfigTab" @tab-change="onConfigTabChange" class="cfg-dialog-tabs">
            <el-tab-pane v-for="cf in configFiles" :key="cf.name" :label="cf.name" :name="cf.name" />
          </el-tabs>

          <div class="cfg-search">
            <el-input v-model="configQuery" size="default" clearable placeholder="搜索配置项（支持键名、值或注释）"
              :prefix-icon="Search" @keyup.enter="stepMatch(1)" />
            <el-button-group>
              <el-button :icon="ArrowUp" :disabled="!configMatches.length" @click="stepMatch(-1)" />
              <el-button :icon="ArrowDown" :disabled="!configMatches.length" @click="stepMatch(1)" />
            </el-button-group>
            <span class="cfg-match-count" v-if="configQuery">
              {{ configMatches.length ? `${configMatchIdx + 1} / ${configMatches.length}` : '无匹配' }}
            </span>
          </div>

          <div class="cfg-code" :class="{ 'is-full': configFullscreen }" ref="cfgCodeEl" v-loading="configLoading">
            <div v-for="ln in configLines" :key="ln.n" class="cfg-line"
              :id="`cfg-l-${ln.n}`"
              :class="{ 'is-hit': ln.hit, 'is-current': ln.n === currentMatchLine }">
              <span class="cfg-ln">{{ ln.n }}</span>
              <span class="cfg-text" v-html="ln.html"></span>
            </div>
            <el-empty v-if="!configLines.length && !configLoading" description="无内容" :image-size="40" />
          </div>
        </el-dialog>
      </el-tab-pane>
      <!-- ================= 日志 ================= -->
      <el-tab-pane label="日志" name="logs">
        <div class="logc">
          <!-- ---------- 左栏：任务列表（顺序 / 失败分组） ---------- -->
          <div class="logc-left">
            <el-radio-group v-model="leftView" size="small" class="logc-left-switch">
              <el-radio-button value="seq">顺序列表</el-radio-button>
              <el-radio-button value="grp">失败类型分组</el-radio-button>
            </el-radio-group>

            <!-- 固定两个实例级日志源 -->
            <div class="logc-fixed">
              <div v-for="f in fixedLogSources" :key="f.value"
                :class="['logc-item', 'logc-item-fixed', { active: logSource === f.value }]"
                @click="selectLogSource(f.value)">
                <el-icon><Document /></el-icon><span>{{ f.label }}</span>
              </div>
            </div>

            <!-- 顺序列表 -->
            <template v-if="leftView === 'seq'">
              <div class="logc-filters">
                <el-input v-model="taskSearch" placeholder="搜索 task 编号…" clearable size="small" />
                <el-select v-model="categoryFilter" placeholder="状态" clearable size="small">
                  <el-option label="成功" value="success" />
                  <el-option label="服务端失败 S" value="S" />
                  <el-option label="任务侧失败 T" value="T" />
                  <el-option label="客户端失败 C" value="C" />
                  <el-option label="未分类 X" value="X" />
                </el-select>
              </div>
              <div class="logc-hint">共 {{ visibleTaskLogFiles.length }} 条</div>
              <el-scrollbar class="logc-list">
                <div v-for="f in pagedTaskFiles" :key="f"
                  :class="['logc-item', { active: logSource === f }]"
                  @click="selectLogSource(f)">
                  <span class="task-dot" :style="{ background: dotColor(taskStatusOf(f)) }"></span>
                  <div class="logc-item-main">
                    <div class="logc-item-name">{{ f.replace('.log', '') }}</div>
                    <div class="logc-item-sub">{{ taskStatusOf(f)?.code || taskStatusOf(f)?.status || '—' }}</div>
                  </div>
                  <el-tag v-if="markerText(taskStatusOf(f))" size="small"
                    :type="markerType(taskStatusOf(f))" disable-transitions>
                    {{ markerText(taskStatusOf(f)) }}
                  </el-tag>
                </div>
                <el-empty v-if="!visibleTaskLogFiles.length" description="无匹配任务" :image-size="40" />
              </el-scrollbar>
              <el-pagination v-if="visibleTaskLogFiles.length > taskListPageSize"
                v-model:current-page="taskListPage" :page-size="taskListPageSize"
                :total="visibleTaskLogFiles.length" layout="prev, pager, next"
                :pager-count="5" small background class="logc-pager" />
            </template>

            <!-- 失败类型分组 -->
            <template v-else>
              <el-scrollbar class="logc-list">
                <el-collapse v-model="expandedGroups" class="logc-groups">
                  <el-collapse-item v-for="grp in overview.error_tree || []" :key="grp.prefix"
                    :name="grp.prefix">
                    <template #title>
                      <span class="logc-grp-title">
                        <span class="task-dot" :style="{ background: dotColor({ category: grp.prefix }) }"></span>
                        <span class="logc-grp-name">{{ grp.label }}</span>
                        <el-tag size="small" type="info">{{ grp.count }}</el-tag>
                      </span>
                    </template>
                    <div v-for="c in grp.codes" :key="c.code" class="logc-code">
                      <div class="logc-code-head" @click="toggleCode(c.code)">
                        <el-icon class="logc-code-arrow">
                          <ArrowDown v-if="openedCode === c.code" /><ArrowRight v-else />
                        </el-icon>
                        <code class="err-code">{{ c.code }}</code>
                        <span class="logc-code-desc">{{ c.desc }}</span>
                        <el-tag size="small" type="info">{{ c.count }}</el-tag>
                      </div>
                      <div v-if="openedCode === c.code" class="logc-code-tasks">
                        <div v-if="codeTasksLoading" class="logc-hint">加载中…</div>
                        <div v-for="t in codeTasks" :key="t.task_idx"
                          :class="['logc-item', 'logc-item-sm',
                                   { active: logSource === `task-${t.task_idx}.log` }]"
                          @click="selectLogSource(`task-${t.task_idx}.log`)">
                          task-{{ t.task_idx }}
                        </div>
                        <el-pagination v-if="!codeTasksLoading && codeTasksTotal > codeTasksPageSize"
                          v-model:current-page="codeTasksPage" :page-size="codeTasksPageSize"
                          :total="codeTasksTotal" layout="prev, pager, next"
                          :pager-count="5" small background class="logc-pager"
                          @current-change="loadCodeTasks" />
                        <div v-if="!codeTasksLoading && !codeTasks.length" class="logc-hint">无数据</div>
                      </div>
                    </div>
                  </el-collapse-item>
                </el-collapse>
                <el-empty v-if="!(overview.error_tree || []).length" description="暂无失败任务" :image-size="40" />
              </el-scrollbar>
            </template>
          </div>

          <!-- ---------- 中栏：日志浏览区 ---------- -->
          <div class="logc-mid">
            <!-- 任务信息卡 -->
            <div class="logc-card">
              <div class="logc-card-main">
                <el-icon class="logc-card-icon"><Document /></el-icon>
                <div>
                  <div class="logc-card-name">{{ logSource.replace('.log', '') }}</div>
                  <div class="logc-card-sub">{{ eventSummary.config || '—' }}</div>
                </div>
              </div>
              <div class="logc-card-meta">
                <div><span class="logc-k">状态</span>
                  <el-tag v-if="eventSummary.status" size="small"
                    :type="eventSummary.status === '任务成功' ? 'success' : 'danger'">
                    {{ eventSummary.status }}
                  </el-tag>
                  <span v-else class="logc-v">—</span>
                </div>
                <div v-if="eventSummary.error_code"><span class="logc-k">错误码</span>
                  <el-tooltip :content="errCodeDesc(eventSummary.error_code)" placement="top">
                    <code class="err-code">{{ eventSummary.error_code }}</code>
                  </el-tooltip>
                </div>
                <div><span class="logc-k">开始</span><span class="logc-v">{{ eventSummary.started_at || '—' }}</span></div>
                <div><span class="logc-k">结束</span><span class="logc-v">{{ eventSummary.ended_at || '—' }}</span></div>
                <div><span class="logc-k">耗时</span><span class="logc-v">{{ fmtElapsed(eventSummary.total_elapsed) }}</span></div>
              </div>
            </div>

            <div class="toolbar">
              <el-input v-model="filterKeyword" placeholder="过滤关键词…" clearable style="width:240px" />
              <el-select v-model="logPageSize" style="width:130px" @change="onLogPageSizeChange">
                <el-option v-for="n in LOG_PAGE_SIZES" :key="n" :label="`每页 ${n} 行`" :value="n" />
              </el-select>
              <el-button @click="scrollToBottom">滚到底部</el-button>
              <el-button @click="loadLogs" :loading="logLoading">刷新</el-button>
              <el-button @click="clearLogs">清屏</el-button>
              <span class="logc-hint" style="margin-left:auto">
                已加载 {{ logLoadedPages }} 页{{ logHasMore ? '（上方还有更早日志）' : '（已到开头）' }}
              </span>
            </div>

            <div ref="logContainer" class="log-container" @scroll="onScroll">
              <!-- 顶部既是状态提示也是按钮：不想靠滚动触发时可以直接点 -->
              <div v-if="rawText !== null"
                :class="['log-more-hint', { 'log-more-btn': logHasMore && !loadingMore }]"
                @click="logHasMore && !loadingMore && loadMore()">
                <span v-if="loadingMore">加载中…</span>
                <span v-else-if="logHasMore">↑ 点击或上滑加载更早的 {{ logPageSize }} 行</span>
                <span v-else>已到日志开头</span>
              </div>
              <!-- 分段渲染：sandbox_detail 方框块默认折叠 -->
              <template v-if="rawText !== null">
                <template v-for="(seg, i) in logSegments" :key="i">
                  <pre v-if="seg.type === 'text'" class="log-raw">{{ seg.content }}</pre>
                  <div v-else class="log-box">
                    <div class="log-box-head" @click="toggleSeg(i)">
                      <el-icon><ArrowDown v-if="segExpanded(i)" /><ArrowRight v-else /></el-icon>
                      <span class="log-box-title">{{ seg.title }}</span>
                      <span class="log-box-lines">{{ seg.lineCount }} 行</span>
                    </div>
                    <pre v-if="segExpanded(i)" class="log-raw log-box-body">{{ seg.content }}</pre>
                  </div>
                </template>
                <div v-if="!logSegments.length" class="logc-hint" style="padding:20px;text-align:center">
                  暂无日志
                </div>
              </template>
              <template v-else>
                <div v-for="(line, idx) in filteredLines" :key="idx"
                  :class="['log-line', lineClass(line)]">{{ line }}</div>
                <div v-if="!filteredLines.length" style="color:#666;padding:20px;text-align:center">
                  暂无日志
                </div>
              </template>
            </div>
          </div>

          <!-- ---------- 右栏：关键事件 ---------- -->
          <div class="logc-right">
            <div class="logc-right-head">
              <el-icon><Clock /></el-icon><span>关键事件 / 错误位置</span>
            </div>
            <el-scrollbar class="logc-timeline">
              <div v-if="eventsLoading" class="logc-hint">加载中…</div>
              <div v-for="(ev, i) in logEvents" :key="i"
                :class="['tl-item', { 'tl-error': ev.kind === 'error',
                                      'tl-clickable': ev.line }]"
                @click="jumpToLine(ev.line)">
                <span class="tl-dot" :style="{ background: eventColor(ev) }"></span>
                <div class="tl-body">
                  <div class="tl-title">
                    {{ ev.title }}
                    <el-tooltip v-if="ev.code" :content="errCodeDesc(ev.code)" placement="left">
                      <code class="err-code">{{ ev.code }}</code>
                    </el-tooltip>
                  </div>
                  <div class="tl-ts">{{ (ev.ts || '').slice(11) }}
                    <span v-if="ev.elapsed" class="tl-elapsed">· {{ fmtElapsed(ev.elapsed) }}</span>
                  </div>
                  <div v-if="ev.detail" class="tl-detail">{{ ev.detail }}</div>
                </div>
              </div>
              <div v-if="eventsTruncated" class="logc-hint">
                事件过多，仅显示前 {{ logEvents.length }} 条
              </div>
              <el-empty v-if="!eventsLoading && !logEvents.length"
                description="无关键事件" :image-size="40" />
            </el-scrollbar>
          </div>
        </div>
      </el-tab-pane>
      <!-- ================= 输出 ================= -->
      <el-tab-pane label="输出" name="outputs">
        <!-- 轨迹保存路径 -->
        <el-card v-if="trajSaveFullPath" style="margin-bottom:16px">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="font-size:13px;font-weight:600;color:var(--text-secondary)">轨迹保存路径</span>
            <span style="font-family:monospace;font-size:13px;word-break:break-all">{{ trajSaveFullPath }}</span>
            <el-button size="small" text type="primary" @click="copyTrajPath">复制</el-button>
          </div>
        </el-card>

        <!-- 统计面板（浅层漏斗） -->
        <el-card style="margin-bottom:16px" v-loading="shallowLoading">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>会话漏斗</span>
              <div>
                <el-button size="small" :loading="exportingFunnel" @click="exportFunnel">导出 Excel</el-button>
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
              <span>
                已定级 {{ shallowProgress.approved }} / {{ shallowProgress.total }} 个会话
                <span v-if="shallowProgress.failed > 0" style="color:#f56c6c;margin-left:8px">
                  · 失败 {{ shallowProgress.failed }}
                </span>
                <span v-if="shallowProgress.undone > 0" style="margin-left:8px">
                  · 未分析 {{ shallowProgress.undone }}
                </span>
              </span>
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
              <!-- 已处理但判失败：不算 L0-L3，以前完全不出现在漏斗里，
                   导致「跑了但全失败」的实例看起来像一条都没跑 -->
              <div class="funnel-bar-col">
                <span class="funnel-bar-count">{{ shallowSummary.failed || '' }}</span>
                <div class="funnel-bar-track">
                  <div class="funnel-bar failed-bar"
                    :style="{ height: failedPct + '%' }" />
                </div>
                <span class="funnel-bar-label">失败</span>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无会话数据（运行中会自动采集）" :image-size="40" />
        </el-card>

        <!-- 模型配置 / 用户模拟配置信息面板（任务创建时的连接信息 + 代理映射 + 导出 Excel） -->
        <el-card style="margin-bottom:16px" v-loading="proxyLoading">
          <template #header>
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span>模型配置 / 用户模拟配置 <span style="color:var(--text-muted);font-weight:400;font-size:12px">任务创建时的连接信息</span></span>
              <el-button size="small" :loading="exportingProxy" @click="exportProxyConfig">导出 Excel</el-button>
            </div>
          </template>
          <el-table v-if="proxyRows.length" :data="proxyRows" size="small" style="width:100%">
            <el-table-column prop="role" label="名称" min-width="100">
              <template #default="{ row }">
                <span style="font-weight:600">{{ row.role }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="baseurl" label="baseurl" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                <span style="font-family:monospace;font-size:12px">{{ row.baseurl || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="key_suffix" label="key 后四位" min-width="90" align="center">
              <template #default="{ row }">
                <span style="font-family:monospace">{{ row.key_suffix || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="proxy_name" label="代理名称" min-width="110" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.proxy_name" size="small" type="success" effect="plain">{{ row.proxy_name }}</el-tag>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="model" label="模型" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">
                <span style="font-size:12px">{{ row.model || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="浏览" min-width="80" align="center">
              <template #default="{ row }">
                <el-button size="small" text type="primary" :disabled="!row.key_suffix"
                  @click="browseProxyExport(row)">浏览</el-button>
              </template>
            </el-table-column>
            <el-table-column label="导出" min-width="80" align="center">
              <template #default="{ row }">
                <el-button size="small" text type="primary" :disabled="!row.key_suffix"
                  @click="openExportDialog(row)">导出</el-button>
              </template>
            </el-table-column>
            <el-table-column prop="export_status" label="导出状态" min-width="110" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.export_status === 'queued' || row.export_status === 'running'"
                  size="small" type="primary" effect="plain">正在进行中</el-tag>
                <el-tag v-else-if="row.export_status === 'success'" size="small" type="success" effect="plain">完成</el-tag>
                <el-tag v-else-if="row.export_status === 'failed'" size="small" type="danger" effect="plain">导出失败</el-tag>
                <span v-else style="color:#c0c4cc">未导出</span>
              </template>
            </el-table-column>
            <el-table-column prop="session_path" label="导出路径" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row.session_path" style="font-family:monospace;font-size:11px">{{ row.session_path }}</span>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
            <el-table-column prop="total_sessions" label="导出的会话数" min-width="90" align="center">
              <template #default="{ row }">
                <span>{{ row.total_sessions ?? '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="启动时间" min-width="150" align="right">
              <template #default>
                <span style="color:var(--text-muted);font-size:12px">{{ fmtProxyTime(proxyStartedAt) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="结束时间" min-width="150" align="right">
              <template #default>
                <span style="color:var(--text-muted);font-size:12px">{{ proxyStoppedAt ? fmtProxyTime(proxyStoppedAt) : '（未结束）' }}</span>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="暂无模型配置信息" :image-size="40" />
        </el-card>

        <!-- 对话导出弹窗：填入导出名称（默认任务名）+ 导出方式下拉 → 提交 -->
        <el-dialog v-model="exportDialogVisible" title="对话导出" width="480px" append-to-body>
          <el-form label-width="90px" size="small">
            <el-form-item label="角色">
              <span style="font-weight:600">{{ exportDialogRole }}</span>
            </el-form-item>
            <el-form-item label="导出名称">
              <el-input v-model="exportDialogName" placeholder="导出名称（默认任务名，建议纯英文）" maxlength="64" />
            </el-form-item>
            <el-form-item label="导出方式">
              <!-- 选项来自导出服务实时接口；label 是中文简称，desc 是括号里的长说明 -->
              <el-select v-model="exportDialogMode" style="width:100%"
                         no-data-text="导出方式列表加载中或不可用">
                <el-option v-for="m in exportModes" :key="m.value" :label="m.label" :value="m.value">
                  <span>{{ m.label }}</span>
                  <span v-if="m.desc" style="float:right;color:var(--text-muted);font-size:12px;margin-left:12px">
                    {{ m.value }}
                  </span>
                </el-option>
              </el-select>
              <div v-if="currentModeDesc" style="color:var(--text-muted);font-size:12px;line-height:1.5;margin-top:4px">
                {{ currentModeDesc }}
              </div>
            </el-form-item>
          </el-form>
          <template #footer>
            <el-button @click="exportDialogVisible = false">取消</el-button>
            <!-- 模式列表没拉到就不让提交：此时下拉是空的，提交等于发一个用户没看见的 mode -->
            <el-button type="primary" :loading="submittingExport"
                       :disabled="!exportModes.length" @click="submitProxyExport">提交</el-button>
          </template>
        </el-dialog>

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
              <!-- 交付物下载步骤：浅层已完成才可进入；进度取自产物下载队列 -->
              <div :style="{ padding: '6px 14px', borderRadius: '6px', background: artifactActive ? 'var(--el-color-primary-light-9)' : 'transparent', border: '1px solid ' + (artifactActive ? 'var(--el-color-primary-light-5)' : 'var(--el-border-color-lighter)') }">
                <div :style="{ fontSize: '12px', color: 'var(--text-muted)' }">交付物下载</div>
                <div :style="{ fontSize: '14px', fontWeight: '600', color: artifactActive ? 'var(--el-color-primary)' : 'var(--text-muted)' }">{{ artifactStepLabel }}</div>
              </div>
            </div>
            <div style="flex:1;min-width:220px;font-size:12px;color:var(--text-muted)">
              <!-- 浅层分级进度条：以「已处理 / 总会话」反映完成度，undone→0 即 100%。
                   原来那段 stageMeta.desc 文字说明（running/preparing…无需手动提交）已去掉，
                   进度条本身更直观，也能看出还要多久跑完。 -->
              <div v-if="shallowProgress && shallowProgress.total > 0">
                <el-progress :percentage="shallowProgressPct" :stroke-width="8"
                  :color="shallowProgressColor" :format="shallowProgressText" />
                <div style="margin-top:4px">
                  正在分析：已定级 <b>{{ shallowProgress.approved }}</b> / {{ shallowProgress.total }}
                  <span v-if="shallowProgress.failed > 0">· 失败 <b>{{ shallowProgress.failed }}</b></span>
                  <span v-if="shallowProgress.undone > 0">· 剩余 <b>{{ shallowProgress.undone }}</b> 未分析</span>
                </div>
              </div>
              <!-- 交付物下载进度条 -->
              <div v-if="artifactProgress.total > 0" style="margin-top:8px">
                <el-progress :percentage="artifactProgressPct" :stroke-width="8"
                  :color="artifactProgressColor" :format="artifactProgressText" />
                <div style="margin-top:4px">
                  交付物已下载 <b>{{ artifactProgress.done }}</b> / {{ artifactProgress.total }}
                  <span v-if="artifactProgress.downloading > 0">· 下载中 <b>{{ artifactProgress.downloading }}</b></span>
                  <span v-if="artifactProgress.queued > 0">· 待下载 <b>{{ artifactProgress.queued }}</b></span>
                  <span v-if="artifactProgress.failed > 0" style="color:#f56c6c">· 失败 <b>{{ artifactProgress.failed }}</b></span>
                </div>
              </div>
            </div>
            <!-- 手动提交浅层：ended 实例且仍有缺口时显示 -->
            <el-button v-if="authStore.isOperator && pipelineStage === 'ungraded'" type="primary" size="small"
              :loading="submittingShallow" @click="submitShallow">提交浅层分析</el-button>
            <!-- running 实例：自动会由 tracker 增量发现，但用户主动点「输出」时
                 也应该能立刻插队（priority=100），不必等下一轮增量 -->
            <el-button v-else-if="authStore.isOperator && pipelineStage === 'grading' && shallowProgress && shallowProgress.undone > 0"
              size="small" :loading="submittingShallow" @click="submitShallow">立即分析未定级会话</el-button>
            <el-button v-else-if="pipelineStage === 'queued' || pipelineStage === 'grading'" size="small" disabled>浅层分析处理中…</el-button>
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
                  <el-option label="未分级" value="fail" />
                </el-select>
                <!-- 三个搜索框各管一件事，不混在一起：用户看到的字段就是他能搜的字段。
                     名称/编号走 DB 列（毫秒级）；内容走 task_records.query_text
                     （由 shallow-query worker 回填，也是 SQL LIKE，毫秒级）。 -->
                <el-input v-model="taskKeyword" size="small" clearable
                  placeholder="任务名称" style="width:150px">
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
                <el-input v-model="taskIdxKeyword" size="small" clearable
                  placeholder="任务编号 task-5921" style="width:160px">
                  <template #prefix><el-icon><Search /></el-icon></template>
                </el-input>
                <el-input v-model="taskContentKeyword" size="small" clearable
                  placeholder="内容，如 帮我画个花" style="width:190px">
                  <template #prefix><el-icon><Document /></el-icon></template>
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
            <!-- 任务编号：traj_name(workspace_builder_xxx) ↔ task-N 的对应关系。
                 排查时要去 logs/task-N.log 看日志，而列表里只有哈希名，
                 之前得手工到 DB 里查 task_idx。task_idx 后端早就返回了（_SORTABLE
                 白名单里也有），这里直接展示并支持排序。 -->
            <el-table-column prop="task_idx" label="任务编号" min-width="110"
              align="center" sortable="custom">
              <template #default="{ row }">
                <span v-if="row.task_idx !== null && row.task_idx !== undefined"
                  style="font-family:monospace;font-size:12px"
                  :title="`日志文件: logs/task-${row.task_idx}.log`">task-{{ row.task_idx }}</span>
                <span v-else style="color:var(--text-muted)">—</span>
              </template>
            </el-table-column>
            <!-- 任务配置：点开看该 task 的输入 json 原文（task_input_path 下每任务一个）。
                 列表里只有哈希名，想知道这个任务具体在跑什么，以前得登机器翻目录。 -->
            <el-table-column label="任务配置" min-width="90" align="center">
              <template #default="{ row }">
                <el-button link type="primary" size="small"
                  @click="openTaskConfig(row)">查看</el-button>
              </template>
            </el-table-column>
            <el-table-column label="等级" min-width="90" align="center">
              <template #default="{ row }">
                <el-tag size="small" :color="levelTagBg(row.traj_level)" style="color:#fff;border:none"
                  v-if="row.traj_level && row.traj_level !== 'failed'">
                  {{ row.traj_level }}
                </el-tag>
                <!-- failed = 浅层已处理但判不出等级（无 tsr / 任务异常）。
                     以前和「未评估」都渲染成灰色 info 标签，看不出「已跑过」，
                     用户会以为一条都没分析。用 danger 色区分。 -->
                <el-tag v-else-if="row.traj_level === 'failed'" size="small" type="danger" effect="plain">
                  失败
                </el-tag>
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
            <el-table-column label="操作" width="130" align="center" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openTaskDetail(row)">详情</el-button>
                <el-button link type="primary" size="small" @click="openArtifacts(row)">交付物</el-button>
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

      <!-- ================= 工作区（沙箱产物浏览） =================
           懒挂载：只有切到这个 tab 才创建组件，避免每次进详情页都拉 OBS 目录。
           每展开一层目录才发一次 obsutil，命中 DB 缓存则为 0 次。 -->
      <el-tab-pane label="沙箱交付物" name="artifacts">
        <ArtifactWorkspace v-if="activeTab === 'artifacts'" :instance-id="$route.params.id"
          :initial-task="artifactTask" />
      </el-tab-pane>

      <!-- ================= 队列信息 ================= -->
      <el-tab-pane v-if="inst.queue_id" label="队列信息" name="queue">
        <div class="glass-card" style="padding:20px">
          <el-descriptions :column="3" border>
            <el-descriptions-item label="所属队列">
              <el-tag size="small" effect="plain">{{ inst.queue_id }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="队列并发量">{{ queueInfo.max_concurrent }}</el-descriptions-item>
            <el-descriptions-item label="当前状态">
              <el-tag :type="queueStatusType(inst.status)" size="small">{{ statusText(inst.status) }}</el-tag>
              <el-tag v-if="inst.status === 'queued' && myTaskJumped" type="danger" size="small"
                effect="dark" style="margin-left:8px">已插队</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="队列位置">
              {{ inst.status === 'queued' ? `${myQueueRank} (等待中)` : '-' }}
            </el-descriptions-item>
            <el-descriptions-item label="前方等待任务数">
              {{ inst.status === 'queued' ? Math.max(0, myQueueRank - 1) : 0 }}
            </el-descriptions-item>
            <el-descriptions-item label="预计开始时间">
              {{ myEstimatedStart || '-' }}
            </el-descriptions-item>
          </el-descriptions>

          <div class="queue-actions">
            <el-button v-if="inst.status === 'queued' && !myTaskJumped" size="small" type="warning"
              :loading="jumping" @click="jumpMyTask">插队</el-button>
            <el-button v-if="inst.status === 'queued' && myTaskJumped" size="small"
              :loading="jumping" @click="cancelMyJump">取消插队</el-button>
            <span class="queue-actions-hint">
              插队只调整放行顺序：下一个空出来的槽位优先给插队任务，不突破队列并发上限。
            </span>
          </div>

          <el-alert type="info" :closable="false" show-icon style="margin-top:16px"
            title="停止：退出当前队列，不再参与队列排队；启动：重新加入队列排队。" />

          <div style="margin-top:20px;font-weight:600">当前队列任务列表</div>
          <el-table :data="queueTasks" v-loading="loadingQueueTasks" size="small" style="width:100%;margin-top:10px">
            <el-table-column prop="queue_position" label="入队序号" width="90" align="center" />
            <el-table-column label="任务名称" min-width="160" show-overflow-tooltip>
              <template #default="{row}">
                <span>{{ row.name }}</span>
                <el-tag v-if="row.is_jumped" type="danger" size="small" effect="plain"
                  style="margin-left:6px">插队</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="100">
              <template #default="{row}">
                <el-tag :type="queueStatusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{row}"><span class="mono-num">{{ fmtTime(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="96" align="center">
              <template #default="{row}">
                <el-button v-if="row.status === 'queued' && !row.is_jumped" link type="warning"
                  size="small" @click="jumpTask(row)">插队</el-button>
                <el-button v-else-if="row.status === 'queued' && row.is_jumped" link
                  size="small" @click="cancelJumpTask(row)">取消插队</el-button>
                <span v-else style="color:#c0c4cc">—</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
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
            <el-tag size="small" :style="harnessTagStyle(detailData.harness)">{{ harnessLabel(detailData.harness) }}</el-tag>
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
            <TrajBlocks v-if="assistantBlocks.length" :blocks="assistantBlocks" />
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
            <!-- 原来这里是「无折叠、不显示 args」的简化版，与上面「轨迹」tab 各写一份。
                 统一到 TrajBlocks 后 evaluator 轨迹也能折叠、也能看到工具入参。 -->
            <TrajBlocks v-if="evaluatorBlocks.length" :blocks="evaluatorBlocks"
              style="margin-top:12px" />
            <el-empty v-else description="无 evaluator 轨迹" :image-size="40" style="margin-top:12px" />
          </el-tab-pane>
        </el-tabs>
      </template>
      <template v-else>
        <el-empty description="详情不可用" :image-size="40">
          <el-button v-if="authStore.isOperator" type="primary" size="small"
            :loading="detailLoading" @click="retryDownloadDetail">下载交付物后重试</el-button>
        </el-empty>
      </template>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button type="primary" @click="openArtifacts({ traj_name: detailTrajName })">
          查看交付物
        </el-button>
      </template>
    </el-dialog>

    <!-- 任务配置查看：该 task 的输入 json 原文（key 已由后端脱敏） -->
    <el-dialog v-model="taskCfgVisible"
      :title="`任务配置 · ${taskCfgName || ''}`" width="820px" append-to-body>
      <div v-if="taskCfgLoading" style="padding:24px;text-align:center;color:var(--text-muted)">
        加载中…
      </div>
      <div v-else-if="taskCfgError" style="padding:16px">
        <el-alert :title="taskCfgError" type="error" :closable="false" show-icon />
      </div>
      <template v-else>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:12px;color:var(--text-muted);word-break:break-all">
            {{ taskCfgPath }}
          </span>
          <el-button link type="primary" size="small" @click="copyTaskCfg">复制</el-button>
        </div>
        <pre class="task-cfg-pre">{{ taskCfgText }}</pre>
      </template>
    </el-dialog>
  </div>
</template>
<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowDown, ArrowRight, ArrowUp, Box, CircleCheckFilled, CircleCloseFilled, Clock,
  CopyDocument, Cpu, DataLine, Document, Download, Files, FolderOpened, FullScreen,
  Loading, Postcard, Search, Setting, Timer, TrendCharts, User, VideoPlay, View,
} from '@element-plus/icons-vue'
import api from '../api'
import ArtifactWorkspace from '../components/ArtifactWorkspace.vue'
import TrajBlocks from '../components/TrajBlocks.vue'
import EChart from '../components/EChart.vue'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'

const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const route = useRoute()
const router = useRouter()
const id = route.params.id

// ============ 共享 / tab 管理 ============
// queue tab 由 v-if="inst.queue_id" 控制是否出现，但 URL 里带 ?tab=queue 时
// 必须先通过这里的白名单校验，否则会被回落成 config —— 用户点重定向过来的
// 队列链接只会落到「配置信息」，看起来像「队列信息没了」。
const VALID_TABS = ['config', 'logs', 'outputs', 'artifacts', 'queue']
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
  // 日志 tab 的「失败类型分组」用的是 overview.error_tree，而 overview 默认只在
  // config tab 拉。直接进 ?tab=logs 时它还是空的，这里补一次。
  else if (tab === 'logs') {
    loadTaskLogFiles(); loadTaskStatusMap(); loadLogs(); loadLogEvents()
    if (!(overview.value.error_tree || []).length) loadData(true)
  }
  else if (tab === 'outputs') { loadShallow(false); loadArtifactProgress(); loadProxyConfig(); setTimeout(refreshProxyExportStatus, 500) }
  // 队列 tab 不单独拉：loadData() 末尾会调 loadQueueInfo()。但切到该 tab 时
  // 不能干等下一次 10s 轮询，这里直接触发一次，插队按钮/名次才不会延迟 10 秒才出来。
  else if (tab === 'queue') { loadQueueInfo() }
}

// ============ 配置信息 tab ============
const overview = ref({ total: 0, completed: 0, failed: 0, running: 0, pending: 0, success_rate: 0, error_breakdown: {}, error_tree: [] })
const prepareInfo = ref({ downloaded_configs: 0 })
const createParams = ref({})
const expandedPanels = ref([])
const expandedErrorGroups = ref([])
let timer = null

// ── 顶部概览卡 ───────────────────────────────────────────────────────────
// 颜色与下面环形图的状态语义色同源（成功绿 / 失败红 / 运行中橙 / 中性）。
// tint 是同色的浅底，只给图标块用，不承担含义。
const statCards = computed(() => {
  const o = overview.value
  return [
    { key: 'total', label: '总任务', value: o.total, icon: Files, color: '#475569', tint: '#eef2f7' },
    { key: 'completed', label: '已完成', value: o.completed, icon: CircleCheckFilled, color: '#0ca30c', tint: '#e8f6e8' },
    { key: 'failed', label: '失败', value: o.failed, icon: CircleCloseFilled, color: o.failed > 0 ? '#d03b3b' : '#475569', tint: '#fdecec' },
    { key: 'running', label: '运行中', value: o.running, icon: VideoPlay, color: '#c98500', tint: '#fdf3e0' },
    { key: 'pending', label: '队列中', value: o.pending, icon: Timer, color: '#475569', tint: '#eef2f7' },
    { key: 'rate', label: '成功率', value: `${o.success_rate}%`, icon: DataLine, color: '#6366f1', tint: '#eeeefc' },
  ]
})

// ── 任务执行情况环形图 ────────────────────────────────────────────────────
// 四类最终归属：成功 / 失败 / 异常 / 未执行。颜色是状态语义色（good / critical /
// warning / 中性灰），不随数据大小或排序变化，同一类永远是同一个色。
// 「未执行」用中性灰：它不是一种结果，只是还没轮到。
const OUTCOME_META = [
  { key: '任务成功', label: '任务成功', color: '#0ca30c' },
  { key: '任务失败', label: '任务失败', color: '#d03b3b' },
  { key: '任务异常', label: '任务异常', color: '#c98500' },
  { key: '未执行', label: '未执行', color: '#8a8a85' },
]

const outcomeItems = computed(() => {
  // backend 的 error_breakdown 里带 └ 前缀的子行是异常分类明细，
  // 已经由下面的折叠树承担，环形图只取四个顶层项。
  const bd = overview.value.error_breakdown || {}
  const total = OUTCOME_META.reduce((n, m) => n + (bd[m.key] || 0), 0)
  return OUTCOME_META
    .map(m => {
      const value = bd[m.key] || 0
      return { ...m, value, pct: total ? Math.round(value / total * 1000) / 10 : 0 }
    })
    .filter(o => o.value > 0)
})

const outcomeTotal = computed(() => outcomeItems.value.reduce((n, o) => n + o.value, 0))

const outcomeOption = computed(() => {
  const items = outcomeItems.value
  return {
    tooltip: {
      trigger: 'item',
      formatter: (p) => `${p.name}<br/><b>${p.value}</b> 个任务（${p.percent}%）`,
    },
    // 不在 echarts 里放 legend：四项固定在右侧自绘，数值和百分比能对齐成列，
    // echarts 的 legend 只能排成一串。
    series: [{
      type: 'pie',
      radius: ['58%', '82%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      // 2px 的表面色描边 = 段与段之间的留白，避免相邻色块直接贴边
      itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 2 },
      label: { show: false },
      // 悬停不画 echarts 自己的标签：饼图的 emphasis label 默认落在扇区外侧，
      // 小扇区上会直接压在圆环上（截图里「任务成功 3」就是这么糊上去的）。
      // 中心总数由 HTML 叠加层渲染（见 .donut-center），悬停信息走 tooltip。
      emphasis: { scale: false, label: { show: false } },
      labelLine: { show: false },
      data: items.map(o => ({
        name: o.label,
        value: o.value,
        itemStyle: { color: o.color },
      })),
    }],
  }
})

function statusColor(s) { return { running: 'success', preparing: 'warning', queued: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', failed: 'danger', created: '' }[s] || '' }
function statusText(s) { return { running: '运行中', preparing: '准备中', queued: '排队中', completed: '已完成', finished: '已结束', stopped: '已停止', failed: '失败', created: '待启动' }[s] || s }
function queueStatusType(s) {
  return { running: 'success', preparing: 'warning', queued: 'warning', completed: 'info', finished: 'warning', stopped: 'danger', failed: 'danger' }[s] || ''
}
function fmtTime(t) {
  if (!t) return '-'
  const d = new Date(t)
  if (isNaN(d)) return String(t).slice(0, 16).replace('T', ' ')
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---- 队列信息 tab ----
const queueInfo = ref({})
const queueTasks = ref([])
const loadingQueueTasks = ref(false)
const jumping = ref(false)
const myEstimatedStart = computed(() => {
  const me = queueTasks.value.find(t => t.id === inst.value.id)
  return me?.estimated_start_at ? fmtTime(me.estimated_start_at) : ''
})

// 本实例是否已插队（用队列详情里自己那一行的派生字段，避免另发一次请求）
const myTaskJumped = computed(() => {
  const me = queueTasks.value.find(t => t.id === inst.value.id)
  return !!me?.is_jumped
})

// 排队名次：本实例在同队列 queued 任务里排第几（不是绝对 queue_position）。
// queue_position 只增不减、会有空洞，直接显示会误导。
// 后端已按完整放行顺序（priority DESC, position ASC）返回，这里只需按 queued 过滤后
// 取下标；**不能**再按 position 重排，否则插队任务会被排到名次后面，与调度器不一致。
const myQueueRank = computed(() => {
  const waiting = queueTasks.value.filter(t => t.status === 'queued')
  return waiting.findIndex(t => t.id === inst.value.id) + 1
})

async function jumpTask(row) {
  jumping.value = true
  try {
    await api.post(`/queues/${encodeURIComponent(inst.value.queue_id)}/tasks/${encodeURIComponent(row.id)}/jump`)
    ElMessage.success('已插队，将优先放行')
    await loadQueueInfo()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '插队失败')
  } finally { jumping.value = false }
}

async function cancelJumpTask(row) {
  jumping.value = true
  try {
    await api.post(`/queues/${encodeURIComponent(inst.value.queue_id)}/tasks/${encodeURIComponent(row.id)}/cancel-jump`)
    ElMessage.success('已取消插队')
    await loadQueueInfo()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '取消插队失败')
  } finally { jumping.value = false }
}

function jumpMyTask() {
  const me = queueTasks.value.find(t => t.id === inst.value.id)
  return jumpTask(me || { id: inst.value.id })
}

function cancelMyJump() {
  const me = queueTasks.value.find(t => t.id === inst.value.id)
  return cancelJumpTask(me || { id: inst.value.id })
}

async function loadQueueInfo() {
  if (!inst.value.queue_id) return
  loadingQueueTasks.value = true
  try {
    const [qs, tasks] = await Promise.all([
      api.get('/queues'),
      api.get(`/queues/${encodeURIComponent(inst.value.queue_id)}/tasks`),
    ])
    queueInfo.value = qs.find(q => q.id === inst.value.queue_id) || {}
    queueTasks.value = tasks
  } catch {
    queueTasks.value = []
  } finally { loadingQueueTasks.value = false }
}
function harnessColor(t) { return harnessStore.color(t) }
function harnessTagStyle(t) { return harnessStore.tagStyle(t) }
function harnessLabel(t) { return harnessStore.label(t) }
function formatDuration(seconds) {
  if (seconds == null) return ''
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h}h${m}m`
  return `${m}min`
}
// 兜底遮罩：后端 /create-params 下发的 key 已经是掩码（mask_secret 幂等，
// 再遮一次不变形）。这里只防「某个字段漏了后端脱敏」的情况，故短值也整体打码，
// 与后端 services/secrets.py 的规则对齐，不留短 key 泄漏口。
function maskKey(key) {
  if (!key) return '-'
  if (key.length <= 8) return '****'
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

// 轨迹保存路径的完整 OBS 路径（含桶名）。
const trajSaveFullPath = computed(() => {
  const cp = createParams.value
  const rawPath = cp.traj_save_path
  if (!rawPath) return ''
  if (String(rawPath).startsWith('obs://')) return rawPath
  const bucket = cp.traj_save_bucket || cp.obs_bucket
  if (!bucket) return rawPath
  return `${String(bucket).replace(/\/+$/, '')}/${String(rawPath).replace(/^\/+/, '')}`
})

// 折叠面板收起时的摘要：每组挑 2~3 个最能定位配置的字段拼一行，
// 省得为了确认「用的哪个模型/哪个桶」把五个面板挨个展开。
// 只取已有值的字段，全空时返回空串（模板里就什么都不显示）。
const peek = computed(() => {
  const cp = createParams.value
  const join = (...parts) => parts.filter(Boolean).join(' · ')
  const short = (v, n = 28) => {
    const s = String(v ?? '')
    return s.length > n ? s.slice(0, n) + '…' : s
  }

  const agentCount = cp.agents?.length
  const agentModel = cp.agents?.[0]?.model || cp.user_proxy_model_name

  return {
    basic: join(
      cp.harness_type && `环境: ${harnessLabel(cp.harness_type)}`,
      cp.task_name && `项目: ${short(cp.task_name)}`,
      cp.harness_config_name && `配置: ${short(cp.harness_config_name)}`,
    ),
    obs: join(
      cp.obs_bucket && `数据桶: ${short(cp.obs_bucket)}`,
      cp.default_skills && `技能: ${short(cp.default_skills, 20)}`,
    ),
    model: join(
      cp.model_id && `模型: ${short(cp.model_id)}`,
      cp.model_api_type && `类型: ${cp.model_api_type}`,
    ),
    agents: join(
      agentCount ? `Agent 数: ${agentCount}` : (agentModel ? '单模型' : ''),
      agentModel && `模拟模型: ${short(agentModel)}`,
    ),
    advanced: join(
      cp.sandbox_config_name && `沙箱: ${short(cp.sandbox_config_name, 20)}`,
      cp.total_num ? `任务数: ${cp.total_num}` : '任务数: 不限',
      inst.value.concurrent_num && `并发: ${inst.value.concurrent_num}`,
    ),
  }
})

async function copyTrajPath() {
  try {
    await navigator.clipboard.writeText(trajSaveFullPath.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择复制')
  }
}

async function loadData(withOverview = true) {
  try {
    const reqs = [api.get(`/instances/${id}`)]
    if (withOverview) reqs.push(api.get(`/instances/${id}/overview`))
    const [i, o] = await Promise.all(reqs)
    inst.value = i
    if (o) overview.value = o
  } catch {}
  // 准备中：拉下载进度（已下载多少个 config）
  if (inst.value.status === 'preparing') {
    try { prepareInfo.value = await api.get(`/instances/${id}/prepare-progress`) } catch {}
  }
  loadQueueInfo()
}
async function loadCreateParams() { try { createParams.value = await api.get(`/instances/${id}/create-params`) } catch {} }
async function startInstance() { await api.post(`/instances/${id}/start`); ElMessage.success('已启动'); loadData() }
async function stopInstance() { await ElMessageBox.confirm('确认停止？', '提示', { type: 'warning' }); await api.post(`/instances/${id}/stop`); ElMessage.success('已停止'); loadData() }
async function retryFailed() { router.push(`/create?copy_from=${id}&copy_complete_from=${id}`) }

// ============ 配置文件查看 ============
// 卡片上只有摘要，正文在弹窗里渲染：按行拆开配号、做 yaml/json 轻量着色，
// 并支持搜索跳转。内容不再随页面常驻，首次打开弹窗时才拉正文。
const configFiles = ref([])
const configLoading = ref(false)
const activeConfigTab = ref('')
const configContent = ref('')
const configViewerOpen = ref(false)
const configFullscreen = ref(false)
const configQuery = ref('')
const configMatchIdx = ref(0)
const cfgCodeEl = ref(null)

async function loadConfigFiles() {
  configLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/configs`)
    configFiles.value = res.files || []
    // 只定位默认文件，正文等打开弹窗再拉——列表页不需要几百行 yaml。
    if (configFiles.value.length && !activeConfigTab.value) {
      activeConfigTab.value = configFiles.value[0].name
    }
  } finally { configLoading.value = false }
}

async function loadConfigContent(filename) {
  if (!filename) return
  configLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/configs/${filename}`)
    configContent.value = res.content
  } catch { configContent.value = '' } finally { configLoading.value = false }
}

// 卡片摘要里的「最后更新」取所有配置文件里最新的 mtime。
const configLastModified = computed(() => {
  const ts = configFiles.value.map(f => f.modified_at).filter(Boolean).sort()
  return ts.length ? ts[ts.length - 1].replace('T', ' ').slice(0, 19) : ''
})

async function openConfigViewer(filename) {
  const target = filename || activeConfigTab.value || configFiles.value[0]?.name
  if (!target) return
  configViewerOpen.value = true
  if (target !== activeConfigTab.value || !configContent.value) {
    activeConfigTab.value = target
    configQuery.value = ''
    await loadConfigContent(target)
  }
}

function onConfigTabChange(name) {
  configQuery.value = ''
  configMatchIdx.value = 0
  loadConfigContent(name)
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

// 轻量着色：只认 yaml/json 里「键名 / 字符串 / 数字与布尔 / 注释」四类，
// 不引语法高亮库——配置文件就这两种格式，正则够用且没有额外依赖。
function highlightConfigLine(raw) {
  const text = escapeHtml(raw)
  const comment = text.match(/(^|\s)#.*$/)
  let code = text, tail = ''
  if (comment) {
    const at = comment.index + comment[1].length
    code = text.slice(0, at)
    tail = `<span class="t-cmt">${text.slice(at)}</span>`
  }
  code = code
    .replace(/^(\s*-?\s*)("?[\w.-]+"?)(\s*:)/, '$1<span class="t-key">$2</span>$3')
    .replace(/:(\s+)('[^']*'|"[^"]*")/g, ':$1<span class="t-str">$2</span>')
    .replace(/:(\s+)(true|false|null|-?\d+(?:\.\d+)?)(\s*,?)$/g, ':$1<span class="t-num">$2</span>$3')
  return code + tail
}

const configLines = computed(() => {
  if (!configContent.value) return []
  const q = configQuery.value.trim().toLowerCase()
  return configContent.value.split('\n').map((raw, i) => ({
    n: i + 1,
    hit: !!q && raw.toLowerCase().includes(q),
    html: highlightConfigLine(raw),
  }))
})

const configMatches = computed(() => configLines.value.filter(l => l.hit).map(l => l.n))
const currentMatchLine = computed(() => configMatches.value[configMatchIdx.value] ?? -1)

// 搜索词一变，命中集就变了，游标必须归零，否则会停在越界的下标上。
watch(configQuery, () => { configMatchIdx.value = 0; scrollToMatch() })

function stepMatch(delta) {
  const n = configMatches.value.length
  if (!n) return
  configMatchIdx.value = (configMatchIdx.value + delta + n) % n
  scrollToMatch()
}

function scrollToMatch() {
  nextTick(() => {
    const line = currentMatchLine.value
    if (line < 0 || !cfgCodeEl.value) return
    cfgCodeEl.value.querySelector(`#cfg-l-${line}`)
      ?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  })
}

async function copyConfigContent() {
  if (!configContent.value) return ElMessage.warning('暂无内容')
  try {
    await navigator.clipboard.writeText(configContent.value)
    ElMessage.success('已复制配置内容')
  } catch { ElMessage.warning('复制失败，请手动选择复制') }
}

async function copyConfigPath() {
  const p = inst.value.config_path
  if (!p) return ElMessage.warning('暂无配置路径')
  try {
    await navigator.clipboard.writeText(p)
    ElMessage.success('已复制配置路径')
  } catch { ElMessage.warning('复制失败，请手动选择复制') }
}

// 下载走前端生成 Blob：正文已在手上（且后端做过 key 脱敏），
// 另开一个下载接口反而会绕过那层脱敏。
async function downloadConfig(filename) {
  const target = filename || activeConfigTab.value
  if (!target) return ElMessage.warning('请先选择配置文件')
  if (target !== activeConfigTab.value || !configContent.value) {
    activeConfigTab.value = target
    await loadConfigContent(target)
  }
  if (!configContent.value) return ElMessage.warning('暂无内容')
  const url = URL.createObjectURL(new Blob([configContent.value], { type: 'text/plain;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `${id}-${target}`
  a.click()
  URL.revokeObjectURL(url)
}

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
const taskSearch = ref('')
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

// 按错误类型 + 关键字筛选 task 列表
const visibleTaskLogFiles = computed(() => {
  let list = taskLogFiles.value
  if (categoryFilter.value) {
    list = list.filter(f => {
      const st = taskStatusOf(f)
      if (!st) return false
      if (categoryFilter.value === 'success') return st.status === '任务成功'
      return st.category === categoryFilter.value
    })
  }
  if (taskSearch.value.trim()) {
    const kw = taskSearch.value.trim().toLowerCase()
    list = list.filter(f => f.toLowerCase().includes(kw))
  }
  return list
})

// 左栏固定的两个实例级日志源
const fixedLogSources = [
  { label: '主进程日志 (main.log)', value: 'main.log' },
  { label: '完整日志 (nohup.log)', value: 'nohup.log' },
]

// 左栏顺序列表分页：大实例有上万个 task，一次性渲染会卡死。
// 用真分页（而非「加载更多」累加）——累加到几千条后 DOM 依然会重，
// 且用户想回到前面还得往回滚很远。
const taskListPage = ref(1)
const taskListPageSize = 100
const pagedTaskFiles = computed(() => {
  const start = (taskListPage.value - 1) * taskListPageSize
  return visibleTaskLogFiles.value.slice(start, start + taskListPageSize)
})

// 筛选条件变化时回到第 1 页，否则可能停在一个超出新结果集的空页
watch([categoryFilter, taskSearch], () => { taskListPage.value = 1 })

// ---- 左栏：失败类型分组 ----
const leftView = ref('seq')
const expandedGroups = ref([])
const openedCode = ref('')
const codeTasks = ref([])
const codeTasksLoading = ref(false)
const codeTasksTotal = ref(0)
const codeTasksPage = ref(1)
const codeTasksPageSize = 50

async function toggleCode(code) {
  if (openedCode.value === code) { openedCode.value = ''; return }
  openedCode.value = code
  codeTasks.value = []
  codeTasksTotal.value = 0
  codeTasksPage.value = 1
  loadCodeTasks()
}

// 按错误码取任务清单（服务端分页：一个码下可能有上万个任务）
async function loadCodeTasks() {
  const code = openedCode.value
  if (!code) return
  codeTasksLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/task-records`, {
      params: { code, page: codeTasksPage.value, page_size: codeTasksPageSize,
                sort_by: 'task_idx', sort_dir: 'asc' },
    })
    // 响应可能在等待期间已被另一次点击取代，丢弃过期结果
    if (openedCode.value !== code) return
    codeTasks.value = res.tasks || []
    codeTasksTotal.value = res.total || 0
  } catch { /* handled by interceptor */ }
  finally { if (openedCode.value === code) codeTasksLoading.value = false }
}

function selectLogSource(v) {
  logSource.value = v
  onLogSourceChange()
}

// ---- 右栏：关键事件 ----
const logEvents = ref([])
const eventSummary = ref({})
const eventsLoading = ref(false)
const eventsTruncated = ref(false)

// 事件圆点配色：错误按错误码前缀取语义色，其余按阶段
function eventColor(ev) {
  if (ev.kind === 'error') return dotColor({ category: (ev.code || '')[0] })
  if (ev.kind === 'end') {
    return ev.title === '任务成功' ? '#10b981' : dotColor({ category: (ev.code || 'X')[0] })
  }
  if (ev.kind === 'begin') return '#409eff'
  return '#10b981'
}

// 错误码释义：与后端 _ERROR_CODE_DESC 同源，取自概览已拉到的 error_tree，
// 避免为一个 tooltip 再开一个接口。查不到时退回码本身。
function errCodeDesc(code) {
  if (!code) return ''
  for (const grp of overview.value.error_tree || []) {
    for (const c of grp.codes || []) {
      if (c.code === code) return c.desc || code
    }
  }
  return code
}

function fmtElapsed(s) {
  if (s == null) return '—'
  if (s < 60) return `${s.toFixed(1)} 秒`
  const m = Math.floor(s / 60)
  return `${m} 分 ${Math.round(s - m * 60)} 秒`
}

async function loadLogEvents() {
  eventsLoading.value = true
  logEvents.value = []
  eventSummary.value = {}
  const src = logSource.value
  try {
    const res = await api.get(`/logs/${id}/task-log/${src}/events`, { silent: true })
    // 切源很快时响应可能乱序到达，只认当前源的结果
    if (logSource.value !== src) return
    logEvents.value = res.events || []
    eventSummary.value = res.summary || {}
    eventsTruncated.value = !!res.truncated
  } catch { /* 事件抽取失败不该影响看日志本身 */ }
  finally { if (logSource.value === src) eventsLoading.value = false }
}

// 点击时间线 → 中栏定位。骨架版用关键字定位：把该事件时间戳填进过滤框，
// 日志正文随即只剩相关行。比重新按字节游标翻页简单得多，且对用户同样有用。
function jumpToLine(line) {
  if (!line) return
  const ev = logEvents.value.find(e => e.line === line)
  if (ev?.ts) filterKeyword.value = ev.ts
  scrollToBottom()
}

// ---- sandbox_detail 方框块分段（只影响渲染，不动 rawText） ----
// expandedSegs 只记录「被用户手动展开过」的块，默认（无键）即折叠。
// 不用 watch(logSegments) 去预填默认值：那个 watch 会在 setup 期间立即求值
// logSegments → 读到后面才声明的 displayRawText，触发 TDZ ReferenceError，
// 整个组件 setup 崩掉（表现为所有 tab 都打不开）。
const expandedSegs = ref({})
function toggleSeg(i) { expandedSegs.value[i] = !expandedSegs.value[i] }
function segExpanded(i) { return !!expandedSegs.value[i] }

// 换日志源 / 刷新时清空展开状态，避免下标错位到新内容上
watch(logSource, () => { expandedSegs.value = {} })

const logSegments = computed(() => {
  const text = displayRawText.value
  if (!text) return []
  const segs = []
  let buf = []
  let box = null
  for (const line of text.split('\n')) {
    const s = line.trimStart()
    if (box === null && s.startsWith('┌')) {
      if (buf.length) { segs.push({ type: 'text', content: buf.join('\n') }); buf = [] }
      box = []
      continue
    }
    if (box !== null) {
      if (s.startsWith('└')) {
        // 首行去掉 ▎/│ 装饰后作为折叠标题
        const title = (box.find(l => l.trim()) || 'sandbox detail')
          .replace(/^[\s▎│|]+/, '').trim()
        segs.push({ type: 'box', title: title.slice(0, 120),
                    content: box.join('\n'), lineCount: box.length })
        box = null
      } else {
        box.push(line)
      }
      continue
    }
    buf.push(line)
  }
  // 方框未闭合（日志被截断在块中间）：当普通文本处理，不吞内容
  if (box !== null) buf.push(...box)
  if (buf.length) segs.push({ type: 'text', content: buf.join('\n') })
  return segs
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
  filterKeyword.value = ''   // 换源时清掉上一源的定位关键字，否则新日志可能整屏空
  loadLogs()
  loadLogEvents()
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

// 日志分页：每页行数可调。默认 500 —— 实测 50 行在长日志里连一屏都不够，
// 上滑后立刻又触顶，形成「滚一下等一下」的卡顿感。
const LOG_PAGE_SIZES = [200, 500, 1000, 2000]
const logPageSize = ref(500)
const logLoadedPages = ref(0)   // 已向上翻过的页数，供「已加载 N 页」提示

async function loadLogs() {
  logLoading.value = true
  try {
    // 首屏取最新 logPageSize 行（raw 模式，1:1 展示），后续上滑用 end 游标向上翻页
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: logPageSize.value, raw: true }
    })
    rawText.value = res.text ?? ''
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    logLines.value = []
    logLoadedPages.value = 1
    scrollToBottom()
  } catch { /* handled by interceptor */ }
  finally { logLoading.value = false }
}

// 上滑到顶时向上加载更早的一页，前置到 rawText，并保持视觉滚动位置不跳
async function loadMore() {
  if (!logHasMore.value || loadingMore.value || logStartOffset.value == null) return
  loadingMore.value = true
  const el = logContainer.value
  const prevHeight = el ? el.scrollHeight : 0
  const prevTop = el ? el.scrollTop : 0
  try {
    const res = await api.get(`/logs/${id}/task-log/${logSource.value}`, {
      params: { tail: logPageSize.value, raw: true, end: logStartOffset.value }
    })
    const chunk = res.text ?? ''
    if (chunk) rawText.value = chunk + rawText.value
    logStartOffset.value = res.start_offset ?? 0
    logHasMore.value = !!res.has_more
    if (chunk) logLoadedPages.value++
    await nextTick()
    // 位置补偿放进 rAF：nextTick 时浏览器可能还没完成布局，scrollHeight 读到旧值，
    // 补偿量算错就会看到一帧跳动。等到下一帧再校正，视觉上完全不动。
    if (el) {
      requestAnimationFrame(() => {
        el.scrollTop = prevTop + (el.scrollHeight - prevHeight)
      })
    }
  } catch { /* ignore */ }
  finally { loadingMore.value = false }
}

// 触发阈值放宽到 200px：40px 太窄，滚轮快一点就冲到顶部才开始加载，
// 看起来就是「卡住了」。提前量留足，内容在到顶前就续上。
function onScroll() {
  const el = logContainer.value
  if (el && el.scrollTop < 200 && logHasMore.value && !loadingMore.value) loadMore()
}

// 切换每页行数后按新粒度重新加载首屏
function onLogPageSizeChange() { loadLogs() }

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
// 统计: GET /instances/{id}/shallow（summary/progress，全量语义，10s 轮询）。
// 表格: GET /instances/{id}/shallow/tasks（分页 + tag/keyword 过滤 + 排序，后端下沉）。
const shallowLoading = ref(false)
const shallowSummary = ref({ L0: 0, L1: 0, 'L1.5': 0, L2: 0, L3: 0, graded: 0, total: 0, task_done: 0, unevaluated: 0, failed: 0 })
const shallowTasks = ref([])
const shallowTaskTotal = ref(0)
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

// 会话行: shallow_tasks 返回的当前页行 ∪ traj_name(leaf) ∪ task_title
const taskRows = computed(() => {
  return shallowTasks.value.map(r => {
    const name = r.config_name || ''
    const leaf = name.includes('/') ? name.split('/').pop() : name
    const stripped = leaf.replace(/\.json$/, '')
    return {
      ...r,
      traj_name: stripped || leaf,
      task_title: _extractTaskTitle(name), // 展示用简短任务名；traj_name 仍是 API key
      tool_fail: false, // task_records 无此列，恒 false（仅保留列位，无数据来源）
    }
  })
})

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

// 交付物下载进度条：已下载 / 总数，全部下完 → 100% 变绿。
const artifactProgressPct = computed(() => {
  const p = artifactProgress.value
  if (!p || !p.total) return 0
  return Math.round((p.done / p.total) * 100)
})
const artifactProgressText = computed(() => `${artifactProgressPct.value}%`)
const artifactProgressColor = computed(() => {
  const p = artifactProgress.value
  if (!p) return '#409eff'
  if (p.failed > 0 && p.done + p.downloading + p.queued === 0) return '#f56c6c'
  return (p.queued > 0 || p.downloading > 0) ? '#e6a23c' : '#10b981'
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
    done:     { label: '浅层已完成',   type: 'success', desc: '所有会话均已定级，可查看详情与交付物' },
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
const artifactStepLabel = computed(() => {
  const p = artifactProgress.value
  if (!p || !p.total) return '未下载'
  if (p.downloading > 0) return `下载中 ${p.downloading}`
  if (p.done > 0 && p.done < p.total) return `已下载 ${p.done}/${p.total}`
  if (p.done >= p.total) return `已完成 ${p.done}`
  if (p.failed > 0 && p.done === 0) return `失败 ${p.failed}`
  if (p.queued > 0) return `待下载 ${p.queued}`
  return '未下载'
})
// 步骤高亮：浅层非空即激活；交付物需浅层已完成且有产物才激活
const shallowActive = computed(() => pipelineStage.value !== 'empty' && pipelineStage.value !== 'ungraded')
const artifactActive = computed(() => {
  if (pipelineStage.value !== 'done') return false
  const p = artifactProgress.value
  return !!(p && p.total > 0)
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
  return (((shallowSummary.value.unevaluated || 0) / total) * 100)
})
// 失败柱的高度口径与「未评估」一致（都按总数），否则两者不可比。
// L0-L3 那几柱按 graded 归一，是漏斗语义（一层层筛选）；失败/未评估是并列状态，按 total。
const failedPct = computed(() => {
  const total = shallowSummary.value.total
  if (!total) return 0
  return (((shallowSummary.value.failed || 0) / total) * 100)
})

// 导出漏斗各层级计数为 Excel（后端 openpyxl 生成 .xlsx，同 proxy-config/export 模式）
const exportingFunnel = ref(false)
async function exportFunnel() {
  exportingFunnel.value = true
  try {
    const blob = await api.get(`/instances/${id}/shallow/export`, { responseType: 'blob' })
    const url = window.URL.createObjectURL(blob)
    const name = `${inst.value?.name || id}_funnel.xlsx`
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)
  } catch { /* interceptor 已提示 */ }
  finally { exportingFunnel.value = false }
}

// ---- 表格: 分页 / 过滤 / 排序（后端 shallow/tasks 下沉，本地只透传参数触发重拉） ----
const levelFilter = ref('')
const taskKeyword = ref('')
// 任务编号搜索（task-5921 / 5921），独立成框 —— 和名称模糊搜的语义不同
const taskIdxKeyword = ref('')
// 内容检索：走 task_records.query_text（shallow-query worker 回填的任务描述）
const taskContentKeyword = ref('')
const taskPage = ref(1)
const taskPageSize = ref(50)
const taskSort = ref({})

// 表格直接展示后端返回的当前页行；过滤/排序/分页已由后端完成
const pagedTasks = computed(() => taskRows.value)
const shallowTotal = computed(() => shallowTaskTotal.value)

function onTaskFilterChange() { taskPage.value = 1; loadTasks() }
function onTaskPage(p) { taskPage.value = p; loadTasks() }
function onTaskSort({ prop, order }) {
  taskSort.value = { prop, order }
  taskPage.value = 1
  loadTasks()
}
let taskKwTimer = null
// 关键词输入防抖：避免每敲一个字符就拉一次分页接口。
// 配置正文检索要读上万个文件（首次约 0.5s，之后走后端缓存），防抖给到 500ms。
watch(taskKeyword, () => {
  if (taskKwTimer) clearTimeout(taskKwTimer)
  taskKwTimer = setTimeout(() => { taskPage.value = 1; loadTasks() }, 300)
})
let taskIdxTimer = null
watch(taskIdxKeyword, () => {
  if (taskIdxTimer) clearTimeout(taskIdxTimer)
  taskIdxTimer = setTimeout(() => { taskPage.value = 1; loadTasks() }, 300)
})
let taskCkTimer = null
// 内容检索：首次搜某实例时后端会同步回填 query_text（约 0.8s），之后都是
// SQL LIKE 毫秒级。防抖给到 500ms，避免边敲边触发那次回填。
watch(taskContentKeyword, () => {
  if (taskCkTimer) clearTimeout(taskCkTimer)
  taskCkTimer = setTimeout(() => { taskPage.value = 1; loadTasks() }, 500)
})

// 当前页表格数据: GET /shallow/tasks（分页 + tag + keyword + 排序）
async function loadTasks() {
  const s = taskSort.value
  const params = {
    page: taskPage.value,
    page_size: taskPageSize.value,
  }
  if (levelFilter.value) params.tag = levelFilter.value
  // 任务名称 → keyword（config_name 模糊）；任务编号 → idx_keyword（task_idx 精确）
  const kw = taskKeyword.value.trim()
  if (kw) params.keyword = kw
  const ik = taskIdxKeyword.value.trim()
  if (ik) params.idx_keyword = ik
  const ck = taskContentKeyword.value.trim()
  if (ck) params.content_keyword = ck
  if (s.prop) {
    params.sort_by = s.prop
    params.sort_dir = s.order === 'descending' ? 'desc' : 'asc'
  }
  try {
    const res = await api.get(`/instances/${id}/shallow/tasks`, { params })
    shallowTasks.value = res.tasks || []
    shallowTaskTotal.value = res.total || 0
  } catch { /* interceptor 已提示；静默保持上次 */ }
}

async function loadShallow(refresh = false) {
  shallowLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/shallow`)
    shallowSummary.value = res.summary || shallowSummary.value
    instanceStatus.value = res.instance_status || ''
    shallowProgress.value = res.progress || null
    // 点击驱动：看到输出数据后，若该实例已结束且仍有缺口，登记一次浅层请求
    maybeTriggerShallow()
    // 表格当前页同刷（分页/过滤/排序由 loadTasks 按当前参数重新请求后端）
    await loadTasks()
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

// ============ 输出 tab: 交付物下载进度 ============
// 数据源: GET /artifacts/{id}/tasks（与「沙箱交付物」tab 同一个接口，口径必然一致）。
// 原「深层轨迹/日志按需下载」已并入产物下载，这里只统计 artifact_complete=1 的任务。
const artifactProgress = ref({ total: 0, done: 0, queued: 0, downloading: 0, failed: 0 })
const artifactTask = ref('')      // 传给 ArtifactWorkspace 的初始选中任务

async function loadArtifactProgress() {
  try {
    const res = await api.get(`/artifacts/${id}/tasks`, { silent: true })
    const tasks = res.tasks || []
    const acc = { total: tasks.length, done: 0, queued: 0, downloading: 0, failed: 0, empty: 0 }
    for (const t of tasks) {
      if (t.downloaded) acc.done += 1
      else if (t.status === 'downloading') acc.downloading += 1
      // empty = OBS 前缀存在但没文件（沙箱没产出），不是故障，单独计数
      else if (t.status === 'empty') acc.empty += 1
      else if (t.status === 'failed' || t.status === 'skipped') acc.failed += 1
      else acc.queued += 1
    }
    artifactProgress.value = acc
  } catch { /* 实例缺 OBS 配置等 → 保持上次值 */ }
}

// 跳转「沙箱交付物」tab 并选中该任务
function openArtifacts(row) {
  detailVisible.value = false
  artifactTask.value = row?.traj_name || ''
  activeTab.value = 'artifacts'
}

// ============ 输出 tab: 模型配置 / 用户模拟配置信息面板 ============
// 数据源: GET /instances/{id}/proxy-config（create_params 加工：key 后四位 + 代理名映射 + 实例起止）。
// 代理名由后端查导出服务 /export/proxy_list 实时映射（本地 proxy_name.json 兜底），未命中留空；
// 导出走后端 openpyxl 生成 .xlsx。
const proxyRows = ref([])
const proxyLoading = ref(false)
const proxyStartedAt = ref('')
const proxyStoppedAt = ref('')
const exportingProxy = ref(false)

function fmtProxyTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 19)
}

async function loadProxyConfig() {
  proxyLoading.value = true
  try {
    const res = await api.get(`/instances/${id}/proxy-config`)
    proxyRows.value = res.rows || []
    proxyStartedAt.value = res.started_at || ''
    proxyStoppedAt.value = res.stopped_at || ''
  } catch { /* interceptor 已提示；静默保持上次 */ }
  finally { proxyLoading.value = false }
}

async function exportProxyConfig() {
  exportingProxy.value = true
  try {
    // api 拦截器已 unwrap 成 res.data，responseType:'blob' 时 res 直接是 Blob
    const blob = await api.get(`/instances/${id}/proxy-config/export`, { responseType: 'blob' })
    const url = window.URL.createObjectURL(blob)
    const name = `${inst.value?.name || id}_proxy_config.xlsx`
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)
  } catch { /* interceptor 已提示 */ }
  finally { exportingProxy.value = false }
}

// ============ 输出 tab: 模型配置面板 — 对话导出（浏览/导出/状态/路径/会话数） ============
// 后端代理外部 EXPORT_BASE /export/view|submit|status，完整 key/access-key 只在服务端。
// 浏览 = 302 跳外部浏览页；导出 = 弹窗填名称+方式 → POST /proxy-config/export；状态轮询 GET /export/status。
const exportDialogVisible = ref(false)
const exportDialogRole = ref('')
const exportDialogName = ref('')
const exportDialogMode = ref('export')
const submittingExport = ref(false)
let exportPollTimer = null

// 导出方式下拉：从 /export-meta/modes 实时取（后端转发导出服务 /export/mode_list），
// 不再硬编码——远端新增模式（小艺导出/关键帧导出…）无需改前端。
const exportModes = ref([])
const exportModeDefault = ref('export')
let exportModesLoaded = false

async function loadExportModes() {
  if (exportModesLoaded) return   // 一个页面生命周期内取一次就够（后端还有 60s 缓存）
  try {
    const res = await api.get('/export-meta/modes')
    exportModes.value = res.modes || []
    exportModeDefault.value = res.default || 'export'
    exportModesLoaded = true
    if (res.source === 'fallback') {
      ElMessage.warning('导出服务暂时不可达，导出方式列表为本地兜底，可能不是最新')
    }
  } catch { /* interceptor 已提示；下拉留空，用户可重开弹窗重试 */ }
}

// 当前选中模式的长说明（远端 label 字段，如「导出原始文件（三元组，不分析）」）
const currentModeDesc = computed(
  () => exportModes.value.find(m => m.value === exportDialogMode.value)?.desc || ''
)

async function browseProxyExport(row) {
  // 后端鉴权接口，裸 window.open 不带 Bearer token 会 401。走带 token 的 axios 取外部
  // /export/view URL（key+access-key 在后端拼好），再新开标签页打开成品链接。
  try {
    const res = await api.get(`/instances/${id}/proxy-config/browse?role=${encodeURIComponent(row.role)}`)
    if (res.url) window.open(res.url, '_blank')
  } catch { /* interceptor 已提示 */ }
}

function openExportDialog(row) {
  exportDialogRole.value = row.role
  exportDialogName.value = inst.value?.name || ''
  exportDialogVisible.value = true
  // 弹窗打开时才拉模式列表（而不是进 tab 就拉）：导出是低频操作
  loadExportModes().then(() => { exportDialogMode.value = exportModeDefault.value })
}

async function submitProxyExport() {
  submittingExport.value = true
  try {
    const res = await api.post(`/instances/${id}/proxy-config/export`, {
      role: exportDialogRole.value,
      export_name: exportDialogName.value,
      mode: exportDialogMode.value,
    })
    exportDialogVisible.value = false
    ElMessage.success(`导出已提交（export_id=${res.export_id}）`)
    // 立即拉一次状态（有进行中任务则启动轮询）
    await refreshProxyExportStatus()
  } catch { /* interceptor 已提示 */ }
  finally { submittingExport.value = false }
}

async function refreshProxyExportStatus() {
  const res = await api.get(`/instances/${id}/proxy-config/export/status`)
  const rows = res.rows || []
  const byRole = {}
  for (const r of rows) byRole[r.role] = r
  const merged = (proxyRows.value || []).map(r => {
    const ex = byRole[r.role]
    if (!ex) return r
    return { ...r, export_status: ex.export_status, session_path: ex.session_path, total_sessions: ex.total_sessions, export_id: ex.export_id }
  })
  proxyRows.value = merged
  // 有进行中任务则轮询，全部到终态停止
  const active = merged.some(r => r.export_status === 'queued' || r.export_status === 'running')
  if (exportPollTimer) { clearInterval(exportPollTimer); exportPollTimer = null }
  if (active) {
    exportPollTimer = setInterval(async () => {
      try {
        const st = await api.get(`/instances/${id}/proxy-config/export/status`, { silent: true })
        const m = (st.rows || []).reduce((acc, r) => { acc[r.role] = r; return acc }, {})
        proxyRows.value = (proxyRows.value || []).map(r => {
          const ex = m[r.role]
          return ex ? { ...r, export_status: ex.export_status, session_path: ex.session_path, total_sessions: ex.total_sessions } : r
        })
        const stillActive = proxyRows.value.some(r => r.export_status === 'queued' || r.export_status === 'running')
        if (!stillActive && exportPollTimer) { clearInterval(exportPollTimer); exportPollTimer = null }
      } catch { /* 轮询失败静默，下轮再试 */ }
    }, 5000)
  }
}

// 离开输出 tab 时停止导出轮询
function stopExportPoll() {
  if (exportPollTimer) { clearInterval(exportPollTimer); exportPollTimer = null }
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

// ---- 任务配置查看（会话列表「任务配置」列）----
const taskCfgVisible = ref(false)
const taskCfgName = ref('')
const taskCfgText = ref('')
const taskCfgPath = ref('')
const taskCfgLoading = ref(false)
const taskCfgError = ref('')

async function openTaskConfig(row) {
  // config_name 优先（后端按它在 task_input_path 下找文件）；
  // 只有 traj_name 时补回 .json —— 两者只差扩展名（traj_name = config stem）。
  const cn = row.config_name
    || (row.traj_name ? `${row.traj_name}.json` : '')
  if (!cn) { ElMessage.warning('该行没有 config 名，无法定位配置'); return }
  taskCfgName.value = cn
  taskCfgText.value = ''
  taskCfgPath.value = ''
  taskCfgError.value = ''
  taskCfgVisible.value = true
  taskCfgLoading.value = true
  try {
    const r = await api.get(`/instances/${id}/task-config`,
      { params: { config_name: cn }, silent: true })
    // 后端返回原文字符串；能解析成 JSON 就缩进美化，不能就原样显示
    // （配置可能被脱敏替换过，或本身不是严格 JSON）。
    try {
      taskCfgText.value = JSON.stringify(JSON.parse(r.content), null, 2)
    } catch {
      taskCfgText.value = r.content
    }
    taskCfgPath.value = r.path || ''
  } catch (e) {
    taskCfgError.value = e?.response?.data?.detail || e?.message || '读取失败'
  } finally {
    taskCfgLoading.value = false
  }
}

async function copyTaskCfg() {
  try {
    await navigator.clipboard.writeText(taskCfgText.value)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器拒绝了剪贴板访问')
  }
}

async function openTaskDetail(row) {
  const name = row.traj_name
  detailTrajName.value = name
  detailVisible.value = true
  detailData.value = null
  detailLoading.value = true
  detailTab.value = 'traj'
  logTab.value = 'task'
  detailLogTailFull.value = false
  detailStatusHint.value = '正在读取本地交付物…'
  await fetchDetail()
}

// 只读本地目录：成功就渲染，409 就是「交付物还没下载」。
// 不再自动触发下载 —— 下载统一由产物 worker 做，用户想下就点弹窗里的按钮。
async function fetchDetail() {
  detailLoading.value = true
  try {
    const d = await api.get(`/instances/${id}/deep/${detailTrajName.value}/detail`, { silent: true })
    detailData.value = d
    detailStatusHint.value = ''
  } catch (e) {
    detailData.value = null
    detailStatusHint.value = e?.status === 409
      ? '该任务的交付物尚未下载到本地'
      : `读取失败: ${e?.message || '未知错误'}`
  } finally {
    detailLoading.value = false
  }
}

// 弹窗内「下载交付物后重试」：入队产物下载，再每 3s 重试读本地详情
async function retryDownloadDetail() {
  try {
    await api.post(`/instances/${id}/deep/${detailTrajName.value}`)
  } catch { return }
  detailStatusHint.value = '已加入下载队列，完成后自动打开…'
  clearInterval(deepPollTimer)
  deepPollTimer = setInterval(pollDeep, 3000)
}

async function pollDeep() {
  if (!detailVisible.value || !detailTrajName.value) return
  let st = null
  try { st = await api.get(`/instances/${id}/deep/${detailTrajName.value}/status`, { silent: true }) } catch { return }
  if (st.status === 'downloading') { detailStatusHint.value = '正在下载交付物…'; return }
  // empty = OBS 前缀确实存在但一个文件都没有（沙箱没产出）。这是终态，
  // 不能当 failed 提示「下载失败」，否则用户会反复重试一个注定为空的目录。
  if (st.status === 'empty') {
    clearInterval(deepPollTimer)
    deepPollTimer = null
    detailStatusHint.value = '该任务没有产出交付物（OBS 上没有任何文件）'
    return
  }
  if (st.status === 'failed' || st.status === 'skipped') {
    clearInterval(deepPollTimer)
    deepPollTimer = null
    detailStatusHint.value = `下载失败: ${st.error || '未知错误'}`
    return
  }
  if (st.status !== 'done') return
  clearInterval(deepPollTimer)
  deepPollTimer = null
  await fetchDetail()
}

// roleLabel / toggleBlock / collapsedBlocks 已随气泡模板搬进
// components/TrajBlocks.vue（折叠态由组件自管）。
function logDisplayText(text) {
  if (detailLogTailFull.value) return text
  return (text || '').slice(-20000)
}

// ============ 生命周期 ============
let shallowTimer = null
let shallowTriggered = false

onMounted(async () => {
  harnessStore.load()
  loading.value = true
  try { inst.value = await api.get(`/instances/${id}`) } catch {}
  loading.value = false
  // 点击驱动：打开详情页即登记浅层请求（任何状态，含 running；由 undone 缺口门控）。
  // 幂等登记，tracker 按 offset 增量投递；flag 防止同一会话重复 POST。漏斗进度由 10s 轮询反映。
  maybeTriggerShallow()
  // 顶部状态条 10s 轮询；overview 只在「配置信息」tab 需要（汇总/错误树都在那），
  // 其它 tab 只刷新实例本身，避免每个周期多打一次带目录扫描的 /overview。
  timer = setInterval(() => loadData(activeTab.value === 'config'), 10000)
  ensureTabLoaded(activeTab.value)
  if (activeTab.value === 'logs') {
    nextTick(() => { if (logContainer.value) logContainer.value.addEventListener('scroll', onScroll) })
  }
  if (activeTab.value === 'outputs') {
    shallowTimer = setInterval(() => loadShallow(false), 10000)
    loadArtifactProgress()
  }
})

async function maybeTriggerShallow() {
  if (shallowTriggered) return
  // 任何状态都允许自动登记（含 running）：浅层已改为纯点击/打开驱动，tracker 按
  // offset 增量投递，运行中实例新完成的 config 会逐轮带进来。只要打开输出 tab 且
  // 还有未定级缺口就登记一次，不必再去找「立即分析未定级会话」那个手动按钮。
  // 唯一的门控是下面的 undone 缺口判定：全部定级就不登记。
  // 用 progress.undone（剩余未分级会话数）判定缺口，而不是依赖表格行已填充：
  //   loadShallow 在 mount 时可能尚未返回，progress 为空会导致永远跳过（既不登记也无提示）。
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

// outputs tab 懒加载时启动浅层轮询；离开时停止。
// 交付物进度跟浅层进度同一个 10s 轮询刷新（loadShallow 里不会拉产物，这里单独发起）。
watch(activeTab, (tab) => {
  if (tab === 'outputs') {
    if (!shallowTimer) shallowTimer = setInterval(() => loadShallow(false), 10000)
    loadArtifactProgress()
    loadProxyConfig()  // 切回输出 tab 刷新模型配置面板
    setTimeout(refreshProxyExportStatus, 500)
  } else {
    if (shallowTimer) { clearInterval(shallowTimer); shallowTimer = null }
    stopExportPoll()
  }
}, { flush: 'sync' })

onUnmounted(() => {
  clearInterval(timer)
  clearInterval(shallowTimer)
  clearInterval(deepPollTimer)
  stopExportPoll()
  if (logContainer.value) logContainer.value.removeEventListener('scroll', onScroll)
})
</script>

<style scoped>
.task-cfg-pre {
  margin: 0;
  padding: 12px;
  max-height: 520px;
  overflow: auto;
  background: var(--bg-secondary, #f5f7fa);
  border: 1px solid var(--border-color, #ebeef5);
  border-radius: 4px;
  font-family: monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.header-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.detail-tabs { margin-top: 4px; }

/* ---- 配置信息 tab ---- */
.stat-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px; }
.progress-head {
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 13px; color: var(--text-muted); margin-bottom: 10px;
}
.stat-card {
  background: #fff; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  padding: 14px 16px; display: flex; align-items: center; gap: 12px;
  box-shadow: var(--shadow-sm);
}
.stat-icon {
  flex: 0 0 40px; width: 40px; height: 40px; border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
}
.stat-body { display: flex; flex-direction: column; min-width: 0; }
.stat-num { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); line-height: 1.15; }
.stat-label { font-size: 13px; color: var(--text-muted); margin-bottom: 2px; }

/* 卡片标题：图标 + 文字。图标只是分区锚点，含义由文字承担。 */
.sec-head { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--text-primary); }
.sec-head-icon { color: var(--accent-purple); font-size: 17px; }

/* 创建参数折叠面板：收起时在标题右侧给一行灰色摘要 */
.param-collapse :deep(.el-collapse-item__header) { height: 46px; line-height: 46px; }
.param-title { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; padding-right: 8px; }
.param-icon { color: var(--accent-purple); font-size: 16px; flex: 0 0 auto; }
.param-name { font-weight: 600; color: var(--text-primary); flex: 0 0 auto; }
.param-peek {
  flex: 1 1 auto; min-width: 0; font-size: 12px; color: var(--text-muted);
  font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 配置文件摘要卡 */
.cfg-summary { display: flex; gap: 12px; align-items: flex-start; }
.cfg-summary-icon {
  flex: 0 0 40px; width: 40px; height: 40px; border-radius: 10px;
  background: #eeeefc; color: var(--accent-purple);
  display: flex; align-items: center; justify-content: center;
}
.cfg-summary-main { flex: 1 1 auto; min-width: 0; }
.cfg-summary-title { font-weight: 600; color: var(--text-primary); }
.cfg-summary-sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
.cfg-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.cfg-chip { cursor: pointer; }
.cfg-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }

/* 配置文件弹窗 */
.cfg-dialog :deep(.el-dialog__body) { padding-top: 8px; }
.cfg-dialog-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.cfg-dialog-ops { display: flex; gap: 8px; }
.cfg-dialog-tabs :deep(.el-tabs__header) { margin-bottom: 12px; }
.cfg-search { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.cfg-match-count { font-size: 12px; color: var(--text-muted); white-space: nowrap; min-width: 56px; }
.cfg-code {
  background: #1e293b; border-radius: var(--radius-sm); padding: 12px 0;
  max-height: 60vh; overflow: auto;
  font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 13px; line-height: 1.6;
}
.cfg-code.is-full { max-height: calc(100vh - 300px); }
.cfg-line { display: flex; padding: 0 12px; }
.cfg-line.is-hit { background: rgba(245, 158, 11, 0.16); }
.cfg-line.is-current { background: rgba(245, 158, 11, 0.34); }
.cfg-ln {
  flex: 0 0 44px; text-align: right; padding-right: 12px;
  color: #64748b; user-select: none; font-variant-numeric: tabular-nums;
}
.cfg-text { flex: 1 1 auto; min-width: 0; color: #e2e8f0; white-space: pre-wrap; word-break: break-all; }
/* yaml/json 着色：键名蓝、字符串绿、数值橙、注释灰（都在深底上过 4.5:1） */
.cfg-text :deep(.t-key) { color: #7dd3fc; }
.cfg-text :deep(.t-str) { color: #86efac; }
.cfg-text :deep(.t-num) { color: #fdba74; }
.cfg-text :deep(.t-cmt) { color: #94a3b8; font-style: italic; }
.breakdown-row { display: flex; justify-content: space-between; padding: 10px 0; border-bottom: 1px solid var(--border-color); color: var(--text-secondary); }
.breakdown-row:last-child { border-bottom: none; }
/* 任务执行情况：左环形图 + 右图例。图例带文字和数值，色块只是辅助识别，
   所以不依赖色相单独传达含义。 */
.donut-wrap { display: flex; align-items: center; gap: 12px; }
.donut-chart { position: relative; flex: 1 1 auto; min-width: 0; }
.donut-center {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; pointer-events: none;
}
.donut-total { font-size: 22px; font-weight: 700; color: var(--text-primary); line-height: 1.1; font-variant-numeric: tabular-nums; }
.donut-total-label { font-size: 11px; color: var(--text-muted); margin-top: 2px; }
.donut-legend { flex: 0 0 152px; display: flex; flex-direction: column; gap: 10px; }
.donut-item {
  display: grid; grid-template-columns: 10px 1fr auto auto; align-items: center;
  gap: 6px; font-size: 13px; color: var(--text-secondary);
}
.donut-item .dot { width: 8px; height: 8px; border-radius: 50%; }
.donut-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.donut-count { font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.donut-pct { color: var(--text-muted); font-size: 12px; font-variant-numeric: tabular-nums; min-width: 42px; text-align: right; }
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

/* ---- 日志中心：三栏布局 ---- */
/* 三栏整体吃满视口剩余高度：用 flex + min-height:0 让子项能正确收缩，
   避免给每一栏写死 calc(100vh - 魔法数) —— 那样页头/信息卡高度一变就对不上，
   表现为日志区够不到底部或溢出。 */
.logc {
  display: flex; gap: 12px; align-items: stretch;
  height: calc(100vh - 230px); min-height: 420px;
}
.logc-left {
  width: 300px; flex-shrink: 0; display: flex; flex-direction: column; gap: 8px;
  border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: 10px;
  min-height: 0;
}
.logc-left-switch { width: 100%; display: flex; }
.logc-left-switch :deep(.el-radio-button) { flex: 1; }
.logc-left-switch :deep(.el-radio-button__inner) { width: 100%; }
.logc-filters { display: flex; flex-direction: column; gap: 6px; }
.logc-fixed { display: flex; flex-direction: column; gap: 4px; }
.logc-hint { color: var(--text-muted); font-size: 12px; padding: 2px 4px; }
.logc-list { flex: 1; min-height: 0; }
.logc-item {
  display: flex; align-items: center; gap: 8px; padding: 7px 8px;
  border-radius: var(--radius-sm, 4px); cursor: pointer; font-size: 13px;
}
.logc-item:hover { background: var(--fill-color, #f4f4f5); }
.logc-item.active { background: var(--el-color-primary-light-9, #ecf5ff); font-weight: 600; }
.logc-item-fixed { border: 1px solid var(--border-color); }
.logc-item-sm { padding: 4px 8px 4px 24px; font-size: 12px; }
.logc-item-main { flex: 1; min-width: 0; }
.logc-item-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.logc-item-sub { color: var(--text-muted); font-size: 11px; }
.logc-more {
  text-align: center; color: var(--el-color-primary); font-size: 12px;
  padding: 8px; cursor: pointer; user-select: none;
}
.logc-pager { justify-content: center; padding: 6px 0 2px; }
/* 日志区顶部「加载更早」提示兼按钮 */
.log-more-btn { cursor: pointer; }
.log-more-btn:hover { color: #e2e8f0; text-decoration: underline; }
.logc-groups :deep(.el-collapse-item__header) { height: 38px; line-height: 38px; }
.logc-grp-title { display: flex; align-items: center; gap: 8px; width: 100%; }
.logc-grp-name { flex: 1; font-size: 13px; }
.logc-code { margin-left: 4px; }
.logc-code-head {
  display: flex; align-items: center; gap: 6px; padding: 4px 6px;
  cursor: pointer; border-radius: var(--radius-sm, 4px);
}
.logc-code-head:hover { background: var(--fill-color, #f4f4f5); }
.logc-code-arrow { font-size: 12px; color: var(--text-muted); }
.logc-code-desc {
  flex: 1; color: var(--text-muted); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.logc-mid { flex: 1; min-width: 0; display: flex; flex-direction: column; }
/* 中栏多了任务信息卡，日志区要相应让出高度（默认 .log-container 是 100vh-340px） */
/* 日志区吃满中栏剩余高度（信息卡/工具栏之外的全部），不再写死 calc */
.logc-mid .log-container { height: auto; flex: 1; min-height: 0; overflow-x: hidden; }
/* 长行自动换行：日志里常有超长的 JSON/路径，默认 white-space:pre 会把整个
   浏览区撑出横向滚动条，读起来要来回拖。改为保留缩进但允许折行。 */
.logc-mid .log-raw { white-space: pre-wrap; overflow-wrap: anywhere; }
.logc-card {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  flex-wrap: wrap; padding: 12px 14px; margin-bottom: 12px;
  background: var(--fill-color-light, #fafafa);
  border: 1px solid var(--border-color); border-radius: var(--radius-md);
}
.logc-card-main { display: flex; align-items: center; gap: 10px; min-width: 0; }
.logc-card-icon { font-size: 20px; color: var(--el-color-primary); }
.logc-card-name { font-size: 15px; font-weight: 600; }
.logc-card-sub {
  color: var(--text-muted); font-size: 12px; max-width: 360px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.logc-card-meta { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; font-size: 13px; }
.logc-k { color: var(--text-muted); margin-right: 6px; }
.logc-v { font-family: 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; }

/* sandbox_detail 折叠块 */
.log-box { margin: 4px 0; border-left: 2px solid #475569; }
.log-box-head {
  display: flex; align-items: center; gap: 6px; padding: 3px 8px;
  cursor: pointer; color: #94a3b8; font-size: 12px; user-select: none;
}
.log-box-head:hover { background: #334155; }
.log-box-title {
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #cbd5e1;
}
.log-box-lines { color: #64748b; flex-shrink: 0; }
.log-box-body { padding-left: 8px; }

/* 右栏：关键事件时间线 */
.logc-right {
  width: 280px; flex-shrink: 0; display: flex; flex-direction: column;
  border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: 10px;
}
.logc-right-head {
  display: flex; align-items: center; gap: 6px;
  font-size: 14px; font-weight: 600; padding-bottom: 8px;
  border-bottom: 1px solid var(--border-color); margin-bottom: 6px;
}
.logc-timeline { flex: 1; min-height: 0; }
.tl-item {
  display: flex; gap: 10px; padding: 8px 6px; position: relative;
  border-radius: var(--radius-sm, 4px);
}
/* 竖线：除最后一项外，用圆点下方的伪元素连起来 */
.tl-item:not(:last-child)::before {
  content: ''; position: absolute; left: 10px; top: 20px; bottom: -4px;
  width: 1px; background: var(--border-color);
}
.tl-clickable { cursor: pointer; }
.tl-clickable:hover { background: var(--fill-color, #f4f4f5); }
.tl-error { background: var(--el-color-danger-light-9, #fef0f0); }
.tl-dot {
  width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;
  margin-top: 4px; margin-left: 2px; z-index: 1;
}
.tl-body { flex: 1; min-width: 0; }
.tl-title { font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
.tl-ts { color: var(--text-muted); font-size: 11px; font-family: 'Cascadia Code', monospace; }
.tl-elapsed { color: var(--el-color-primary); }
.tl-detail {
  color: var(--text-muted); font-size: 11px; margin-top: 2px;
  word-break: break-all; line-height: 1.4;
}

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
.failed-bar {
  background: #f56c6c;
}
.funnel-bar-label {
  font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums;
  position: absolute; bottom: -20px; white-space: nowrap;
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
/* 轨迹气泡的样式已搬进 components/TrajBlocks.vue（scoped） */
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
.queue-actions {
  display: flex; align-items: center; gap: 10px;
  margin-top: 16px;
}
.queue-actions-hint { font-size: 12px; color: var(--text-muted); }
</style>






