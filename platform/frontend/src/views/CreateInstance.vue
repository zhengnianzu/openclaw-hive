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
          <div class="reg-load-bar">
            <span class="reg-load-hint">可手工填写 OBS 目录，或从已有数据集一次性带入</span>
            <el-button type="primary" plain size="small" @click="openRegDialog">
              <el-icon><FolderOpened /></el-icon>&nbsp;从数据集加载
            </el-button>
          </div>

          <el-form-item label="技能目录">
            <el-input v-model="form.skill_dir" placeholder="skills/260325/skill_localize/skills_library">
              <template #append>
                <el-button @click="openObsBrowser('skill_dir')"><el-icon><FolderOpened /></el-icon></el-button>
              </template>
            </el-input>
          </el-form-item>

          <el-form-item label="默认技能">
            <el-input v-model="form.default_skills" placeholder="find-skills,skill-scope（逗号分隔，留空使用模板默认值）" />
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
          <!-- 候选来自历史用量 + 已登记代理（见 /export-meta/base-urls）。
               用 autocomplete 而不是 select：它渲染的是真正的 <input>，地址可以选中复制，
               且天然允许填候选以外的值（新代理/外部网关）。select 的显示区不是输入框，
               改成 select 后就复制不了地址了。 -->
          <el-form-item label="模型 Base URL">
            <el-autocomplete v-model="form.model_base_url" :fetch-suggestions="queryBaseUrls"
              :placeholder="['opencode','pi','codex','grok','taichu'].includes(form.harness_type) ? '选择或输入，例如：http://192.168.30.95:8084（自动补 /v1）' : '选择或输入，例如：http://192.168.30.95:8084'"
              style="width:100%" clearable :trigger-on-focus="true" fit-input-width>
              <template #default="{ item }">
                <span>{{ item.value }}</span>
                <span style="float:right;color:#999;font-size:12px">
                  {{ item.proxy_name || '未登记' }}{{ item.used ? ` · ${item.used} 次` : '' }}
                </span>
              </template>
            </el-autocomplete>
          </el-form-item>

          <el-form-item :label="form.harness_type === 'claude-code' ? '模型 Token' : '模型 API Key'">
            <div style="display:flex;gap:8px;width:100%">
              <!-- key 只写不回显：已设置时输入框只显示掩码且不可编辑（无 show-password 眼睛），
                   避免原文出现在屏幕/截图/录屏里。原文只存在 form 里随创建请求提交。
                   未设置时可正常手工粘贴（线上 17% 的实例是粘贴已有 key，不能只留「新建KEY」）；
                   要换 key 先点「清除」。 -->
              <el-input v-if="form.model_api_key" :model-value="maskKey(form.model_api_key)"
                style="flex:1" readonly />
              <el-input v-else v-model="keyDraft['']" placeholder="留空使用模板默认值"
                style="flex:1" type="password" autocomplete="off"
                @blur="commitKeyDraft('')" @keyup.enter="commitKeyDraft('')" />
              <el-button v-if="form.model_api_key" @click="clearKey('')">清除</el-button>
              <el-button type="primary" @click="openKeyDialog('harness')">新建KEY</el-button>
            </div>
            <div v-if="form.model_api_key" style="font-size:12px;color:#999;margin-top:4px">
              已设置 Key，仅显示掩码；需更换请点「清除」后重新粘贴或新建
            </div>
          </el-form-item>

          <el-form-item v-if="['openclaw','openclaw-tui','opencode','pi','dsh','hermes','grok','jiuwenswarm-dev','jiuwenswarm-xiaoyi'].includes(form.harness_type)" label="API 类型">
            <el-select v-model="form.model_api_type" placeholder="留空使用模板默认值" clearable style="width:100%">
              <el-option label="Anthropic Messages" value="anthropic-messages" />
              <el-option label="OpenAI Completions" value="openai-completions" />
            </el-select>
            <div v-if="form.harness_type === 'opencode'" style="font-size:12px;color:#999;margin-top:4px">决定 provider 的 npm 包：anthropic 用 @ai-sdk/anthropic，openai 用 @ai-sdk/openai-compatible</div>
            <div v-if="form.harness_type === 'pi'" style="font-size:12px;color:#999;margin-top:4px">写入 models.json 的 providers.custom.api 字段</div>
            <div v-if="form.harness_type === 'hermes'" style="font-size:12px;color:#999;margin-top:4px">写入 config.yaml 的 providers.&lt;name&gt;.transport</div>
            <div v-if="form.harness_type === 'grok'" style="font-size:12px;color:#999;margin-top:4px">写入 grok/config.toml 的 model.default-model.api_backend</div>
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
            <div style="display:flex;gap:8px;width:100%">
              <el-input v-model="form.model_id" style="flex:1" :placeholder="form.harness_type === 'claude-code' ? '例如：claude-opus-4-7-thinking' : '例如：claude-opus-4-7-thinking'">
                <template v-if="['openclaw','openclaw-tui'].includes(form.harness_type)" #prepend>local/</template>
              </el-input>
              <el-button :loading="validatingSlot['']" @click="validateModel('')">验证模型</el-button>
            </div>
            <div v-if="['openclaw','openclaw-tui'].includes(form.harness_type)" style="font-size:12px;color:#999;margin-top:4px">同时更新 agents.defaults.model.primary 和 models[0].id/name</div>
            <div style="font-size:12px;color:#999;margin-top:4px">
              用上面的 Base URL / API Key / 模型 ID 实打一次最小请求。指定了「API 类型」就按它调，
              未指定则先试 Anthropic，不通再试 OpenAI
            </div>

            <ValidateModelResult :result="validateResults['']" />
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

              <!-- 候选带 /v1：后端对 agents[].base_url 是原样写入 user_proxy_model.json，
                   不像主 Agent 那样按 harness 自动补 /v1，所以这里必须给带后缀的值。 -->
              <el-form-item label="模型 Base URL">
                <el-autocomplete v-model="ag.base_url" :fetch-suggestions="queryAgentBaseUrls"
                  placeholder="选择或输入，例如：http://192.168.30.95:8084/v1（需带 /v1）"
                  style="width:100%" clearable :trigger-on-focus="true" fit-input-width>
                  <template #default="{ item }">
                    <span>{{ item.value }}</span>
                    <span style="float:right;color:#999;font-size:12px">
                      {{ item.proxy_name || '未登记' }}{{ item.used ? ` · ${item.used} 次` : '' }}
                    </span>
                  </template>
                </el-autocomplete>
                <div style="font-size:12px;color:#999;margin-top:4px">user_simulator 的 Base URL 需带 /v1 后缀</div>
              </el-form-item>

              <el-form-item label="模型 API Key">
                <div style="display:flex;gap:8px;width:100%">
                  <!-- 与主 Agent 的 key 框同口径：已设置只显示掩码、不回显原文、无眼睛图标 -->
                  <el-input v-if="ag.api_key" :model-value="maskKey(ag.api_key)"
                    style="flex:1" readonly />
                  <el-input v-else v-model="keyDraft[idx]" placeholder="留空使用模板默认值"
                    style="flex:1" type="password" autocomplete="off"
                    @blur="commitKeyDraft(idx)" @keyup.enter="commitKeyDraft(idx)" />
                  <el-button v-if="ag.api_key" @click="clearKey(idx)">清除</el-button>
                  <el-button type="primary" @click="openKeyDialog('agent', idx)">新建KEY</el-button>
                </div>
                <div v-if="ag.api_key" style="font-size:12px;color:#999;margin-top:4px">
                  已设置 Key，仅显示掩码；需更换请点「清除」
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
                <div style="display:flex;gap:8px;width:100%">
                  <el-input v-model="ag.model" style="flex:1" placeholder="例如：gemini-3-flash-preview" />
                  <el-button :loading="validatingSlot[idx]" @click="validateModel(idx)">验证模型</el-button>
                </div>
                <div style="font-size:12px;color:#999;margin-top:4px">
                  用该 agent 的 Base URL / API Key / 模型 ID 实打一次最小请求（base_url 带 /v1 也能识别）
                </div>

                <ValidateModelResult :result="validateResults[idx]" />
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

          <el-form-item label="沙箱存活时间（秒）">
            <el-input-number v-model="form.max_ttl_second" :min="1800" :max="36000" :step="600" />
            <div style="font-size:12px;color:#999;margin-top:4px">
              沙箱 max_ttl_second，范围 1800 ~ 36000 秒（最长 10 小时）；主程序超时为该值 − 600 秒，每条 queries[].timeout 为该值 − 1200 秒
            </div>
          </el-form-item>

          <el-form-item label="沙箱配置">
            <el-select v-model="form.sandbox_config_id" placeholder="选择沙箱运行环境" style="width:100%">
              <el-option label="默认（不替换，用模板原样）" :value="0" />
              <el-option v-for="sb in sandboxList" :key="sb.id"
                :label="`${sb.name}${sb.is_default ? ' (默认)' : ''}`" :value="sb.id">
                <span>{{ sb.name }}<el-tag v-if="sb.is_default" size="small" type="info" style="margin-left:6px">默认</el-tag></span>
                <span style="float:right;color:#999;font-size:12px">
                  {{ sb.sandbox_type === 'windows' ? 'Windows' : 'Linux' }} · {{ cores(sb.cpu_request) }}/{{ cores(sb.cpu_limit) }} 核
                  <template v-if="sb.sandbox_namespace"> · {{ sb.sandbox_namespace }}</template>
                </span>
              </el-option>
            </el-select>
            <div style="font-size:12px;color:#999;margin-top:4px">
              决定沙箱的资源规格、超时、环境变量与镜像；选「默认」则不替换，用 harness 模板原样
            </div>
          </el-form-item>
          <el-form-item label="镜像名称">
            <el-select v-model="form.image_name" filterable allow-create default-first-option
              placeholder="选择或输入镜像地址" style="width:100%" clearable>
              <el-option v-for="img in imageOptions" :key="img.id" :label="img.name" :value="img.address"
                :style="img.remark ? 'height:auto;line-height:1.5;padding-top:6px;padding-bottom:6px' : ''">
                <div>
                  <el-tag v-if="img.address === defaultImageAddress" size="small" type="success" effect="light" style="margin-right:6px">推荐</el-tag>
                  <span>{{ img.name }}</span>
                  <span style="float:right;color:#999;font-size:12px">{{ img.address.length > 40 ? '...' + img.address.slice(-40) : img.address }}</span>
                </div>
                <div v-if="img.remark" style="color:#999;font-size:12px;line-height:1.3;white-space:normal">{{ img.remark }}</div>
              </el-option>
            </el-select>
          </el-form-item>

          <el-form-item label="Key 追加沙箱后缀">
            <el-switch v-model="form.suffix_api_keys" active-text="开启" inactive-text="关闭" />
            <div style="font-size:12px;color:#999;margin-top:4px">
              仅自部署模型（网关按 user_id 计费）需要：上传前给模型 Key 追加 <code>_&lt;沙箱标识&gt;</code>。第三方模型请关闭，否则鉴权失败
            </div>
          </el-form-item>

          <el-form-item label="启动本地代理">
            <el-switch v-model="form.proxy_enable" active-text="开启" inactive-text="关闭" />
            <div style="font-size:12px;color:#999;margin-top:4px">
              开启后沙箱内启动本地代理：模型流量经本地端口转发并记录轨迹日志。默认关闭
            </div>
          </el-form-item>

          <el-form-item label="代码仓">
            <el-select v-model="form.code_repo_id" placeholder="选择代码仓（可选）" style="width:100%" clearable>
              <el-option v-for="repo in codeRepoOptions" :key="repo.id" :label="`${repo.name} / ${repo.version}`" :value="repo.id">
                <span>
                  <el-tag v-if="repo.id === defaultRepoId" size="small" type="success" effect="light" style="margin-right:6px">默认</el-tag>{{ repo.name }} / {{ repo.version }}
                </span>
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
      <!-- 路径输入框：可手工改、可全选复制（面包屑是按钮，选不中文本）。
           回车或点「加载」即跳转；与 TaskRegister/CodeManagement 的对话框一致。 -->
      <div style="margin-bottom:8px;display:flex;gap:8px;align-items:center">
        <el-input v-model="obsCurrentPath" placeholder="OBS 路径" size="small"
          @keyup.enter="loadObsDir" spellcheck="false">
          <template #prepend>路径</template>
        </el-input>
        <el-button size="small" @click="loadObsDir" :loading="obsLoading">加载</el-button>
      </div>
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

    <!-- 从数据集加载 OBS 配置 -->
    <el-dialog v-model="regDialogVisible" title="从数据集加载 OBS 配置" width="480px">
      <el-select v-model="selectedRegId" filterable style="width:100%" placeholder="选择数据集">
        <el-option
          v-for="r in regList" :key="r.id"
          :label="`#${r.seq || r.id}  ${r.task_name}（${r.data_total || 0} 条）`"
          :value="r.id" />
      </el-select>
      <div style="margin-top:8px;font-size:12px;color:#999">
        只覆盖 OBS 目录字段，不改实例名称、模型和 Agent 配置。
      </div>
      <template #footer>
        <el-button @click="regDialogVisible = false">取消</el-button>
        <el-button type="primary" :disabled="!selectedRegId" @click="confirmRegSelect">确认带入</el-button>
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
import ValidateModelResult from '../components/ValidateModelResult.vue'
import { useAuthStore } from '../stores/auth'
import { useHarnessStore } from '../stores/harness'
import { obsBucket, stripBucket, joinBucket } from '../api/obsConfig'
import { toCores as cores } from '../utils/quantity'

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

