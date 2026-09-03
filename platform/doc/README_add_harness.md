# Platform 新增 Harness 接入指南

本指南描述在 openclaw-hive platform 中新增一个 harness 类型需要执行的所有操作。

> 重构后(配置驱动化),新增 harness 只需改 **1 个 JSON + 1 处 Python 分支 + 1 个 settings 模板**,前后端下拉/颜色/标签/文件名/路径映射全部自动派生。

## 修改清单总览

| 序号 | 位置 | 改动 | 必须 |
|------|------|------|------|
| 1 | `platform/settings/harness_config.json` | 加一条 harness 条目 | ✅ |
| 2 | `prepare_config.py` | `_FRAMEWORK_LAYOUTS` 加沙箱路径布局 | ✅ |
| 3 | `platform/settings/<agent_config 文件名>` | 新建模板文件 + `.example` | ✅ |
| 4 | `platform/settings/field_mappings.json` | 新增字段映射块 | ✅ |
| 5 | `platform/api/routers/instances.py` `create_instance` | 新增 harness 配置文件生成分支 | 仅当配置结构非通用时 |
| 6 | `prepare_config.py` `prepare_local_agent_config` | 新增 framework 分支 | 仅当需要本地 mutate 时 |

---

## 一、harness_config.json(核心,唯一数据源)

文件位置:`platform/settings/harness_config.json`

在 `_meta` 之后追加一条 harness 条目,字段含义:

```json
"myharness": {
    "id": "myharness",
    "name": "MyHarness",
    "hive_config": "config.yaml",
    "user_config": "user_proxy_model.json",
    "agent_config": "myharness.json",
    "ui_color": "#FF5733"
}
```

| 字段 | 说明 | 示例 |
|------|------|------|
| `id` | harness 类型标识(用于 config.yaml 的 `run_config.harness_type`) | `"myharness"` |
| `name` | 前端显示名(下拉选项文本 + el-tag 文字) | `"MyHarness"` |
| `hive_config` | 共用的 hive 主配置文件名(所有 harness 都是 `config.yaml`) | `"config.yaml"` |
| `user_config` | 共用的 user_proxy_model 文件名 | `"user_proxy_model.json"` |
| `agent_config` | 该 harness 自己的配置文件名(沙箱内 harness_local_config_file) | `"myharness.json"` |
| `ui_color` | 前端 el-tag 背景色(hex) | `"#FF5733"` |

---

## 二、prepare_config.py(沙箱路径布局)

文件位置:`/root/zengxiang/openclaw-hive/prepare_config.py`

在 `_FRAMEWORK_LAYOUTS` 字典追加:

```python
"myharness": {
    "harness_dir":            "/home/ma-user/.myharness",
    "harness_local_config":   "uploads/myharness.json",
    "harness_sandbox_config": "/home/ma-user/.myharness/myharness.json",
    "upload_paths": [
       "/home/ma-user/.myharness/sessions",
    ],
    "workspace_base": "/home/ma-user/.myharness/workspace",
    "skill_subdir": ".agents/skills",
},
```

各字段含义:
- `harness_dir`:沙箱内 harness 主目录
- `harness_local_config`:本地(平台侧)配置文件相对路径(`uploads/<文件名>`)
- `harness_sandbox_config`:沙箱内配置文件绝对路径
- `upload_paths`:任务完成后需上传到 OBS 的沙箱内路径列表
- `workspace_base`:沙箱内 workspace 根目录(None 表示隔离在 profiles 里)
- `skill_subdir`:workspace 下 skills 子目录名

---

## 三、settings 模板文件

在 `platform/settings/` 下新建该 harness 的 agent_config 模板文件,敏感字段用占位符。

以 `myharness.json` 为例:

```json
{
  "model": "myharness-v2",
  "base_url": "https://api.myharness.com/v1",
  "api_key": "your-api-key-here"
}
```

同时创建 `.example` 后缀副本:
- `settings/myharness.json`
- `settings/myharness.json.example`

---

## 四、field_mappings.json(前端字段映射提示)

文件位置:`platform/settings/field_mappings.json`

新增一个 key,key 名 = harness 的 id,描述前端表单字段到配置文件 JSON 路径的映射:

```json
"myharness": {
  "model_id": "model",
  "model_base_url": "base_url",
  "model_api_key": "api_key"
}
```

该映射用于 HarnessConfigs 页面的"字段映射提示"功能,帮助用户理解创建实例时哪些配置项会被覆盖。

---

## 五、instances.py 配置文件生成分支

文件位置:`platform/api/routers/instances.py` 的 `create_instance` 函数,"--- 2. 生成 harness 配置文件 ---"区域。

顶层 "model/base_url/api_key 注入",或格式特殊(如 TOML/YAML),则需新增 `elif req.harness_type == "myharness"` 分支。


以一个假设的简单 JSON 结构为例:

```python
elif req.harness_type == "myharness":
    myharness_template = os.path.join(harness_settings_dir, "myharness.json")
    if not os.path.exists(myharness_template):
        myharness_template = os.path.join(settings.SETTINGS_DIR, "myharness.json")
    with open(myharness_template, "r", encoding="utf-8") as f:
        myharness_cfg = json.load(f)

    if req.model_id:
        myharness_cfg["model"] = req.model_id
    if req.model_base_url:
        myharness_cfg["base_url"] = req.model_base_url
    if req.model_api_key:
        myharness_cfg["api_key"] = req.model_api_key

    with open(myharness_path, "w", encoding="utf-8") as f:
        json.dump(myharness_cfg, f, indent=2, ensure_ascii=False)
```

> 注:上方代码中的 `myharness_path` 局部变量对应步骤一中的 `agent_config` 文件名,需在 `create_instance` 函数顶部的路径变量定义区加一行:
> `myharness_path = os.path.join(instance_dir, "myharness.json")`
> 这些路径变量保留是因为各 harness 配置生成分支按原变量名引用。

如有"自动申请 API Key 回写"需求,在 "--- 3. 自动申请 API Key ---" 区域加对应回写分支。

---

## 六、prepare_config.py 配置 mutate 分支

文件位置:`prepare_config.py` 的 `prepare_local_agent_config` 函数。

**何时需要**:如果新 harness 在 spawn worker 前需要修改本地配置文件(如注入 provider/model),则新增 `elif framework == "myharness"` 分支。

**何时不需要**:如果该 harness 不需要本地 mutate(直接用 settings 模板),则跳过。

参考现有 `dsh` / `codex` / `pi` 等分支的实现模式。

---


---

## 附:现有 harness 配置对照表

| id | name | agent_config | ui_color | traj_prefix |
|----|------|--------------|----------|-------------|
| openclaw | OpenClaw | openclaw.json | #409eff | openclaw_trajs |
| hermes | Hermes | hermes_config.yaml | #e6a23c | hermes_trajs |
| claude-code | Claude Code | cc_settings.json | #67c23a | cc_trajs |
| openjiuwen | Jiuwen Claw | openjiuwen.json | #f56c6c | openjiuwen_trajs |
| opencode | OpenCode | opencode.json | #909399 | opencode_trajs |
| codex | Codex | config.toml | #8e44ad | codex_trajs |
| pi | Pi | models.json | #17a2b8 | pi_trajs |
| grok | Grok | grok_config.toml | #00d084 | grok_trajs |
| dsh | DSH | dsh_settings.yaml | #0A3D91 | dsh_trajs |
| common | 通用 | (无) | #c0c4cc | (兜底 openclaw_trajs) |
