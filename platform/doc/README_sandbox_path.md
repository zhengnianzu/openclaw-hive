# 沙箱输出路径查询 API（对外接口）

> 状态：**已实现**（2026-10-23）
> 目标：给外部调用方一个带 AK 鉴权的 HTTP 接口，按任务的首条 query 查到该任务的沙箱输出路径。

## 它解决什么问题

平台内部所有接口都在 `/api/*` 下、只认 JWT Bearer（`api/core/security.py` 的 `get_current_user`），
外部系统没法用。这一组接口挂在 `/openapi/v1` 下，用**固定 AK** 鉴权，不涉及用户身份，
也不查 `platform.db` —— 直接扫实例目录，所以库还没同步时也能用。

## 认证

请求头带 `X-API-Key`：

```
X-API-Key: <key>
```

key 配在 `platform/.env`：

```ini
# 逗号分隔可配多个
PUBLIC_API_KEYS=hive-local-dev-key
```

服务端逐个 key 用 `secrets.compare_digest` 常量时间比较（且**不短路**，避免按耗时猜前缀）。

| 情况 | 返回 |
|---|---|
| key 正确 | 正常响应 |
| 未带 key / key 错误 | `401` |
| 服务端 `PUBLIC_API_KEYS` 为空 | `503`（接口整体不可用，**不会**退化成无鉴权） |

> ⚠️ `.env` 里的默认值是开发用的，上线前务必换成真随机串。
> 改了 `.env` 需要重启服务（4 个 uvicorn worker 各自读配置）。

## 名词定义

| 词 | 含义 |
|---|---|
| **q1** | 任务配置文件里 `queries[0].text`，即该任务的首条 query 正文 |
| **任务配置文件** | `instances/<实例>/configs/**/*.json` 中含 `queries` 数组的那些（每个 2–3KB） |
| **输出路径** | OBS 上的轨迹目录，规则同 [hive.py:1106](../hive.py)：`<traj_save_bucket>/<traj_save_path>/<config 文件名去后缀>` |

`traj_save_bucket` / `traj_save_path` 取自实例的 `instances/<实例>/config.yaml`
（`run_config.obs.*`），与 [logs.py 的 `_get_obs_base_path`](../api/routers/logs.py) 同一来源。

## 接口一：按 q1 查输出路径

```
GET /openapi/v1/task-output-path?q1=<query 文本>&instance_id=<可选>
GET /openapi/v1/task-output-path?task=<任务名>&instance_id=<可选>
```

```bash
curl -H 'X-API-Key: hive-local-dev-key' \
  -G 'http://127.0.0.1:8087/openapi/v1/task-output-path' \
  --data-urlencode 'q1=中国的首都在哪里？' \
  --data-urlencode 'instance_id=2606161049-test-7607'
```

```json
{"task": "001", "output_path": "obs://rl-agentdata/openclaw_trajs/traj_test/001"}
```

`q1` 与 `task` 二选一（至少给一个，否则 400）。`task` 即 config 文件名去后缀，
用于**列表里的 q1 被截断后**仍能查回（见下节）。

**为什么需要 `instance_id`**：同一批语料常被多个实例各跑一遍，同一道 q1 会命中几十个任务。
不替你挑，而是返回候选让你收敛：

```json
// 409
{"detail": {"message": "q1 匹配到 28 个任务，请加 instance_id 参数收敛",
            "candidates": [{"task": "001", "instance_id": "2607031827-admin-709a"}, ...]}}
```

| 状态码 | 含义 |
|---|---|
| 200 | 命中唯一任务 |
| 400 | `q1` 和 `task` 都没给，或 `q1` 为空 |
| 404 | 未匹配到任务（或 `instance_id` 过滤后为空） |
| 409 | 匹配到多条，`detail.candidates` 里选一个带 `instance_id` 重试 |
| 502 | 实例 `config.yaml` 缺 `run_config.obs.*`，无法确定路径 |

## 接口二：列出所有 q1

```
GET /openapi/v1/list_key_tasks?instance_id=<可选>&q1_max_chars=<可选，默认 200>
```

```bash
curl -H 'X-API-Key: hive-local-dev-key' \
  'http://127.0.0.1:8087/openapi/v1/list_key_tasks'
```

```json
{
  "total": 56,
  "items": [
    {"task": "001", "instance_id": "2606161048-test-8352",
     "q1": "中国的首都在哪里？", "q1_truncated": false,
     "output_path": "obs://rl-agentdata/openclaw_trajs/traj_test/001"}
  ]
}
```