// Key 掩码：与后端 services/secrets.py 的 mask_secret 及 InstanceDetail 的 maskKey
// 规则对齐（4 + *** + 4，短值整体打码，不留短 key 泄漏口）。
function maskKey(key) {
  if (!key) return ''
  if (key.length <= 8) return '****'
  return key.slice(0, 4) + '***' + key.slice(-4)
}

// Key 录入用的临时缓冲：主 Agent 用 '', 各 agent 用其下标。
// 不直接 v-model 到 form.*_api_key —— 那样第一个字符一落进去就会把输入框切成
// 「已设置」的只读掩码态，后面的字符没法再输。改成失焦/回车时一次性提交。
const keyDraft = ref({})

function commitKeyDraft(slot) {
  const v = String(keyDraft.value[slot] || '').trim()
  if (!v) return
  if (slot === '') form.value.model_api_key = v
  else form.value.agents[slot].api_key = v
  keyDraft.value[slot] = ''
}

function clearKey(slot) {
  if (slot === '') form.value.model_api_key = ''
  else form.value.agents[slot].api_key = ''
  keyDraft.value[slot] = ''
}

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
    ElMessage.success(`API Key 已生成并填入：${maskKey(res.api_key)}（原文不回显）`)
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
  skill_dir: '', default_skills: '', agent_dir: '', user_config_dir: '', user_profile_dir: '',
  traj_save_path: '', start_index: 0, total_num: 0, image_name: '',
  max_ttl_second: 36000,
  model_api_key: '', model_base_url: '', model_api_type: '', model_id: '',
  suffix_api_keys: true,
  proxy_enable: false,
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
  // splice 会让后面的 agent 下标整体前移，按下标键控的验证结果/录入缓冲就对不上人了
  // （agent2 的结果会显示在 agent1 底下）。索引变动后一律丢弃，重新验证即可。
  for (const k of Object.keys(validateResults.value)) {
    if (k !== '') delete validateResults.value[k]
  }
  for (const k of Object.keys(keyDraft.value)) {
    if (k !== '') delete keyDraft.value[k]
  }
}

