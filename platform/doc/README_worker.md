   # 离线 Worker 架构与配置(Worker)

本文描述 openclaw-hive platform 的**离线分析链路**:四个常驻进程的职责划分、协作方式、
全部可调参数与运维命令。

> 核心目标:用户点「输出」能秒级看到分级结果,点「下载交付物」能秒级拿到文件,
> 而几十万条历史积压在背景慢慢跑,两者互不干扰。

---

## 一、四个 Worker

当前架构共 **5 个常驻进程**。切分维度是**场景**(是不是用户在等),不是阶段(读本地还是读 OBS)。

| 进程 | 启动命令 | 职责 | 车道 | 日志 |
|------|---------|------|------|------|
| **tracker** | `offline/task_tracker.py` | 发现投递(**点击 + running 自动**):增量读 jsonl,把新完成的 config 投进队列 | 按实例状态 | `tracker.log` |
| **auto-tracker** | `offline/auto_tracker.py` | 发现投递(**历史补全**):每轮挑一个已结束+有缺口的实例 | 恒 `priority=0` | `auto_tracker.log` |
| **shallow-realtime** | `offline/shallow_worker.py --mode realtime` | 浅层分析(两阶段),**仍在跑的实例** | `priority >= 100` | `shallow_realtime.log` |
| **shallow-auto** | `offline/shallow_worker.py --mode auto` | 浅层分析(两阶段),**已结束实例**,批量补全,最新优先 | `priority < 100` | `shallow_auto.log` |
| **deep-realtime** | `offline/output_worker.py --realtime-only` | 交付物下载,只处理用户点击的 | `artifact_priority >= 100` | `realtime_worker.log` |

### 1.1 浅层车道怎么分:按实例是否还在跑

`tracker` 投递时按实例 `status` 决定 `priority`(`_lane_priority()`):

| 实例 status | 车道 | `priority` | 为什么 |
|------------|------|-----------|--------|
| `running` / `preparing` | 实时 | 100 | 实例还在产出,用户点开输出页就是在等新完成的任务逐个冒出来。每轮只新增几行,队列天然很短,16/8 并发够用 |
| `finished` / `stopped` / `completed` / `queued` / `created` | 背景 | 0 | 实例已不动,剩下的是「把几万行历史一次性补全」—— 吞吐活而非响应活,交给 64/16 并发吃满 |

> **踩过的坑**:改前 `priority=100` 是写死的,而 tracker 只扫点击过的实例,
> 于是**所有行都进实时车道**,shallow-auto 永远分不到活 ——
> 实测 30 分钟 realtime 处理 8986 行、auto 只有 266 行,而 auto 的 64 并发全程闲着;
> 一个 3.4 万 config 的已结束实例本该用 64 并发快清,却挤在 16 并发里跑。
>
> 连带一处:`submit_batch` 原来用 `excluded.priority > 0` 当「是否用户点击」的判据,
> 来决定要不要把 done/skipped 的行复活成 queued。priority 变成车道号后这个判据失效
> (背景车道 priority=0 但同样是点击投进来的),已改为显式的 `reset` 参数。
> `artifacts.py` 的懒发现传 `reset=False` —— 它只为显示列表补建行,
> 打回 queued 会让两个 worker 白跑几千行已分析完的任务。

`priority` 取 `MAX()`,所以实例从 running 变 finished 后,老行保持 100 不回退 ——
那些行在 running 期间已分析完,留在实时车道不占活;新增的行按当前 status 走背景车道。

> **车道只对「新投递」生效**。改动上线时,已结束实例的历史行仍带着旧的 100,
> 而 `MAX()` 不会让它们降级 —— 加上这些实例的 offset 早已读到文件尾(没有新行),
> 结果就是「auto 看着还是不干活」。2026-11-03 做过一次性降车道:
>
> ```sql
> -- 分批执行(每批 5000),别一条 UPDATE 几万行:
> -- 实测一条 876 万行的 UPDATE 持锁 22.8s,期间用户点下载全部 500
> UPDATE shallow_queue SET priority=0 WHERE id IN (
>   SELECT sq.id FROM shallow_queue sq JOIN task_instances ti ON ti.id=sq.instance_id
>    WHERE sq.priority>=100 AND ti.status NOT IN ('running','preparing') LIMIT 5000);
> ```
>
> 实测 73,334 行、1.2s 降完(其中 22,489 行仍待处理,随即被 auto 的 64 并发清空)。
> 以后实例自然结束时新行会直接进背景车道,不需要再手工降。

