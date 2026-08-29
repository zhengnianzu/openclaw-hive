# Platform 新增 Harness 接入指南

本指南以 `grok` 为例，描述在 openclaw-hive platform 中新增一个 harness 类型需要修改的所有位置。整个平台目前已支持的 harness 类型：`openclaw` / `hermes` / `claude-code` / `openjiuwen` / `opencode` / `codex` / `pi`。

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
    "opencode": "opencode.json",
    "codex": "config.toml",
    "pi": "models.json",
    "grok": "grok.json",          # ← 新增
    "common": None,
}
```

**(b) EXPECTED_FILES 字典** — 新增一行，声明该 harness 期望的配置文件列表（用于文件管理界面扫描）：

```python
EXPECTED_FILES = {
    "openclaw": ["openclaw.json", "config.yaml", "user_proxy_model.json"],
    "hermes": ["hermes_config.yaml", "config.yaml", "user_proxy_model.json"],
    "claude-code": ["cc_settings.json", "config.yaml", "user_proxy_model.json"],
    "openjiuwen": ["openjiuwen.json", "config.yaml", "user_proxy_model.json"],
    "opencode": ["opencode.json", "config.yaml", "user_proxy_model.json"],
    "codex": ["config.toml", "config.yaml", "user_proxy_model.json"],
    "pi": ["models.json", "config.yaml", "user_proxy_model.json"],
    "grok": ["grok.json", "config.yaml", "user_proxy_model.json"],  # ← 新增
    "common": ["config.yaml", "user_proxy_model.json"],
}
```

**(c) ensure_defaults() 函数** — 循环列表追加新类型名，使系统启动时自动注册默认配置：

```python
for htype in ["openclaw", "hermes", "claude-code", "openjiuwen", "opencode", "codex", "pi", "grok"]:
```

### 2. platform/api/routers/instances.py

五处修改：

**(a) ALLOWED_CONFIG_FILES 集合** — 追加新配置文件名，允许实例配置查看接口访问该文件：

```python
ALLOWED_CONFIG_FILES = {..., "grok.json"}
```

**(b) traj_prefixes 字典** — 追加轨迹保存路径前缀（OBS 上的目录名）：

```python
traj_prefixes = {
    "hermes": "hermes_trajs", 
    "claude-code": "cc_trajs", 
    "openjiuwen": "openjiuwen_trajs", 
    "opencode": "opencode_trajs", 
    "codex": "codex_trajs", 
    "pi": "pi_trajs",
    "grok": "grok_trajs"  # ← 新增
}
```

**(c) 实例配置路径变量**（`create_instance` 函数内，约 401-407 行）：

```python
openclaw_path = os.path.join(instance_dir, "openclaw.json")
hermes_config_path = os.path.join(instance_dir, "hermes_config.yaml")
cc_settings_path = os.path.join(instance_dir, "cc_settings.json")
openjiuwen_path = os.path.join(instance_dir, "openjiuwen.json")
opencode_path = os.path.join(instance_dir, "opencode.json")
codex_path = os.path.join(instance_dir, "config.toml")
pi_path = os.path.join(instance_dir, "models.json")
grok_path = os.path.join(instance_dir, "grok.json")    # ← 新增
user_proxy_path = os.path.join(instance_dir, "user_proxy_model.json")
```

**(d) harness_local_config_file 赋值分支**（`create_instance` 函数内，约 410-424 行）：

```python
if req.harness_type == "hermes":
    base.run_config.sandbox.harness_local_config_file = hermes_config_path
elif req.harness_type == "claude-code":
    base.run_config.sandbox.harness_local_config_file = cc_settings_path
elif req.harness_type == "openjiuwen":
    base.run_config.sandbox.harness_local_config_file = openjiuwen_path
elif req.harness_type == "opencode":
    base.run_config.sandbox.harness_local_config_file = opencode_path
elif req.harness_type == "codex":
    base.run_config.sandbox.harness_local_config_file = codex_path
elif req.harness_type == "pi":
    base.run_config.sandbox.harness_local_config_file = pi_path
elif req.harness_type == "grok":                           # ← 新增分支
    base.run_config.sandbox.harness_local_config_file = grok_path
else:
    base.run_config.sandbox.harness_local_config_file = openclaw_path
```

**(e) harness 配置文件生成逻辑**（`create_instance` 函数内，"--- 2. 生成 harness 配置文件 ---" 区域）：

新增 `elif req.harness_type == "grok"` 分支。核心逻辑：从模板加载 JSON，按新 harness 的配置结构注入 `model_base_url` / `model_api_key` / `model_id`。

以 grok 为例，假设其配置结构为简单的顶层字段 `{model, base_url, api_key}`（请根据实际 grok 配置结构调整）：

```python
elif req.harness_type == "grok":
    grok_template = os.path.join(harness_settings_dir, "grok.json")
    if not os.path.exists(grok_template):
        grok_template = os.path.join(settings.SETTINGS_DIR, "grok.json")
    with open(grok_template, "r", encoding="utf-8") as f:
        grok_cfg = json.load(f)

    # 注入配置（根据 grok 实际结构调整路径）
    if req.model_id:
        grok_cfg["model"] = req.model_id
    if req.model_base_url:
        grok_cfg["base_url"] = req.model_base_url
    if req.model_api_key:
        grok_cfg["api_key"] = req.model_api_key

    with open(grok_path, "w", encoding="utf-8") as f:
        json.dump(grok_cfg, f, indent=2, ensure_ascii=False)
