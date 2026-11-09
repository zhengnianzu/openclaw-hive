# 配置体系详解

面向维护这套代码的开发。本文覆盖平台上**所有配置入口**：从 Harness 配置模板，到新建任务表单，到沙箱配置与超时派生，再到最终落盘的实例配置文件。

> 本文由 `platform/README_Config.md` 迁移并扩充而来（原文已删除）。相比原文，新增了「沙箱配置」「三层超时派生」「资源字段」三章，并把 harness 类型从硬编码的 3 种更正为数据驱动的 14 种。

## 目录

- [一、整体流程](#一整体流程)
- [二、Harness 类型注册表](#二harness-类型注册表)
- [三、Harness 配置](#三harness-配置)
- [四、新建任务](#四新建任务)
- [五、沙箱配置](#五沙箱配置)
- [六、三层超时派生](#六三层超时派生)
- [七、CPU / 内存资源字段](#七cpu--内存资源字段)
- [八、任务登记](#八任务登记)
- [九、配置模板](#九配置模板)
- [十、已知问题](#十已知问题)

---

## 一、整体流程

```
Harness 类型注册表 (settings/harness_config.json)
        │  决定每种 harness 用哪个 agent 配置文件名
        ▼
Harness 配置 (三个模板文件: config.yaml / <agent_config> / user_proxy_model.json)
        │
        ▼
    沙箱配置 (settings/sandbox/*.yaml，整段覆盖 config.yaml 的沙箱相关顶层键)
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

- **任务登记**：预填任务相关信息（OBS 路径、模型名等），点击「创建任务」时自动填入新建任务表单
- **配置模板**：保存用户常用的 API 配置（base_url、model_id、agents 等），按用户隔离，创建任务时可复用

---

## 二、Harness 类型注册表

**真源**：`platform/settings/harness_config.json`

Harness 类型不是硬编码的。新增一种 harness **只需在此文件加一条**，前后端自动派生（下拉选项、标签颜色、配置文件名、各处 `harness_type` 分支的文件解析）。

字段含义：

| 字段 | 作用 |
|------|------|
| `id` | 类型标识，等于 JSON 的 key |
| `name` | 前端显示名 |
| `hive_config` | 任务配置文件名，目前全部是 `config.yaml` |
| `user_config` | 模拟配置文件名，目前全部是 `user_proxy_model.json` |
| `agent_config` | **各 harness 自己的模型接入配置文件名**，这是唯一真正有差异的字段 |
| `ui_color` | 标签颜色（前端 `harnessStore.tagStyle()` 会把它调成浅底同色深字） |

`_meta.exclude_from_lists` 列出不在前端下拉里出现的类型（当前为 `common`、`jiuwenswarm`）。

当前注册的 14 种：

| id | 显示名 | agent_config |
|----|--------|--------------|
| `openclaw` | OpenClaw | `openclaw.json` |
| `openclaw-tui` | OpenClaw TUI | `openclaw_tui.json` |
| `hermes` | Hermes | `hermes_config.yaml` |
| `claude-code` | Claude Code | `cc_settings.json` |
| `jiuwenswarm-dev` | JiuwenSwarm-dev | `jw_dev_config.yaml` |
| `jiuwenswarm-xiaoyi` | JiuwenSwarm-xiaoyi | `jw_xiaoyi_config.yaml` |
| `openjiuwen` | Jiuwen Claw | `openjiuwen.json` |
| `opencode` | OpenCode | `opencode.json` |
| `codex` | Codex | `config.toml` |
| `pi` | Pi | `models.json` |
| `grok` | Grok | `grok_config.toml` |
| `dsh` | DeepSeek Harness | `dsh_settings.yaml` |
| `jiuwenswarm` | JiuwenSwarm（旧标识） | `jw_config.yaml` |
| `common` | 通用 | `null`（无专属配置文件） |

> `JIUWENSWARM_TYPES = {"jiuwenswarm", "jiuwenswarm-dev", "jiuwenswarm-xiaoyi"}`（`instances.py:40`）这三种共用同一套注入逻辑，但**实例内文件名跟随各自的 `agent_config`**，不要硬编码成 `jw_config.yaml`。

---

## 三、Harness 配置

管理页面：`Harness 配置管理`　｜　路由：`platform/api/routers/harness_configs.py`

每个 Harness 配置包含三个文件，具体文件名由上一章的注册表按 `harness_type` 决定：

| 文件分类 | 文件名来源 | 作用 |
|---------|-----------|------|
| 任务配置 | `hive_config`（`config.yaml`） | 运行参数（并发、OBS 路径、任务路径、超时） |
| Harness 配置 | `agent_config`（因 harness 而异） | 模型接入（API 地址、密钥、模型 ID） |
| 模拟配置 | `user_config`（`user_proxy_model.json`） | Agent 配置（user_simulator、evaluator） |

### 存储路径

| 版本 | 路径 | 说明 |
|------|------|------|
| 默认 | `settings/` | 所有 harness_type 共用同一目录 |
| 自定义 | `settings/{harness_type}/{version}/` | 例如 `settings/openclaw/v1/` |

### 默认注册（启动时自动）

系统启动时 `ensure_defaults()` 为各 harness 类型注册一个「默认」版本，指向 `settings/` 目录。

### 文件来源

每个文件可独立获取：

1. **从模板初始化** — 从 `settings/{filename}` 复制
2. **从 OBS 拉取** — 每个文件有独立的 OBS 路径（`obs_harness_path` / `obs_task_path` / `obs_proxy_path`）
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

## 四、新建任务

页面：`新建任务实例`　｜　核心逻辑：`platform/api/routers/instances.py` 的 `create_instance()`

### 4.1 主表单

| 表单字段 | 写入文件 | 写入路径 | 代码 |
|---------|---------|---------|------|
| 实例名称 (`name`) | DB | `task_instances.name` | 仅展示 |
| 任务标识 (`task_name`) | config.yaml | `sandbox_id_prefix` | `instances.py:919` |
| Harness 类型 | config.yaml | `run_config.harness_type` | `instances.py:920` |
| Harness 配置 | — | — | 选 `harness_config_id`，决定模板来源目录 |
| 并发数 | config.yaml | `run_config.concurrent_num` | `instances.py:921` |

### 4.2 Tab: OBS 配置

| 表单字段 | 写入路径（均在 config.yaml） | 代码 |
|---------|----------------------------|------|
| 技能目录 (`skill_dir`) | `run_config.obs.skill_download_path` | `:956` |
| 默认技能 (`default_skills`) | `run_config.obs.default_skills`（逗号 → 列表） | `:958` |
| Agent 目录 (`agent_dir`) | `run_config.obs.agents_download_path` | `:960` |
| 用户 Config 目录 (`user_config_dir`) | `run_config.task.task_input_path`（下载后设为本地路径） | |
| 用户 Profile 目录 (`user_profile_dir`) | `run_config.obs.user_profile_download_path` | `:964` |
| 轨迹保存路径 (`traj_save_path`) | `run_config.obs.traj_save_path` | `:969` |

> **`traj_save_path` 留空时由后端生成**（`instances.py:965-969`）：
> `{harness_meta.traj_prefix(harness_type)}/traj_{task_name}`。
> `create_params` 保存的是**最终生效值**而非用户输入的空串，所以详情页显示的一定是实际落盘路径。

另有几个固定路径，不由表单控制（`instances.py:1005-1006`）：

- `run_config.task.task_output_path` = `{instance_dir}/outputs`
- `run_config.task.task_download_path` = `{instance_dir}/downloads`
- `run_config.sandbox.harness_local_config_file` = `{instance_dir}/{agent_config}`
- `run_config.sandbox.user_proxy_model_local_file` = `{instance_dir}/user_proxy_model.json`

### 4.3 Tab: Harness 配置（模型接入）

四个表单字段 `model_base_url` / `model_api_key` / `model_api_type` / `model_id` 注入到哪里，**每种 harness 完全不同**。代码是 `instances.py:1026` 起的一长串 `elif req.harness_type == ...` 分支。

| harness | base_url | api_key | model_id |
|---------|----------|---------|----------|
| `openclaw` 系 | `models.providers.local.baseUrl` | `models.providers.local.apiKey` | `models[0].id/name` + `agents.defaults.model.primary` |
| `hermes` | `model.base_url` | `model.api_key` | `model.default` + `model.model` |
| `claude-code` | `env.ANTHROPIC_BASE_URL` | `env.ANTHROPIC_AUTH_TOKEN` | `env.ANTHROPIC_MODEL` |
| `openjiuwen` | `default.base_url` | `default.api_key` | `default.model`（另有 `default.provider` ← `model_api_type`，默认 `Anthropic`） |
| `opencode` | `provider.<k>.options.baseURL` | `provider.<k>.options.apiKey` | 写入 `provider.<k>.models` 并设 `agent.main.model = <k>/<model_id>` |
| `codex` | `model_providers.<k>.base_url` | `model_providers.<k>.experimental_bearer_token` | 顶层 `model` |
| `pi` | `providers.<k>.baseUrl` | `providers.<k>.apiKey` | `providers.<k>.models[0].id/name`（另有 `.api` ← `model_api_type`） |
| `grok` | `model.base_url` | `model.api_key` | `model.model` + `model.name` |
| `dsh` | provider 块 | `apiKeyEnv` 指向的环境变量 | provider 块（`api` ← `model_api_type`） |
| `jiuwenswarm` 系 | `models.defaults[is_default].model_client_config` | 同左 `.api_key` | 同左 |

**`/v1` 后缀处理**（容易踩）：`opencode`、`codex`、`grok` 会强制给 `base_url` 补 `/v1`；`pi` 则按 `api` 类型双向调整——`openai-completions` 补 `/v1`，`anthropic-messages` 反而**去掉** `/v1`。所以同一个 base_url 填进不同 harness，落盘结果不一样。

**jiuwenswarm 系用 pyyaml 而非 OmegaConf 往返**（`instances.py:1277-1283`）：该文件大量使用 `${VAR:-default}` 占位符（jiuwenswarm 自己的 env 模板语法），OmegaConf 会当成 interpolation 解析并抛 `UnsupportedInterpolationType`。模板查找顺序是 版本目录 → 扁平 `settings/` → `.example` 兜底。

### 4.4 Tab: 用户模拟配置

| 表单字段 | 写入 `user_proxy_model.json` |
|---------|------------------------------|
| `agents[i].name` | 顶层 key（如 `user_simulator`、`evaluator`） |
| `agents[i].model` | `{name}.model` |
| `agents[i].base_url` | `{name}.base_url` |
| `agents[i].api_key` | `{name}.api_key` |
| `agents[i].provider` | `{name}.provider` |
| `agents[i].api` | `{name}.api` |

> ⚠️ 当 `agents` 数组有值时，`user_proxy_model.json` 会被**完全覆盖**为 agents 数组内容，模板中的原有配置不保留。见[已知问题 3](#3-agents-填写时-user_proxy_modeljson-被完全覆盖)。

### 4.5 Tab: 高级配置

| 表单字段 | 写入路径 | 代码 |
|---------|---------|------|
| 起始索引 (`start_index`) | `run_config.start_index` | `:922` |
| 任务总数 (`total_num`) | `run_config.total_num` | `:923` |
| 沙箱配置 (`sandbox_config_id`) | 整段覆盖沙箱相关顶层键 | 见[第五章](#五沙箱配置) |
| 沙箱存活时间 (`max_ttl_second`) | 派生三层超时 | 见[第六章](#六三层超时派生) |
| 镜像名称 (`image_name`) | `sandbox.<active_key>.sandbox.image` | `:947` |
| 代码仓 (`code_repo_id`) | `run_config.task.main_code_tar` | `:1010-1019` |
| Key 沙箱后缀 (`suffix_api_keys`) | DB 标记，执行时给 key 打沙箱后缀 | |

代码仓只**记录路径不做下载**：`main_code_tar` 指向 `platform/code/tar/{name}/{version}/openclaw-task.tar`，同时清空 `main_code_dir`，若登记了 `main_python_file` 也一并写入。

### 4.6 配置文件来源优先级

```
1. settings/{harness_type}/{version}/{filename}   （Harness 配置选中的自定义版本）
2. settings/{filename}                             （默认 fallback）
3. settings/{filename}.example                     （仅 jiuwenswarm 系有这层兜底）
```

### 4.7 生成的实例目录结构

```
instances/{instance_id}/
  ├── config.yaml              # 任务配置（模板 + 沙箱配置 + 表单覆盖 + 超时派生）
  ├── {agent_config}           # openclaw.json / hermes_config.yaml / cc_settings.json / ...
  ├── user_proxy_model.json    # Agent 配置
  ├── configs/                 # 从 OBS 下载的用户任务数据
  ├── outputs/                 # 任务输出
  └── downloads/               # 任务下载
```

---

## 五、沙箱配置

管理页面：`沙箱配置`　｜　路由：`platform/api/routers/sandbox_configs.py`

### 5.1 两层模型：文件是权威，DB 只是索引

权威内容在 `platform/settings/sandbox/*.yaml`；DB 表 `sandbox_configs` 只存索引列，**没有任何资源/超时字段**。

**DB 列**（`platform/api/core/database.py:1060-1072`，共 12 列）：

| 列 | 说明 |
|----|------|
| `id` | 自增主键 |
| `name` | UNIQUE，受 `_NAME_RE` 校验 |
| `sandbox_type` | `linux` \| `windows` |
| `sandbox_key` | config.yaml 顶层 `sandbox.<key>`，如 `x86_cpu` / `windows` |
| `file` | 派生文件名（basename），如 `windows_2.yaml` |
| `is_default` | 同 `sandbox_type` 下唯一 |
| `enabled` | |
| `description` | |
| `created_by` / `created_at` / `updated_at` | |

**YAML 内容**：4 个顶层段 `global` / `sandbox` / `sandbox_namespace` / `s3`。

**表单字段 ↔ YAML 路径**（`_extract_summary()` 读，`_apply_summary()` 写）：

| 表单字段 | YAML 路径 | 默认值 |
|---------|-----------|--------|
| `controlplane_base_url` | `global.controlplane_base_url` | `""` |
| `controlplane_api_key` | `global.api_key`（**名字不同**） | `""` |
| `sandbox_namespace` | 顶层 `sandbox_namespace` | `omni-env-default-worker` |
| `cpu_request` / `cpu_limit` | 见 [5.4](#54-linux-与-windows-的差异) | `"2"` / `"3"` |
| `memory_request` / `memory_limit` | 见 [5.4](#54-linux-与-windows-的差异) | `"6Gi"` / `"8Gi"` |
| `env_json` | `sandbox.<key>.sandbox.env` | `[]` |
| `ttl_seconds` | `sandbox.<key>.**max_ttl_second**`（**名字不同**） | `8000` |
| `lease_ttl_seconds` | `sandbox.<key>.lease_ttl_seconds` | `3600` |
| `wait_timeout_seconds` | `sandbox.<key>.wait_timeout_seconds` | `3600` |
| `image` | `sandbox.<key>.sandbox.image`（+ `boot_source.ref`） | `""` |
| `s3_uploader_script` | `s3.s3_uploader_script` | `""` |
| `s3_download_script` | `s3.s3_download_script`，缺失时回退 uploader | |

### 5.2 模板与派生文件

```python
_TEMPLATES = {"linux": ("linux.yaml", "x86_cpu"), "windows": ("windows.yaml", "windows")}
```

- 派生文件名规则：`<type>_<id>.yaml`
- 创建 = 复制模板 (`shutil.copy2`) 再按表单回写 (`_apply_summary`)
- 种子数据：`ensure_defaults()` 仅在表为空时建 `default-linux` / `default-windows`
- 重置：重新复制模板，但**保留 `sandbox_namespace`**

### 5.3 `sandbox_active_key`

`active_sandbox_key(base)` 返回 `base["sandbox_active_key"]`，缺失时取 `base["sandbox"]` 的**第一个** key。

这是平台写入实例 config.yaml 的元字段，排序上紧跟 `s3` 之后。选了沙箱配置时由 `apply_sandbox_to_config` 写入；没选时取 harness 模板自带的 key（`instances.py:941`）。

消费方：`_parse_resources`、`create_params`、`task_export`、前端详情页「沙箱块」。

### 5.4 linux 与 windows 的差异

结构差异集中在 `sandbox.<key>.sandbox.resources`：

| | linux (`sandbox.x86_cpu`) | windows (`sandbox.windows`) |
|---|---|---|
| resources 形状 | **扁平**：`cpu_request` / `cpu_limit` / `memory_request` / `memory_limit` | **嵌套**：`resources.requests.{cpu,memory}` / `resources.limits.{cpu,memory}` |
| 模板默认值 | 2 / 3 核，6Gi / 8Gi | 16 / 16 核，32Gi / 32Gi |
| 块内额外字段 | `architecture: x86`、`accelerator: null` | 另有 `runtime_type: windows`、`node_name`、`boot_source`、`security_context.privileged`、`volumes`(kvm hostPath)、`pod_spec.nodeSelector`、`wait_ready` |
| 模板超时 | lease 900 / max_ttl 8000 / wait 1200 | lease 3600 / max_ttl 8000 / wait 3600 |
| `s3.*_script` | `/home/ma-user/obsutil/obsutil` | `C:\ProgramData\OmniEnv\tools\obsutil\obsutil.exe` |

> ⚠️ 形状判断是**运行时嗅探而非按类型**：`if "requests" in res or "limits" in res`。所以一个写成嵌套形式的 linux 文件会被当作 windows 形状处理，反之亦然。

Windows 的镜像有两处要同步：`_set_inner_image()` 在设 `image` 的同时，**仅当 `boot_source.type == "image"`** 才同步 `boot_source.ref`。该函数用 `MutableMapping` 而非 `dict` 做类型判断，因为 OmegaConf 的 DictConfig 过不了 `isinstance(..., dict)`。

### 5.5 如何应用到实例 config.yaml

`create_instance()` 中的顺序（`instances.py:915-950`）：

1. 加载 harness 模板 `base = OmegaConf.load(template_path)`
2. 若选了 `sandbox_config_id` → `apply_sandbox_to_config(base, row)`；否则只设 `sandbox_active_key` 并做排序整理
3. `apply_image(base, image_name)` — 镜像**刻意不放在第 2 步**，它来自任务表单而非沙箱配置
4. `_apply_timeout_settings(base, max_ttl_second)` — 见下一章

`apply_sandbox_to_config` 的要点：

```python
CONFIG_SECTION_ORDER = ("global", "sandbox", "sandbox_id_prefix", "sandbox_namespace", "s3", "run_config")
SANDBOX_SECTIONS = CONFIG_SECTION_ORDER[:5]
```

虽然 `sandbox_id_prefix` 在 `SANDBOX_SECTIONS` 里，但随后会被 `sections.pop()` 掉——**前缀永远来自任务表单的 `task_name`**，所以实际只从派生文件复制 4 段。

排序是「合并覆盖」而非替换：未列入 `CONFIG_SECTION_ORDER` 的其它顶层键原样保留，统一放在 `run_config` 之前。`rewrite_config_sections` 先算好有序字典再清空重插，避免中间态。

### 5.6 校验

- `_validate_namespace`：RFC-1123 label、≤63 字符，**且必须在 `resource_pools.yaml` 里注册过**
- `_validate_s3_script`：必须是 obsutil 可执行路径，拒绝 shebang 和 shell 元字符
- `PUT` 文件内容：必须是可解析、且含顶层 `sandbox` 的 YAML dict
- 删除：拒绝删默认配置，拒绝删被实例引用的配置

---

## 六、三层超时派生

**代码**：`platform/api/routers/instances.py:41-71`（commit `4a92215` 引入）

用户只填最外层的**沙箱存活上限**，另两层从它减固定余量得到：

```python
DEFAULT_MAX_TTL_SECOND = 36000        # 10 小时
MAX_MAX_TTL_SECOND = 36000            # 与前端 el-input-number 的 :max 一致
MAIN_TIMEOUT_MARGIN_SECONDS = 600
QUERY_TIMEOUT_MARGIN_SECONDS = 1200

max_ttl_second = max(1201, int(max_ttl_second))   # 下限：保证最内层 ≥ 1s
max_ttl_second = min(36000, max_ttl_second)       # 上限：防止绕过界面直接打 API
main_timeout  = max_ttl_second - 600
query_timeout = max_ttl_second - 1200
```

| 层 | 字段路径 | 公式 | 消费方 |
|----|---------|------|--------|
| 1. 沙箱 TTL（**真源**） | `sandbox.<active_key>.max_ttl_second` | 用户输入，钳制到 `[1201, 36000]` | 环境 SDK / 控制面 —— 沙箱生命周期 |
| 2. 主程序超时 | `run_config.task.main_python_timeout` | `max_ttl − 600` | `hive.py` 的 `ExtendExecCommand(timeout=...)` |
| 3. 单 query 超时 | `run_config.task.query_timeout` | `max_ttl − 1200` | `hive.py` 的 `_upload_data_config` 注入到**上传副本**的 `queries[].timeout` |

**为什么是这个顺序**：沙箱 TTL > 主程序超时 > query 超时，保证内层先触发、外层还活着——query 卡死时 harness 还有 600s 收尾落盘，主程序超时退出后沙箱还有 600s 供 OBS 上传轨迹。反过来（沙箱先消失）会把日志和轨迹一起带走，失败原因就查不到了。

默认值 36000 → `max_ttl=36000`、`main=35400`、`query=34800`。

几个要点：

- `queries[].timeout` **不在平台侧改写**，原始任务配置保持原样，只在 hive 上传副本里注入
- 前端输入框 `:min="1800"`，后端下限是 1201 —— 直接打 API 可以比界面更小
- `_apply_timeout_settings` 在 `apply_sandbox_to_config` **之后**调用，所以会覆盖派生文件和 harness 模板里自带的 `max_ttl_second`
- 该函数只有一个调用点（创建时），`_prepare_instance` 里的重算已删除 —— **创建阶段即是终值**，用户手改实例 config.yaml 的超时不会在启动时被覆盖

---

## 七、CPU / 内存资源字段

### 7.1 单位与转换

- **YAML 里是字符串**：CPU 按核（`"2"`、`"16"`，支持 `500m` 毫核），内存按 K8s quantity（`"6Gi"`、`"32Gi"`）
- `_to_quantity(v) -> float`：K8s 内存 → **MiB**，乘数 `{"gi":1024, "mi":1, "g":1000, "m":1}`，解析失败返回 `0.0`
- `to_cores(v, default) -> float`：K8s CPU → **核**，`500m` → 0.5
- `_gib(v)`（在 `instances.py:682`）= `_to_quantity(v) / 1024`，MiB → GiB

所以：**沙箱配置 API 暴露的是 K8s 原生单位的字符串，实例/统计层暴露的是核与 GiB 的浮点数。**

### 7.2 读取链路

`_parse_resources(cfg)`（`instances.py:692-725`）三个分支：

1. 有 `sandbox_active_key` + 嵌套形状 → `requests/limits.{cpu,memory}`
2. 有 `sandbox_active_key` + 扁平形状 → `cpu_request` / `cpu_limit` / ...
3. 旧结构 → `env_make.args.resources.*`

都不匹配时回退 `_RES_FALLBACK = {cpu_request: 2.0, cpu_limit: 3.0, memory_request: 6.0, memory_limit: 8.0}`（核 / GiB）。

`_sandbox_resources(row)` 优先解析实例的 `config_snapshot`，快照缺失或损坏时回退到引用的沙箱配置 DB 行 → 派生文件。结果按 `(id, md5(snapshot), md5(create_params))` 缓存，并有跨 worker 共享的磁盘分片。

### 7.3 资源池（分母）

`platform/settings/sandbox/resource_pools.yaml` + `platform/api/routers/resource_pools.py`：
`total_cpu`（核）、`total_memory_gb`（GiB）、`max_sandboxes`、`frames`；
`usable_sandboxes_per_node = min(max_sandboxes_per_node, cpu_per_node)`；
默认命名空间 `omni-env-default-worker`。

---

## 八、任务登记

管理页面：`任务登记`

任务登记是「新建任务」的**预填数据源**。从登记点击「创建任务」时，以下字段自动填入新建任务表单：

| 登记字段 | → 新建任务字段 | 说明 |
|---------|--------------|------|
| `task_name` | `name`（拼接） | `{task_name}-{requester}` |
| — | `task_name` | 自动生成 `{username}_{timestamp}_{rand}` |
| `task_path_obs` | `user_config_dir` | 去掉 `obs://rl-agentdata/` 前缀 |
| `skill_dir_obs` | `skill_dir` | 同上 |
| `agent_dir_obs` | `agent_dir` | 同上 |
| `user_folder_obs` | `user_profile_dir` | 同上 |
| `default_skills` | `default_skills` | |
| `data_total` | `total_num` | |
| `harness_type` | `harness_type` | |
| `model_name` | `model_id` | |
| `base_url` | `model_base_url` | `CreateInstance.vue:746` |
| `eval_model_name` | `agents[0].model` | 覆盖第一个 agent 的 model |
| `eval_config_model` | 新增 evaluator agent | 如果不存在则添加 |
| `config_template_id` | → 加载配置模板 | 间接填充 API 配置 |

### 登记字段中未直接使用的

| 登记字段 | 状态 |
|---------|------|
| `api_key` | **未使用** — 不会填入 `model_api_key` |
| `eval_config_base_url` | **未使用** |
| `eval_config_api_key` | **未使用** |
| `eval_config_api` | **未使用** |
| `user_proxy_model_name` | **未使用** |

---

## 九、配置模板

管理页面：`配置模板`

配置模板保存用户常用的 **API 相关配置**，按用户隔离（每人看自己的）。

当从登记创建任务且登记关联了 `config_template_id` 时，调用 `applyTemplate(tpl)` 填充：

| 模板字段 | → 新建任务字段 |
|---------|--------------|
| `model_base_url` | `model_base_url` |
| `invite_code` | `invite_code` |
| `model_api_type` | `model_api_type` |
| `model_id` | `model_id` |
| `image_name` | `image_name` |
| `code_repo_id` | `code_repo_id` |
| `agents_json` | `agents[]`（JSON 解析后覆盖） |

> `applyTemplate` 会清空 `model_api_key` —— 模板不存密钥，每次需重新申请或填写。

---

## 十、已知问题

### 1. 登记中有字段无输入控件或执行时未映射

| 字段 | 有输入控件？ | 执行时映射？ | 说明 |
|------|:-----------:|:-----------:|------|
| `base_url` | 有（高级配置折叠区） | **是** | 原 `README_Config.md` 记为「未映射」，现已接上（`CreateInstance.vue:746`） |
| `api_key` | 有（高级配置折叠区） | 否 | 填了但不传给 `model_api_key` |
| `eval_config_base_url` | **无** | 否 | form 中声明了但无 UI 控件 |
| `eval_config_api_key` | **无** | 否 | 同上 |
| `eval_config_api` | **无** | 否 | 同上 |
| `user_proxy_model_name` | **无** | 否 | 同上 |

除 `base_url` 外，这些字段在 Pydantic model 和数据库中都存在，form ref 中也声明了（随提交存入 DB），但不产生任何效果。要启用需：① 在 `TaskRegister.vue` 加输入控件；② 在 `CreateInstance.vue` 的 `fromRegistration` 里加映射。

### 2. `settings/harness/` 与 `settings/{harness_type}/` 两套目录并存

原 `README_Config.md` 记载的「`settings/harness/1/`、`settings/harness/2/` 按数字 ID 存储的旧目录」**已不存在**（已清理）。

但 `settings/harness/` 目录本身还在，下面是 13 个按 harness 类型命名的子目录（`openclaw/`、`hermes/`、`codex/` …），与第三章描述的 `settings/{harness_type}/{version}/` 是两个不同层级的位置。排查「模板到底从哪读的」时需留意这两处都可能有同名文件。另有 `settings/harness.bak.mig_152908/` 是迁移备份，可清理。

### 3. agents 填写时 user_proxy_model.json 被完全覆盖

当表单 `agents` 数组有值时，`user_proxy_model.json` 模板内容被**完全替换**。如果某个 agent 漏填了字段（如 `base_url`），模板中该 agent 原有的值也不会保留。

### 4. openclaw 的 agent provider 注入不完整

表单中 agents 的 `provider` 仅写入 `user_proxy_model.json`。若某 agent 的 provider（如 `local-evaluator`）需要在 `openclaw.json` 的 `models.providers` 中注册对应连接信息，当前代码没有处理这个注入 —— openclaw.json 里只有 `local` 一个 provider 定义，运行时可能找不到其它 provider。

### 5. 沙箱资源形状靠运行时嗅探

`if "requests" in res or "limits" in res` 决定扁平/嵌套，与 `sandbox_type` 无关。手写 YAML 时若用了与该类型模板不同的形状，读写都会走错分支。

### 6. `update_sandbox_config` 的 request ≤ limit 校验形同虚设

校验用 `data.get(f, row.get(f))`，而 `row` 是 **DB 行**、DB 里根本没有 `cpu_request` / `memory_request` 列，所以回退值恒为 `None`；只有当一次 PUT 同时带上 request 和 limit 时该校验才真正生效。

### 7. 超时上下限与前端不一致

后端下限 1201 秒，前端输入框 `:min="1800"`。直接调 API 可以设出比界面允许值更小的 TTL。

### 8. `platform/doc/README_sandbox_config.md` 存在文档漂移

该文档的「已实现」部分仍准确，但以下内容与当前代码不符，查阅时注意：

- 称 `sandbox_configs` 为 9 列（实际 12 列）
- 其中的建表 SQL（`resource_pool`、`cpu_request`、`env_json`、`ttl_seconds`、`extra_json` 等列）**从未实现**，这些只存在于 YAML
- `render_sandbox_block(row)` 函数不存在，已被整段替换的 `apply_sandbox_to_config` 取代
- `POST /api/sandbox-configs/import` 未实现
- 称 `hive.py` 读 `sandbox_active_key`；实际读的是 `sandbox_config.sandbox_type`，`hive.py` 中不存在对 `sandbox_active_key` 的任何引用
- 完全没有提及本文[第六章](#六三层超时派生)的三层超时派生
