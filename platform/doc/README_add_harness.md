# Platform 新增 Harness 接入指南

本指南以 `opencode` 为例，描述在 openclaw-hive platform 中新增一个 harness 类型需要修改的所有位置。整个平台目前已支持的 harness 类型：`openclaw` / `hermes` / `claude-code` / `openjiuwen` / `opencode`。

> 前提条件：hive.py 中的 `_FRAMEWORK_LAYOUTS` 字典已注册新 harness 的沙箱路径布局（harness_dir / harness_local_config / harness_sandbox_config / upload_paths / workspace_base）。如果 hive.py 中没有，需要先在 hive.py 中新增。

## 修改清单总览

共需修改 3 类文件，分布在后端 Python、settings 配置、前端 Vue 三层：

| 层级 | 文件 | 修改内容 |
|------|------|----------|
| 后端 | `api/routers/harness_configs.py` | HARNESS_FILES、EXPECTED_FILES、ensure_defaults |
| 后端 | `api/routers/instances.py` | ALLOWED_CONFIG_FILES、traj_prefixes、配置路径变量、harness配置生成、API Key回写 |
| Settings | `settings/<harness配置文件名>` | 新建模板文件 + .example |
| Settings | `settings/field_mappings.json` | 新增字段映射块 |
| 前端 | 8 个 .vue 文件 | 下拉选项、标签颜色、标签文本、映射提示 |

---

## 一、后端 Python（2 个文件）

### 1. platform/api/routers/harness_configs.py

三处修改：

**(a) HARNESS_FILES 字典** — 新增一行，声明该 harness 的配置文件名：

```python
HARNESS_FILES = {
    "openclaw": "openclaw.json",
    "hermes": "hermes_config.yaml",
    "claude-code": "cc_settings.json",
    "openjiuwen": "openjiuwen.json",
    "opencode": "opencode.json",          # ← 新增
    "common": None,
}
```

**(b) EXPECTED_FILES 字典** — 新增一行，声明该 harness 期望的配置文件列表（用于文件管理界面扫描）：

```python
EXPECTED_FILES = {
    ...
    "opencode": ["opencode.json", "config.yaml", "user_proxy_model.json"],  # ← 新增
    "common": ["config.yaml", "user_proxy_model.json"],
}
```

**(c) ensure_defaults() 函数** — 循环列表追加新类型名，使系统启动时自动注册默认配置：

```python
for htype in ["openclaw", "hermes", "claude-code", "openjiuwen", "opencode"]:
```

### 2. platform/api/routers/instances.py

四处修改：

**(a) ALLOWED_CONFIG_FILES 集合** — 追加新配置文件名，允许实例配置查看接口访问该文件：

```python
ALLOWED_CONFIG_FILES = {..., "opencode.json"}
```

**(b) traj_prefixes 字典** — 追加轨迹保存路径前缀（OBS 上的目录名）：

```python
traj_prefixes = {"hermes": "hermes_trajs", "claude-code": "cc_trajs", "openjiuwen": "openjiuwen_trajs", "opencode": "opencode_trajs"}
```

**(c) 实例配置路径变量 + harness_local_config_file 赋值分支**（`create_instance` 函数内，约 396 行）：

```python
opencode_path = os.path.join(instance_dir, "opencode.json")    # ← 新增路径变量

if req.harness_type == "hermes":
    base.run_config.sandbox.harness_local_config_file = hermes_config_path
elif req.harness_type == "claude-code":
    base.run_config.sandbox.harness_local_config_file = cc_settings_path
elif req.harness_type == "openjiuwen":
    base.run_config.sandbox.harness_local_config_file = openjiuwen_path
elif req.harness_type == "opencode":                           # ← 新增分支
    base.run_config.sandbox.harness_local_config_file = opencode_path
else:
    base.run_config.sandbox.harness_local_config_file = openclaw_path
```

**(d) harness 配置文件生成逻辑**（`create_instance` 函数内，"--- 2. 生成 harness 配置文件 ---" 区域）：

