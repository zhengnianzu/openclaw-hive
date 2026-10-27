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

## 七、harness md 管理(模板 → 方案 → 版本 → 实例)

md 是给 agent 用的系统提示文件(`AGENTS.md` / `AGENT.md` / `SOUL.md` / `IDENTITY.md` /
`HEARTBEAT.md` / `USER.md` 等),它们和 agent 配置文件一样**进版本快照**,
但来源、白名单和注入路径都是独立的一套。

### 7.1 目录约定:`settings/harness_mds/<harness_type>/`

- 目录名默认**与 harness id 完全一致**;只有「目录名 ≠ id」时才需要在
  `platform/api/routers/harness_meta.py` 的 `_MD_TEMPLATE_ALIASES` 里登记例外
  (目前为空——`jiuwenswarm-dev` / `jiuwenswarm-xiaoyi` 各有独立目录,不要指向旧目录,
  否则两个 harness 会共享同一批模板,前端改一个等于改两个)。
- 只扫**顶层 `*.md`**,不递归。`jiuwenswarm/memory/MEMORY.md` 这类子目录模板
  不会被识别(历史上递归扫描曾把整个 settings/ 卷进去)。
- 模板文件名即白名单:`_assert_md_name` 用它做路径穿越防护,
  任何不在该目录顶层列表里的文件名一律 404。

新增 harness 时这一目录**必须建**,否则该 harness 的可选 md 列表为空:
`md_templates()` 找不到目录直接返回 `[]`,工作台里就一个 md 都看不到。

### 7.2 校验清单

| 检查项 | 命令 / 位置 | 期望 |
|--------|-------------|------|
| 目录存在 | `ls platform/settings/harness_mds/<id>/` | 至少一个 `.md` |
| 别名未错配 | `harness_meta._MD_TEMPLATE_ALIASES` | 不含该 id(除非目录名确实不同) |
| 枚举正确 | 见下方脚本 | 输出与目录内容一致 |

```bash
cd platform && python3 -c "
from api.routers import harness_meta as hm
for t in ['jiuwenswarm-dev','jiuwenswarm-xiaoyi']:
    print(t, hm.md_templates(t))
"
```

### 7.3 md 的四层流转

```
settings/harness_mds/<type>/*.md        模板(提交进 git)
        │  工作台「从模板初始化」
        ▼
settings/harness/<type>/harness-N/draft/md/*.md     方案草稿
        │  发布版本
        ▼
settings/harness/<type>/harness-N/<version>/md/*.md 版本快照(只读)
        │  创建实例
        ▼
instances/<id>/workspace/*.md           → config.yaml 的 run_config.harness_md
        │  hive 上传到沙箱
        ▼
workspace/<code_stem>/<agent_dir>/__default__/*.md
```

关键点:

1. **草稿可写、版本只读**:`PUT /{config_id}/md/{name}` 作用于草稿;
   版本里的 md 只能查看(`GET .../versions/{vid}/md/{name}`)。
2. **未初始化 ≠ 不存在**:`_md_file_list()` 会把模板里有的文件全部列出,
   并带 `exists` 标志——前端据此显示「未初始化」而不是隐藏。
3. **保存 md 会同步 config.yaml**:`_sync_harness_md()` 用**文本行插入**
   重写 `run_config.harness_md`(绝对路径列表),不用 YAML round-trip,
   避免用户手写的缩进/注释/引号被整形。md 为空时写 `harness_md: []`。
4. **实例侧拷到 `workspace/`**:`create_instance` 把版本目录 `md/` 下所有 `.md`
   拷进 `instances/<id>/workspace/`,并把实例内绝对路径写进 `harness_md`。
   没有 md 的旧版本写空列表,不报错。
5. **hive 侧兜底上传**:`_upload_harness_md()` 在 `_download_s3_agents` **之前**
   把 md 传到 `workspace/<code_stem>/<agent_dir>/__default__/`;
   OBS 上同名文件会覆盖它。task config 没有 `input_dir.agent_dir` 时整步跳过并告警。

### 7.4 常见坑

| 现象 | 原因 |
|------|------|
| 工作台看不到任何 md | `harness_mds/<id>/` 目录不存在,或 `_MD_TEMPLATE_ALIASES` 指到了别的目录 |
| 两个 harness 的 md 互相影响 | 两者在 `_MD_TEMPLATE_ALIASES` 里被指向同一目录 |
| 保存 md 后 config.yaml 格式被打乱 | 有人把 `_sync_harness_md` 改成了 YAML 库 round-trip |
| 沙箱里没有 md | task config 缺 `input_dir.agent_dir`,或 OBS agents 下载覆盖 |

---



## 附:现有 harness 配置对照表

| id | name | agent_config | ui_color | traj_prefix |
|----|------|--------------|----------|-------------|
| openclaw | OpenClaw | openclaw.json | #409eff | openclaw_trajs |
| openclaw-tui | OpenClaw TUI | openclaw_tui.json | #2563eb | openclaw-tui_trajs |
| hermes | Hermes | hermes_config.yaml | #e6a23c | hermes_trajs |
| claude-code | Claude Code | cc_settings.json | #67c23a | cc_trajs |
| openjiuwen | Jiuwen Claw | openjiuwen.json | #f56c6c | openjiuwen_trajs |
| opencode | OpenCode | opencode.json | #909399 | opencode_trajs |
| codex | Codex | config.toml | #8e44ad | codex_trajs |
| pi | Pi | models.json | #17a2b8 | pi_trajs |
| grok | Grok | grok_config.toml | #00d084 | grok_trajs |
| dsh | DeepSeek Harness | dsh_settings.yaml | #ff8c00 | dsh_trajs |
| jiuwenswarm-dev | JiuwenSwarm-dev | jw_dev_config.yaml | #e91e63 | jiuwenswarm-dev_trajs |
| jiuwenswarm-xiaoyi | JiuwenSwarm-xiaoyi | jw_xiaoyi_config.yaml | #9c27b0 | jiuwenswarm-xiaoyi_trajs |
| jiuwenswarm | JiuwenSwarm（旧标识） | jw_config.yaml | #e91e63 | jiuwenswarm_trajs |
| common | 通用 | (无) | #c0c4cc | (兜底 openclaw_trajs) |
