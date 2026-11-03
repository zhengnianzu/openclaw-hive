# 「输出」页 · 模型配置 / 用户模拟配置信息面板（含代理映射 + Excel 导出）

> 目标页面：实例详情页 Tab「输出」，卡片「会话漏斗」下方新增一个信息面板。
> 展示当前实例实际使用的 **主Agent / 用户模拟 / Evaluator** 三组模型连接信息（baseurl、key 后四位、代理名称、运行时间），
> 并支持导出 Excel。数据来源为 **任务创建时录入的表单信息**（`task_instances.create_params`），不读实例目录下任何配置文件。

## 一、背景与目标

同一个平台会跑多个代理（`jumper*` / `proxy-004` 等），每个实例的模型请求打到不同代理 IP。
排查问题时需要一眼看出：**这个任务的三个角色（主Agent / 用户模拟 / Evaluator）分别连到哪个代理、用的哪把 key、跑在什么时间段**。

| 项 | 现在 | 目标 |
|---|---|---|
| 模型连接信息 | 散落在实例目录 `openclaw.json` / `user_proxy_model.json`，需逐个打开文件 | 「输出」页直接一个面板看到三组 baseurl / key 后4位 / 代理名 / 运行时间 |
| 代理识别 | 需人工对照 IP 记忆 | 自动把 baseurl 里的 IP 映射成代理名（导出服务 `/export/proxy_list` 实时取，`proxy_name.json` 兜底），无映射则留空 |
| 导出 | 无 | 一键导出 Excel（`.xlsx`） |
| 导出方式下拉 | 前后端各一份硬编码（易与远端不一致） | 取导出服务 `/export/mode_list` 实时列表，远端新增模式自动出现 |

> `settings/proxy_name.json` 自此**只作降级兜底**：正常路径走远端实时接口，不必再手工维护。

## 二、数据来源：任务创建表单信息（`task_instances.create_params`）

**明确不读实例目录文件。** 面板数据完全取自 `task_instances.create_params`（创建实例时前端提交、后端落库的 JSON 快照）。
理由：
- `create_params` 就是你录入的那些字段（`model_base_url` / `agents[]` 等），与「任务创建的表格信息」一一对应；
- 实例目录下的 `openclaw.json` / `user_proxy_model.json` 是运行产物，部分实例目录不存在（completed 实例可能被清理），而 `create_params` 随实例记录常驻 platform.db。

### 三组角色的字段映射

| 角色 | create_params 字段 | baseurl | key（取后四位） | model |
|---|---|---|---|---|
| 主Agent | 顶层 | `model_base_url` | `model_api_key` | `model_id` |
| 用户模拟 | `agents[]` 中 `name != 'evaluator'` 的项 | `base_url` | `api_key` | `model` |
| Evaluator | `agents[]` 中 `name == 'evaluator'` 的项 | `base_url` | `api_key` | `model` |

说明：
- `agents[]` 数组长度不固定（创建表单可加多组模拟用户）。**用户模拟可能多行**，全量列出；Evaluator 通常一行。
- 主Agent 恒一行（顶层字段）。
- key 只展示 **后四位**（`key[-4:]`），不泄露完整 key。

### 运行时间（实例级）

`task_instances` 的 `started_at` / `stopped_at`。三个角色共用同一份：
- 运行中实例：机 `started_at`，结束时间留空（未结束）；
- 已结束实例：`started_at ~ stopped_at`。

> user 确认：运行时间取 **实例级**，不从各角色会话/日志单独解析。

## 三、代理映射：导出服务实时接口（本地文件兜底）

代理名（IP → 名称）的**权威源是导出服务**，不再是手写的本地文件：

```bash
curl -s "$EXPORT_BASE/export/proxy_list?access-key=$AK" | python3 -m json.tool
# {"proxies":[{"source":"jumper-002","root_id":"52e24556",
#              "root_path":"/mnt/sfs_turbo/.../jumper-002/...","ips":["115.33.106.42"]}, ...]}
```

`source` 即代理名，`ips[]` 逐个展开成 `{IP: source}` 映射。实现见
`api/services/export_meta.py`（取数 + 60s 缓存）与 `api/services/proxy.py:load_proxy_map_async()`。