const imageList = ref([])

// ============ 启用过滤 + os 过滤 + 默认(推荐) ============
// 镜像下拉: 只列启用的, 按所选沙箱配置的 os 过滤(未选/过滤后为空则不过滤)。
// 默认镜像 = 该 harness 映射里 is_default 的; 无则 common 默认; 都无则不标。
const imageOptions = computed(() => {
  let list = (imageList.value || []).filter(i => i.enabled !== false)
  const sb = (sandboxList.value || []).find(s => s.id === form.value.sandbox_config_id)
  if (sb) {
    const filtered = list.filter(i => i.os === sb.sandbox_type)
    if (filtered.length) list = filtered
  }
  const def = defaultImageAddress.value
  if (!def) return list
  return [...list.filter(i => i.address === def), ...list.filter(i => i.address !== def)]
})

const defaultImageAddress = computed(() => {
  const list = (imageList.value || []).filter(i => i.enabled !== false)
  const ht = form.value.harness_type
  const hit = list.find(i => (i.defaults || []).includes(ht))
  if (hit) return hit.address
  const common = list.find(i => (i.defaults || []).includes('common'))
  return common ? common.address : ''
})

// 代码仓下拉: 只列启用的; 默认代码仓置顶打标。
const codeRepoOptions = computed(() => {
  const list = (codeRepoList.value || []).filter(r => r.enabled !== false)
  const d = defaultRepoId.value
  if (!d) return list
  return [...list.filter(r => r.id === d), ...list.filter(r => r.id !== d)]
})

