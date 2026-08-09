# 后端性能治理：task_records 写风暴与卡顿

本文记录一次线上「使用中卡顿」的排查、根因、修复与验证，供后续维护参考。

## 1. 现象

平台使用过程中前端偶发卡顿：`/overview`、`/task-records` 等接口响应时快时慢，偶尔接近数秒。

## 2. 排查过程

关键观测指标：

| 指标 | 排查时的值 | 说明 |
|---|---|---|
| `platform.db` | **1.26 GB** | SQLite 单文件严重膨胀 |
| `task_records` 行数 | **463 万**（仅 410 个实例） | 单实例最高 12.8 万行 |
| `platform.db-wal` | 一路涨到 20MB+ | WAL 未被及时 checkpoint，写压力大 |
| running 实例 | 13 个，合计 **25.6 万 tasks** | 每轮后台刷新的写入基数 |
| 每实例 distinct config_name | ≈ distinct task_idx 的 **2 倍** | 每个任务被写了两行（phantom） |

## 3. 根因

### 3.1 写路径「全量重写」（卡顿主因）

链路：`background_cache_refresher`（每 8s）→ `_refresh_running_once` → 对每个 running 实例：

1. `_sync_instance_status(persist=True)`：更新计数/状态（轻量）。
2. `_compute_analyze()`：扫描 `logs/`，按 `(mtime,size)` 签名**只重读变化过的** `task-N.log`。**这步是增量的 ✅**。
3. `_sync_task_records()`：调 `_compute_task_rows()` 取出该实例**所有**任务行，**无条件逐行 UPSERT**，每行都重刷 `updated_at=CURRENT_TIMESTAMP`。**这步是全量的 ❌**。

问题：读日志已增量，但**写库没有增量**。一个 running 实例几万任务，每 8 秒就是几万次写；13 个实例合计每 8 秒约 25.6 万次写（因 phantom 翻倍到 ~50 万），还要维护 3 个索引。

SQLite 是**单写者**，`busy_timeout=5000`：后台写事务长时间占写锁，前端读请求撞上就最多干等 5 秒 → **卡顿**。WAL 也因此持续增长。

### 3.2 phantom 行导致数据翻倍

`_scan_task_file()` 从日志行解析 `config=<name>`；解析不到时 `_compute_task_rows()` 用日志文件名 `task-N.log` 兜底当 `config_name`。而 `task_records` 唯一键是 `UNIQUE(instance_id, config_name)`，于是同一任务：

- 早期日志无 `config=` → 写一行，`config_name = task-N.log`（status 也为 None）
- 后期日志有 `config=` → 又写一行，`config_name = 真实名`

→ 同一任务两行，行数翻倍，把表撑到 463 万行 / 1.26GB。

### 3.3 孤儿行

`delete_instance` 只删 `task_instances`，不删 `task_records` → 已删实例残留 8.8 万孤儿行。

### 3.4 多 worker 放大（隐患，非当时主因）

`run.sh` 用 `--workers 4` 启动，`background_cache_refresher` 挂在每个 worker 的 startup 上 → 4 个 worker 各跑一份全量扫描+全量写，争抢同一 SQLite。排查时线上恰好只剩 1 个孤儿 worker（master 已退出、worker 被 init 收养），掩盖了这个隐患；正常 4-worker 重启后会 ×4 放大。

## 4. 修复

### 4.1 写路径增量化（`api/routers/instances.py`）

- **`_compute_analyze`**：新增模块级 `_analyze_changed`，扫描时把**实际重读过**（签名变化/新增）的 fname 收集为集合，写 `_analyze_changed[config_path] = changed`。
- **`_compute_task_rows(config_path, only_fnames=None)`**：`only_fnames` 非 None 时只产出这些文件对应的行；`config_name` 为 None 时**跳过不生成**（不再用文件名兜底 → 根除新 phantom）。
- **`_sync_task_records(instance_id, config_path, full=False)`**：
  - `changed = _analyze_changed.get(config_path)`
  - `full=True` 或 `changed is None`（冷启动未扫过）→ 全量兜底；
  - `changed` 为空集 → 直接 return（本轮零写入）；
  - 否则只 UPSERT `changed` 内的行。

### 4.2 调用点传参

| 调用点 | 传参 | 语义 |
|---|---|---|
| `_refresh_running_once`（8s 后台） | `full=False` | 增量 |
| `_ensure_task_records` running 路径 | `full=False` | 增量 |
| `_ensure_task_records` 非 running 且 0 行兜底 | `full=True` | 首次全量填充 |
| `logs.py` 两处（新 eval/traj 分数落盘后） | `full=True` | 新分数需刷回对应行 |

### 4.3 防止未来孤儿行

`delete_instance` 在删实例时同事务补 `DELETE FROM task_records WHERE instance_id=?`，并清理 `_analyze_file_state` / `_analyze_changed` 内存缓存。

