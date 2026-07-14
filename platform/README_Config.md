# 配置体系梳理

## 整体流程

```
Harness 配置 (三个模板文件)
        │
        ▼
    新建任务 (表单补充选项，覆盖模板中的字段)
        │
        ▼
    任务实例 (instances/{id}/ 下生成最终配置文件)
        │
        ▼
      执行 (hive.py --config config.yaml)
```

辅助模块：
- **任务登记**：预填任务相关信息（OBS路径、模型名等），点击"创建任务"时自动填入新建任务表单
- **配置模板**：保存用户常用的 API 配置（base_url、model_id、agents 等），按用户隔离，创建任务时可复用

---

## 一、Harness 配置

管理页面：`Harness 配置管理`

每个 Harness 配置包含三个文件，按 `harness_type` 决定具体文件：

| 文件分类 | openclaw | hermes | claude-code | 作用 |
|---------|----------|--------|-------------|------|
| Harness 配置 | `openclaw.json` | `hermes_config.yaml` | `cc_settings.json` | 模型接入（API地址、密钥、模型ID） |
| 任务配置 | `config.yaml` | `config.yaml` | `config.yaml` | 运行参数（并发、OBS路径、任务路径） |
| 模拟配置 | `user_proxy_model.json` | `user_proxy_model.json` | `user_proxy_model.json` | Agent 配置（user_simulator、evaluator） |

### 存储路径

| 版本 | 路径 | 说明 |
|------|------|------|
| 默认 | `settings/` | 三种 harness_type 共用同一目录 |
| 自定义 | `settings/{harness_type}/{version}/` | 例如 `settings/openclaw/v1/` |

### 默认注册（启动时自动）

系统启动时 `ensure_defaults()` 为 openclaw / hermes / claude-code 各注册一个「默认」版本，指向 `settings/` 目录。

### 文件来源

每个文件可独立获取：
1. **从模板初始化** — 从 `settings/{filename}` 复制（如 `settings/openclaw.json`）
2. **从 OBS 拉取** — 每个文件有独立的 OBS 路径（obs_harness_path / obs_task_path / obs_proxy_path）
3. **在线编辑** — 文件管理弹窗中直接编辑保存

### 数据表字段

```
harness_configs:
  id, name, harness_type, version, description,
  is_default, config_files_json,
  obs_harness_path, obs_task_path, obs_proxy_path,
  created_by, created_at, updated_at
```

---

## 二、新建任务

页面：`新建任务实例`

### 表单字段 → 最终写入哪个文件

#### 主表单

| 表单字段 | 写入文件 | 写入路径 | 说明 |
|---------|---------|---------|------|
| 实例名称 (name) | DB | task_instances.name | 仅展示 |
| 任务标识 (task_name) | config.yaml | `sandbox_id_prefix` | 用于 Pod 命名和 OBS 路径 |
| Harness 类型 | config.yaml | `run_config.harness_type` | 决定使用哪种 harness 文件 |
| Harness 配置 | — | — | 选择 harness_config_id，决定模板文件来源目录 |
| 并发数 | config.yaml | `run_config.concurrent_num` | |

#### Tab: OBS 配置

| 表单字段 | 写入文件 | 写入路径 |
|---------|---------|---------|
| 技能目录 (skill_dir) | config.yaml | `run_config.obs.skill_download_path` |
| 默认技能 (default_skills) | config.yaml | `run_config.obs.default_skills` (逗号→列表) |
| Agent 目录 (agent_dir) | config.yaml | `run_config.obs.agents_download_path` |
| 用户 Config 目录 (user_config_dir) | config.yaml | `run_config.task.task_input_path` (下载后设为本地路径) |
| 用户 Profile 目录 (user_profile_dir) | config.yaml | `run_config.obs.user_profile_download_path` |
| 轨迹保存路径 (traj_save_path) | config.yaml | `run_config.obs.traj_save_path` |