const defaultRepoId = computed(() => {
  const d = (codeRepoList.value || []).find(r => r.enabled !== false && r.is_default)
  return d ? d.id : null
})

// harness 切换后自动填默认镜像/代码仓(可手动改选其他); suppressDefaultFill
// 用于复制实例场景 —— 保留源实例的选择不被覆盖。
let suppressDefaultFill = false
function applyDefaults() {
  if (suppressDefaultFill) { suppressDefaultFill = false; return }
  const addr = defaultImageAddress.value
  if (addr) form.value.image_name = addr
  const rid = defaultRepoId.value
  if (rid) form.value.code_repo_id = rid
}
const baseUrlOptions = ref([])
const codeRepoList = ref([])
const harnessConfigList = ref([])
const versionList = ref([])
const versionsLoading = ref(false)
const queueList = ref([])
const sandboxList = ref([])
const queuePickerVisible = ref(false)

// 「验证模型」按钮状态。按槽位键控：'' = 主 Agent（Harness配置），
// 数字下标 = 用户模拟配置里的第 N 个 agent。值为 null/undefined = 还没验证过。
const validatingSlot = ref({})
const validateResults = ref({})

// 实打一次模型调用验证连通性。后端一律返回 200，成败看 body.ok；
// 带 silent 让 api/index.js 不弹全局 toast —— 报错要内联原样展示。
//
// 主 Agent 与 agent 的字段名不同（model_base_url/model_id vs base_url/model），
// 这里统一映射成后端的入参名。agent 的 base_url 本就带 /v1，后端会先剥再按协议拼，
// 不需要前端特殊处理。
async function validateModel(slot = '') {
  const src = slot === ''
    ? {
        base_url: form.value.model_base_url,
        api_key: form.value.model_api_key,
        api_type: form.value.model_api_type,
        model_id: form.value.model_id,
      }
    : {
        base_url: form.value.agents[slot]?.base_url,
        api_key: form.value.agents[slot]?.api_key,
        api_type: form.value.agents[slot]?.api,
        model_id: form.value.agents[slot]?.model,
      }
  validatingSlot.value[slot] = true
  validateResults.value[slot] = null
  try {
    validateResults.value[slot] = await api.post('/harness-meta/validate-model', {
      model_base_url: src.base_url || '',
      model_api_key: src.api_key || '',
      model_api_type: src.api_type || '',
      model_id: src.model_id || '',
    }, { silent: true })
  } catch (e) {
    // 网络层/鉴权异常也内联显示，口径与后端返回的失败一致
    validateResults.value[slot] = {
      ok: false, tried: [], request_url: '',
      error: e.response?.data?.detail || e.message || '请求失败',
    }
  } finally {
    validatingSlot.value[slot] = false
  }
}

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
  // 模板配置优先; 未配置时回落 harness 默认镜像/默认代码仓(而非清空)
  form.value.image_name = tpl.image_name || defaultImageAddress.value || ''
  form.value.code_repo_id = tpl.code_repo_id || defaultRepoId.value || null
  form.value.model_api_key = ''
  validateResults.value = {}
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
  applyDefaults()
}