```

**API Key 自动申请回写逻辑**（"--- 3. 自动申请 API Key ---" 区域，约 648-700 行）：

```python
elif req.harness_type == "grok" and os.path.exists(grok_path):
    with open(grok_path, "r", encoding="utf-8") as f:
        grok_cfg = json.load(f)
    grok_cfg["api_key"] = req.model_api_key
    with open(grok_path, "w", encoding="utf-8") as f:
        json.dump(grok_cfg, f, indent=2, ensure_ascii=False)
```

---

## 二、Settings 配置文件（3 个文件）

### 1. platform/settings/<配置文件名>

新建模板文件，含 harness 基础配置，敏感字段用占位符。同时创建 `.example` 后缀的副本。

以 `grok.json` 为例（请根据 grok 实际配置格式调整）：

```json
{
  "model": "grok-2-latest",
  "base_url": "https://api.x.ai/v1",
  "api_key": "xai-..."
}
```

创建两个文件：
- `settings/grok.json`
- `settings/grok.json.example`

### 2. platform/settings/field_mappings.json

新增一个 key，描述前端表单字段到配置文件 JSON 路径的映射。该映射用于 HarnessConfigs 页面的"字段映射提示"功能，帮助用户理解创建实例时哪些配置项会被覆盖。

```json
"grok": {
  "model_id": "model",
  "model_base_url": "base_url",
  "model_api_key": "api_key"
}
```

完整示例（追加到现有 field_mappings.json 末尾）：

```json
{
  "config.yaml": { ... },
  "openclaw": { ... },
  "hermes": { ... },
  "claude-code": { ... },
  "openjiuwen": { ... },
  "opencode": { ... },
  "codex": { ... },
  "pi": { ... },
  "grok": {
    "model_id": "model",
    "model_base_url": "base_url",
    "model_api_key": "api_key"
  },
  "user_proxy_model": { ... }
}
```

---

## 三、前端 Vue 文件（8 个文件）

所有 Vue 文件中，需要在新 harness 类型出现的下拉选项、标签颜色映射、标签文本映射中补充。通用模式：

### 下拉选项（el-select / el-option）追加

```html
<el-option label="Grok" value="grok" />
```

### HARNESS_COLORS 常量追加（HarnessConfigs.vue）

```javascript
const HARNESS_COLORS = {
  openclaw: '#409eff', hermes: '#e6a23c', 'claude-code': '#67c23a',
  openjiuwen: '#f56c6c', opencode: '#909399',
  codex: '#8e44ad', pi: '#17a2b8',
  grok: '#00d084',  // ← 新增（绿色调，x.ai 主题色）
  common: '#c0c4cc',
}
```

### typeLabel 函数追加

```javascript
function typeLabel(t) {
  return { 
    openclaw: 'OpenClaw', hermes: 'Hermes', 'claude-code': 'Claude Code', 
    openjiuwen: 'Jiuwen Claw', opencode: 'OpenCode', codex: 'Codex', pi: 'Pi', 
    grok: 'Grok',  // ← 新增
    common: '通用' 
  }[t] || t
}
```

### computeMappingHints 函数追加（HarnessConfigs.vue）

```javascript
function computeMappingHints() {
  if (!currentFile.value || !fieldMappings.value) { mappingHints.value = []; return }
  const hints = []
  const fname = currentFile.value
  let section = null
  if (fname === 'config.yaml') section = fieldMappings.value['config.yaml']
  else if (fname === 'openjiuwen.json') section = fieldMappings.value['openjiuwen']
  else if (fname === 'openclaw.json') section = fieldMappings.value['openclaw']
  else if (fname === 'opencode.json') section = fieldMappings.value['opencode']
  else if (fname === 'config.toml') section = fieldMappings.value['codex']
  else if (fname === 'models.json') section = fieldMappings.value['pi']
  else if (fname === 'grok.json') section = fieldMappings.value['grok']  // ← 新增
  else if (fname.includes('hermes')) section = fieldMappings.value['hermes']
  else if (fname.includes('cc_settings')) section = fieldMappings.value['claude-code']
  else if (fname.includes('user_proxy')) section = fieldMappings.value['user_proxy_model']
  // ... 后续逻辑
}
```

### 各文件具体修改点

| 文件 | 修改点 |
|------|--------|
| `HarnessConfigs.vue` | 筛选下拉 + 新建下拉 + HARNESS_COLORS + typeLabel + computeMappingHints |
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
"grok": {
    "harness_dir":            "/home/ma-user/.grok",
    "harness_local_config":   "uploads/grok.json",
    "harness_sandbox_config": "/home/ma-user/.grok/grok.json",
    "upload_paths":           [
        {"src": "uploads/grok.json", "dest": "/home/ma-user/.grok/grok.json"}
    ],
    "workspace_base":         "/home/ma-user/.grok/workspace",
},
```

同时检查 hive.py 的 `_copy_agent_config` 方法中是否有针对新 harness 的特殊处理逻辑。

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
| openclaw | openclaw.json | #409eff (蓝) | OpenClaw |
| hermes | hermes_config.yaml | #e6a23c (橙) | Hermes |
| claude-code | cc_settings.json | #67c23a (绿) | Claude Code |
| openjiuwen | openjiuwen.json | #f56c6c (红) | Jiuwen Claw |
| opencode | opencode.json | #909399 (灰) | OpenCode |
| codex | config.toml | #8e44ad (紫) | Codex |
| pi | models.json | #17a2b8 (青) | Pi |
| grok | grok.json | #00d084 (青绿) | Grok |
| common | (无) | #c0c4cc (浅灰) | 通用 |