新增 `elif req.harness_type == "opencode"` 分支。核心逻辑：从模板加载 JSON，按新 harness 的配置结构注入 `model_base_url` / `model_api_key` / `model_id`。

以 opencode 为例，其配置结构为 `provider.<name>.{npm, options.{baseURL,apiKey}, models}` + `agent.main.model = "<provider>/<model_id>"`。

注意两个特殊处理：
- **npm 包名由 API 类型决定**：`anthropic-messages` → `@ai-sdk/anthropic`；`openai-completions` → `@ai-sdk/openai-compatible`。前端需为 opencode 显示 API 类型选择器（与 openclaw 共用同一个 `model_api_type` 字段）。
- **baseURL 自动补 `/v1`**：opencode 要求 baseURL 带 `/v1` 后缀，代码会自动补全。

```python
elif req.harness_type == "opencode":
    opencode_template = os.path.join(harness_settings_dir, "opencode.json")
    if not os.path.exists(opencode_template):
        opencode_template = os.path.join(settings.SETTINGS_DIR, "opencode.json")
    with open(opencode_template, "r", encoding="utf-8") as f:
        opencode_cfg = json.load(f)

    providers = opencode_cfg.setdefault("provider", {})
    main_provider_key = "local-provider"
    # 根据 model_api_type 决定 npm 包名；缺省时保留模板中的值
    if req.model_api_type == "anthropic-messages":
        npm_pkg = "@ai-sdk/anthropic"
    elif req.model_api_type == "openai-completions":
        npm_pkg = "@ai-sdk/openai-compatible"
    else:
        npm_pkg = None  # 保留模板原有值

    if main_provider_key not in providers:
        providers[main_provider_key] = {
            "name": main_provider_key,
            "npm": npm_pkg or "@ai-sdk/anthropic",
            "options": {"baseURL": "", "apiKey": ""},
            "models": {},
        }
    elif npm_pkg:
        providers[main_provider_key]["npm"] = npm_pkg

    main_opts = providers[main_provider_key].setdefault("options", {})
    if req.model_base_url:
        # opencode 要求 baseURL 带 /v1 后缀
        base_url = req.model_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        main_opts["baseURL"] = base_url
    if req.model_api_key:
        main_opts["apiKey"] = req.model_api_key

    if req.model_id:
        models_map = providers[main_provider_key].setdefault("models", {})
        models_map[req.model_id] = {"name": req.model_id}
        agents_section = opencode_cfg.setdefault("agent", {})
        main_agent = agents_section.setdefault("main", {})
        main_agent["model"] = f"{main_provider_key}/{req.model_id}"

    with open(opencode_path, "w", encoding="utf-8") as f:
        json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
```

同时在 "--- 3. 自动申请 API Key ---" 区域，新增 opencode 的 key 回写逻辑：

```python
elif req.harness_type == "opencode" and os.path.exists(opencode_path):
    with open(opencode_path, "r", encoding="utf-8") as f:
        opencode_cfg = json.load(f)
    opencode_cfg.setdefault("provider", {}).setdefault("local-provider", {}).setdefault("options", {})["apiKey"] = req.model_api_key
    with open(opencode_path, "w", encoding="utf-8") as f:
        json.dump(opencode_cfg, f, indent=2, ensure_ascii=False)
```

---

## 二、Settings 配置文件（3 个文件）

### 1. platform/settings/<配置文件名>

新建模板文件，含 harness 基础配置，敏感字段用占位符。同时创建 `.example` 后缀的副本。