async function loadCodeRepos() {
  try {
    codeRepoList.value = await api.get('/code-repos')
  } catch { codeRepoList.value = [] }
}

// --- 从数据集加载 ---
const regList = ref([])
const regDialogVisible = ref(false)
const selectedRegId = ref(null)

async function openRegDialog() {
  try { regList.value = await api.get('/registrations') } catch { regList.value = [] }
  selectedRegId.value = null
  regDialogVisible.value = true
}

function confirmRegSelect() {
  const reg = regList.value.find(r => r.id === selectedRegId.value)
  if (!reg) { regDialogVisible.value = false; return }
  applyRegistration(reg)
  regDialogVisible.value = false
  ElMessage.success('已从数据集带入 OBS 配置')
}

// 只覆盖 OBS 目录字段，不动实例名 / 模型 / agents 等。
// 实例表单存相对路径（剥桶名），登记表存完整路径，这里统一剥一层。
// from_registration 路由分支和「从数据集加载」弹窗共用。
function applyRegistration(reg) {
  if (reg.task_name && !form.value.name) form.value.name = reg.task_name
  form.value.user_config_dir = stripBucket(reg.task_path_obs || '')
  form.value.skill_dir = stripBucket(reg.skill_dir_obs || '')
  form.value.agent_dir = stripBucket(reg.agent_dir_obs || '')
  form.value.user_profile_dir = stripBucket(reg.user_folder_obs || '')
  form.value.default_skills = reg.default_skills || ''
  if (reg.traj_save_path) form.value.traj_save_path = stripBucket(reg.traj_save_path)
  form.value.total_num = 0
}