### 1.2 两个 tracker 的分工

`task_tracker` 覆盖两类实例:

1. **点击登记的**(`shallow_requests`)—— 任意状态,用户显式要求
2. **所有 running/preparing 的**(自动,**无需点击**)—— 实例还在产出就说明结果随时在变,
   用户点开输出页时应该已经有数据,而不是「点一下、等 5 秒、才开始分析」。
   实测 26 个 running 实例里只有 6 个被点过,另外 20 个正在产出却零分析
   (`complete.jsonl` 已攒到 230KB);开启自动跟踪后一轮补投 **64,710 行**。

> 自动跟踪的实例**不写 `shallow_requests`** —— 那张表的语义是「用户显式要求」,
> 污染它会带偏 `_clear_shallow_request` 的出队回收和 `output_status` 门控。
> 查询里用 `src` 列区分(0=点击,1=自动),排序上点击的优先(用户在等)。

剩下的缺口是**已结束且没点过**的实例:实测全库 **2126 个已结束实例只有 16 个被跟踪**,
其中 243 个实例共 **301 万行** `traj_level IS NULL` —— tracker 不碰它们
(已结束的实例不该每 5 秒重扫一遍几十万行 jsonl),交给 `auto_tracker` 逐个补全。

`auto_tracker` 补这个缺口:

|            | `task_tracker`            | `auto_tracker`                 |
|------------|---------------------------|--------------------------------|
| 实例来源   | 点击登记 **+ 所有 running/preparing**(自动,无需点击) | 全库扫「已结束 + 有缺口」 |
| 实例状态   | 任意(点击) / running(自动) | 仅 `finished`/`stopped`/`completed` |
| 车道       | 按实例状态(100 / 0)       | **恒 0**(背景)                  |
| 粒度       | 每轮扫全部被跟踪实例      | **每轮一个实例**                |
| 增量       | offset + inode + 头指纹   | 同款(复用 tracker 的函数)       |
| 间隔       | 5s                        | 60s(没活时 ×5 退避)             |

**为什么每轮只投一个实例**:历史缺口 301 万行,一次全投会把 `shallow_queue`
瞬间灌成几百万行、单次写持锁过久(实测一条 876 万行的 UPDATE 持锁 22.8s,
期间用户点下载全部 500),还会把新点击的实例挤在几百万条历史后面。
每轮一个、投完等 shallow-auto 消化,历史缺口以「实例」为粒度稳步推进,
任何时刻队列里的背景行都是有界的。实测单实例投 10,000 行耗时 0.48s。

`auto_tracker_state` 表记录投过哪些实例。必须记 —— 投完后缺口要等 shallow-auto
慢慢消化,期间 `traj_level` 仍是 NULL,不记状态下一轮又会选中它、又整扫一遍 jsonl。
缺口小于 `--min-gap` 的实例也记一笔(跳过),同样是防反复选中。

两者写同一张 `shallow_queue`,靠 `ON CONFLICT(instance_id, config_name)` 去重,
同时跑安全:某实例既被点过又被 auto_tracker 选中时,`priority=MAX()` 保证它
留在实时车道(用户在等,优先级更高)。

### 1.3 两个 tracker 的日志怎么读

```
[tracker] 跟踪=26 新行=5 投递=5 0.41s
[auto-tracker] 2611022313-c92d 缺口=6,468 新行=10000 投递=10000 0.48s
```

| 字段 | 含义 |
|------|------|
| `跟踪=26` | 当前 `shallow_requests` 里被跟踪的实例**总数**(不是编号) |
| `新行=5` | 本轮从这些实例的 jsonl 里增量读到的新完成任务数 |
| `投递=5` | 实际投进队列的行数(与新行数通常相等,去重后可能略少) |
| `2611022313-c92d` | auto-tracker 本轮选中的实例短 ID(首段时间 + 末段后缀) |
| `缺口=6,468` | 该实例 `traj_level IS NULL` 的行数 |

auto-tracker 每轮只处理一个实例,所以直接打实例 ID;tracker 每轮扫全部被跟踪实例,
只报总数(每轮通常只有 1~2 个有新行,逐个列出反而刷屏)。