匹配逻辑：
1. 从角色 `baseurl` 用正则提取 IP（`_IP_RE`，非 IP 时退回 host 段）；
2. 用 IP 查映射，命中 → 代理名称；未命中 / 无 IP → **代理名称留空**；
3. **降级**：远端不可达或未配 `EXPORT_BASE` 时，回退本地 `settings/proxy_name.json`
   （同样是 IP → 名称的 JSON 字典）；该文件也缺失/损坏则静默降级为空映射，
   不报错、不影响面板其他列。

> 两边命名略有差异：远端是 `jumper-002` / `proxy-004-latest`，本地兜底文件里是
> `jumper002` / `proxy-004`。降级时面板上的名字会跟着变，属预期行为。

缓存：成功 60s、失败 30s（负缓存）。`/api/stats` 每 30s 被每个打开看板的客户端拉一次，
远端故障时若不做负缓存，每次请求都要吃一个 5s 超时。

## 四、后端接口

新增到 `api/routers/batch_output.py`（与 `/shallow`、`/deep` 同属「输出」页端点，复用 `_get_instance`）。

### `GET /api/instances/{instance_id}/proxy-config`

返回加工好的面板数据（key 已截后四位、代理名已映射、运行时间已拼）：

```json
{
  "instance_id": "...",
  "name": "...",
  "status": "stopped",
  "started_at": "2026-08-25T09:43:53",
  "stopped_at": "2026-08-26T15:12:05",
  "rows": [
    { "role": "主Agent",   "baseurl": "http://115.120.113.66:8082/v1", "key_suffix": "hZc5", "proxy_name": "jumper003", "model": "tokenfly-01/deepseek-v4-pro-0813" },
    { "role": "用户模拟",  "baseurl": "http://115.120.113.66:8082/v1", "key_suffix": "JBYQ", "proxy_name": "jumper003", "model": "tokenfly-01/gemini-3.5-flash" },
    { "role": "Evaluator", "baseurl": "http://115.120.113.66:8082",    "key_suffix": "mRcX", "proxy_name": "jumper003", "model": "tkfly_glm5.2" }
  ]
}
```

- `create_params` 缺失 / JSON 解析失败 → 返回 `rows: []`（前端空态提示），不 500。
- `instance_id` 不存在 → 404（复用 `_get_instance`）。

### `GET /api/instances/{instance_id}/proxy-config/export`

用后端 `openpyxl`（已装 3.1.5）生成真正的 `.xlsx`，`FileResponse` 返回下载：

- Sheet「模型配置」：表头 `名称 | baseurl | key 后四位 | 代理名称 | 启动时间 | 结束时间 | 模型`，逐行写入 `rows`；
- 文件名：`<实例name>_proxy_config.xlsx`（`content-disposition` 附 `attachment`）。

> 选后端 openpyxl 而非前端引 xlsx 库：零新依赖、不破坏现有 vite 构建、后端直接产 `.xlsx`。
> 与 `logs.py` 已有 `FileResponse` 模式一致。

### `GET /api/export-meta/modes`

「导出方式」下拉的选项来源（`api/routers/export_meta.py`），转发导出服务
`GET /export/mode_list`，裁剪成前端要的字段：

```json
{
  "modes": [
    { "value": "export",   "label": "导出",     "desc": "导出原始文件（三元组，不分析）" },
    { "value": "reformat", "label": "合并导出", "desc": "合并导出（三元组合并成单 JSON，不质检）" }
  ],
  "default": "export",
  "source": "remote"
}
```

- `label` 取远端 `mode_label`（中文简称），`desc` 取远端 `label`（括号长说明）；
- `source`：`remote` = 取到远端实时列表；`fallback` = 远端不可达，用的是
  `batch_output._FALLBACK_EXPORT_MODES` 本地兜底白名单，前端会提示「可能不是最新」；
- 只返回 `public=True` 的模式（缺省视为 `True`，兼容远端老版本无该字段）。

**新增导出模式不需要改平台代码**：远端 `/export/mode_list` 加一条，前后端自动跟上。
兜底白名单只在远端挂掉时生效，可按需补齐。

`POST /proxy-config/export` 的 mode 校验同样以远端列表为准（远端不可达时退回兜底白名单），
避免「远端挂了连导出都提不了」。