// Base URL 候选：后端按归一化地址聚合历史用量并补上代理名（/export-meta/base-urls）。
// 取不到就留空数组——下拉是 allow-create 的，没候选也能照常手填。
async function loadBaseUrls() {
  try {
    const res = await api.get('/export-meta/base-urls')
    baseUrlOptions.value = res.options || []
  } catch { baseUrlOptions.value = [] }
}

// 用户模拟 agent 的 base_url 要带 /v1，而后端聚合时已把 /v1 归一化掉了
// （否则同一台代理会在候选里重复出现）。这里按需补回后缀。
const agentBaseUrlOptions = computed(() => baseUrlOptions.value.map(o => ({
  ...o,
  value: /\/v\d+$/.test(o.value) ? o.value : `${o.value.replace(/\/$/, '')}/v1`,
})))

// el-autocomplete 的候选过滤：已输入内容按子串匹配，空输入给全部（trigger-on-focus）。
// 候选只是建议，输入框里填什么都不拦——新代理、外部网关照旧手填。
function filterBaseUrls(list, queryString) {
  const q = (queryString || '').trim().toLowerCase()
  if (!q) return list
  return list.filter(o =>
    o.value.toLowerCase().includes(q) ||
    (o.proxy_name || '').toLowerCase().includes(q))
}
function queryBaseUrls(queryString, cb) {
  cb(filterBaseUrls(baseUrlOptions.value, queryString))
}
function queryAgentBaseUrls(queryString, cb) {
  cb(filterBaseUrls(agentBaseUrlOptions.value, queryString))
}