短 ID 取 `instance_id` 的**首段时间 + 末段后缀**
(`2611031754-wanyi26110317530533-70b2` → `2611031754-70b2`),固定 15 字符。
不用 `name`:实测同批次的 name 只差末尾几个字(「…lego」/「…lego-copy」/
「…lego-copy-copy」),截断后分不出谁是谁;而且含中文、长到 50+,对齐难看。

### 为什么没有 deep-auto

交付物是整个 OBS 前缀 `obsutil cp -r`,单任务动辄数十 MB(实测离群值 3.3 万文件 / 388 MB)。
曾经有过 `--deep-only` 背景全量下载,结果积压 97 万条、按 210 条/分需 3.2 天,
而磁盘只剩 214G —— **全量下完会直接写爆盘**。

所以交付物改成**纯点击驱动**:tracker 投递时置 `pending_click`(静默态,无人领取),
用户点开才入队。浅层分级不受影响 —— 它只读 1KB 的 `traj_stats_result.json` 和本地日志,
**根本不需要交付物**。

### 为什么浅层按场景拆而不按阶段拆

浅层有两个**阶段**,都要做且顺序固定:

1. **阶段 1(local)**:读实例本地 `logs/task-<idx>.log` 判 DONE → `task_records.task_done`
2. **阶段 2(obs)**:读 OBS 的 `logs/traj_stats_result.json` 定级 → `task_records.traj_level`

旧架构按阶段拆成 `shallow-local`(并发 64) + `shallow-obs`(并发 16)两个进程。
问题是两种**场景**混在同一条队列里抢并发:阶段 2 只有 16 并发,要和 92 万条背景行竞争,
用户点的那批排不上 —— 实测 2.3 万行卡在 `local_state='done'` 但 `obs_state='queued'`。

现在一个进程内**顺序跑完两阶段**,好处是**同一轮内阶段 1 刚写出的 `task_done`,
阶段 2 立刻就能领走**(阶段 2 的 claim 条件含 `task_done IS NOT NULL`)。
实测一轮 0.97s 完成「判 DONE → OBS 定级」全程;旧架构下阶段 2 要等下一轮且要和背景行抢。

---

## 二、协作方式

权威队列是 `shallow_queue` **一张表、三个独立状态维**,靠 CAS 领取 + DB 层 UNIQUE 去重协作。

### 2.1 数据流

```
用户点「输出」 → POST /api/batch-output/{id}/shallow      (batch_output.py)
                   └→ INSERT shallow_requests status='queued'

tracker 每 5s  → pick_clicked() 读 shallow_requests
                   ├ 'queued'   → _reset_offsets() 清零 → 全量扫一遍 → 转 'tracking'
                   └ 'tracking' → read_new_lines() 按 (inode, offset, head_hash) 增量读
                                  complete.jsonl / failed.jsonl
                   └→ submit_batch()  INSERT ... ON CONFLICT(instance_id, config_name)
                        local_state='queued', obs_state='queued',
                        artifact_state='pending_click',  priority=100

shallow-realtime / shallow-auto(各自车道,同一进程内串联两阶段):
  阶段1 → CAS claim(local_state='queued' AND 车道) → 读 logs/task-<idx>.log
            →【Task_Done】或旧版「所有任务执行完成!」→ task_done=0/1
            → 「任务异常」走 done_failed:一步置 obs_state='done' + traj_level='failed'
              (把无轨迹的行挡在 OBS 之外,实测省掉 7 万次 OBS 调用)
  阶段2 → CAS claim(obs_state='queued' AND traj_level IS NULL
                     AND task_done IS NOT NULL AND 车道)      ← 串联依赖阶段1
            → OBS 拉 tsr → compute_level → traj_level = L0/L1/L1.5/L2/L3
            → 拉不到则 log_fallback_grade 本地日志兜底

用户点「下载交付物」 → POST /api/artifacts/{id}/request          (单个)
                     → POST /api/artifacts/{id}/request-batch    (批量,上限 500)
                     → POST /api/batch-output/{id}/deep/{traj}   (会话详情里的下载)
                       三者同口径:artifact_state='queued' + artifact_priority=100

deep-realtime 每 2s → _claim_and_download() 领 artifact_priority>=100
                     → obsutil cp -r → output_cache/<batch>/<leaf>/

用户点开会话详情 → GET /deep/{traj}/detail  只读本地;没有则 409 + 入队按钮
```