以 `opencode.json` 为例：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "local-provider": {
      "npm": "@ai-sdk/anthropic",
      "name": "local-provider",
      "options": { "baseURL": "http://.../v1", "apiKey": "..." },
      "models": { "model-id": { "name": "model-id" } }
    }
  },
  "agent": {
    "main": {
      "description": "main actor",
      "mode": "primary",
      "model": "local-provider/model-id",
      "prompt": "",
      "tools": { "write": true, "edit": true }
    }
  }
}
```

### 2. platform/settings/field_mappings.json

新增一个 key，描述前端表单字段到配置文件 JSON 路径的映射。该映射用于 HarnessConfigs 页面的"字段映射提示"功能，帮助用户理解创建实例时哪些配置项会被覆盖。

```json
"opencode": {
  "model_api_key": "provider.local-provider.options.apiKey",
  "model_base_url": "provider.local-provider.options.baseURL",
  "model_api_type": "provider.local-provider.npm",
  "model_id": [
    "provider.local-provider.models.{model_id}.name",
    "agent.main.model"
  ]
}
```

> 注意：`model_id` 的路径中 `{model_id}` 是动态键名占位符，表示以实际 model_id 值作为 JSON key。

---

## 三、前端 Vue 文件（8 个文件）

所有 Vue 文件中，需要在新 harness 类型出现的下拉选项、标签颜色映射、标签文本映射中补充。通用模式：

### 下拉选项（el-select / el-option）追加

```html
<el-option label="OpenCode" value="opencode" />
```

### tagType / harnessTagType 函数追加

```javascript
opencode: 'info'   // 选一个颜色: primary/warning/success/danger/info
```

### typeLabel / harnessLabel 函数追加

```javascript
opencode: 'OpenCode'
```

### 各文件具体修改点

| 文件 | 修改点 |
|------|--------|
| `HarnessConfigs.vue` | 筛选下拉 + 新建下拉 + HARNESS_FILE_LABELS + tagType + typeLabel + computeMappingHints |
| `CreateInstance.vue` | harness 类型下拉 |
| `Dashboard.vue` | 筛选下拉 + 表格列 tag 颜色 |
| `InstanceDetail.vue` | 创建参数展示区 tag 颜色 |
| `ImageManagement.vue` | 筛选下拉 + 新建下拉 + tagType + harnessLabel |
| `TaskRegister.vue` | harness 类型下拉 |
| `TaskRegistrationList.vue` | 编辑下拉 + tagType + harnessLabel |
| `ConfigTemplates.vue` | harness 类型下拉 + tagType + harnessLabel |

---

## 四、hive.py（前提条件）

hive.py 中的 `_FRAMEWORK_LAYOUTS` 字典定义了每个 harness 的沙箱路径布局。如果新 harness 已在 hive.py 中注册，则 platform 侧无需修改 hive.py。如果 hive.py 中没有，需要新增：

```python
"<新harness>": {
    "harness_dir":            "/home/ma-user/.<harness名>",
    "harness_local_config":   "uploads/<配置文件名>",
    "harness_sandbox_config": "/home/ma-user/.<harness名>/<配置文件名>",
    "upload_paths":           [...],
    "workspace_base":         "/home/ma-user/.<harness名>/workspace",
},
```

同时检查 hive.py 的 `_copy_agent_config` 方法中是否有针对新 harness 的特殊处理逻辑（如 opencode 需要注入 provider/agent 从 user_proxy_model.json）。

---

## 五、验证步骤

1. 重启后端 API 服务，确认 `ensure_defaults()` 自动注册了新 harness 的默认配置
2. 前端访问 HarnessConfigs 页面，确认新类型出现在筛选和新建下拉中
3. 新建一个新 harness 的配置版本，在文件管理中确认能正确扫描到期望的配置文件
4. 在 CreateInstance 页面选择新 harness 类型，确认 harness 配置下拉能加载对应配置
5. 创建实例后，检查实例目录下是否正确生成了 harness 配置文件，且 model_base_url/model_api_key/model_id 已正确注入
6. 启动实例，确认 hive.py 能正确识别新 harness 类型并执行

---

## 附：各 harness 的配置文件与颜色对照

| harness_type | 配置文件名 | 标签颜色 | 显示名称 |
|---|---|---|---|
| openclaw | openclaw.json | primary | OpenClaw |
| hermes | hermes_config.yaml | warning | Hermes |
| claude-code | cc_settings.json | success | Claude Code |
| openjiuwen | openjiuwen.json | danger | Jiuwen Claw |
| opencode | opencode.json | info | OpenCode |
| common | (无) | info | 通用 |