### 4.4 一次性清理与收缩

脚本 `cleanup_task_records.py`（低峰、建议先停后端执行）：备份 → 删孤儿行 → 删存量 phantom（`config_name LIKE 'task-%.log'`）→ `wal_checkpoint(TRUNCATE)` + `VACUUM`。

## 5. 验证结果

| 项 | 修复前 | 修复后 |
|---|---|---|
| WAL 增长 | 一路涨到 20MB+ | 0 ~ 2.3MB 波动、周期性 checkpoint 归零 |
| `platform.db` | 1.26 GB | **632 MB** |
| `task_records` 行数 | 463 万 | **232 万** |
| phantom 行 | 228 万 | **0** |
| 孤儿行 | 8.8 万 | **0** |
| 卡顿 | 偶发接近 5s | 消失 |

结论：写风暴消除（每轮只写变化的几十行），4-worker 下 WAL 仍稳定低位、无写锁争用。冷启动第一轮每个 worker 会走一次全量兜底（缓存为空），几轮后落到低水位，属正常。

## 6. 运维备忘

### 重启（加载新代码）

`run.sh` 默认 `--workers 4`。注意 `run.sh stop` 依赖 `platform.pid`，若该文件失效需手动 kill 监听 8087 的进程。手动单 worker 启动示例：

```bash
cd /root/openclaw-hive/platform
nohup /root/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8087 \
      --workers 1 --limit-concurrency 100 > platform.log 2>&1 &
echo $! > platform.pid
```

- **worker 数**：增量写后 4-worker 已无写锁争用，可保持；若想省掉 4× 冗余日志扫描、单写者更干净，可降为 1。
- **停后端不影响任务**：各实例的 `hive.py` 是独立子进程，后端停了仍继续跑并写自己的日志，只是前端面板暂时无数据。

### 数据清理与收缩

```bash
cd /root/openclaw-hive/platform
python3 cleanup_task_records.py   # 会先备份 platform.db.bak.<时间戳>
```

VACUUM 需独占锁且占用「与当前库同等大小」的临时磁盘，务必低峰、最好停后端后执行，勿中断。

### 回滚

清理脚本自动备份 `platform.db.bak.<时间戳>`；异常时停后端 → 用备份覆盖 `platform.db` → 重启。代码无 schema 变更，回滚仅需 `git` 还原。

## 7. 未尽事项（后续可选）

- **多 worker 后台任务收敛**：4 worker 各跑一份 8s 后台扫描+写，存在 4× 冗余。可考虑选主/独立进程只让一份跑，或固定单 worker。
- **访问日志治理**：`platform.log`（uvicorn access log）会无限增长（曾达 100MB），建议接 logrotate 或关闭 access log。
- **`run.sh` 默认 workers**：如需固定单 worker，改 `run.sh` 中 `start`/`restart` 分支的 `--workers 4`。

---

## 附录 A：数据链路详解（读写全景）

理解这套后端只需抓住一条主线：**任务在别处产生日志，后端把日志汇总进缓存/库，前端只读结果**。

### A.1 三类角色

```
┌─────────────┐      写日志       ┌──────────────────────────┐   查表    ┌──────────┐
│ hive.py ×N  │ ───────────────▶ │  平台后端 (FastAPI)        │ ◀─────── │  前端     │
│ (跑任务的   │  task-N.log       │   ├ 后台线程: 8s一轮扫+写   │  只读     │ (轮询)    │
│  实例子进程) │  complete.jsonl   │   ├ 内存缓存 (dict)         │ ───────▶ │           │
└─────────────┘  failed.jsonl     │   └ SQLite: platform.db    │           └──────────┘
                                  └──────────────────────────┘
```

- **生产者**：每个实例是一个 `hive.py` 子进程，边跑任务边往自己目录写日志（`logs/task-N.log`、`complete.jsonl`、`failed.jsonl`，以及 obsutil 拉下来的 `evaluator_use.log`）。后端**不参与**任务执行，只负责「读文件 → 汇总 → 给前端看」。
- **后端**：一个 FastAPI 进程，内部有 ① 一个 8 秒一轮的后台协程，② 几个内存缓存 dict，③ 一个 SQLite 库。
- **前端**：高频轮询 `/overview`、`/prepare-progress`、`/task-records`。

**关键设计原则**（代码注释反复强调）：GET 请求绝不扫文件、绝不写库，一切重活推给后台任务，请求只读缓存。方向正确，问题曾出在后台第 (3) 步（见下）。

### A.2 写路径：后台每 8 秒一轮

入口 `background_cache_refresher`，`asyncio.sleep(8)` 循环，每轮把 `_refresh_running_once` 丢进 8 线程的 `_db_executor`：