- 按 `(实例, 任务)` 逐条返回，**不合并**重复的 q1。
- `q1` 是**原文**（不做全角/半角归一化），方便直接对照 config.json。
- 某个实例的 `config.yaml` 有问题时，只有那一行 `output_path: null` 且带 `error`，其余照常返回。

### q1 截断（省带宽）

`q1` 默认**截断到 200 字**，超出部分用 `…` 结尾，同时 `q1_truncated: true`。

一条几百字的 query × 上千个任务，全量返回纯属浪费带宽。真实数据里最长的 q1 是 104 字，
但外部语料不可控，所以默认按 200 截。

- **只影响列表回显，不影响索引**：全文 q1 拿去 `/task-output-path` 照常匹配。
- 被截断的文本**不能**当 q1 用（末尾多了 `…`，必然 404）——改用同一条目里的
  `task` + `instance_id` 查详情。
- `?q1_max_chars=0` 关闭截断，返回全文；上限 10000。

## 两种调用方式

**两步走**（推荐，先看有什么再查）：

```bash
# 1. 列出候选
curl -s -H 'X-API-Key: KEY' 'http://127.0.0.1:8087/openapi/v1/list_key_tasks' \
  | jq -r '.items[] | [.instance_id, .task, .output_path] | @tsv'

# 2. 用挑中的组合查详情（q1 被截断时用 task，未截断用 q1 都行）
curl -s -H 'X-API-Key: KEY' -G 'http://127.0.0.1:8087/openapi/v1/task-output-path' \
  --data-urlencode 'task=001' \
  --data-urlencode 'instance_id=2606161049-test-7607' | jq -r .output_path
```

**只按 q1 查**：知道完整 `q1` 且确信唯一时直接调；命中多条会拿到 409 和候选列表。

> 别把列表返回的 `q1` 原样回传给详情接口——它可能带 `…`。看 `q1_truncated` 决定用 `task` 还是 `q1`。

## 实现要点

代码在 [api/routers/public_api.py](../api/routers/public_api.py)，路由在 [main.py](../main.py) 里
`app.include_router(public_api.router)`。

**索引怎么建的**：`instances/*/configs/**/*.json` 共约 1.5 万个文件、780MB，但真正的任务配置
只有几十个、每个 2–3KB，其余都是 eval 中间产物（transcript / result / raw request）。
所以只读前 64KB 查 `"queries"` 标记做预筛，命中才整读 —— 整读量从 780MB 降到几十 MB，
实测 12.5s → 约 2s。索引 `{归一化首条 query → [(实例, 路径, 原文)]}`，
配 60s TTL + 各实例 `configs` 目录 mtime 签名缓存，重建时一并清空路径前缀缓存。

**⚠️ 符号链接**：`instances/<id>/configs` 常是指向 `platform/downloads/configs/...` 的符号链接。
求实例根目录时**不能用 `Path.resolve()`**，解析后路径会跑到 `instances/` 外面。
目前用字面路径切分（`_instance_dir_of`），已有测试覆盖。

**q1 比较**：NFKC 归一化 + 统一换行 + 合并空白后比全等，所以首尾空白、全角半角、
`\r\n` / `\n` 这类差异不影响匹配。**不做模糊/子串匹配**（如需可改成扫描，接口不变）。

**q1 截断**：索引里存的始终是原文，`Q1_MAX_CHARS`（默认 200）只在列表序列化时生效，
所以截断不会污染匹配。这也是为什么列表截断后必须靠 `task` 兜底，而不是靠"截断前缀去匹配"。

## 测试

```bash
cd platform && python -m unittest discover -s test -p 'test_public_api.py' -v
```

覆盖：鉴权三种拒绝路径、404 / 409 / 按实例收敛、只认 `queries[0]`、列表接口的
符号链接回溯与坏实例降级、归一化、q1 截断（含截断后仍可用 task 查回）。

## 已知限制

- 索引是**每 worker 一份**的进程内缓存（`run.sh` 用 4 个 worker），各自 60s 内可能新旧不一致。
- 只认 `queries[0]`，第二条（评审/verify query）不作为 q1。
- 只扫 `settings.INSTANCE_DIR` 下的实例；实例目录被移走后该行从列表消失。
- 按 `task` 查是全索引线性扫（几十到几千条，够用），未建反向索引以免多一份要失效的缓存。
