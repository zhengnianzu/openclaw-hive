<template>
  <div class="bc-page">
    <!-- ================= 顶部：标题 + 三步导航 ================= -->
    <header class="bc-head">
      <el-button text class="back" @click="router.push('/batch')">
        <el-icon :size="18"><ArrowLeft /></el-icon>
      </el-button>
      <h2 class="bc-title">新建批量实验</h2>

      <div class="stepper">
        <template v-for="(s, i) in STEPS" :key="s">
          <div class="step" :class="{ active: step === i, done: step > i }" @click="gotoStep(i)">
            <span class="num">{{ i + 1 }}</span>
            <span class="txt">{{ s }}</span>
          </div>
          <span v-if="i < STEPS.length - 1" class="step-line" />
        </template>
      </div>
    </header>

    <div class="bc-body">
      <!-- ================= 主区 ================= -->
      <main class="bc-main">
        <!-- 顶部三栏：实验名称 / 测试集 / 任务数 -->
        <div class="card topbar">
          <div class="field grow">
            <label class="lbl"><i class="req">*</i>实验名称</label>
            <el-input v-model="form.name" placeholder="例如：科研能力评测 - 多 Harness × 多模型" />
          </div>
          <div class="field grow">
            <label class="lbl"><i class="req">*</i>测试集</label>
            <el-select v-model="form.registration_id" filterable style="width:100%"
              placeholder="选择数据集" @change="onRegChange">
              <el-option v-for="r in regList" :key="r.id"
                :label="`${r.task_name}（${r.data_total || 0} 条）`" :value="r.id" />
              <template #footer>
                <el-button text type="primary" style="width:100%" @click="router.push('/task-register')">
                  <el-icon><Plus /></el-icon> 新建数据集
                </el-button>
              </template>
            </el-select>
          </div>
          <div class="field shrink">
            <label class="lbl">任务数</label>
            <div class="tasknum">{{ sampleTotal }}</div>
          </div>
        </div>

        <!-- ================= 步骤 1：配置实验 ================= -->
        <template v-if="step === 0">
          <!-- 四个配置 Tab -->
          <div class="tabs">
            <button v-for="t in TABS" :key="t.key" class="tab" :class="{ active: tab === t.key }"
              @click="tab = t.key">
              <el-icon :size="16"><component :is="t.icon" /></el-icon>
              <span>{{ t.label }}</span>
              <el-badge v-if="t.key === 'harness' && form.harnesses.length" :value="form.harnesses.length" class="tab-badge" />
              <el-badge v-else-if="t.key === 'hconfig' && form.models.length" :value="form.models.length" class="tab-badge" />
              <el-badge v-else-if="t.key === 'sim' && form.agents.length" :value="form.agents.length" class="tab-badge" />
            </button>
          </div>

          <!-- ---------- Tab: Harness 类型（只配 类型 / 方案 / 版本） ---------- -->
          <div v-if="tab === 'harness'" class="split">
            <!-- 左：Harness 列表 -->
            <aside class="list-col">
              <div class="list-head">
                <span>Harness 列表（{{ form.harnesses.length }}）</span>
              </div>
              <el-button class="add-btn" @click="addArm">
                <el-icon><Plus /></el-icon> 添加 Harness
              </el-button>

              <div v-if="!form.harnesses.length" class="list-empty">
                还没有添加 Harness。<br />每个 Harness 是矩阵的一列。
              </div>

              <div v-for="(a, i) in form.harnesses" :key="a._uid" class="list-item"
                :class="{ active: selectedArm === i }" @click="selectedArm = i">
                <span class="li-tag">H{{ i + 1 }}</span>
                <div class="li-main">
                  <div class="li-name">{{ a.label || configName(a.harness_config_id) || '未命名' }}</div>
                  <div class="li-sub">{{ versionName(a) || '未选版本' }}</div>
                </div>
                <el-dropdown trigger="click" @command="cmd => onArmCommand(cmd, i)">
                  <el-button text class="li-more" @click.stop>
                    <el-icon><MoreFilled /></el-icon>
                  </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="copy">复制</el-dropdown-item>
                      <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </div>
            </aside>

            <!-- 右：类型 / 方案 / 版本 -->
            <section class="detail-col">
              <div v-if="!curArm" class="detail-empty">
                <el-empty :image-size="80" description="请从左侧添加或选择一个 Harness" />
              </div>
              <template v-else>
                <div class="detail-head">
                  <div class="dh-left">
                    <span class="dh-tag">H{{ selectedArm + 1 }}</span>
                    <span class="dh-title">{{ curArm.label || configName(curArm.harness_config_id) || '未命名' }}</span>
                    <el-tag v-if="versionName(curArm)" size="small" class="ver-tag">{{ versionName(curArm) }}</el-tag>
                    <el-button text class="dh-edit" @click="renameArm">
                      <el-icon><EditPen /></el-icon>
                    </el-button>
                  </div>
                  <div class="dh-right">
                    <el-button @click="copyArm(selectedArm)">
                      <el-icon><CopyDocument /></el-icon> 复制
                    </el-button>
                    <el-button type="danger" plain @click="removeArm(selectedArm)">
                      <el-icon><Delete /></el-icon> 删除
                    </el-button>
                  </div>
                </div>

                <div class="subpanel">
                  <div class="field">
                    <label class="lbl"><i class="req">*</i>配置 Harness 类型</label>
                    <el-select v-model="curArm.harness_type" style="width:100%"
                      placeholder="选择 Harness 类型" @change="onArmTypeChange">
                      <el-option v-for="t in armHarnessTypes" :key="t"
                        :label="harnessStore.label(t)" :value="t" />
                    </el-select>
                    <div class="hint">只列出已有已发布配置的 Harness</div>
                  </div>

                  <div class="field">
                    <label class="lbl"><i class="req">*</i>配置方案</label>
                    <el-select v-model="curArm.harness_config_id" style="width:100%"
                      placeholder="选择配置方案"
                      :disabled="!curArm.harness_type" @change="onArmSchemeChange">
                      <el-option v-for="c in configsOf(curArm.harness_type)" :key="c.config_id"
                        :label="c.config_name" :value="c.config_id" />
                    </el-select>
                  </div>

                  <div class="field">
                    <label class="lbl"><i class="req">*</i>配置版本</label>
                    <el-select v-model="curArm.harness_config_version_id" style="width:100%"
                      :placeholder="versionsOf(curArm).length ? '选择已发布版本' : '该方案暂无已发布版本'">
                      <el-option v-for="v in versionsOf(curArm)" :key="v.id"
                        :label="`${v.version}${v.published_at ? ' · ' + formatShortTime(v.published_at) + ' 发布' : (v.description ? ' · ' + v.description : '')}`" :value="v.id" />
                    </el-select>
                    <div v-if="curArm.harness_config_id && !versionsOf(curArm).length" class="hint danger">
                      该方案没有已发布版本，无法用于实验。请到「配置工作台」创建并发布。
                    </div>
                    <div class="hint">实例只能引用发布过的版本快照；新发布不会自动改变已配置的块。</div>
                  </div>

                  <el-divider content-position="left">运行环境（跟 Harness 走）</el-divider>

                  <div class="field">
                    <label class="lbl">镜像名称</label>
                    <el-select v-model="curArm.image_name" filterable allow-create clearable
                      default-first-option placeholder="留空用 Harness 模板默认镜像" style="width:100%"
                      @blur="normalizeImageName(curArm)"
                      @change="v => onImageChange(v, curArm)">
                      <el-option v-for="img in imageList" :key="img.id"
                        :label="`${img.name}（${img.os}）`" :value="img.address" />
                    </el-select>
                    <div v-if="curArm.image_name && !isValidImage(curArm.image_name)"
                      class="hint danger">
                      镜像未在镜像库中注册，请从下拉选择已有镜像，或先到「镜像管理」登记
                    </div>
                  </div>

                  <div class="field">
                    <label class="lbl">沙箱配置</label>
                    <el-select :model-value="curArm.sandbox_config_id == null ? -1 : curArm.sandbox_config_id"
                      style="width:100%"
                      @update:model-value="v => curArm.sandbox_config_id = v === -1 ? null : v">
                      <el-option label="继承（用 Harness 模板原样）" :value="-1" />
                      <el-option label="不替换（同继承）" :value="0" />
                      <el-option v-for="sb in sandboxList" :key="sb.id"
                        :label="sb.name + (sb.is_default ? ' (默认)' : '')" :value="sb.id" />
                    </el-select>
                    <div class="hint">决定该 Harness 的沙箱资源规格、超时与环境变量；不同 Harness 常需不同镜像/资源池</div>
                  </div>
                </div>
              </template>
            </section>
          </div>

          <!-- ---------- Tab: 主Agent模型配置（矩阵的另一条轴，独立于 Harness） ---------- -->
          <div v-else-if="tab === 'hconfig'" class="split">
            <!-- 左：模型列表。一条扁平的轴 —— 每个模型都在所有 Harness 上各跑一遍 -->
            <aside class="list-col">
              <div class="list-head">
                <span>模型列表（{{ totalModels }}）</span>
              </div>
              <el-button class="add-btn" @click="addModel">
                <el-icon><Plus /></el-icon> 添加模型
              </el-button>

              <div v-if="!totalModels" class="list-empty">
                还没有配置模型。<br />模型与 Harness 正交，列表里的每个模型都会在每个 Harness 上各跑一遍。
              </div>

              <div v-for="(m, mi) in form.models" :key="m._uid" class="list-item"
                :class="{ active: selectedModel === mi }" @click="selectedModel = mi">
                <span class="li-tag model">M{{ mi + 1 }}</span>
                <div class="li-main">
                  <div class="li-name">{{ m.model_id || '未填模型 ID' }}</div>
                  <div class="li-sub">对所有 Harness 生效</div>
                </div>
                <el-button text class="li-more" @click.stop="removeModelAt(mi)">
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </aside>

            <!-- 右：选中模型的参数（字段顺序照搬单任务「Harness配置」tab） -->
            <section class="detail-col">
              <div v-if="!curModel" class="detail-empty">
                <el-empty :image-size="80" description="点左侧「添加模型」配置矩阵的另一条轴" />
              </div>
              <template v-else>
                <div class="detail-head">
                  <div class="dh-left">
                    <span class="dh-tag">M{{ selectedModel + 1 }}</span>
                    <span class="dh-title">{{ curModel.model_id || '未填模型 ID' }}</span>
                    <el-tag size="small">所有 Harness</el-tag>
                  </div>
                  <div class="dh-right">
                    <el-button type="danger" plain @click="removeModelAt(selectedArm, selectedModel)">
                      <el-icon><Delete /></el-icon> 删除
                    </el-button>
                  </div>
                </div>

                <div class="subpanel">
                  <div class="hint" style="margin-bottom:2px">
                    所属 Harness：<b>{{ curArm.label || configName(curArm.harness_config_id) || '未命名' }}</b>
                    （{{ harnessStore.label(curArm.harness_type) }}）。
                    每个模型独立生成一列矩阵；一个都不配则该 Harness 用兜底模型、只占 1 列。
                  </div>

                  <!-- 字段顺序照搬单任务：Base URL → API Key → API 类型 → 模型 ID（含验证） -->
                  <div class="field">
                    <label class="lbl2">模型 Base URL</label>
                    <el-autocomplete v-model="curModel.model_base_url" :fetch-suggestions="queryBaseUrls"
                      :placeholder="['opencode','pi','codex','grok'].includes(curArm.harness_type)
                        ? '选择或输入，例如：http://192.168.30.95:8084（自动补 /v1）'
                        : '选择或输入，例如：http://192.168.30.95:8084'"
                      style="width:100%" clearable :trigger-on-focus="true" fit-input-width>
                      <template #default="{ item }">
                        <span>{{ item.value }}</span>
                        <span class="opt-meta">{{ item.proxy_name || '未登记' }}{{ item.used ? ` · ${item.used} 次` : '' }}</span>
                      </template>
                    </el-autocomplete>
                  </div>

                  <div class="field">
                    <label class="lbl2">
                      {{ curArm.harness_type === 'claude-code' ? '模型 Token' : '模型 API Key' }}
                    </label>
                    <div class="row">
                      <el-input v-if="curModel.model_api_key"
                        :model-value="maskKey(curModel.model_api_key)" readonly />
                      <el-input v-else :model-value="keyDraft[curModel._uid] || ''"
                        @update:model-value="v => keyDraft[curModel._uid] = v"
                        @blur="commitDraftKey(curModel._uid, curModel)"
                        @keyup.enter="commitDraftKey(curModel._uid, curModel)"
                        placeholder="留空则按 Invite Code 自动申请" type="password" autocomplete="off" />
                      <el-button v-if="curModel.model_api_key"
                        @click="curModel.model_api_key = ''">清除</el-button>
                      <el-button type="primary"
                        @click="openKeyDialog('model', curModel._uid)">新建KEY</el-button>
                    </div>
                    <div v-if="curModel.model_api_key" class="hint">
                      已设置 Key，仅显示掩码；需更换请点「清除」后重新粘贴或新建
                    </div>
                  </div>

                  <!-- Invite Code 是最高优先级：填了它就表示「每个子任务各要一把独立 key」。
                       已填的 key 不浪费 —— 留给第一个子任务用，其余由后端各自申请。 -->
                  <div class="field">
                    <label class="lbl2">Invite Code</label>
                    <el-input v-model="curModel.invite_code" placeholder="例如：glm_coding_plan（留空则不自动申请）" />
                    <div class="hint">
                      <b>批量默认推荐填这里</b>：填了之后每个子任务都会用这个 invite code
                      各自申请一把独立 key，上游按 key 拆分用量，互不挤占额度。
                      <template v-if="curModel.model_api_key">
                        <b style="color:#e6a23c">已填的 Key 会分配给第一个子任务使用（不浪费），
                        其余子任务另行申请。</b>
                      </template>
                    </div>
                  </div>

                  <!-- API 类型始终可见：它的取值集合按 harness 变（openjiuwen 用
                       Anthropic/OpenAI，其余用协议名），但字段本身不该在某些 harness 下消失 -->
                  <div class="field">
                    <label class="lbl2">{{ curArm.harness_type === 'openjiuwen' ? 'Provider' : 'API 类型' }}</label>
                    <el-select v-model="curModel.model_api_type"
                      :placeholder="curArm.harness_type === 'openjiuwen'
                        ? '留空使用模板默认值 (Anthropic)' : '留空继承兜底值'"
                      clearable style="width:100%">
                      <el-option v-for="o in apiTypeOptions(curArm.harness_type)" :key="o.value"
                        :label="o.label" :value="o.value" />
                    </el-select>
                    <div v-if="apiTypeHint(curArm.harness_type)" class="hint">
                      {{ apiTypeHint(curArm.harness_type) }}
                    </div>
                  </div>

                  <div class="field">
                    <label class="lbl2">模型 ID</label>
                    <div class="row">
                      <el-input v-model="curModel.model_id" placeholder="例如：claude-opus-4-7-thinking" />
                      <el-button :loading="validating[curModel._uid]"
                        @click="validateModel(curModel)">验证模型</el-button>
                    </div>
                    <div class="hint">
                      用上面的 Base URL / API Key / 模型 ID 实打一次最小请求。指定了「API 类型」就按它调，
                      未指定则先试 Anthropic，不通再试 OpenAI
                    </div>
                    <ValidateModelResult :result="validateResults[curModel._uid]" />
                  </div>
                </div>
              </template>
            </section>
          </div>

          <!-- ---------- Tab: 用户模拟配置（通用配置，对所有 Harness 生效） ---------- -->
          <div v-else-if="tab === 'sim'" class="card">
            <div class="panel-title">
              用户模拟配置
              <span class="panel-note">（通用配置 · 对本批次全部 Harness 生效，不是矩阵维度）</span>
            </div>
            <div class="hint" style="margin-bottom:14px">
              这里配置的 Agent 会写进每个子任务的 <code>user_proxy_model.json</code>。
              与「Harness 类型」「Harness 配置」两处不同 —— 那两处决定矩阵有几列，这里只决定每个子任务内部怎么跑。
              不添加任何 Agent 时，子任务使用 Harness 模板自带的默认配置。
            </div>

            <div class="section-head" style="margin-bottom:10px">
              <span class="muted">Agent 列表（{{ sharedAgents.length }}）</span>
              <el-button type="primary" size="small" @click="addAgent">
                <el-icon><Plus /></el-icon> 添加 Agent
              </el-button>
            </div>

            <el-empty v-if="!sharedAgents.length" :image-size="60"
              description="未添加 Agent —— 使用 Harness 模板默认的 user_proxy_model.json" />

            <!-- 与单任务「用户模拟配置」tab 同口径：card 式 agent 分页，首张不可关 -->
            <el-tabs v-else :model-value="String(selectedAgentTab)"
              type="card" class="agent-tabs" @update:model-value="v => selectedAgentTab = Number(v)"
              @tab-remove="tabName => removeAgent(Number(tabName))">
              <el-tab-pane v-for="(ag, ai) in sharedAgents" :key="ai"
                :label="ag.name || '未命名'" :name="String(ai)" :closable="ai !== 0">
                <div class="field">
                  <label class="lbl2">Agent 名称</label>
                  <el-input v-model="ag.name" placeholder="例如：user_simulator" />
                </div>

                <!-- 候选带 /v1：后端对 agents[].base_url 是原样写入 user_proxy_model.json，
                     不像主 Agent 那样按 harness 自动补 /v1，所以这里必须给带后缀的值。 -->
                <div class="field">
                  <label class="lbl2">模型 Base URL</label>
                  <el-autocomplete v-model="ag.base_url" :fetch-suggestions="queryAgentBaseUrls"
                    placeholder="选择或输入，例如：http://192.168.30.95:8084/v1（需带 /v1）"
                    style="width:100%" clearable :trigger-on-focus="true" fit-input-width>
                    <template #default="{ item }">
                      <span>{{ item.value }}</span>
                      <span class="opt-meta">{{ item.proxy_name || '未登记' }}{{ item.used ? ` · ${item.used} 次` : '' }}</span>
                    </template>
                  </el-autocomplete>
                  <div class="hint">user_simulator 的 Base URL 需带 /v1 后缀</div>
                </div>

                <div class="field">
                  <label class="lbl2">模型 API Key</label>
                  <div class="row">
                    <!-- 与主 Agent 的 key 框同口径：已设置只显示掩码、不回显原文、无眼睛图标 -->
                    <el-input v-if="ag.api_key" :model-value="maskKey(ag.api_key)" readonly />
                    <el-input v-else :model-value="keyDraft[agentSlot(ai)] || ''"
                      @update:model-value="v => keyDraft[agentSlot(ai)] = v"
                      @blur="commitDraftKey(agentSlot(ai), ag)"
                      @keyup.enter="commitDraftKey(agentSlot(ai), ag)"
                      placeholder="留空则按 Invite Code 自动申请" type="password" autocomplete="off" />
                    <el-button v-if="ag.api_key" @click="ag.api_key = ''">清除</el-button>
                    <el-button type="primary" @click="openKeyDialog('agent', agentSlot(ai))">
                      新建KEY
                    </el-button>
                  </div>
                  <div v-if="ag.api_key" class="hint">
                    已设置 Key，仅显示掩码；需更换请点「清除」
                  </div>
                </div>

                <!-- agent 的 invite code：与主 Agent 同口径，填了就每个子任务各申请一把 -->
                <div class="field">
                  <label class="lbl2">Invite Code</label>
                  <el-input v-model="ag.invite_code" placeholder="例如：glm_coding_plan（留空则不自动申请）" />
                  <div class="hint">
                    填了之后，每个子任务都会用这个 invite code 为「{{ ag.name || '该 agent' }}」
                    各自申请一把独立 key。
                    <template v-if="ag.api_key">
                      <b style="color:#e6a23c">已填的 Key 会分配给第一个子任务使用（不浪费），
                      其余子任务另行申请。</b>
                    </template>
                  </div>
                </div>

                <div class="field">
                  <label class="lbl2">Provider</label>
                  <el-input v-model="ag.provider" placeholder="例如：local-evaluator" />
                  <div class="hint">留空时该 agent 不会被注入到 openclaw.json</div>
                </div>

                <div class="field">
                  <label class="lbl2">API 类型</label>
                  <el-select v-model="ag.api" placeholder="可选" clearable style="width:100%">
                    <el-option label="OpenAI Completions" value="openai-completions" />
                    <el-option label="Anthropic Messages" value="anthropic-messages" />
                  </el-select>
                </div>

                <div class="field">
                  <label class="lbl2">模型 ID</label>
                  <div class="row">
                    <el-input v-model="ag.model" placeholder="例如：gemini-3-flash-preview" />
                    <el-button :loading="validating[agentSlot(ai)]"
                      @click="validateAgent(ai)">验证模型</el-button>
                  </div>
                  <div class="hint">
                    用该 agent 的 Base URL / API Key / 模型 ID 实打一次最小请求（base_url 带 /v1 也能识别）
                  </div>
                  <ValidateModelResult :result="validateResults[agentSlot(ai)]" />
                </div>
              </el-tab-pane>
            </el-tabs>
          </div>

          <!-- ---------- Tab: 批量任务配置（队列配置 + 批次级参数） ---------- -->
          <div v-else-if="tab === 'batch'" class="card">
            <div class="panel-title">
              批量任务配置
              <span class="panel-note">（整批共用一条队列）</span>
            </div>
            <div class="hint" style="margin-bottom:14px">
              实际吞吐由两级并发相乘决定：<b>队列并发</b>（同时跑几个子任务）×
              <b>独立并发</b>（每个子任务内部同时开几个沙箱）。
              队列并发最终还会受平台全局上限封顶。
            </div>

            <div class="form-grid">
              <div class="field">
                <label class="lbl">队列名称</label>
                <el-input v-model="form.queue.name" :placeholder="form.name || '留空则用实验名称'" />
              </div>
              <div class="field">
                <label class="lbl">队列并发量</label>
                <el-input-number v-model="form.queue.max_concurrent" :min="1" :max="50" />
                <div class="hint">这条队列同时放行几个子任务</div>
              </div>
              <div class="field">
                <label class="lbl">独立并发</label>
                <el-input-number v-model="form.common.concurrent_num" :min="1" />
                <div class="hint">每个子任务内部同时跑几个沙箱</div>
              </div>
              <div class="field">
                <label class="lbl">每个样本重复次数</label>
                <el-input-number v-model="form.repeat_times" :min="1" :max="20" />
                <div class="hint">同一组合重复跑几轮（观察稳定性），会乘进总任务数</div>
              </div>
              <div class="field">
                <label class="lbl">长尾跳过</label>
                <el-switch v-model="form.queue.tail_skip_enabled"
                  active-text="开启" inactive-text="关闭" />
                <div class="hint">
                  队列里剩余任务已喂不满一轮并发时，额外放行一个，避免尾部拖长
                </div>
              </div>
              <div class="field">
                <label class="lbl">长尾阈值（%）</label>
                <el-input-number v-model="form.queue.tail_skip_percent" :min="1" :max="20"
                  :disabled="!form.queue.tail_skip_enabled" />
                <div class="hint">分母是独立并发；10% 且独立并发 50 → 剩余不足 5 个时触发</div>
              </div>
            </div>

            <div class="field" style="margin-top:16px">
              <label class="lbl">队列描述</label>
              <el-input v-model="form.queue.description" type="textarea" :rows="2"
                placeholder="可选，说明这条队列的用途" />
            </div>

            <div class="total-box" style="margin-top:18px">
              <div class="tb-left">
                <div class="tb-k">总执行实例数</div>
                <div class="tb-formula">
                  {{ sampleTotal }} 样本 × {{ modelCount }} 模型<template
                    v-if="form.repeat_times > 1"> × {{ form.repeat_times }} 轮</template>
                </div>
              </div>
              <div class="tb-v" :class="{ warn: totalTasks > BIG_THRESHOLD }">{{ totalTasks }}</div>
            </div>

            <div class="hint" style="margin-top:10px">
              同时最多占用沙箱 ≈ 队列并发 {{ form.queue.max_concurrent }}
              × 独立并发 {{ form.common.concurrent_num }} =
              <b>{{ form.queue.max_concurrent * form.common.concurrent_num }}</b>
              （实际以平台全局上限为准）
            </div>
          </div>

          <!-- ---------- Tab: 高级配置（子任务内部行为，不涉及矩阵维度） ---------- -->
          <div v-else class="card">
            <div class="panel-title">
              高级配置
              <span class="panel-note">（通用配置 · 对所有子任务生效）</span>
            </div>

            <div class="form-grid">
              <div class="field">
                <label class="lbl">沙箱最大存活（秒）</label>
                <el-input-number v-model="form.common.max_ttl_second" :min="1800" :max="36000" :step="600" />
                <div class="hint">
                  范围 1800 ~ 36000 秒；主程序超时 = 该值 − 600 秒，每条 queries[].timeout = 该值 − 1200 秒
                </div>
              </div>
              <div class="field">
                <label class="lbl">起始索引</label>
                <el-input-number v-model="form.common.start_index" :min="0" />
                <div class="hint">从测试集的第几条开始跑</div>
              </div>
              <div class="field">
                <label class="lbl">任务总数</label>
                <el-input-number v-model="form.common.total_num" :min="0" />
                <div class="hint">
                  每个子任务跑几条。<b>0 表示按测试集样本数</b>（{{ sampleTotal }} 条）；
                  填正数则只跑前 N 条，用于小样本冒烟或截断长数据集
                </div>
              </div>
              <div class="field">
                <label class="lbl">代码仓</label>
                <el-select v-model="form.common.code_repo_id" placeholder="选择代码仓（可选）"
                  style="width:100%" clearable>
                  <el-option v-for="r in codeRepoList" :key="r.id"
                    :label="`${r.name} · ${r.version}`" :value="r.id" />
                </el-select>
              </div>
              <div class="field">
                <label class="lbl">Key 追加沙箱后缀</label>
                <el-switch v-model="form.common.suffix_api_keys" active-text="开启" inactive-text="关闭" />
                <div class="hint">仅自部署模型需要；第三方模型须关闭，否则鉴权失败</div>
              </div>
              <div class="field">
                <label class="lbl">启动本地代理</label>
                <el-switch v-model="form.common.proxy_enable" active-text="开启" inactive-text="关闭" />
                <div class="hint">模型流量经本地端口转发并记录轨迹日志</div>
              </div>
            </div>

            <div class="field" style="margin-top:16px">
              <label class="lbl">任务队列</label>
              <div class="hint" style="margin-top:4px">
                本批次全部子任务由「批量任务配置」里的调度策略统一编入同一个批次队列
                （<code>batch-&lt;实验ID&gt;</code>），不单独选择队列。
              </div>
            </div>

            <div class="field" style="margin-top:16px">
              <label class="lbl">数据集 OBS 配置（只读）</label>
              <el-descriptions v-if="selectedReg" :column="2" border size="small">
                <el-descriptions-item label="任务路径">
                  <span :class="{ missing: !selectedReg.task_path_obs }">
                    {{ selectedReg.task_path_obs || '未填写 —— 无法作为测试集' }}
                  </span>
                </el-descriptions-item>
                <el-descriptions-item label="Skill 目录">
                  {{ selectedReg.skill_dir_obs || '未填写 —— 用模板默认值' }}
                </el-descriptions-item>
                <el-descriptions-item label="Agent 目录">
                  {{ selectedReg.agent_dir_obs || '未填写 —— 用模板默认值' }}
                </el-descriptions-item>
                <el-descriptions-item label="用户 Profile 目录">
                  {{ selectedReg.user_folder_obs || '未填写（可选）' }}
                </el-descriptions-item>
                <el-descriptions-item label="默认技能">
                  {{ selectedReg.default_skills || '未填写 —— 用模板默认值' }}
                </el-descriptions-item>
                <el-descriptions-item label="轨迹保存路径">
                  {{ selectedReg.traj_save_path || '自动生成 {harness}_trajs/traj_{任务标识}' }}
                </el-descriptions-item>
              </el-descriptions>
              <div class="hint">
                OBS 配置由数据集统一管理，批量实验内不可改。需要调整请到
                <el-link type="primary" @click="router.push('/task-register')">数据集管理</el-link>。
              </div>
            </div>
          </div>
        </template>

        <!-- ================= 步骤 2：矩阵预览 ================= -->
        <template v-else-if="step === 1">
          <div class="card">
            <div class="panel-title">任务矩阵</div>
            <div class="seg">
              <button class="seg-item" :class="{ active: matrixView === 'combo' }"
                @click="matrixView = 'combo'">组合视图</button>
              <button class="seg-item" :class="{ active: matrixView === 'tasks' }"
                @click="matrixView = 'tasks'">任务视图</button>
            </div>

            <template v-if="matrixView === 'combo'">
              <table class="matrix">
                <thead>
                  <tr>
                    <th class="corner">模型 \ Harness 类型</th>
                    <th v-for="(a, i) in form.harnesses" :key="a._uid" class="mh">
                      <div class="mh-name">
                        <el-tag :style="harnessStore.tagStyle(a.harness_type)" size="small"
                          style="margin-right:5px">{{ harnessStore.label(a.harness_type) }}</el-tag>
                        {{ a.label || configName(a.harness_config_id) || `H${i + 1}` }}
                      </div>
                      <div class="mh-sub">{{ versionName(a) || '未选版本' }}</div>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, ri) in matrixRows" :key="ri">
                    <td class="row-head">
                      <div>{{ row.label }}</div>
                      <div class="muted" style="font-size:11px">{{ row.sub }}</div>
                    </td>
                    <td v-for="(c, ci) in row.cells" :key="ci" class="cell">
                      <el-icon v-if="c" color="#22c55e" :size="18"><CircleCheckFilled /></el-icon>
                      <span v-else class="muted">—</span>
                    </td>
                  </tr>
                </tbody>
              </table>
              <div class="hint" style="margin-top:10px">
                行 = 模型，列 = Harness 类型。勾选单元格 = 一个可执行的子任务；
                每个子任务再 × {{ form.repeat_times }} 次重复，完整跑一遍 {{ effectiveSampleTotal }} 条样本。
                共 <b>{{ matrixRows.length }}</b> 个组合。用户模拟配置是通用配置，对各列一视同仁，不额外乘。
              </div>
            </template>

            <template v-else>
              <el-table :data="taskPreview" size="small" border max-height="520" style="width:100%">
                <el-table-column type="index" label="#" width="60" />
                <el-table-column label="Harness" min-width="180">
                  <template #default="{ row }">
                    <el-tag :style="harnessStore.tagStyle(row.harness_type)" size="small">
                      {{ harnessStore.label(row.harness_type) }}
                    </el-tag>
                    <span style="margin-left:8px">{{ row.label }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="用户模拟" width="150">
                  <template #default>{{ agentsLabel() }}</template>
                </el-table-column>
                <el-table-column prop="model_id" label="模型" width="180" />
                <el-table-column prop="repeat_idx" label="轮次" width="80" align="center" />
                <el-table-column prop="sample_total" label="样本数" width="100" align="right" />
              </el-table>
              <div v-if="taskPreviewTotal > taskPreview.length" class="hint" style="margin-top:8px">
                共 {{ taskPreviewTotal }} 个子任务，上表只预览前 {{ taskPreview.length }} 条。
              </div>
            </template>
          </div>
        </template>

        <!-- ================= 步骤 3：确认创建 ================= -->
        <template v-else>
          <div class="card">
            <div class="panel-title">确认创建</div>
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="实验名称">{{ form.name }}</el-descriptions-item>
              <el-descriptions-item label="测试集">
                {{ selectedReg?.task_name }}（{{ sampleTotal }} 条）
              </el-descriptions-item>
              <el-descriptions-item label="Harness 数">{{ form.harnesses.length }}</el-descriptions-item>
              <el-descriptions-item label="主Agent模型配置数">{{ modelCount }}</el-descriptions-item>
              <el-descriptions-item label="用户模拟 Agent">{{ form.agents.length || '模板默认' }}</el-descriptions-item>
              <el-descriptions-item label="重复次数">{{ form.repeat_times }}</el-descriptions-item>
              <el-descriptions-item label="队列">
                {{ form.queue.name || form.name || '未命名' }}
                （并发 {{ form.queue.max_concurrent }} × 独立 {{ form.common.concurrent_num }}）
              </el-descriptions-item>
              <el-descriptions-item label="子任务数">{{ subtaskCount }}</el-descriptions-item>
            </el-descriptions>

            <div v-if="blockingIssues.length" class="issue-box">
              <div class="issue-title">
                <el-icon><WarningFilled /></el-icon> 还有 {{ blockingIssues.length }} 项需要处理
              </div>
              <ul>
                <li v-for="(m, i) in blockingIssues" :key="i">{{ m }}</li>
              </ul>
            </div>
            <div v-else class="ok-box">
              <el-icon><CircleCheckFilled /></el-icon>
              配置完整，可以创建。将生成 {{ subtaskCount }} 个子任务、共 {{ totalTasks }} 条任务。
            </div>

            <div class="field" style="margin-top:16px">
              <label class="lbl">实验描述</label>
              <el-input v-model="form.description" type="textarea" :rows="3" placeholder="可选" />
            </div>
          </div>
        </template>
      </main>

      <!-- ================= 右侧：矩阵预览 + 执行规模 ================= -->
      <aside class="bc-side">
        <div class="card side-card">
          <div class="side-title">
            <el-icon :size="17"><Grid /></el-icon> 任务矩阵预览
          </div>

          <div class="mini-matrix">
            <div class="mm-head">
              <div class="mm-corner">模型 \ Harness</div>
              <div v-for="(a, i) in sideCols" :key="a._uid || i" class="mm-col">
                <div class="mm-col-name">{{ a.short }}</div>
                <div class="mm-col-sub">{{ a.sub }}</div>
              </div>
            </div>
            <div v-for="(r, ri) in sideRows" :key="ri" class="mm-row">
              <div class="mm-row-name">{{ r.short }}</div>
              <div v-for="(c, ci) in r.cells" :key="ci" class="mm-cell">
                <el-icon v-if="c" color="#22c55e" :size="16"><CircleCheckFilled /></el-icon>
                <span v-else class="dot">—</span>
              </div>
            </div>
          </div>
        </div>

        <div class="card side-card">
          <div class="side-title">
            <el-icon :size="17"><DataAnalysis /></el-icon> 执行规模
          </div>
          <div class="stat-grid">
            <div class="stat">
              <div class="stat-ic"><el-icon><Tickets /></el-icon></div>
              <div>
                <div class="stat-k">测试任务数</div>
                <div class="stat-v">{{ sampleTotal }}</div>
              </div>
            </div>
            <div class="stat">
              <div class="stat-ic"><el-icon><Files /></el-icon></div>
              <div>
                <div class="stat-k">Harness 数</div>
                <div class="stat-v">{{ form.harnesses.length }}</div>
              </div>
            </div>
            <div class="stat">
              <div class="stat-ic"><el-icon><User /></el-icon></div>
              <div>
                <div class="stat-k">用户模拟 Agent</div>
                <div class="stat-v">{{ form.agents.length }}</div>
              </div>
            </div>
            <div class="stat">
              <div class="stat-ic"><el-icon><Box /></el-icon></div>
              <div>
                <div class="stat-k">主Agent模型配置数</div>
                <div class="stat-v">{{ modelCount }}</div>
              </div>
            </div>
          </div>

          <div class="total-box">
            <div class="tb-left">
              <div class="tb-k">总执行实例数</div>
              <div class="tb-formula">
                {{ sampleTotal }} × {{ modelCount }}<template v-if="form.repeat_times > 1"> × {{ form.repeat_times }}</template>
              </div>
            </div>
            <div class="tb-v" :class="{ warn: totalTasks > BIG_THRESHOLD }">{{ totalTasks }}</div>
          </div>

          <div v-if="totalTasks > BIG_THRESHOLD" class="warn-box">
            <el-icon><WarningFilled /></el-icon>
            任务量较大（{{ totalTasks }}），请确认资源池能承载
          </div>
          <div v-else class="ok-box small">
            <el-icon><CircleCheckFilled /></el-icon>
            当前共 {{ form.harnesses.length }} 个 Harness（{{ modelCount }} 个主Agent模型配置），
            用户模拟为通用配置，将生成 {{ totalTasks }} 个执行任务。
          </div>
        </div>
      </aside>
    </div>

    <!-- ============ 新建 API Key（与单任务同口径） ============ -->
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

    <!-- ================= 底部操作 ================= -->
    <footer class="bc-foot">
      <el-button v-if="step === 0" @click="router.push('/batch')">取消</el-button>
      <el-button v-else @click="step--">上一步</el-button>
      <el-button v-if="step < 2" type="primary" :disabled="!canNext" @click="step++">
        下一步：{{ STEPS[step + 1] }}
        <el-icon style="margin-left:4px"><ArrowRight /></el-icon>
      </el-button>
      <el-button v-else type="primary" :loading="creating" :disabled="blockingIssues.length > 0"
        @click="submit">创建实验</el-button>
    </footer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft, ArrowRight, Plus, Delete, CopyDocument, EditPen, User, Box,
  Files, Grid, DataAnalysis, Tickets, CircleCheckFilled, WarningFilled,
  MoreFilled, Setting, Collection, MagicStick, Document,
} from '@element-plus/icons-vue'
import api from '../api'
import { useHarnessStore } from '../stores/harness'
import ValidateModelResult from '../components/ValidateModelResult.vue'

