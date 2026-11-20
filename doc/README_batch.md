# 批量实验（Batch Experiment）实现说明

> 面向维护者。用户向说明见文档中心 `平台使用 / 批量实验使用说明`
> （`platform/wiki/md/batch-usage.md`）。
>
> 整理日期：2026-11-20

---

## 1. 它是什么

一次批量实验 = **同一份测试集** × **N 个 Harness** × **M 个模型** × 重复次数。

执行引擎全部复用现有的「一个 `task_instances` 跑完整个测试集」，批量实验只做三件事：

1. 按矩阵批量建子任务（直接调 `instances.create_instance`，不复制其逻辑）
2. 把它们放进同一条队列，交给 `queue_scheduler`
3. 聚合对比（数据本来就已落库，不新增采集）

子任务与手工创建的任务**完全无差别**：同样的详情页、日志、轨迹、产物、导出。

---

## 2. 矩阵模型：两条正交轴

```text
Harness 列表 × 模型列表 × 重复次数 × 用户模拟轴（可选）
        ↑ 每条轴都独立，做纯笛卡尔积
```

| 轴 | 请求字段 | 模型 | 说明 |
|---|---|---|---|
| Harness | `harnesses: List[BatchHarness]` | `api/models/batch_experiment.py` | 类型 / 方案 / 已发布版本 / 镜像 / 沙箱 |
| 模型 | `models: List[BatchModelParams]` | 同上 | Base URL / Key / API 类型 / model_id |
| 用户模拟 | `sim_arms: List[BatchSimArm]` | 同上 | 为空则该轴退化成长度 1，吃模板自带的 `user_proxy_model.json` |

### 2.1 为什么模型不挂在 Harness 下

早期的形状是「Harness 块 × **块内**模型」（`blocks[].models`）。模型参数里唯一与 Harness 强相关的是 `model_api_type` 与 base_url 的 `/v1` 写法，但这两个都在 `instances.create_instance` 里按 Harness 各自解析：

- `claude-code`：固定 anthropic，`ANTHROPIC_BASE_URL` **剥掉** `/v1`
- `openclaw` / `openclaw-tui`：`openclaw.json` 的 `baseUrl` **补上** `/v1`
- `opencode` / `pi` / `codex` / `grok` / `hermes`：各写各的格式

也就是说「传什么进去，各 Harness 自己会解析」，模型并不需要绑定某一个 Harness。旧形状带来的实际问题是：同一个模型想跑两个 Harness 必须重复配两遍；块内 0 模型时会静默退化成 `common.model_*` 兜底（而兜底无 UI 入口），最终写进模板占位模型（如 `claude-opus-4-7-thinking` / `token=keys`），跑出不可用的数据。

### 2.2 展开算法

`api/routers/batch_experiments.py::_expand_arms`：

```text
1. harnesses 非空 且 models 非空
   → 对每个 harness × 每个 model 生成一个臂
   → label = "{harness_label} · {model_id}"
2. 否则 blocks 非空（旧形状）
   → 逐块展开块内 models；块内为空则该块退化成 1 臂（吃 common 兜底）
3. 否则直接用 arms（最老客户端）
```

关键约束：

- **`model_base_url` 原样透传**，不做任何 `/v1` 规整，避免两套 Harness 规则在这里打架。
- Harness 级参数（`image_name` / `sandbox_config_id`）来自 Harness 轴，模型级参数来自模型轴，互不串。
- 新实验的 `params_json` 同时保存 `harnesses`、`models`、摊平后的 `arms`；旧形状才有 `blocks`。

---

## 3. 数据模型

| 表 | 作用 |
|---|---|
| `batch_experiments` | 实验头：`registration_id` + 创建时的测试集快照（`test_set_path` / `sample_total`）+ `queue_id` + `params_json` + 展示序号 `seq` |
| `batch_experiment_items` | 一个子任务一行：`instance_id` / `harness_type` / 三件套 ID / `arm_label` / `sim_label` / `sim_idx` / `repeat_idx` / `create_error` |
| `batch_experiment_seq` | 展示序号发号 |
| `task_queues` / `task_instances` | 复用现有执行引擎，未新增字段 |

`params_json` 是**唯一**保存矩阵原始结构的地方（脱敏后），详情页与复制页都从这里还原。

---

## 4. 创建流程

`POST /api/batch-experiments`（`create_experiment`）：

1. 校验：队列并发 ≥1；Harness 至少一个；**`models` 为空直接 400**；重复次数 ≥1
2. 读 `task_registrations` → `test_set_path`（去桶前缀）、`sample_total = data_total`
   - `task_path_obs` 为空 → 400（这条路无法作为测试集）