### 2.2 三个不能破坏的约定

**(1) 双维解耦**
阶段 1 只写 `local_state`,阶段 2 只认 `obs_state`。历史上两者共用一个 `state`,
阶段 1 置 done 后阶段 2 永远领不到,`traj_level` 永久 NULL。

**(2) 阶段串联**
阶段 2 的 claim 条件含 `task_done IS NOT NULL` —— 必须等阶段 1 写完,
否则会拿到不完整的 tsr。合并进程后这个顺序由 `run_once` 里「先阶段 1、再阶段 2」保证。

**(3) `artifact_priority` 是产物维自己的车道号,与 `priority` 无关**

| 列 | 语义 | 谁写 |
|---|------|------|
| `priority` | 浅层**车道号**:实例还在跑=100,已结束=0(见 1.1) | tracker,每 5s 重写 |
| `artifact_priority` | 产物「用户点了**这一个** task」 | 三个点击入口 |

拿 `priority` 分产物车道会让整个实例上千行涌进实时车道,真正的单任务点击反而排在后面。
而且 tracker 每 5s 重投一遍,**产物维必须完全不看 `priority`** ——
否则已下完的行被无限打回队列(线上实测同一 task 重下 8 轮以上)。

### 2.3 状态机

`shallow_queue` 的三个维度:

| 维度 | 取值 | 写入方 |
|------|------|--------|
| `local_state` | `queued` → `claimed` → `done` / `skipped` | 阶段 1 |
| `obs_state` | `queued` → `claimed` → `done` / `skipped` | 阶段 2 |
| `artifact_state` | **`pending_click`**(静默) / `queued` → `claimed` → `done` / `failed` / `skipped` / `empty` | tracker 投递 + 点击入口 + deep-realtime |

`pending_click` 是本次改造新增的取值,含义是「任务已完成、交付物可下载,但**没人点过**」。
它不被任何 worker 领取,所以不占下载配额、不占磁盘。
`artifacts_status`(`task_traj_records` 镜像列)同口径 —— 前端据此显示「未下载」而非「排队中」。

### 2.4 实例出队时的回收

`output_worker._clear_shallow_request` 在实例浅层跑完(无缺口)时:

1. 删 `shallow_requests` 登记
2. 把该实例仍 `queued` 的**背景**产物行(`artifact_priority < 100`)置 `pending_click`
   —— 实例已出队没人在看,不该继续占配额;**用户点击的行(>=100)保留**
3. 删该实例的 `tracker_state` offset 记录

不做第 2、3 步就是 92.6 万条孤儿产物行的产生机制(实测 159 个已出队实例的残留)。

---

## 三、配置

### 3.1 浅层(shallow_worker.py)

两种模式的差异**只有车道过滤 + 并发/间隔**,判定与定级逻辑完全共用
(`shallow_local.judge_one` / `shallow_obs.grade_one`)。

| 模式 | claim 过滤 | 排序 | 阶段1并发 | 阶段2并发 | 间隔 | 单轮预算 |
|------|-----------|------|----------|----------|------|---------|
| `realtime` | `priority >= 100` | `id` 升序 | 16 | 8 | 2s | 10s |
| `auto` | `priority < 100` | **`id DESC`** | 64 | 16 | 5s | 30s |

命令行可覆盖:`--local-concurrency` / `--obs-concurrency` / `--interval` / `--max-seconds`。

**排序的讲究**:
- `auto` 用 `id DESC`(最新投递优先):积压几十万条时,新跑完的批次应该先出结果,
  而不是排在历史行后面等几天。
- **不能再带 `priority DESC`**:车道已由 WHERE 分流,再按 priority 排序会让 id 游标跳行
  (这个坑 `_claim_and_download` 踩过,见其注释)。
- `realtime` 用 `id` 升序:同一实例内保持投递序,用户看到的顺序稳定。

**并发为何不同**:本地读 NFS 约 900 个/秒且无配额;OBS 单次 0.4~2s,
并发超过 32 会触发 `obsutil exit 8`(请求数超限),16 是安全值。
`realtime` 队列通常很短,不需要开满。