const route = useRoute()
const router = useRouter()
const harnessStore = useHarnessStore()

const BIG_THRESHOLD = 2000
const PREVIEW_LIMIT = 200

const STEPS = ['配置实验', '矩阵预览', '确认创建']
// icon 用组件引用而不是字符串：<component :is="'X'"> 走的是全局注册表，
// 而 main.js 只全局注册了部分图标，Collection / MagicStick 不在其中。
const TABS = [
  { key: 'harness', label: 'Harness 类型', icon: Collection },
  { key: 'hconfig', label: '主Agent模型配置', icon: Setting },
  { key: 'sim', label: '用户模拟配置', icon: User },
  { key: 'batch', label: '批量任务配置', icon: Tickets },
  { key: 'adv', label: '高级配置', icon: MagicStick },
]

const step = ref(0)
const tab = ref('harness')
const matrixView = ref('combo')
const creating = ref(false)
const selectedArm = ref(-1)
// 「Harness 配置」tab 里当前选中的模型（相对所属 Harness 的下标）
const selectedModel = ref(0)

const regList = ref([])
const imageList = ref([])
const sandboxList = ref([])
const codeRepoList = ref([])
const armOptions = ref([])
const baseUrlOptions = ref([])

let _uidSeq = 0
const uid = () => `u${++_uidSeq}`