#### Tab: Harness 配置

| 表单字段 | 写入文件 (openclaw) | 写入文件 (hermes) | 写入文件 (claude-code) |
|---------|-------------------|------------------|---------------------|
| 模型 Base URL | `openclaw.json` → `models.providers.local.baseUrl` | `hermes_config.yaml` → `model.base_url` | `cc_settings.json` → `env.ANTHROPIC_BASE_URL` |
| 模型 API Key / Token | `openclaw.json` → `models.providers.local.apiKey` | `hermes_config.yaml` → `model.api_key` | `cc_settings.json` → `env.ANTHROPIC_AUTH_TOKEN` |
| API 类型 (仅 openclaw) | `openclaw.json` → `models.providers.local.api` + `models[0].api` | — | — |
| 模型 ID | `openclaw.json` → `models[0].id/name` + `agents.defaults.model.primary` | `hermes_config.yaml` → `model.default` + `model.model` | `cc_settings.json` → `env.ANTHROPIC_MODEL` |

#### Tab: 用户模拟配置

| 表单字段 | 写入文件 | 说明 |
|---------|---------|------|
| agents[i].name | `user_proxy_model.json` → 顶层 key | 如 `user_simulator`、`evaluator` |
| agents[i].model | `user_proxy_model.json` → `{name}.model` | |
| agents[i].base_url | `user_proxy_model.json` → `{name}.base_url` | |
| agents[i].api_key | `user_proxy_model.json` → `{name}.api_key` | |
| agents[i].provider | `user_proxy_model.json` → `{name}.provider` | |
| agents[i].api | `user_proxy_model.json` → `{name}.api` | |

> 注意：当 agents 数组有值时，`user_proxy_model.json` 会被**完全覆盖**为 agents 数组内容，模板中的原有配置不保留。

#### Tab: 高级配置

| 表单字段 | 写入文件 | 写入路径 |
|---------|---------|---------|
| 起始索引 (start_index) | config.yaml | `run_config.start_index` |
| 任务总数 (total_num) | config.yaml | `run_config.total_num` |
| 镜像名称 (image_name) | config.yaml | `sandbox.x86_cpu.sandbox.image` |
| 代码仓 (code_repo_id) | config.yaml | `run_config.task.main_code_tar` (下载打包后设路径) |

### 配置文件来源优先级

创建任务时，模板文件按以下顺序查找：

```
1. settings/{harness_type}/{version}/{filename}  （Harness 配置选中的自定义版本）
2. settings/{filename}                            （默认 fallback）
```

### 生成的实例目录结构

```
instances/{instance_id}/
  ├── config.yaml              # 任务配置（从模板生成 + 表单覆盖）
  ├── openclaw.json             # 或 hermes_config.yaml / cc_settings.json
  ├── user_proxy_model.json     # Agent 配置
  ├── configs/                  # 从 OBS 下载的用户任务数据
  ├── outputs/                  # 任务输出
  └── downloads/                # 任务下载
```

---

## 三、任务登记

管理页面：`任务登记`

任务登记是"新建任务"的**预填数据源**。从登记点击"创建任务"时，以下字段自动填入新建任务表单：

| 登记字段 | → 新建任务字段 | 说明 |
|---------|--------------|------|
| task_name | name (拼接) | `{task_name}-{requester}` |
| — | task_name | 自动生成 `{username}_{timestamp}_{rand}` |
| task_path_obs | user_config_dir | 去掉 `obs://rl-agentdata/` 前缀 |
| skill_dir_obs | skill_dir | 同上 |
| agent_dir_obs | agent_dir | 同上 |
| user_folder_obs | user_profile_dir | 同上 |
| default_skills | default_skills | |
| data_total | total_num | |
| harness_type | harness_type | |
| model_name | model_id | |
| eval_model_name | agents[0].model | 覆盖第一个 agent 的 model |
| eval_config_model | 新增 evaluator agent | 如果不存在则添加 |
| config_template_id | → 加载配置模板 | 间接填充 API 配置 |