```
_refresh_running_once()
 │
 ├─ SELECT * FROM task_instances WHERE status IN ('running','preparing')   ← 拿到当前 running
 │
 └─ 对每个 running 实例:
     │
     ├─(1) _sync_instance_status(persist=True)         → 更新计数/状态
     │      · 数 complete.jsonl / failed.jsonl 行数
     │      · pid 死了或全跑完 → 状态改 finished/completed
     │      · 写内存 _status_cache + UPDATE task_instances 一行           ← 轻量
     │
     ├─(2) _compute_analyze(config_path, total_tasks)   → 扫日志(增量✅)
     │      · scandir logs/ 目录
     │      · 按 (mtime,size) 签名，只重读“变过”的 task-N.log
     │      · 正则抓“任务执行状态=… error_code=… config=…”
     │      · 结果存内存 _analyze_file_state / _analyze_cache / _analyze_tree_cache
     │      · 【修复后】把本轮变化的 fname 记入 _analyze_changed
     │
     └─(3) _sync_task_records(id, config_path)          → 写库
            · 【修复前】_compute_task_rows() 取【所有】任务行逐行 UPSERT → 全量重写 = 风暴
            · 【修复后】只 UPSERT _analyze_changed 内变化过的行；无变化则零写入
```

三步里，(1) 轻、(2) 一直是增量。**卡顿根因曾在 (3)**：第 (2) 步明明已知「哪些文件变了」，第 (3) 步却没用这个信息、把整个实例的行全刷一遍。修复即让 (3) 复用 (2) 的变化集（详见正文第 4 节）。

### A.3 读路径：前端请求怎么走

以最热的 `/overview` 为例：

```
GET /overview
 ├─ SELECT * FROM task_instances WHERE id=?          ← 读一行, 快
 ├─ _sync_instance_status(persist=False)             ← 只读 _status_cache, 不扫盘不写库
 └─ _analyze_task_status(...)                        ← 只读 _analyze_cache, 不扫盘
        └─ if _bg_refresh_active and cached: return cached[0]   ← 命中就直接返回
```

`/overview` 本身很轻——吃后台预热好的内存缓存；`/task-records` 直接 `SELECT ... FROM task_records` 查物化表。

**关键矛盾（修复前）**：读路径本身不写库，但它要读的 `task_records` / `task_instances` 表，正被后台第 (3) 步的写风暴锁着。SQLite 单写者 + `busy_timeout=5000`，读请求撞上写事务最多等 5 秒；前端高频轮询下等锁请求排队 → 卡顿。修复后 (3) 只写变化行、瞬间完成，读锁不再被长时间挤占。

### A.4 涉及的缓存（都是进程内 dict，重启即失）

| 缓存 | 存什么 | 谁写 | 谁读 |
|---|---|---|---|
| `_status_cache` | 实例的 completed/failed/status | 后台(1) | overview/list |
| `_analyze_file_state` | 每个 task 日志的 `(mtime,size,status,code,cfg)` | 后台(2) | 后台(3) 复用 |
| `_analyze_changed` | 本轮签名变化过的 fname 集合 | 后台(2) | 后台(3)（增量写依据） |
| `_analyze_cache` | 错误分类汇总 `{任务成功:N,...}` | 后台(2) | overview |
| `_analyze_tree_cache` | 错误码折叠树 | 后台(2) | overview |
| `_bg_refresh_active` | 布尔开关，标记「后台已接管」 | 后台启动 | 所有读路径 |

`_bg_refresh_active` 是总闸：一旦为 True，所有 GET 只读缓存、绝不自己扫盘。这保证读路径干净，代价是数据新鲜度完全依赖后台那 8 秒一轮。

### A.5 一条数据的完整旅程（以「task-42 刚跑成功」为例）

1. `hive.py` 往 `logs/task-42.log` 追加一行 `任务执行状态=任务成功 error_code=... config=xxx_q1.json`。
2. 下一个 8 秒周期，后台 (2) `scandir` 发现 `task-42.log` 的 `(mtime,size)` 变了 → 重读 → 更新 `_analyze_file_state`，重算错误汇总存 `_analyze_cache`，并把 `task-42.log` 记入 `_analyze_changed`。
3. 后台 (3)【修复后】只 UPSERT `_analyze_changed` 里的 task-42 一行（修复前会把该实例几万行全部重写）。
4. 前端下次 `GET /overview` 命中 `_analyze_cache` 看到「成功 +1」；`GET /task-records` 从表里查到 task-42 的新状态。

**一句话**：修复前，第 (3) 步为同步 1 个变化任务重写了几万个没变的任务；修复后 (3) 与 (2) 对齐、只处理变化行，写库量从「~25 万/8s」降到「这 8 秒真正跑完的那几十个」——读路径、缓存、前端接口均无需改动。