3. `_expand_arms()` 展开成臂；总任务数超过 `_MAX_TOTAL_TASKS = 100000` → 400
4. 建实验行 + 一条专属队列 `batch-<实验ID>`（小写）
5. 按 `arm × sim_arm × repeat` 三重循环**顺序 await** 调 `create_instance`
   - **刻意不并发**：`create_instance` 多次写库，`platform.db` 很大，WAL checkpoint 期间独占写锁，并发建会成片撞 `database is locked`
   - 单个子任务失败不整体回滚，异常写进 `items.create_error`，返回 `created/failed` 计数

子任务命名：

- 展示名：`{实验名}-配置{N}`（N 为展开后的全局序号）
- `task_name`：`{batch_id}-c{N}`（小写，用于 Pod 命名与 OBS 路径）
- 轨迹：登记表有 `traj_save_path` 时拼 `{前缀}/traj_{task_name}`，否则由后端按 `{harness}_trajs/traj_{task_name}` 生成

前端提交时同时发 `harnesses` + `models` + 摊平后的 `arms`（兼容字段）。

### 4.1 任务总数（`common.total_num`）

每个子任务的取数条数：

```python
req_total = int(req.common.total_num or 0)
limit_num = req_total if req_total > 0 else sample_total   # sample_total = 登记的 data_total
```

- `0` → 沿用登记的 `data_total`（批量实验的既有口径）
- `>0` → 截断到前 N 条，配合 `start_index` 实现「从第几条开始取几条」
- **不做「0 = 不限制」**：`create_instance` 里 `total_num=0` 会让 `_prepare_instance` 按实际文件数统计总任务数，进度分母随之漂移，与实验页展示的固定分母对不上。单任务页的 0 语义是「不限制」，批量里刻意不沿用。
- 前端 `effectiveSampleTotal` 用同一规则算「总执行实例数」预览，避免页面数字与落库值不一致。

---

## 5. 调度与并发

- 整批共用一条队列，`queue_id = "batch-" + batch_id.lower()`。
- 吞吐 = 两级并发相乘：

```text
同时运行的子任务 = min(QUEUE_MAX_CONCURRENT(全局, 默认10), 队列 max_concurrent)
同时运行的沙箱   = 上面的结果 × common.concurrent_num
```

- 长尾跳过：`剩余任务数 < max(1, concurrent_num) * tail_skip_percent/100` 时额外放行一个（`queue_scheduler.slot_condition_sql`）。
- 启动入口：`POST /api/queues/{queue_id}/start-all` 把 `created/stopped` 的子任务置 `queued` 并触发调度；它会顺手清掉队列的暂停位。
- 暂停只冻结放行，不打断已在跑的；恢复时清暂停位并主动调一次 `schedule_queue`。

---

## 6. 脱敏与 Key 还原

`params_json` 入库前过 `_masked_params()`，掩码规则为 `前4 + "****" + 后4`。**五处** key 必须覆盖，漏一处就是明文落库：

```text
common.model_api_key / common.agents[].api_key
arms[].model_api_key
models[].model_api_key            ← 模型轴
blocks[].models[].model_api_key   ← 旧形状（历史上漏过，已修）
sim_arms[].agents[].api_key
```

复制实验需要**可用的** key，两条路径都走 `_restore_params_secrets(batch_id, params)`：

- 明文来源：`task_instances.create_params` 一直存着原文（只有 `/create-params` 接口层做掩码），按子任务取回即可，不需要放宽入库脱敏
- 匹配键：
  - 模型轴 / 块内模型 → `(model_id, 归一化 base_url)`（与 Harness 无关，同一个模型可从任意子任务取回）
  - 臂 → `(harness_type, config_id, version_id, model_id, 归一化 base_url)`，撞不上时退回 `(三件套, model_id)`
  - agent → `name`
- **还原不到时把掩码清成空**，绝不保留 `sk-l****nDEj`：留着会让新任务带假 key 静默失败，比留空（回落模板默认值）更糟
- 对外接口 `GET /{batch_id}/params/full`（`require_operator`）返回明文；`GET /{batch_id}` 仍只返回掩码

前端复制：`BatchCreate.vue::prefillFromCopy` 同时拉 `GET /{id}` 与 `GET /{id}/params/full`，模型 / Agent 的 key 一并回填，输入框走掩码只读分支。

---