const form = ref({
  name: '',
  description: '',
  registration_id: null,
  // 矩阵的两条正交轴：Harness 列表 × 模型列表，每个模型在每个 Harness 上都跑。
  harnesses: [],
  models: [],
  // 用户模拟 = 通用配置，写进每个子任务的 user_proxy_model.json。
  // 默认给一行 user_simulator（与单任务表单一致），清空则用模板默认值。
  agents: [emptyAgent()],
  repeat_times: 1,
  // 整批一条队列；吞吐 = 队列并发 × 独立并发(common.concurrent_num)
  queue: {
    name: '',
    max_concurrent: 10,
    tail_skip_enabled: false,
    tail_skip_percent: 10,
    description: '',
  },
  common: {
    concurrent_num: 100,
    image_name: '',
    max_ttl_second: 36000,
    start_index: 0,
    total_num: 0,
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

function emptyModel() {
  return {
    _uid: uid(), model_id: '', model_base_url: '', model_api_key: '',
    model_api_type: '', invite_code: '',
  }
}
function emptyHarness() {
  return {
    _uid: uid(), harness_type: '', harness_config_id: null,
    harness_config_version_id: null, label: '', description: '',
    run_mode: 'standard', sys_file_version: '', skill_set: '',
    image_name: '', sandbox_config_id: null,
  }
}
function emptyAgent() {
  return { name: '', model: '', provider: '', base_url: '', api_key: '', api: '', invite_code: '' }
}

const curArm = computed(() =>
  selectedArm.value >= 0 ? form.value.harnesses[selectedArm.value] || null : null)

// 「主Agent模型配置」tab：一条扁平的模型轴，不属于任何 Harness —— 每个模型都在所有
// Harness 上各跑一遍（与 Harness 正交）。原名 allModels 保留，模板里直接用。
const allModels = computed(() => form.value.models)
const totalModels = computed(() => form.value.models.length)

const curModel = computed(() =>
  form.value.models[selectedModel.value] || null)
// 用户模拟配置是通用配置：一份 agents，作用于全部子任务
const sharedAgents = computed(() => form.value.agents)

const selectedReg = computed(() =>
  regList.value.find(r => r.id === form.value.registration_id) || null)
const sampleTotal = computed(() => selectedReg.value?.data_total || 0)

const modelCount = computed(() => form.value.models.length)
// 每个子任务实际跑几条：任务总数 0 = 按测试集样本数；>0 = 按填的值截断。
// 与后端 create_experiment 里的 limit_num 同一口径，否则预览的执行规模会对不上。
const effectiveSampleTotal = computed(() => {
  const n = Number(form.value.common.total_num) || 0
  return n > 0 ? Math.min(n, sampleTotal.value || n) : sampleTotal.value
})
// 矩阵两条轴：Harness 数 × 模型数；用户模拟与高级配置是通用配置，不进乘法
const subtaskCount = computed(() =>
  form.value.harnesses.length * modelCount.value * form.value.repeat_times)
const totalTasks = computed(() => subtaskCount.value * effectiveSampleTotal.value)

function agentsLabel() {
  const names = (form.value.agents || []).map(x => (x.name || '').trim()).filter(Boolean)
  return names.length ? names.join('+') : '模板默认'
}

// ---------- 右侧迷你矩阵 ----------
const sideCols = computed(() => {
  if (!form.value.harnesses.length) return [{ short: '—', sub: '未配置' }]
  return form.value.harnesses.map((a, i) => ({
    _uid: a._uid,
    short: a.label || configName(a.harness_config_id) || `H${i + 1}`,
    sub: versionName(a) ? `（${versionName(a)}）` : '',
  }))
})

// 矩阵两轴：列 = Harness（类型/方案/版本），行 = 模型。
// 两条轴正交，所以每个格子都打勾（Harness 数 × 模型数 = 子任务配置数）。
// 模型列表为空时没有可跑的配置，画一行「未配置模型」提示，而不是假装有兜底。
const matrixAxisRows = computed(() => {
  const rows = form.value.models.map((m, mi) => ({
    key: m._uid,
    name: m.model_id || `模型 ${mi + 1}`,
    cells: form.value.harnesses.map(() => true),
  }))
  if (!rows.length) {
    rows.push({
      key: '__empty__',
      name: '未配置模型',
      cells: form.value.harnesses.map(() => false),
    })
  }
  return rows
})

const sideRows = computed(() => matrixAxisRows.value.map(r => ({
  short: r.name,
  cells: r.cells,
})))

const matrixRows = computed(() => matrixAxisRows.value.map(r => ({
  label: r.name,
  sub: '',
  cells: r.cells,
})))

// ---------- 任务视图预览 ----------
const flatArms = computed(() => flattenArms())
const taskPreviewTotal = computed(() =>
  flatArms.value.length * form.value.repeat_times)

const taskPreview = computed(() => {
  const out = []
  for (const a of flatArms.value) {
    for (let r = 1; r <= form.value.repeat_times; r++) {
      out.push({
        harness_type: a.harness_type,
        label: a.label,
        model_id: a.model_id || '兜底',
        repeat_idx: r,
        sample_total: effectiveSampleTotal.value,
      })
      if (out.length >= PREVIEW_LIMIT) return out
    }
  }
  return out
})

// 提交时把两条正交轴原样发给后端：harnesses（Harness 轴）+ models（模型轴）。
// 后端按笛卡尔积展开成子任务，params_json 里也原样保存这两条轴 ——
// 详情页/复制页因此还原得出「哪几个 Harness、哪几个模型」，而不是从摊平结果反推。
function buildHarnesses() {
  return form.value.harnesses.map(a => ({
    harness_type: a.harness_type,
    harness_config_id: a.harness_config_id,
    harness_config_version_id: a.harness_config_version_id,
    label: a.label || configName(a.harness_config_id) || a.harness_type,
    image_name: a.image_name || null,
    sandbox_config_id: a.sandbox_config_id ?? null,
  }))
}

function buildModels() {
  return form.value.models.map(m => ({
    model_id: (m.model_id || '').trim(),
    // base_url 原样传，不做 /v1 规整：各 harness 在 create_instance 里自己补或剥。
    model_base_url: (m.model_base_url || '').trim(),
    model_api_key: m.model_api_key || '',
    model_api_type: m.model_api_type || '',
    invite_code: (m.invite_code || '').trim(),
  }))
}

// 摊平后的臂：显式给后端一份「Harness × 模型」，同时用作前端预览。
// label 带上 Harness 名，保证对比页每一列唯一可辨。
function flattenArms() {
  const out = []
  for (const a of form.value.harnesses) {
    const base = a.label || configName(a.harness_config_id) || a.harness_type
    for (const m of form.value.models) {
      out.push({
        harness_type: a.harness_type,
        harness_config_id: a.harness_config_id,
        harness_config_version_id: a.harness_config_version_id,
        label: m.model_id ? `${base} · ${m.model_id}` : base,
        model_base_url: (m.model_base_url || '').trim(),
        model_api_key: m.model_api_key || '',
        model_id: (m.model_id || '').trim(),
        model_api_type: m.model_api_type || '',
        invite_code: (m.invite_code || '').trim(),
      })
    }
  }
  return out
}

// ---------- 校验 ----------
const blockingIssues = computed(() => {
  const issues = []
  if (!form.value.name.trim()) issues.push('实验名称为空')
  if (!form.value.registration_id) issues.push('未选择测试集')
  else if (!selectedReg.value?.task_path_obs) issues.push('该数据集未填写任务路径（task_path_obs）')
  if (!form.value.harnesses.length) issues.push('至少需要添加一个 Harness')
  // 模型轴独立于 Harness，空列表没有业务含义 —— 老版本正是靠「块内 0 模型 +
  // 兜底模型」静默退化成模板占位模型（token 字面量 keys），跑出来的数据不可用。
  if (!form.value.models.length) issues.push('至少需要添加一个模型')

  form.value.harnesses.forEach((a, i) => {
    const no = `H${i + 1}`
    if (!a.harness_type) issues.push(`${no} 未选择 Harness 类型`)
    if (a.harness_config_id == null) issues.push(`${no} 未选择执行方案`)
    if (a.harness_config_version_id == null) issues.push(`${no} 未选择配置版本`)
    if (a.image_name && !isValidImage(a.image_name)) {
      issues.push(`${no} 的镜像「${a.image_name}」未在镜像库中注册，请从下拉选择或先到「镜像管理」登记`)
    }
  })
  form.value.models.forEach((m, mi) => {
    if (!(m.model_id || '').trim()) issues.push(`第 ${mi + 1} 个模型未填模型 ID`)
  })
  form.value.agents.forEach((ag, ai) => {
    if (!(ag.name || '').trim()) issues.push(`第 ${ai + 1} 个模拟 Agent 未填名称`)
  })
  if (totalTasks.value > BIG_THRESHOLD) {
    // 只是提醒，不阻止创建
  }
  return issues
})

const canNext = computed(() => {
  if (step.value === 0) {
    return !!form.value.name.trim() && !!form.value.registration_id &&
      !!selectedReg.value?.task_path_obs && form.value.harnesses.length > 0 &&
      form.value.harnesses.every(a => a.harness_config_version_id != null) &&
      true
  }
  if (step.value === 1) return blockingIssues.value.length === 0
  return true
})

function gotoStep(i) {
  // 只能往回走；往前走要过校验
  if (i < step.value) { step.value = i; return }
  if (i > step.value && !canNext.value) {
    ElMessage.warning(blockingIssues.value[0] || '请先完善当前步骤')
    return
  }
  if (i > step.value + 1) return
  step.value = i
}

// ---------- 展示辅助 ----------
function configName(id) {
  const o = armOptions.value.find(x => x.config_id === id)
  return o ? o.config_name : ''
}
function versionName(a) {
  const o = armOptions.value.find(x => x.config_id === a.harness_config_id)
  if (!o) return ''
  const v = o.versions.find(x => x.id === a.harness_config_version_id)
  return v ? v.version : ''
}
// 版本下拉的发布时间格式，与 CreateInstance 的同名函数保持完全一致 —
// 两页展示同一批版本，文案不一致会让人以为版本不同（如「v1.0 · 11-18 发布」
// vs「1.0 · 从旧配置迁移」）。
function formatShortTime(t) {
  return String(t || '').replace('T', ' ').slice(5, 16)
}
function configsOf(t) {
  return armOptions.value.filter(o => o.harness_type === t)
}
function versionsOf(a) {
  const o = armOptions.value.find(x => x.config_id === a.harness_config_id)
  return o ? o.versions : []
}
const armHarnessTypes = computed(() => {
  const seen = []
  for (const o of armOptions.value) {
    if (!seen.includes(o.harness_type)) seen.push(o.harness_type)
  }
  return seen
})

const API_TYPE_HINTS = {
  opencode: '决定 provider 的 npm 包：anthropic 用 @ai-sdk/anthropic，openai 用 @ai-sdk/openai-compatible',
  pi: '写入 models.json 的 providers.custom.api 字段',
  hermes: '写入 config.yaml 的 providers.<name>.transport',
  grok: '写入 grok/config.toml 的 model.default-model.api_backend',
  'jiuwenswarm-dev': '写入 harness 配置文件（dev= jw_dev_config.yaml）的 client_provider；base_url 的 /v1 由后端按此自动补齐或去除',
  'jiuwenswarm-xiaoyi': '写入 harness 配置文件（xiaoyi= jw_xiaoyi_config.yaml）的 client_provider；base_url 的 /v1 由后端按此自动补齐或去除',
  openjiuwen: 'tokenfly 网关走 anthropic-messages 协议，一般选 Anthropic',
}
function apiTypeHint(t) {
  return API_TYPE_HINTS[t] || ''
}

// 所有 Harness 都给出可选项 —— 用不上的（如 claude-code / codex）留空即可，
// 不显示「该类型不使用此字段」这类提示。取值集合只在 openjiuwen 不同：
// 它写的是 Provider 名（Anthropic/OpenAI），其余写协议名。
function apiTypeOptions(t) {
  if (t === 'openjiuwen') {
    return [{ label: 'Anthropic', value: 'Anthropic' }, { label: 'OpenAI', value: 'OpenAI' }]
  }
  return [
    { label: 'Anthropic Messages', value: 'anthropic-messages' },
    { label: 'OpenAI Completions', value: 'openai-completions' },
  ]
}

function maskKey(key) {
  if (!key) return ''
  if (key.length <= 8) return '****'
  return key.slice(0, 4) + '***' + key.slice(-4)
}

const keyDraft = ref({})

// 用户模拟配置里当前查看的 agent 卡片下标
const selectedAgentTab = ref(0)

// ---------- 镜像名称校正 ----------
// imageList 里：name = 短名（如 multiharness:26.9.24v1），address = 完整地址
// 两个查找表：短名 → 地址、地址 → 短名
const imgNameToAddr = computed(() => {
  const m = {}
  for (const img of imageList.value) m[img.name] = img.address
  return m
})
const imgAddrSet = computed(() => new Set(imageList.value.map(i => i.address)))

// 传入的值是否合法（在镜像库里注册过）
function isValidImage(v) {
  if (!v) return true  // 空 = 留空，合法
  return imgAddrSet.value.has(v) || !!imgNameToAddr.value[v]
}

// 把短名归一成完整地址；已是完整地址不变；查不到的保留原值（让 hint 提示）
function resolveImageAddr(v) {
  if (!v) return v
  if (imgAddrSet.value.has(v)) return v
  return imgNameToAddr.value[v] || v
}

function normalizeImageName(arm) {
  if (!arm || !arm.image_name) return
  const resolved = resolveImageAddr(arm.image_name)
  if (resolved !== arm.image_name) {
    arm.image_name = resolved
  }
}

// @change 时立刻尝试归一（下拉选时值就是 address，手打时走归一）
function onImageChange(v, arm) {
  if (!arm) return
  arm.image_name = resolveImageAddr(v || '')
}

// ---------- 新建 KEY 弹窗（与单任务同口径） ----------
const keyDialogVisible = ref(false)
const keyGenerating = ref(false)
const keyForm = ref({
  invite_code: localStorage.getItem('last_invite_code') || 'pangu',
  name: '',
})
// target: { type: 'model' | 'agent', slot } —— slot 是模型 _uid 或 agent 的槽位键
let keyTarget = { type: 'model', slot: '' }

// 按 slot 找回真正要写入的对象
function findKeyTarget() {
  const { type, slot } = keyTarget
  if (type === 'model') {
    return form.value.models.find(x => x._uid === slot) || null
  }
  const m = /^a(\d+)$/.exec(slot)
  if (!m) return null
  return form.value.agents[Number(m[1])] || null
}

function openKeyDialog(type, slot) {
  keyTarget = { type, slot }
  const now = new Date()
  const ts = [
    String(now.getFullYear()).slice(2),
    String(now.getMonth() + 1).padStart(2, '0'),
    String(now.getDate()).padStart(2, '0'),
    String(now.getHours()).padStart(2, '0'),
    String(now.getMinutes()).padStart(2, '0'),
  ].join('')
  keyForm.value.name = `${ts}-${form.value.name || '批量实验'}`
  keyDialogVisible.value = true
}

async function generateApiKey() {
  keyGenerating.value = true
  try {
    const params = {
      invite_code: keyForm.value.invite_code,
      name: keyForm.value.name,
    }
    const target = findKeyTarget()
    const baseUrl = keyTarget.type === 'model'
      ? (target?.model_base_url || form.value.common.model_base_url)
      : (target?.base_url || '')
    if (baseUrl) params.base_url = baseUrl

    const res = await api.post('/generate-api-key', null, { params })
    if (target) {
      if (keyTarget.type === 'model') target.model_api_key = res.api_key
      else {
        target.api_key = res.api_key
        target.invite_code = keyForm.value.invite_code
      }
    }
    localStorage.setItem('last_invite_code', keyForm.value.invite_code)
    ElMessage.success(`API Key 已生成并填入：${maskKey(res.api_key)}（原文不回显）`)
    keyDialogVisible.value = false
  } catch {
    // 错误已由 api 拦截器提示
  } finally {
    keyGenerating.value = false
  }
}
function commitDraftKey(slot, target) {
  const v = String(keyDraft.value[slot] || '').trim()
  if (v && target && !target.api_key) target.api_key = v
  keyDraft.value[slot] = ''
}

// ---------- Harness 增删改 ----------
function addArm() {
  form.value.harnesses.push(emptyHarness())
  selectedArm.value = form.value.harnesses.length - 1
  tab.value = 'harness'
}

function selectArm(i) {
  selectedArm.value = i
}

function onArmTypeChange() {
  const a = curArm.value
  a.harness_config_id = null
  a.harness_config_version_id = null
  const cs = configsOf(a.harness_type)
  if (cs.length === 1) {
    a.harness_config_id = cs[0].config_id
    onArmSchemeChange()
  } else {
    applyArmDefaults(a)
  }
}

function onArmSchemeChange() {
  const a = curArm.value
  a.harness_config_version_id = null
  const vs = versionsOf(a)
  // 接口按 created_at DESC 返回，[0] 是最新已发布版本
  if (vs.length) a.harness_config_version_id = vs[0].id
  applyArmDefaults(a)
}

// ---------- 镜像 / 代码仓默认带出 ----------
// 与 CreateInstance 的 defaultImageAddress / defaultRepoId / applyDefaults 同口径：
// 选了 harness 就自动带出该 harness 映射的默认镜像与默认代码仓，用户可再改。
//
// 为什么批量页也要：单实例页一直有这个行为，两边不一致会让用户以为批量页"漏配了"。
// 镜像按 arm 各自生效（不同 harness 的默认镜像不同）；代码仓是批次级 common 字段，
// 只在为空时带出，不覆盖用户已选的值。
const defaultRepoId = computed(() => {
  const d = (codeRepoList.value || []).find(r => r.enabled !== false && r.is_default)
  return d ? d.id : null
})

function defaultImageAddressFor(harnessType) {
  const list = (imageList.value || []).filter(i => i.enabled !== false)
  if (!harnessType) {
    const common = list.find(i => (i.defaults || []).includes('common'))
    return common ? common.address : ''
  }
  const hit = list.find(i => (i.defaults || []).includes(harnessType))
  if (hit) return hit.address
  const common = list.find(i => (i.defaults || []).includes('common'))
  return common ? common.address : ''
}

function applyArmDefaults(a) {
  if (!a) return
  // 镜像：只在 arm 还没选过时带出，避免把用户手选的值冲掉
  if (!a.image_name) {
    const addr = defaultImageAddressFor(a.harness_type)
    if (addr) a.image_name = addr
  }
  // 代码仓是批次级字段，同样只在空时带出
  if (form.value.common.code_repo_id == null) {
    const rid = defaultRepoId.value
    if (rid) form.value.common.code_repo_id = rid
  }
}

function copyArm(i) {
  const src = form.value.harnesses[i]
  const clone = { ...src, _uid: uid() }
  form.value.harnesses.splice(i + 1, 0, clone)
  selectedArm.value = i + 1
  ElMessage.success('已复制')
}

async function removeArm(i) {
  if (form.value.harnesses.length <= 1) {
    try {
      await ElMessageBox.confirm('删除最后一个 Harness 后将无法创建，确定？', '提示', { type: 'warning' })
    } catch { return }
  }
  form.value.harnesses.splice(i, 1)
  if (selectedArm.value >= form.value.harnesses.length) {
    selectedArm.value = form.value.harnesses.length - 1
  }
}

function onArmCommand(cmd, i) {
  if (cmd === 'copy') copyArm(i)
  else if (cmd === 'delete') removeArm(i)
}

async function renameArm() {
  if (!curArm.value) return
  try {
    const { value } = await ElMessageBox.prompt('展示名', '重命名', {
      inputValue: curArm.value.label || '',
      inputPlaceholder: configName(curArm.value.harness_config_id) || '留空用方案名',
    })
    curArm.value.label = (value || '').trim()
  } catch { /* cancelled */ }
}

// ---------- 模型轴（独立于 Harness，对所有 Harness 生效） ----------
function addModel() {
  form.value.models.push(emptyModel())
  selectedModel.value = form.value.models.length - 1
}
function copyModel(i) {
  const src = form.value.models[i]
  if (!src) return
  form.value.models.splice(i + 1, 0, { ...src, _uid: uid() })
  selectedModel.value = i + 1
  ElMessage.success('已复制')
}
function removeModelAt(mi) {
  form.value.models.splice(mi, 1)
  if (selectedModel.value >= form.value.models.length) {
    selectedModel.value = Math.max(form.value.models.length - 1, 0)
  }
}

// ---------- 用户模拟（通用配置，一份 agents 作用于全部子任务） ----------
function addAgent() {
  form.value.agents.push(emptyAgent())
  selectedAgentTab.value = form.value.agents.length - 1
}
function removeAgent(ai) {
  const list = form.value.agents
  if (list.length <= 1) {
    ElMessage.warning('模拟配置至少要保留一个 Agent，或直接清空列表')
    return
  }
  list.splice(ai, 1)
  // 删掉的正好是当前查看的那张卡（或更靠后的），收敛到最后一张
  if (selectedAgentTab.value >= list.length) selectedAgentTab.value = list.length - 1
  // splice 让后面的 agent 下标整体前移，按下标键控的验证结果/录入缓冲就对不上人了
  // （agent2 的结果会显示在 agent1 底下）。索引变动后一律丢弃，重新验证即可。
  for (const k of Object.keys(validateResults.value)) {
    if (k.startsWith('a')) delete validateResults.value[k]
  }
  for (const k of Object.keys(keyDraft.value)) {
    if (k.startsWith('a')) delete keyDraft.value[k]
  }
}
function agentSlot(ai) {
  return `a${ai}`
}

// 清空整份模拟配置 → 退回「用 Harness 模板默认的 user_proxy_model.json」
function clearAgents() {
  form.value.agents = []
  selectedAgentTab.value = 0
  validateResults.value = {}
  keyDraft.value = {}
}

// ---------- Base URL 候选 ----------
async function loadBaseUrls() {
  try {
    const res = await api.get('/export-meta/base-urls')
    baseUrlOptions.value = res.options || []
  } catch { baseUrlOptions.value = [] }
}
const agentBaseUrlOptions = computed(() => baseUrlOptions.value.map(o => ({
  ...o,
  value: /\/v\d+$/.test(o.value) ? o.value : `${o.value.replace(/\/$/, '')}/v1`,
})))
function filterBaseUrls(list, q) {
  const s = (q || '').trim().toLowerCase()
  if (!s) return list
  return list.filter(o =>
    o.value.toLowerCase().includes(s) || (o.proxy_name || '').toLowerCase().includes(s))
}
function queryBaseUrls(q, cb) { cb(filterBaseUrls(baseUrlOptions.value, q)) }
function queryAgentBaseUrls(q, cb) { cb(filterBaseUrls(agentBaseUrlOptions.value, q)) }

// ---------- 验证模型 ----------
const validating = ref({})
const validateResults = ref({})

async function validateModel(m) {
  const slot = m._uid
  validating.value[slot] = true
  validateResults.value[slot] = null
  try {
    validateResults.value[slot] = await api.post('/harness-meta/validate-model', {
      model_base_url: (m.model_base_url || '').trim() || form.value.common.model_base_url,
      model_api_key: m.model_api_key || form.value.common.model_api_key,
      model_api_type: m.model_api_type || form.value.common.model_api_type,
      model_id: (m.model_id || '').trim() || form.value.common.model_id,
    }, { silent: true })
  } catch (e) {
    validateResults.value[slot] = {
      ok: false, tried: [], request_url: '',
      error: e.response?.data?.detail || e.message || '请求失败',
    }
  } finally {
    validating.value[slot] = false
  }
}

async function validateAgent(ai) {
  const ag = form.value.agents[ai]
  if (!ag) return
  const slot = agentSlot(ai)
  validating.value[slot] = true
  validateResults.value[slot] = null
  try {
    validateResults.value[slot] = await api.post('/harness-meta/validate-model', {
      model_base_url: ag.base_url || '',
      model_api_key: ag.api_key || '',
      model_api_type: ag.api || '',
      model_id: ag.model || '',
    }, { silent: true })
  } catch (e) {
    validateResults.value[slot] = {
      ok: false, tried: [], request_url: '',
      error: e.response?.data?.detail || e.message || '请求失败',
    }
  } finally {
    validating.value[slot] = false
  }
}

// ---------- 加载 ----------
async function loadArmOptions() {
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
  }
}

function onRegChange() {
  if (!form.value.name && selectedReg.value) {
    form.value.name = `${selectedReg.value.task_name} 横向评测`
  }
}

async function submit() {
  if (blockingIssues.value.length) {
    ElMessage.warning(blockingIssues.value[0])
    step.value = 0
    return
  }
  // 失焦没触发就点了提交 —— 把缓冲里的 key 补回来，别丢用户输入
  for (const m of form.value.models) commitDraftKey(m._uid, m)
  form.value.agents.forEach((ag, ai) => commitDraftKey(agentSlot(ai), ag))

  creating.value = true
  try {
    const payload = {
      name: form.value.name.trim(),
      description: form.value.description,
      registration_id: form.value.registration_id,
      // 两条正交轴：Harness 列表 × 模型列表 —— 后端据此存 params_json 并按
      // 笛卡尔积展开成子任务（一个模型在一个 Harness 上 = 一个子任务）
      harnesses: buildHarnesses(),
      models: buildModels(),
      // 兼容字段：摊平后的臂，后端在 harnesses/models 缺省时才用
      arms: flattenArms(),
      // 用户模拟是通用配置：进 common，由后端写进每个子任务的 user_proxy_model.json
      sim_arms: [],
      repeat_times: form.value.repeat_times,
      queue: form.value.queue,
      common: { ...form.value.common, agents: form.value.agents },
    }
    const res = await api.post('/batch-experiments', payload)
    if (res.failed) ElMessage.warning(res.message)
    else ElMessage.success(res.message || '创建成功')
    router.push(`/batch/${res.experiment.id}`)
  } catch { /* interceptor */ } finally {
    creating.value = false
  }
}

// 复制实验 → 打开本页并预填（?copy_from=Batch-xxx）。
// 走 /params/full（operator 限定）而不是 GET /{id}：后者的 params_json 里 key
// 已脱敏成 sk-l****nDEj，回填进去会让新任务带着假 key 去跑（静默失败）。
// 明文取自源子任务的 create_params，见后端 _restore_params_secrets。
async function prefillFromCopy(batchId) {
  try {
    // 名称/描述/队列/重复次数仍在实验本体上，参数矩阵（含 key）走 /full
    const [src, full] = await Promise.all([
      api.get(`/batch-experiments/${batchId}`),
      api.get(`/batch-experiments/${batchId}/params/full`),
    ])
    // 去掉旧的「（副本）」后缀再拼，避免复制多次后变成「xxx（副本）（副本）」。
    const baseName = (src.name || '').replace(/（副本）$/, '')
    form.value.name = `${baseName}（副本）`
    form.value.description = src.description || ''
    form.value.registration_id = src.registration_id ?? null
    form.value.repeat_times = src.repeat_times || 1
    // 队列名留空 → 后端用新实验 ID 兜底，不再累加「副本」后缀。
    form.value.queue.name = ''
    form.value.queue.max_concurrent = src.max_concurrent || 10
    form.value.queue.tail_skip_enabled = !!src.tail_skip_enabled
    form.value.queue.tail_skip_percent = src.tail_skip_percent || 10

    // 还原两条正交轴：优先 params.harnesses + params.models（新形状）。
    // 老实验只有 blocks（块内嵌模型）或摊平的 arms —— 把其中的模型去重后
    // 摊成模型轴，保证「2 个 Harness 各配 1 个模型」不会退化成两个独立块。
    let saved = (full && full.params) || {}
    if (typeof saved !== 'object') saved = {}

    const newHarness = (b = {}) => ({
      _uid: uid(), harness_type: b.harness_type || '',
      harness_config_id: b.harness_config_id ?? null,
      harness_config_version_id: b.harness_config_version_id ?? null,
      label: b.label || '', description: '', run_mode: 'standard',
      sys_file_version: '', skill_set: '',
      image_name: b.image_name || '', sandbox_config_id: b.sandbox_config_id ?? null,
    })
    const newModel = (m = {}) => ({
      _uid: uid(), model_id: m.model_id || '',
      model_base_url: m.model_base_url || '',
      // 已经是源子任务的明文 key，直接回填；取不到时才为空（回落模板默认值）
      model_api_key: m.model_api_key || '',
      model_api_type: m.model_api_type || '',
    })

    const harnessList = Array.isArray(saved.harnesses) ? saved.harnesses : []
    const modelList = Array.isArray(saved.models) ? saved.models : []
    const blocks = Array.isArray(saved.blocks) ? saved.blocks : []
    const arms = Array.isArray(saved.arms) ? saved.arms : []

    if (harnessList.length) {
      form.value.harnesses = harnessList.map(newHarness)
      form.value.models = modelList.map(newModel)
    } else if (blocks.length) {
      // 老形状：块 = Harness + 块内模型。模型去重（同 model_id + base_url 只留一套）
      form.value.harnesses = blocks.map(newHarness)
      const seen = new Set()
      const models = []
      for (const b of blocks) {
        for (const m of (b.models || [])) {
          const key = `${m.model_id || ''}|${m.model_base_url || ''}`
          if (seen.has(key)) continue
          seen.add(key)
          models.push(newModel(m))
        }
      }
      form.value.models = models
    } else {
      // 更老的形状：只有摊平的 arms，按 harness 三件套归组成 Harness 轴
      const byHarness = new Map()
      const seen = new Set()
      const models = []
      for (const a of arms) {
        const key = `${a.harness_type}|${a.harness_config_id}|${a.harness_config_version_id}`
        if (!byHarness.has(key)) byHarness.set(key, newHarness(a))
        if (a.model_id || a.model_base_url) {
          const mk = `${a.model_id || ''}|${a.model_base_url || ''}`
          if (!seen.has(mk)) {
            seen.add(mk)
            models.push(newModel(a))
          }
        }
      }
      form.value.harnesses = [...byHarness.values()]
      form.value.models = models
    }
    selectedArm.value = form.value.harnesses.length ? 0 : -1
    selectedModel.value = 0

    // 通用参数
    const common = saved
    if (common && typeof common === 'object') {
      for (const k of ('concurrent_num','max_ttl_second','start_index','suffix_api_keys','proxy_enable')) {
        if (common[k] !== undefined && common[k] !== null) form.value.common[k] = common[k]
      }
      form.value.common.sandbox_config_id = common.sandbox_config_id ?? 0
      form.value.common.code_repo_id = common.code_repo_id ?? null
      if (Array.isArray(common.agents)) {
        form.value.agents = common.agents.map(a => ({
          name: a.name || '', model: a.model || '', provider: a.provider || '',
          base_url: a.base_url || '',
          // 与模型 key 同源：后端已按 agent 名字从子任务 create_params 还原明文
          api_key: a.api_key || '', api: a.api || '', invite_code: a.invite_code || '',
        }))
      }
    }
    ElMessage.info('已从已有实验预填配置（Key 已继承，仅显示掩码）；需要更换请点各字段的「清除」后重填')
  } catch {
    ElMessage.warning('无法加载源实验配置')
  }
}

onMounted(async () => {
  harnessStore.load()
  loadArmOptions()
  if (route.query.copy_from) prefillFromCopy(String(route.query.copy_from))
  loadBaseUrls()
  try { regList.value = await api.get('/registrations') } catch { regList.value = [] }
  try { imageList.value = await api.get('/images') } catch { imageList.value = [] }
  try {
    sandboxList.value = await api.get('/sandbox-configs', { params: { enabled_only: true } })
  } catch { sandboxList.value = [] }
  try { codeRepoList.value = await api.get('/code-repos') } catch { codeRepoList.value = [] }
})
</script>

<style scoped>
.bc-page { display: flex; flex-direction: column; gap: 16px; }

/* ---------- 顶部 ---------- */
.bc-head { display: flex; align-items: center; gap: 10px; }
.back { padding: 4px; color: var(--text-primary); }
.bc-title { margin: 0; font-size: 21px; font-weight: 700; color: var(--text-primary); }
.stepper { margin-left: auto; display: flex; align-items: center; gap: 6px; }
.step { display: flex; align-items: center; gap: 7px; cursor: pointer; padding: 4px 2px; }
.step .num {
  width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
  font-size: 12px; font-weight: 700; background: #eef0f5; color: #8b90a0;
}
.step.active .num { background: #6366f1; color: #fff; }
.step.done .num { background: #dcfce7; color: #16a34a; }
.step .txt { font-size: 13px; color: #8b90a0; }
.step.active .txt { color: #4f46e5; font-weight: 600; }
.step-line { width: 56px; height: 1px; background: #e3e6ee; }

/* ---------- 布局 ---------- */
.bc-body { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 16px; align-items: start; }
.bc-main { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.bc-side { display: flex; flex-direction: column; gap: 14px; position: sticky; top: 16px; }

.card {
  background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e6e8f0);
  border-radius: 12px; padding: 16px 18px;
}

/* ---------- 顶部三栏 ---------- */
.topbar { display: flex; gap: 24px; align-items: flex-end; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field.grow { flex: 1; }
.field.shrink { width: 90px; flex: none; }
.lbl {
  font-size: 12.5px; color: #5b6070; font-weight: 500;
  display: flex; align-items: center; gap: 3px;
}
.lbl2 { font-size: 12px; color: #8b90a0; }
.req { color: #ef4444; font-style: normal; }
.tasknum { font-size: 20px; font-weight: 700; color: var(--text-primary); line-height: 32px; }

/* ---------- Tab ---------- */
.tabs {
  display: flex; gap: 6px; background: #f5f6fa; border-radius: 10px; padding: 5px;
}
.tab {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 7px;
  border: 0; background: transparent; cursor: pointer; padding: 10px 6px;
  border-radius: 8px; font-size: 13.5px; color: #5b6070; font-family: inherit;
  transition: all .15s; position: relative;
}
.tab:hover { background: #eceefa; }
.tab.active { background: #6366f1; color: #fff; font-weight: 600; box-shadow: 0 2px 8px rgba(99,102,241,.3); }
.tab-badge { margin-left: 2px; }
.tab :deep(.el-badge__content) { font-size: 10px; }

/* ---------- 左列表 / 右详情 ---------- */
.split { display: grid; grid-template-columns: 240px minmax(0, 1fr); gap: 14px; align-items: start; }
.list-col {
  background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e6e8f0);
  border-radius: 12px; padding: 14px 12px; display: flex; flex-direction: column; gap: 8px;
}
.list-head { font-size: 13px; font-weight: 600; color: var(--text-primary); padding: 0 2px 4px; }
.add-btn { width: 100%; }
.list-empty {
  font-size: 12px; color: #9aa0b0; line-height: 1.8; text-align: center;
  padding: 20px 6px; border: 1px dashed #e3e6ee; border-radius: 8px;
}
.list-item {
  display: flex; align-items: center; gap: 9px; padding: 10px; border-radius: 9px;
  cursor: pointer; border: 1px solid transparent; transition: all .15s;
}
.list-item:hover { background: #f7f8fc; }
.list-item.active { background: #eef0ff; border-color: #c7ccf8; }
.li-tag {
  width: 26px; height: 26px; flex: none; border-radius: 7px; display: grid; place-items: center;
  font-size: 11px; font-weight: 700; background: #eef0ff; color: #4f46e5;
}
.li-tag.warn { background: #fff4e5; color: #d97706; }
.li-main { min-width: 0; flex: 1; }
.li-name { font-size: 13px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.li-sub { font-size: 11.5px; color: #9aa0b0; margin-top: 2px; }
.li-more { padding: 2px 4px; color: #9aa0b0; }

.detail-col {
  background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e6e8f0);
  border-radius: 12px; min-height: 420px; display: flex; flex-direction: column;
}
.detail-empty { padding: 60px 20px; }
.detail-head {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 14px 18px; border-bottom: 1px solid #eef0f5; flex-wrap: wrap;
}
.dh-left { display: flex; align-items: center; gap: 9px; min-width: 0; }
.dh-tag {
  width: 26px; height: 26px; border-radius: 7px; display: grid; place-items: center;
  font-size: 11px; font-weight: 700; background: #eef0ff; color: #4f46e5; flex: none;
}
.dh-tag.warn { background: #fff4e5; color: #d97706; }
.dh-title { font-size: 16px; font-weight: 700; color: var(--text-primary); }
.ver-tag { background: #f1f2f7; border-color: #e3e6ee; color: #5b6070; }
.dh-edit { padding: 2px 4px; color: #9aa0b0; }
.dh-right { display: flex; gap: 8px; flex: none; }

.detail-body { display: grid; grid-template-columns: 168px minmax(0, 1fr); flex: 1; }
.subnav {
  border-right: 1px solid #eef0f5; padding: 10px 8px;
  display: flex; flex-direction: column; gap: 2px;
}
.subnav-item {
  display: flex; align-items: center; gap: 8px; width: 100%; text-align: left;
  border: 0; background: transparent; cursor: pointer; padding: 9px 10px;
  border-radius: 7px; font-size: 13px; color: #5b6070; font-family: inherit;
}
.subnav-item:hover { background: #f7f8fc; }
.subnav-item.active { background: #eef0ff; color: #4f46e5; font-weight: 600; }
.subpanel { padding: 18px; display: flex; flex-direction: column; gap: 14px; }
.panel-title { font-size: 14.5px; font-weight: 700; color: var(--text-primary); }
.panel-note { font-size: 12px; font-weight: 400; color: #9aa0b0; }

.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 20px; }
.hint { font-size: 11.5px; color: #9aa0b0; line-height: 1.6; }
.hint.danger { color: #ef4444; }
.missing { color: #ef4444; }
.muted { color: #9aa0b0; font-size: 12px; }
.spacer { flex: 1; }
.row { display: flex; gap: 8px; }
.readonly {
  font-size: 13px; color: #5b6070; background: #f7f8fc; border-radius: 7px;
  padding: 7px 11px; border: 1px solid #eef0f5;
}
.opt-meta { float: right; color: #9aa0b0; font-size: 11.5px; }

/* 单选卡片 */
.radio-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.rcard {
  display: flex; align-items: flex-start; gap: 9px; padding: 12px;
  border: 1px solid #e3e6ee; border-radius: 9px; cursor: pointer; transition: all .15s;
}
.rcard input { display: none; }
.rc-radio {
  width: 15px; height: 15px; border-radius: 50%; border: 2px solid #d0d4e0;
  flex: none; margin-top: 1px; position: relative;
}
.rcard.active { border-color: #c7ccf8; background: #f7f8ff; }
.rcard.active .rc-radio { border-color: #6366f1; }
.rcard.active .rc-radio::after {
  content: ''; position: absolute; inset: 2.5px; border-radius: 50%; background: #6366f1;
}
.rc-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
.rc-desc { font-size: 11.5px; color: #9aa0b0; margin-top: 3px; }

.agent-tabs { margin-top: 6px; }
.agent-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }
.agent-tabs :deep(.el-tabs__item) { font-size: 12.5px; height: 34px; line-height: 34px; }

/* 块 / 模型 / agent 卡 */
.cfg-block, .model-card, .agent-card {
  border: 1px solid #e6e8f0; border-radius: 10px; padding: 12px 14px;
  display: flex; flex-direction: column; gap: 12px;
}
.cfg-block { margin-bottom: 12px; }
.model-card, .agent-card { background: #fafbfe; margin-bottom: 12px; }
.block-top { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.block-name { font-size: 13.5px; font-weight: 600; }
.block-body { display: flex; flex-wrap: wrap; gap: 6px 20px; font-size: 12px; color: #5b6070; }
.kv b { font-weight: 500; color: #9aa0b0; margin-right: 5px; }
.mc-head { display: flex; align-items: center; gap: 8px; }
.mc-idx {
  width: 24px; height: 24px; border-radius: 6px; display: grid; place-items: center;
  font-size: 11px; font-weight: 700; background: #eef0ff; color: #4f46e5;
}
.mc-name { font-size: 13px; font-weight: 600; }

/* ---------- 右侧 ---------- */
.side-card { padding: 16px; }
.side-title {
  display: flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 700;
  color: var(--text-primary); margin-bottom: 14px;
}
.mini-matrix { border: 1px solid #eef0f5; border-radius: 9px; overflow: hidden; }
.mm-head { display: flex; background: #f7f8fc; }
.mm-corner {
  width: 104px; flex: none; padding: 9px 8px; font-size: 10.5px; color: #9aa0b0;
  line-height: 1.4; border-right: 1px solid #eef0f5;
}
.mm-col { flex: 1; padding: 9px 6px; text-align: center; border-right: 1px solid #eef0f5; min-width: 0; }
.mm-col:last-child { border-right: 0; }
.mm-col-name {
  font-size: 11.5px; font-weight: 600; color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mm-col-sub { font-size: 10px; color: #9aa0b0; margin-top: 2px; }
.mm-row { display: flex; border-top: 1px solid #eef0f5; }
.mm-row-name {
  width: 104px; flex: none; padding: 9px 8px; font-size: 11px; color: #5b6070;
  border-right: 1px solid #eef0f5; line-height: 1.4;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mm-cell {
  flex: 1; display: grid; place-items: center; padding: 9px 4px;
  border-right: 1px solid #eef0f5; min-width: 0;
}
.mm-cell:last-child { border-right: 0; }
.dot { color: #d0d4e0; font-size: 11px; }

.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.stat {
  display: flex; align-items: center; gap: 10px; padding: 11px 12px;
  background: #f7f8fc; border-radius: 9px;
}
.stat-ic { color: #6366f1; font-size: 16px; display: flex; }
.stat-k { font-size: 11px; color: #9aa0b0; }
.stat-v { font-size: 18px; font-weight: 700; color: var(--text-primary); line-height: 1.2; }

.total-box {
  margin-top: 12px; padding: 14px 16px; border-radius: 10px;
  background: linear-gradient(135deg, #eef0ff 0%, #f5f3ff 100%);
  border: 1px solid #dcdffb; display: flex; align-items: center; justify-content: space-between;
}
.tb-k { font-size: 12.5px; font-weight: 600; color: #4f46e5; }
.tb-formula { font-size: 11.5px; color: #8b8fd4; margin-top: 4px; }
.tb-v { font-size: 30px; font-weight: 800; color: #4f46e5; line-height: 1; }
.tb-v.warn { color: #d97706; }

.ok-box {
  margin-top: 12px; padding: 11px 13px; border-radius: 9px; font-size: 12px;
  background: #f0fdf4; border: 1px solid #bbf7d0; color: #15803d;
  display: flex; align-items: flex-start; gap: 8px; line-height: 1.7;
}
.ok-box.small { font-size: 11.5px; }
.warn-box {
  margin-top: 12px; padding: 11px 13px; border-radius: 9px; font-size: 12px;
  background: #fffbeb; border: 1px solid #fde68a; color: #b45309;
  display: flex; align-items: flex-start; gap: 8px; line-height: 1.7;
}
.issue-box {
  margin-top: 14px; padding: 12px 14px; border-radius: 9px;
  background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; font-size: 12.5px;
}
.issue-title { display: flex; align-items: center; gap: 7px; font-weight: 600; }
.issue-box ul { margin: 8px 0 0; padding-left: 20px; line-height: 1.9; }

/* ---------- 矩阵（大表） ---------- */
.seg { display: inline-flex; background: #f5f6fa; border-radius: 8px; padding: 3px; margin-bottom: 14px; }
.seg-item {
  border: 0; background: transparent; cursor: pointer; padding: 6px 16px;
  border-radius: 6px; font-size: 13px; color: #5b6070; font-family: inherit;
}
.seg-item.active { background: #fff; color: #4f46e5; font-weight: 600; box-shadow: 0 1px 4px rgba(0,0,0,.08); }

.matrix { width: 100%; border-collapse: collapse; font-size: 13px; }
.matrix th, .matrix td { border: 1px solid #eef0f5; padding: 10px 8px; }
.matrix .corner { font-size: 11px; color: #9aa0b0; font-weight: 400; text-align: left; background: #f7f8fc; }
.matrix .mh { background: #f7f8fc; }
.mh-name { font-size: 12.5px; font-weight: 700; color: var(--text-primary); }
.mh-sub { font-size: 10.5px; color: #9aa0b0; margin-top: 2px; font-weight: 400; }
.matrix .row-head { font-size: 12.5px; color: #5b6070; background: #fcfcfe; text-align: left; }
.matrix .row-head b { color: #4f46e5; margin-right: 5px; }
.matrix .cell { text-align: center; }

/* ---------- 底部 ---------- */
.bc-foot {
  display: flex; justify-content: flex-end; gap: 10px;
  padding: 14px 0 4px;
}
</style>