**其它常量**:

| 常量 | 值 | 位置 | 说明 |
|------|-----|------|------|
| `RECLAIM_EVERY_SEC` | 300 | `shallow_worker.py` | 回收超时 claimed 的最小间隔,环境变量 `SHALLOW_RECLAIM_EVERY_SEC` |
| `CLAIM_TIMEOUT_SEC` | 600 / 900 | `shallow_local` / `shallow_obs` | claimed 超过多久算卡死 |
| `MAX_ATTEMPTS` | 3 | 两者 | **只对背景行生效**,`priority>=100` 不限次 |

> `attempts` 上限对点击行不生效是刻意的:判不出终态的背景行会无限循环
> (实测 attempts 涨到 938、9488 条毒行占满并发槽);但用户点击卡在 3 次之后
> 永远领不到,页面表现为「点了没反应」(实测某实例 20 条全被挡住)。

### 3.2 产物下载(settings/worker/config.yaml)

优先级:**环境变量 > yaml > 代码内置默认**。改完执行 `./run.sh realtime-restart` 生效。

> ⚠ 该文件缺失或格式错误时 worker **静默**退回内置默认,不报错(见 `_cfg`)。
> 怀疑参数没生效时先确认这里能正常解析。

| 配置项 | 当前值 | 环境变量 | 说明 |
|--------|-------|---------|------|
| `realtime.priority_threshold` | 100 | `ARTIFACT_REALTIME_PRIORITY` | 车道分界线 |
| `realtime.interval_seconds` | 2 | `ARTIFACT_REALTIME_INTERVAL` | 单 task 中位 1.6s,2s 足够「点完几秒内看到」 |
| `realtime.concurrency` | 4 | `ARTIFACT_REALTIME_CONCURRENCY` | 与积压 worker 的并发相加才是 OBS 总并发 |
| `download.concurrency` | 8 | `ARTIFACT_CONCURRENCY` | 仅在手工跑 `--deep-only` 时用到 |
| `download.round_budget_seconds` | 300 | `ARTIFACT_ROUND_BUDGET_SEC` | 轮内连续领取的预算 |
| `download.max_task_bytes` | 2 GiB | `ARTIFACT_MAX_TASK_BYTES` | 超限删除并标 `skipped` |
| `download.downloading_timeout_minutes` | 30 | `ARTIFACT_DOWNLOADING_TIMEOUT_MIN` | claimed 回收阈值 |
| `download.fail_retry_seconds` | 600 | `ARTIFACT_FAIL_RETRY_SECONDS` | failed 行退避重试间隔 |
| `download.max_attempts` | 3 | `ARTIFACT_MAX_ATTEMPTS` | 超过判死 `skipped`(没上限会死循环,实测 168 次) |
| `worker.interval_seconds` | 60 | — | 积压 worker 的轮间空档 |
| `worker.heartbeat_keep_rows` | 2000 | `HEARTBEAT_KEEP_ROWS` | 见 3.4 |

**已废弃键**(发现逻辑迁到 tracker 后无调用方,保留仅为旧配置文件能解析):
`download.max_tasks_per_round` / `max_instances_per_round` / `auto_discover_days`。

### 3.3 tracker

| 常量 | 值 | 说明 |
|------|-----|------|
| `DEFAULT_INTERVAL` | 5s | `--interval` 可覆盖 |
| `RECORDS` | `complete.jsonl`, `failed.jsonl` | 增量读的两个文件 |

**增量读的关键**:`tracker_state` 记 `(instance_id, file_path) → (inode, offset, head_hash)`。
必须用 **inode + 文件头指纹**双重检测文件被替换 —— hive 重跑时会把 `failed.jsonl`
**原地 truncate 重写**(NFSv3 不支持 rename,inode 不变),只靠 inode 的话
offset 会指向新内容中间,前面的行被整段跳过。

**running 实例的增量**:登记后 `shallow_requests.status` 有两态,running 实例靠它持续跟踪:

| status | 行为 |
|--------|------|
| `queued` | 刚点,还没扫过。先 `_reset_offsets()` 清零 → 从头整扫一遍已完成的 config → 转 `tracking` |
| `tracking` | 已在增量跟踪。**每轮只读新增行**,running 实例新完成的 config 逐轮带进来;实例跑完后文件不再增长,自然安静 |