## 7. 接口一览

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/batch-experiments` | 登录 | 列表；状态在 Python 层推导后过滤 |
| GET | `/api/batch-experiments/{id}` | 登录 | 详情（`params_json` 为掩码版本） |
| GET | `/api/batch-experiments/{id}/params/full` | operator | 复制预填用，含明文 key |
| GET | `/api/batch-experiments/{id}/items` | 登录 | 各臂进度 / 任务列表 |
| GET | `/api/batch-experiments/{id}/compare` | 登录 | 结果对比（按臂聚合） |
| GET | `/api/batch-experiments/{id}/matrix` | 登录 | 逐样本横向矩阵（分页） |
| POST | `/api/batch-experiments` | operator | 创建（含批量建子任务） |
| POST | `/api/batch-experiments/{id}/pause` | operator | 暂停调度 |
| POST | `/api/batch-experiments/{id}/resume` | operator | 恢复调度 |
| POST | `/api/batch-experiments/{id}/cancel` | operator | 停掉全部非终态子任务 |
| POST | `/api/batch-experiments/{id}/rerun-failed` | operator | 只重跑 `failed` |
| POST | `/api/batch-experiments/{id}/copy` | operator | 复制（继承 key） |
| DELETE | `/api/batch-experiments/{id}` | operator | 删实验与编组，**子任务保留** |

状态推导：库里的 `status` 只在 pause/resume/cancel 时写入；`running/completed/failed` 由子任务状态在 `_build_info → _derive_status` 里推导，否则子任务全 failed 时实验仍显示「待开始」。

---

## 8. 代码地图

| 文件 | 内容 |
|---|---|
| `api/models/batch_experiment.py` | `BatchHarness` / `BatchModelParams`（两条轴）、`BatchBlock` / `BatchArm`（旧形状）、`BatchExperimentCreate`、`BatchCommonParams`、`BatchQueueConfig` |
| `api/routers/batch_experiments.py` | 全部端点 + `_expand_arms` / `_masked_params` / `_restore_params_secrets` / `_aggregate_rows` / `_derive_status` |
| `api/services/queue_scheduler.py` | 队列放行、并发封顶、长尾跳过、抢占重试 |
| `api/routers/instances.py` | `create_instance`（各 Harness 的配置文件生成、`/v1` 解析）、`_prepare_and_launch` |
| `frontend/src/views/BatchCreate.vue` | 三步建实验页：Harness 列表 + 模型列表 + 矩阵预览 + 校验 + 复制预填 |
| `frontend/src/views/BatchDetail.vue` | 执行概览 / 结果对比 / 参数详情（四个分组读 `harnesses` + `models`，兼容 `blocks`） |
| `frontend/src/views/BatchList.vue` | 实验列表 |

---

## 9. 兼容性

- **旧实验只读可用**：`params_json` 里没有 `harnesses` / `models` 时，详情页与复制页会从 `blocks`（或摊平的 `arms`）按 harness 三件套归组还原 Harness 轴，并把模型去重后摊成模型轴。
- **旧客户端可继续提交** `blocks`（块内嵌模型）或 `arms`；只有提交 `harnesses` 的新客户端才要求 `models` 非空。
- `_masked_params` 同时覆盖新旧两种形状，历史 `params_json` 可用 `tmp/cleanup_batch_keys.py --apply` 重新掩码（先备份 `platform.db`）。

---

## 10. 测试

```bash
cd platform

# 矩阵展开 / 校验 / 旧形状回归 / 接口级落库与掩码
/root/.venv/bin/python test/test_batch_expand.py -v

# 复制时的明文还原（含模型轴跨 Harness 还原）
/root/.venv/bin/python test/test_batch_copy.py -v

# /params/full 的权限与掩码口径
/root/.venv/bin/python test/test_batch_params_full.py -v
```

三个用例都用隔离的临时 DB，不碰生产 `platform.db`；接口级用例把
`instances.create_instance` 换成桩函数，不会真实创建实例或拉 OBS。

前端验证：`cd platform/frontend && node_modules/.bin/vite build`。

---

## 11. 已知限制

- 子任务配置是创建时快照，**不能在实验内改模型或 Harness**；要改只能复制实验重建。
- 模型列表为空不允许创建；因此 `common.model_*` 兜底只服务历史数据，页面上没有对应输入框。
- `data_total` 填 0 时实验页的任务数 / 进度分母会失真（子任务按「不限制条数」跑完全部文件）。
- 单个实验总任务数上限 100000。
- 同一实验的多个子任务各自申请 / 复用 key，`suffix_api_keys` 逻辑与单任务一致。