### 登记字段中未直接使用的

| 登记字段 | 状态 | 说明 |
|---------|------|------|
| base_url | **未使用** | 登记中的 base_url 不会填入 model_base_url |
| api_key | **未使用** | 登记中的 api_key 不会填入 model_api_key |
| eval_config_base_url | **未使用** | 不会填入 evaluator agent 的 base_url |
| eval_config_api_key | **未使用** | 不会填入 evaluator agent 的 api_key |
| eval_config_api | **未使用** | 不会填入 evaluator agent 的 api |
| user_proxy_model_name | **未使用** | 不会填入任何字段 |

---

## 四、配置模板

管理页面：`配置模板`

配置模板保存用户常用的 **API 相关配置**，按用户隔离（每人看自己的）。

### 模板字段 → 新建任务字段

当从登记创建任务且登记关联了 config_template_id 时，调用 `applyTemplate(tpl)` 填充：

| 模板字段 | → 新建任务字段 | 说明 |
|---------|--------------|------|
| model_base_url | model_base_url | |
| invite_code | invite_code | |
| model_api_type | model_api_type | |
| model_id | model_id | |
| image_name | image_name | |
| code_repo_id | code_repo_id | |
| agents_json | agents[] | JSON 解析后覆盖 agents 数组 |

> 注意：`applyTemplate` 会清空 `model_api_key`（模板不存密钥，每次需重新申请或填写）。

---

## 五、已知问题

### 1. 登记中有字段无输入控件或执行时未映射

**提交登记页面（TaskRegister.vue）中的字段情况：**

| 字段 | 有输入控件？ | 执行时映射到新建任务？ | 说明 |
|------|:-----------:|:-------------------:|------|
| base_url | 有（高级配置折叠区） | 未映射 | 登记中填了但执行时不传给 model_base_url |
| api_key | 有（高级配置折叠区） | 未映射 | 同上 |
| eval_config_base_url | **无** | 未映射 | form 中声明了但无 UI 控件 |
| eval_config_api_key | **无** | 未映射 | 同上 |
| eval_config_api | **无** | 未映射 | 同上 |
| user_proxy_model_name | **无** | 未映射 | 同上 |

这些字段在 Pydantic model 和数据库中都存在，form ref 中也声明了（随提交一起存入DB），但实际不产生任何效果。

如果这些字段已确认不需要，可以从 model 和 form 中清理掉。如果需要使用，需要：
1. 在 TaskRegister.vue 中添加对应的输入控件
2. 在 CreateInstance.vue 的 `fromRegistration` 处理逻辑中添加映射

### 2. 旧版 `settings/harness/` 目录残留

`settings/harness/1/`、`settings/harness/2/` 等旧版按数字 ID 存储的目录仍然存在。新版已改为 `settings/{harness_type}/{version}/` 结构，旧目录可以清理。

### 3. agents 填写时 user_proxy_model.json 被完全覆盖

当表单中 `agents` 数组有值时，`user_proxy_model.json` 模板内容会被**完全替换**为 agents 数组的内容（见 `instances.py` 第 363-377 行）。如果 agents 中某个 agent 漏填了字段（如 base_url），模板中该 agent 原有的 base_url 也不会保留。

### 4. openclaw 的 agent provider 注入不完整

表单中 agents 的 `provider` 字段仅写入 `user_proxy_model.json`。如果某个 agent 的 provider 值（如 `local-evaluator`）需要在 `openclaw.json` 的 `models.providers` 中注册对应的 provider 配置（baseUrl、apiKey 等），当前代码没有处理这个注入。这意味着 openclaw.json 中只有 `local` 这一个 provider 定义，如果 user_proxy_model.json 引用了其他 provider（如 `local-evaluator`），openclaw 运行时可能找不到该 provider 的连接信息。