所以 running 实例**不需要反复点「输出」** —— 点一次之后,后续新完成的任务会每 5 秒
自动投进队列。实测 running 实例的 offset 在持续推进(单实例已读到 120 万字节)。
首轮清零是必须的:实例此前可能被旧版自动发现跟踪过、offset 已到文件尾,
不清零的话点击后读不到任何「新增」行,等于点了没反应。

**实例处理顺序**:`pick_clicked` 按 **`status='queued'` 优先,再按 `created_at DESC`**
(最新点击优先)。

> 两层排序各有原因:`queued` 的实例界面上完全空白、一行结果都没有,优先扫它;
> 已 `tracking` 的每轮只增量读几行、早就有结果可看。同组内按登记时间倒序,
> 因为用户最后点的那个通常就是他正盯着的。
>
> 原来没有 ORDER BY,SQLite 按 `shallow_requests` 主键(instance_id 字符串)返回,
> 实测**最老的登记(10-31)先扫、最新的(11-03 08:09)最后扫**,
> 与 shallow-auto 的「最新优先」(`ORDER BY id DESC`)口径正好相反。

同一实例内部,jsonl 按文件顺序读(先完成的在前),所以 `id` 递增 ≈ 完成时间递增;
而 shallow-auto 按 `id DESC` 领取,**新完成的任务先定级**,两层口径一致。

### 3.4 心跳与监控

`worker_heartbeats` 表,**按 worker 分组各留 `KEEP_ROWS=2000` 行**
(`offline/heartbeat.py`)。

> 原来是**全表** 2000 行上限、4 个进程共用 → tracker 每 5s 写一行,
> 几分钟就把别人的历史冲掉,实测窗口只剩约 36 分钟,
> 看板 `/trend?hours=4` 根本取不到 4 小时数据。

每个 worker **每轮都写心跳**(哪怕 0 产出):监控页靠「最后心跳时间」判断存活,
只在有产出时写会让空闲 worker 看起来已经死了。
`processed` 的语义各进程不同(tracker=新投递数、shallow=两阶段 done 之和、
deep-realtime=下载完成数),**混加没有意义**,看板按 worker 分别画曲线。

相关接口:`/api/worker-monitor/overview` | `/queue` | `/trend` | `/eta` | `/reclaim`,
前端 `frontend/src/components/WorkerMonitor.vue`。

`/eta` 按**车道**分别估算(`by_lane`),每条车道用自己 worker 的吞吐 ÷ 自己队列的积压:

| 车道 | 分子(心跳 worker) | 分母(权威队列 `shallow_queue`) |
|------|------------------|------------------------------|
| `shallow_realtime` | `shallow-realtime` | 两阶段任一 queued 且 `priority>=100` |
| `shallow_auto` | `shallow-auto` | 同上,`priority<100` |
| `artifact` | `deep-realtime`(兼容旧名 `deep`/`realtime`/`artifacts`) | `artifact_state='queued'` |

> 阶段 2 的积压统计**必须带 `_OBS_CLAIMABLE`**(EXISTS + `traj_level IS NULL`),
> 与 `shallow_obs.claim` 同源。线上有 2.5 万行 `obs_state='queued'` 但 `traj_level`
> 早已写好(旧架构遗留),worker 根本不会领 —— 不带 EXISTS 会算出「181 天」的假积压。

---

## 四、缓存与数据库

### 4.1 本地缓存布局

根目录 = `.env` 的 `OUTPUT_CACHE`,当前值
`/mnt/sfs_turbo/s3-asset-b-hd-cce-aifm-nlp-exp/zhengnianzu/output_cache_v1`。

子目录规则(`oa._cache_subdir_for`):**丢桶名 + 丢桶内第一个 in-bucket 段,保留 batch**。

```
obs://bucket/zhangchen/smoke_test_oc_traj/acad_000012
  → output_cache/smoke_test_oc_traj/acad_000012
```

`oa.task_cache_dir(cache_root, obs_base, traj_name)` 可从 `config_snapshot`
**纯派生**出任务目录,不依赖任何 DB 列 —— 这是定位缓存最可靠的方式。

**一个重要区别**(判断「交付物是否已下载」时的坑):