### `GET /api/export-meta/proxies`

代理（src 来源）列表：`{proxies: [{source, root_id, ips}], source}`。
`root_path` 不外露（导出服务那边的内部挂载路径，前端用不到）。

### 路由注册顺序

`/proxy-config` 与 `/proxy-config/export` 均为静态段，无 `{traj_name}` 类参数路由冲突，直接注册即可。

导出元数据端点**必须单独开 router**（前缀 `/api/export-meta`），不能挂在 `/api/instances` 下：
`instances.py` 有 `GET /{instance_id}` 通配路由，会把 `/api/instances/export-meta` 当成实例 ID 吃掉。

## 五、前端（`frontend/src/views/InstanceDetail.vue`）

### 1. 信息面板插入位置

「会话漏斗」卡片（约 L262 结束）之后、「状态分析」卡片（约 L264）之前，新增一个 `el-card`：

- header：`模型配置 / 用户模拟配置`（浅色小字注释「任务创建时的连接信息」）+ 右侧「导出 Excel」按钮；
- 主体：`el-table`（`data = proxyRows`），列：
  | 列 | 字段 |
  |---|---|
  | 名称 | `role` |
  | baseurl | `baseurl`（超长省略 + tooltip）|
  | key 后四位 | `key_suffix` |
  | 代理名称 | `proxy_name`（无色/命中时突出显示，如加 tag）|
  | 启动时间 | `started_at` |
  | 结束时间 | `stopped_at`（未结束显示「—」）|
  | 模型 | `model` |
- 空态：`rows` 为空时 `el-empty`「暂无模型配置信息」。

### 2. 逻辑

- `proxyRows` / `proxyStartedAt` / `proxyStoppedAt` / `proxyLoading` ref；
- `loadProxyConfig()` —— `GET /instances/{id}/proxy-config`，写入 rows + 起止时间；
- `exportProxyConfig()` —— 前端发起 `GET /instances/{id}/proxy-config/export` 下载（用 `window.open` / 临时 `<a>` 挂 token header 下载，或 `axios` blob 后触发下载）；
- 触发：切到「输出」tab 时（`watch(activeTab)` 的 `outputs` 分支）调用 `loadProxyConfig()`；不常驻轮询（连接信息基本不变，与漏斗的 10s 轮询不同）。

### 3. 与代理「联动」

代理名称列命中 `proxy_name.json` 时以高亮标签展示，便于一眼区分当前实例跑在哪个代理。
「不存在就映射为空」：未命中映射的 IP，代理名称列显示空，不报错。

## 六、改动清单

| 文件 | 改动 |
|---|---|
| **新增** `platform/settings/proxy_name.json` | IP→代理名映射（5 条初始映射），无则映射为空 |
| `api/routers/batch_output.py` | 新增 `GET /{id}/proxy-config` + `GET /{id}/proxy-config/export`；`_proxy_rows()` 处理 create_params 解析 / IP 提取 / key 截尾 / proxy 映射 / 起止时间 |
| `frontend/src/views/InstanceDetail.vue` | 会话漏斗下插入信息面板（el-card + el-table + 导出按钮）；`loadProxyConfig()` / `exportProxyConfig()`；tab 切换加载 |
| （可选）`api/core/config.py` | 无需改：`SETTINGS_DIR` 已指向 `<platform>/settings` |

> 服务改动需用户自行重启生效（平台惯例，Claude 不代劳重启）。

## 七、验证要点

1. 目标实例 `2608250943-wanyi2608250941-7cf6`（`openclaw_生活学习_0822_..._0942`，stopped）应显示三行：主Agent / 用户模拟 / Evaluator，三者 baseurl 均为 `115.120.113.66` → 代理 `jumper003`；
2. key 列只显示后四位（如 `hZc5` / `JBYQ` / `mRcX`），无完整 key 泄漏；
3. 启动/结束时间 = 实例 `started_at`（2026-08-25 09:43:53）~ `stopped_at`（2026-08-26 15:12:05）；
4. 导出 `.xlsx` 可正常打开、中文表头无乱码；
5. 变更映射 / 删除 `proxy_name.json` 各测一次：命中→显示代理名；未命中/文件缺失→代理名称留空，面板不报错。