function onHarnessChange() {
  suppressDefaultFill = false  // 用户显式切换 harness, 恢复默认自动填
  form.value.image_name = ''
  form.value.code_repo_id = null
  form.value.harness_config_id = null
  form.value.harness_config_version_id = null
  versionList.value = []
  validateResults.value = {}
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
    await api.post('/instances', form.value)
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

// 以字段里已填的路径为浏览起点（与 TaskRegister/CodeManagement 的行为一致）。
// 注意本页字段存的是 stripBucket 后的相对路径（见 confirmObsSelect），
// 必须 joinBucket 还原成 obs:// 全路径才能交给 /obs/list。
function openObsBrowser(field) {
  obsTargetField = field
  const current = String(form.value[field] || '').trim()
  obsCurrentPath.value = current ? joinBucket(current) : obsBucket()
  obsItems.value = []
  obsVisible.value = true
  loadObsDir()
}

// 起点可能是用户手打的、OBS 上并不存在的路径 —— obsutil 会非零退出，
// 后端转成 500（api/routers/obs.py）。这时退回桶根重列，而不是让对话框空着。
async function loadObsDir() {
  // 输入框是可编辑的，手打的值可能缺桶前缀/缺尾斜杠/带多余空格。
  // 统一经 stripBucket→joinBucket 归一化，顺带让面包屑能正确分段。
  obsCurrentPath.value = joinBucket(stripBucket(String(obsCurrentPath.value || '').trim()))
  obsLoading.value = true
  try {
    const res = await api.get('/obs/list', { params: { path: obsCurrentPath.value }, silent: true })
    obsItems.value = res.items
  } catch (e) {
    const atRoot = obsCurrentPath.value === obsBucket()
    if (!atRoot) {
      ElMessage.warning(`路径 ${stripBucket(obsCurrentPath.value) || '/'} 无法浏览，已回到桶根`)
      obsCurrentPath.value = obsBucket()
      obsLoading.value = false
      return loadObsDir()
    }
    obsItems.value = []
    ElMessage.error(e.response?.data?.detail || e.message || 'OBS 列目录失败')
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
  // 实例表单存相对路径（剥桶名），与 config.yaml 里 *_download_path 的口径一致。
  // 登记表存完整路径，浏览器内部用完整路径，这里落地时统一剥一层。
  form.value[obsTargetField] = stripBucket(obsCurrentPath.value)
  obsVisible.value = false
}

onMounted(async () => {
  harnessStore.load()
  const copyFrom = route.query.copy_from
  const fromRegistration = route.query.from_registration
  if (copyFrom) {
    try {
      // 走 /full（operator 限定）而非 /create-params：后者 key 已脱敏，
      // 直接回填会让复制出的任务带着掩码当 key 去跑。
      const params = await api.get(`/instances/${copyFrom}/create-params/full`)
      Object.assign(form.value, params)
      // 复制保留源实例的镜像/代码仓选择: 压制下一次默认自动填
      suppressDefaultFill = true
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
      const rand = String(Math.floor(Math.random() * 10000)).padStart(4, '0')
      applyRegistration(reg)
      form.value.task_name = `${genTaskId()}${rand}`
      ElMessage.info('已从数据集预填配置')
    } catch {
      ElMessage.warning('无法加载数据集信息')
    }
  }
  loadImages()
  loadBaseUrls()
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
.reg-load-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  margin-bottom: 12px;
  background: var(--el-fill-color-extra-light, #f9fafb);
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 6px;
}
.reg-load-hint {
  font-size: 12px;
  color: var(--text-secondary, #909399);
}
</style>