| 目录内容 | 含义 |
|---------|------|
| 只有 `logs/`(可能加 `workdir/`) | **浅层分析**的产物(tsr、run.log),不是交付物 |
| 含 `workspace/` `workspace-evaluator/` `projects/` | 真正的沙箱交付物 |

所以**不能用 `os.path.isdir` 判断「已下载」** —— 实测某实例 1426 行里 1315 个目录
只有 `logs/`,用 isdir 判断会全部误报已下载(历史上实测 11678 行全被误标,
其中 11459 行一条交付物都没下)。判定一律看 DB 的 `artifacts_status == 'done'`。

### 4.2 分库

| 库 | 内容 | 量级(2026-11-03,持续增长) |
|---|------|------|
| `platform.db` | 在线表 + `shallow_queue` + `shallow_requests` + `tracker_state` + `worker_heartbeats` | 约 0.4 GB |
| `database/task_data.db` | `task_records`(1030 万+行) + `task_traj_records`(915 万+行) | 约 9 GB |

`get_connection()` 自动 `ATTACH` 成 `taskdata` schema,**查询 SQL 一行不用改**
(不带前缀时 SQLite 按 main → attached 顺序查找)。

**为什么分**:两张巨表连同索引占了原库 85%(9.1GB / 10.6GB)。SQLite 的 WAL checkpoint
要把 WAL 回写进主库,库越大越久,而 checkpoint **独占写锁** —— 10.6GB 的库在 NFS 盘上
轻易超过 `busy_timeout`(15s),于是到处报 `database is locked`。
分库 + VACUUM 后主库从 10.6GB 缩到 0.33GB,**checkpoint 从 >15s 降到 0.2~0.4ms**。

**DDL 必须加 schema 前缀**(查询不用):`CREATE TABLE IF NOT EXISTS task_records`
在 ATTACH 之后仍会建到 **main**(IF NOT EXISTS 只看 main),而不带前缀的查询是
main 优先 → 一张空表把 attached 库的 1030 万行**整个遮蔽且不报错**。
由 `qualify_task_data_ddl()` / `exec_task_data_ddl()` 统一改写。
注意 `CREATE INDEX` 的 schema 加在**索引名**上,不是表名上
(`CREATE INDEX taskdata.idx ON task_records(c)`,反之语法错误)。

迁移脚本:`platform/migrate_split_db.py`
(`--dry-run` → `--copy` → `--drop-old --yes` → `--vacuum`)。

### 4.3 写锁注意事项

SQLite 的 WAL **只允许一个写者**。`BUSY_TIMEOUT_MS=15000`(`DB_BUSY_TIMEOUT` 可覆盖)。

已做的防护:

- `reclaim_stuck`(两个阶段各一处)**先 SELECT 探测,0 行直接返回**,不开写事务。
  无条件 UPDATE 会让「本来没活干」的 worker 每轮也抢一次写锁。
- `shallow_worker.run_once` 把 reclaim 节流到 300s 一次。
- `queue_scheduler.schedule_queue` 对 `_try_claim_next` 做 3 次指数退避重试
  (0.5/1/2s + 30% 抖动)。原来**没有 try**,异常从 `asyncio.create_task` 逃逸成
  `Task exception was never retrieved`,**那一次调度被整个丢掉**。

**运维提醒**:手工跑百万行级的 UPDATE 要**分批提交**。
实测一条 876 万行的 UPDATE 持锁 22.8s,期间所有写请求(包括用户点下载)全部 500。

---

## 五、运维命令

```bash
cd /root/openclaw-hive/platform

# ── 启停(4 个进程一起)──
./run.sh worker-start          # = tracker + auto-tracker + shallow-realtime + shallow-auto + deep-realtime
./run.sh worker-stop           # 停新旧全部(含历史进程,避免升级期留孤儿)
./run.sh worker-restart
./run.sh shallow-all-start     # 同 worker-start

# ── 单个进程 ──
./run.sh tracker-start|tracker-stop|tracker-restart       # 点击驱动投递
./run.sh atracker-start|atracker-stop|atracker-restart    # 历史实例投递
./run.sh rt-start|rt-stop|rt-restart            # shallow-realtime
./run.sh auto-start|auto-stop|auto-restart      # shallow-auto
./run.sh realtime-start|realtime-stop|realtime-restart   # deep-realtime

# ── 日志 ──
./run.sh logs-tracker          # tracker.log
./run.sh logs-atracker         # auto_tracker.log
./run.sh logs-rt               # shallow_realtime.log
./run.sh logs-auto             # shallow_auto.log
./run.sh logs-dl               # realtime_worker.log
./run.sh shallow-logs          # 四个一起 tail -f
./run.sh worker-status         # 4 个 PID/运行时长 + 各自日志尾部 5 行

# ── 调试(跑一轮即退出)──
python offline/task_tracker.py --once
python offline/auto_tracker.py --once            # 投一个历史实例
python offline/auto_tracker.py --once --min-gap 100
python offline/shallow_worker.py --mode realtime --once
python offline/shallow_worker.py --mode auto --once
```

`worker-status` 会额外检查旧进程(`worker.pid` / `deep_worker.pid` /
`shallow_local.pid` / `shallow_obs.pid`)有没有漏停 —— 它们的 claim **不带车道过滤**,
和新进程同时跑会打乱车道隔离。

### 已停用的历史进程

保留启停函数仅为回滚,正常不要和新进程同时跑:

| 旧进程 | 命令 | 被谁取代 |
|--------|------|---------|
| `shallow-local` | `./run.sh local-start` | shallow-realtime / auto 的阶段 1 |
| `shallow-obs` | `./run.sh obs-start` | shallow-realtime / auto 的阶段 2 |
| `--shallow-only` | `./run.sh shallow-start` | 同上(更早的单体循环) |
| `--deep-only` | `./run.sh deep-start` | **无替代,刻意不启**(deep-auto 会写爆磁盘) |

---

## 六、实测性能基线

| 项 | 实测值 |
|---|-------|
| tracker 一轮 | 0.2~0.3s(增量读 15 个实例) |
| shallow-realtime 一轮 | 0.5~0.9s(两阶段串联,领 2~6 行) |
| shallow-auto 一轮 | 0.6~9.7s(阶段 1 满载 64 行) |
| 阶段 1 吞吐 | 约 900 行/秒(本地 NFS) |
| 阶段 2 吞吐 | 单次 OBS 0.4~2s,16 并发约 467 行/分 |
| 交付物下载 | 单 task 中位 1.6s;长尾 85s;离群 3.3 万文件/388MB |
| OBS 枚举(`_list_obs_leaves`) | 固定约 1.15s,与行数无关 |
| OBS 并发饱和点 | 约 32(再高回落,超限返回 exit 8) |
| checkpoint(分库后) | 0.2~0.4ms(分库前 >15s) |

---

## 七、相关文档与代码

| 文档 | 内容 |
|------|------|
| `doc/README_outputview.md` | 输出页的页面交互与状态机设计 |
| `doc/README_trajdb.md` | `task_traj_records` 表结构与轨迹字段 |
| `doc/README_queue.md` | 任务队列串联执行(与本文的 worker 无关) |

| 代码 | 职责 |
|------|------|
| `offline/task_tracker.py` | 发现投递(点击驱动)、增量读 jsonl |
| `offline/auto_tracker.py` | 发现投递(历史补全),每轮一个实例 |
| `offline/shallow_worker.py` | 浅层双模式、两阶段串联、车道过滤 |
| `offline/shallow_local.py` | 阶段 1 的 `claim` / `judge_one` / `apply_results` |
| `offline/shallow_obs.py` | 阶段 2 的 `claim` / `grade_one` / `apply_results` |
| `offline/output_worker.py` | 交付物下载(`_claim_and_download`)、出队回收 |
| `offline/heartbeat.py` | 心跳写入与按 worker 分组裁剪 |
| `src/offline_analysis.py` | 分级引擎(`compute_level`)、缓存路径、详情解析 |
| `api/routers/worker_monitor.py` | 监控接口(overview/queue/trend/eta/reclaim) |
| `api/routers/artifacts.py` | 交付物工作区(列表/树/内容/入队) |
| `api/routers/batch_output.py` | 浅层登记、会话详情(deep) |
| `test/test_shallow_worker.py` | 车道隔离、最新优先、阶段串联、投递顺序、车道分配、reset 语义、auto-tracker 挑选(28 项) |
| `test/test_artifact_queue.py` | 产物状态机(20 项) |
| `test/test_worker_monitor.py` | 心跳、ETA、回收(11 项) |
