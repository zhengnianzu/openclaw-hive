# 任务队列串联执行(Queue)设计与实现

本文描述 openclaw-hive platform 的**队列串联执行**功能:排队规则、调度流程、数据结构与涉及的全部代码位置。

> 设计稿:`platform/queque.png`
> 核心目标:多个任务实例归属同一个队列后,按加入顺序**依次串行执行**,前一个结束后一个自动开始。

---

## 一、排队规则

### 1.1 基本规则

| 规则 | 说明 |
|------|------|
| **队列内并发** | 同一队列同时最多 `min(QUEUE_MAX_CONCURRENT, 队列的 max_concurrent)` 个任务 |
| **长尾跳过(可选)** | 队列开启后,跑着的任务**剩余量 ≤ 阈值百分比**时再额外放行 1 个;峰值 = 基础并发 + 1 |
| **队列间并行** | 不同队列互不影响,各自独立调度 |
| **全局上限** | `QUEUE_MAX_CONCURRENT`(`.env`,默认 2)封顶所有队列的并发;沙箱总量(`MAX_TOTAL_SANDBOXES`)已取消 |
| **顺序依据** | 队列内按 `queue_position` **升序**放行(数字越小越先跑) |
| **入队顺序** | 创建任务并选队列时,`queue_position = MAX(同队列 position) + 1`(排到队尾) |

### 1.1.1 长尾跳过

**动机**:一批任务的耗时常呈长尾分布——最后几个任务可能比前面几十个都慢。
严格串行时,这段时间只看得到「一个任务在跑」,任务量掉下来。

**规则**:

| 项 | 说明 |
|---|---|
| 配置位置 | `task_queues.tail_skip_enabled` / `tail_skip_percent`,队列新建或编辑时设置 |
| 进度口径 | `剩余任务数 < concurrent_num × tail_skip_percent / 100`(严格小于) |
| 为什么不用总量 | 分母是该任务的**沙箱并发量**,不是 `total_tasks`。用总量作分母时,总量 10000 要等到最后 500 个才算「10%」,早已不是长尾;按并发量判定才是「剩下的活喂不满一轮并发」 |
| 触发条件 | 存在一个 `running` 的占用者处于尾部窗口 |
| 基础并发 | `min(QUEUE_MAX_CONCURRENT, 队列 max_concurrent)`——与长尾窗口无关 |
| 长尾额度 | 基础并发 **+1**(仅在尾部窗口内) |
| 回落 | 长尾任务结束、下一个任务刚开始(剩余 100%)时,额度回落到基础并发 |
| 队列级配置 | `task_queues.tail_skip_enabled` / `tail_skip_percent` / `max_concurrent` |
| 全局配置 | `.env` 的 `QUEUE_MAX_CONCURRENT`(默认 2)只封顶,不抬高 |
| 未知总量 | `total_tasks = 0`(任务数还没统计出来)时不触发 |
| 阈值下限 | 乘积不足 1 时兜底为 1:并发 10、阈值 5% → 剩余 0 个才触发 |
| 并发量为 0 | 按 1 处理(异常数据兜底) |
| 阈值范围 | 1–20%,超出按边界截断 |

`failed_tasks` 计入「已处理」,与终态判定 `completed + failed >= total_tasks` 的口径一致。

**为什么用任务数而不是时间**:时间估算依赖历史平均耗时,冷启动无数据、任务间耗时差异又大;
任务数比例是精确的、每 8s 刷新一次、不依赖任何预测模型。

> **实现要点**:长尾窗口的进入不是状态变化事件,后台 refresher 在 `_running` 分支里
> 每轮都会把该队列加入待调度集合(见 `_refresh_running_once`)。`_try_claim_next`
> 内部的完整条件保证重复触发幂等。

### 1.2 「占用槽位」的状态

占用队列槽位、会挡住后续任务的状态有**两个**:

```python
OCCUPYING_STATUSES = ("preparing", "running")
```

**`preparing` 必须一起算**:任务在准备阶段(OBS 下载、打包、统计任务数)还没变成 `running`,
若此刻只认 `running`,同队列的第二个任务会被同时放行,**串行就失效了**。

> 这是一个实际踩过的坑:早期实现只检查 `running`,导致 prepare 期间(可能好几分钟)两个任务并跑。

### 1.3 任务状态流转

```
                    ┌─────────────┐
   创建任务 ────────►│   created   │◄────────── 停止队列
   (选队列)         └──────┬──────┘
                           │ 启动(队列空闲)
                           ▼
                    ┌─────────────┐
                    │  preparing  │  ← 占槽位(OBS下载/统计任务数)
                    └──────┬──────┘
                           │ 准备完成,拉起 hive.py
                           ▼
                    ┌─────────────┐
                    │   running   │  ← 占槽位
                    └──────┬──────┘
                           │ 进程结束/completed.jsonl 达标
                           ▼
        ┌──────────────────────────────┐
        │ completed (无失败) / finished │
        │         (有失败)             │
        └──────────────────────────────┘

   排队状态:created ──启动时队列被占──► queued ──队首放行──► preparing ──► running
   停止:   任意状态 ──停止──► stopped(保留 queue_id,不再排队)
```

| 状态 | 含义 | 是否占队列槽位 | 是否排队候选 |
|------|------|:---:|:---:|
| `created` | 待启动 | ✗ | ✗ |
| `queued` | 排队中 | ✗ | ✅ |
| `preparing` | 准备中(下载/统计) | ✅ | ✗ |
| `running` | 运行中 | ✅ | ✗ |
| `completed` / `finished` | 已完成 / 已结束 | ✗ | ✗ |
| `stopped` | 已停止 | ✗ | ✗ |
| `failed` | 准备/启动失败（见 `error_summary`） | ✗ | ✗ |

### 1.4 准备失败 = `failed`,而不是退回 `created`

准备工作(OBS 下载用户配置、下载代码仓、打包、统计任务数)失败时,**不能**把任务退回 `created`。

`created` 的语义是「用户还没点启动」。若失败也写 `created`,用户点了一次启动、看到状态变回「待启动」,
既不知道是配置错了(例如 `user_config_dir` 指向的 OBS 路径不存在),
也会误以为「没启动成功,再点一次」——于是反复点、反复失败。

三处失败写库点统一置 `failed`,并写 `error_summary`:

| 位置 | 触发 |
|---|---|
| `_prepare_and_launch` 准备阶段异常 | OBS 下载失败、打包失败等 |
| `_prepare_and_launch` 拉起 hive.py 异常 | `subprocess.Popen` 失败 |
| `recover_stuck_preparing` 看门狗 | preparing 超过 `PREPARE_TIMEOUT_SECONDS` |

保护条件不变,仍是 `AND status='preparing'`,避免覆盖用户刚设的 `stopped`。

**`failed` 与 `created` 的可启动性差异:**

| | `created` / `stopped` | `failed` |
|---|---|---|
| 单个「启动」 | ✅ | ✅ 允许手动重试(修复配置后) |
| 「启动队列」批量拉起 | ✅ | ❌ **不拉起** |

> 为什么不批量拉起 `failed`:「启动队列」是一次性批量操作,若把 `failed` 也算进去,
> 一条配置错误的任务会被反复拉起、反复失败,把整条队列堵死。
> 失败必须由用户看过 `error_summary`、修好配置后**手动**重试。
>
> 为此 `GET /api/queues` 单独返回 `failed_tasks_in_queue`,前端在队列列表上标注失败数,
> 否则失败任务在队列里会「隐身」(它既不占槽位、也不排队、也不可批量启动)。

重新启动时会清空 `error_summary`,避免上一次的失败原因挂在新一次尝试上。

> 例外:`recover_orphan_preparing`(启动时回收「后端重启导致协程消失」的孤儿)仍退回 `created`。
> 那不是任务配置的问题,而是后端重启打断了准备,重启动即可,不该判为失败。

### 1.4 启动 / 停止的语义

| 操作 | 行为 |
|------|------|
| **启动**(单个) | 检查队列是否有任务占槽位:有 → 转 `queued` 排队;无 → 直接 `preparing` 启动。<br>**同时把该任务重新排到队尾**(`queue_position` 重算为 `MAX+1`) |
| **启动队列** | 把队列内 `created` / `stopped` 的任务**全部**转 `queued`,交给调度器按位置依次放行 |
| **停止**(单个) | 杀掉进程,状态转 `stopped`;**保留 `queue_id`**(保住队列归属,下次启动不必重选队列),但不再占用槽位、不参与排队 |
| **停止队列** | 把队列内 `queued` 的任务重置为 `created`;正在运行的任务**不受影响** |
| **重试失败** | 单独点「启动」即可;成功后 `error_summary` 清空 |

> **停止为何保留 `queue_id`**:`stopped` 状态既不占槽位也不是排队候选,所以不会挡住后面的任务;
> 同时保留归属,用户下次点「启动」即可重新入队,不用重选队列。

---

## 二、调度流程

### 2.1 事件驱动 + 启动兜底

调度**不是轮询**,而是在任务状态变化时主动触发;另有启动时兜底与后台刷新两条补充路径。

**触发点全景:**

| # | 触发时机 | 位置 |
|---|---------|------|
| 1 | 单个任务启动成功 | `api/routers/instances.py` `_prepare_and_launch` 启动成功分支 |
| 2 | 准备阶段失败 | 同上,失败分支(否则整个队列卡死) |
| 3 | 拉起进程失败 | 同上,失败分支 |
| 4 | 停止任务 | `stop_instance` 末尾(腾出槽位,放行下一个) |
| 5 | **任务跑完** | `background_cache_refresher` 每 8s 跑 `_refresh_running_once`,发现 `running/preparing → completed/finished` 时上报队列 id |
| 6 | 启动队列 | `POST /api/queues/{id}/start-all` |
| 7 | **后端重启兜底** | `main.py` startup 调 `resume_all_queues()` |

### 2.2 `schedule_queue` 核心流程

```
schedule_queue(queue_id)
   │
   ├─ 1. _try_claim_next(queue_id)   ← 单条 SQL,原子
   │      条件:队首 queued 存在 且 队列内无 preparing/running
   │      动作:该任务 status → preparing, RETURNING *
   │      抢不到 → 直接 return(队列被占 / 已空)
   │
   └─ 2. 交给 _prepare_and_launch(task_id, row)
          走与「单个启动」完全相同的真实路径
          (OBS 下载 / 统计任务数 / 记录 pid / 队列续跑)
          协程挂 done callback:内部异常会被记录,并把实例置 failed 释放槽位
```

### 2.3 原子抢占(为什么必须写一条 SQL)

生产是 `uvicorn --workers 4`,**多个 worker 会同时调度同一个队列**。

如果拆成「先查占用 → 再选任务 → 再 UPDATE」(各自独立语句),两个 worker 会同时通过占用检查,
然后**各自领走一个不同任务**,串行被破坏。实测可复现:4 进程并发时曾同时放行 3 个任务。

因此把「队列无占用者」写成 UPDATE 的 `WHERE` 条件,由 SQLite 的写锁保证原子性,
只有真正命中的那个调用才拿到任务:

```sql
UPDATE task_instances
   SET status = 'preparing'
 WHERE id = (
     SELECT id FROM task_instances
      WHERE queue_id = ? AND status = 'queued'
      ORDER BY queue_position ASC
      LIMIT 1
 )
   AND NOT EXISTS (
     SELECT 1 FROM task_instances
      WHERE queue_id = ? AND status IN ('preparing','running')
   )
RETURNING *
```

### 2.4 后端重启兜底(`resume_all_queues`)

调度是事件驱动的。若后端在**整条队列都处于 `queued`、没有任何 `running/preparing` 兄弟任务**时重启,
就没有任何事件会再触发调度 —— 后台 refresher 只扫 `running/preparing/终态` 行,看不到这些 `queued` 任务,
它们会被**永久搁死**。

所以在 `main.py` 的 startup 里,对所有「含排队任务的队列」兜底调度一次。

### 2.5 完成任务检测

任务完成由后台 refresher 判定,不依赖进程自己上报:

```python
# api/routers/instances.py
background_cache_refresher(interval=8.0)      # 每 8s 一轮
   └─ _refresh_running_once()                 # 返回「刚跑完」的 queue_id 集合
         ├─ 数 outputs/config/complete.jsonl 与 failed.jsonl 的行数
         ├─ pid 已死 或 已完成数 >= total_tasks  →  判终态
         └─ 记录 prev_status,若 running/preparing → 终态 且 有 queue_id,加入待调度集合
```

**注意 pid 判定**:`os.kill(pid, 0)` 对**僵尸进程**(已退出、父进程未回收)会返回成功,
导致任务被误判为「还在运行」而**永远卡在 running**,同队列后续任务全部堵死。
实现里额外读 `/proc/<pid>/stat` 的状态字段,`Z`(zombie)视为已结束 —— 见 `_is_pid_running`。

### 2.6 停止中断准备(状态保护)

`_prepare_and_launch` 是挂事件循环的**独立协程**,与 HTTP 请求的生命周期无关。
用户在 `preparing` 阶段点「停止」后,协程仍在跑,最后会写库转 `running`。

因此**三处写库全部带 `AND status='preparing'` 保护**:

| 写库点 | 保护后的语义 |
|---|---|
| 准备成功 → `running` | 若期间已被停止(`rowcount=0`),**杀掉刚拉起的进程组**并放弃,不触发调度 |
| 准备失败 → `failed` | 不覆盖用户刚设的 `stopped` |
| 拉起进程失败 → `failed` | 同上 |

> 另有一处 SELECT 复查(拉起进程前重新读一次状态),不属于写库点。
>
> 终态写入同样带保护:`_sync_instance_status` 落库时带 `status IN ('running','preparing')`,
> 避免 refresher 读到快照后用户已停止、或调度器已把下一个任务领成 `preparing` 时把它覆盖掉。
> `stop_instance` 也改为 `status NOT IN ('completed','finished')` 的条件更新。

没有这层保护会出现:停止后状态被写回 `running`,与已放行的下一个任务**同时占槽位**,串行被破坏。

### 2.7 卡住的 preparing 看门狗(`recover_stuck_preparing`)

`preparing` 期间不做状态同步(`_sync_instance_status` 对该状态直接返回),所以它**没有自然的退出路径**。
若协程因 worker reload / 事件循环异常而消失,实例会永久占住队列槽位,整条队列堵死。

后台 refresher 每轮(8s)额外跑一次回收:

```
status='preparing' AND pid IS NULL
  AND COALESCE(prepare_started_at, created_at) 超过 PREPARE_TIMEOUT_SECONDS（julianday 比较）
    → status='failed' + error_summary，并触发同队列调度
```

阈值默认 1800s(下载 600s×2 + 打包 120s + 余量),可用 `PREPARE_TIMEOUT_SECONDS` 覆盖。

> **时间基准**:进入 `preparing` 的语句会写入 `prepare_started_at = datetime('now')`(UTC),
> 看门狗用 `julianday()` 与之比较,两端基准一致。
> **不能**用 `created_at`:它是任务创建时间,且由 Python 以本地时间(`datetime.now().isoformat()`)
> 写入,而 SQLite 的 `datetime('now')` 是 UTC —— 结果是「排队超过约 8 小时的任务一进 preparing
> 就被判失败,真正卡住的实例却要等 8 小时以上」。旧数据没有 `prepare_started_at` 时才回退到
> `created_at`。比较必须用 `julianday()`,不能直接比字符串:应用写入的 `'...T...'` 与 SQLite 的
> `'... ...'` 在 `T`(0x54) > 空格(0x20) 下同一天永远不成立。

> **配套约束**:进入 `preparing` 时必须同时清空 `pid`/`started_at`(`_try_claim_next` 与
> `start_instance` 均已如此)。否则「跑过—被停止—重新启动」的实例会带着上一次的旧
> `started_at`,既不满足 `recover_orphan_preparing` 的条件,也干扰看门狗的时间判断。

> ⚠️ **已知偏差**:`recover_orphan_preparing`(startup 时跑一次)的条件是
> `preparing AND pid IS NULL AND started_at IS NULL`,没有时间阈值,而这正是「刚进入 preparing、
> 还没拉起进程」的样子。`--workers 4` 下若某个 worker 晚于其它 worker 启动(reload、扩容),
> 它会把其它 worker 正在 OBS 下载的实例打回 `created`。文档原先「多 worker 各调用一次也安全」
> 只在所有 worker 同时启动时成立。

---

## 三、数据结构

### 3.1 队列表 `task_queues`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 队列 ID(用户输入,全局唯一,即队列名) |
| `name` | TEXT | 显示名(当前与 id 相同) |
| `max_concurrent` | INTEGER | 本队列并发上限,默认 1(仍受全局 `QUEUE_MAX_CONCURRENT` 封顶) |
| `tail_skip_enabled` | INTEGER | 长尾跳过开关(0/1),默认 0 |
| `tail_skip_percent` | INTEGER | 剩余任务数百分比阈值,默认 10(1–50) |
| `description` | TEXT | 描述 |
| `created_by` | TEXT | 创建者 |
| `created_at` / `updated_at` | TIMESTAMP | 时间戳 |

### 3.2 `task_instances` 扩展字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `queue_id` | TEXT | 所属队列;NULL = 未入队 |
| `queue_position` | INTEGER | 入队序号,`MAX+1` 递增,**只增不减(会有空洞)** |
| `prepare_started_at` | TIMESTAMP | 进入 `preparing` 的时刻(UTC,SQLite `datetime('now')` 写入);看门狗据此判超时 |

索引:`idx_task_queue (queue_id, queue_position)`

> ⚠️ **`queue_position` 不是「队伍里排第几」**,而是只增不减的序号。
> 因为「启动」会重新入队(+1),反复重启后会出现 `1,2,3,7,8,9` 这样的空洞。
> **UI 展示「排队名次」时不能直接用它**,必须按同队列 `queued` 任务重新计算名次。

### 3.3 状态枚举

`created` / `queued` / `preparing` / `running` / `completed` / `finished` / `stopped` / `failed`

---

## 四、API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/queues` | 队列列表(含 `active_tasks` / `queued_tasks` / `startable_tasks` / `failed_tasks_in_queue` / `task_count`) |
| GET | `/api/queues/check-id/{id}` | 队列名去重校验,返回 `{available: bool}` |
| POST | `/api/queues` | 新建队列(重名返回 400),可带 `tail_skip_enabled` / `tail_skip_percent` |
| PATCH | `/api/queues/{id}` | 编辑队列设置(并发量/描述/长尾跳过),立即生效 |
| GET | `/api/queues/{id}/tasks` | 队列详情:按 `queue_position` 排序,含**排队名次**与预计开始时间 |
| POST | `/api/queues/{id}/start-all` | 启动队列:`created`/`stopped` → `queued` |
| POST | `/api/queues/{id}/stop-all` | 停止队列:`queued` → `created`(运行中的不动) |
| POST | `/api/queues/{id}/cancel-all` | (保留兼容)`queued` → `stopped` |
| DELETE | `/api/queues/{id}` | 删除队列,**仅允许删除空队列** |

任务实例接口新增:`POST /api/instances` 支持 `queue_id`;`GET /api/instances` 返回 `queue_id` / `queue_position`。

### 4.1 任务详情导出

**队列管理页**(`frontend/src/views/Queues.vue`)是**队列列表**与**任务列表**两个并列面板,
互不是从属关系:

- 队列列表:管理队列(新建/启动/停止/编辑/删除),即原有功能。
- 任务列表:就是「任务管理」的任务列表(`GET /api/instances`),放这里主要用于**筛选与批量导出** ——
  避免在任务管理页本身堆导出入口。

顶部概览条:队列数、**所有队列正在运行的任务数汇总**、**排队中的任务数汇总**、任务总数。

导出入口(都走 `api/routers/task_export.py`,输出 `.xlsx`):

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/export/queues.xlsx` | 队列级汇总,body `{queue_ids: [...]}`;**空数组 = 全部队列**。页面入口是队列列表里的「导出选中队列」 |
| POST | `/api/export/instances.xlsx` | 按 id 集合导出任务详情(POST 避免 URL 过长),body `{instance_ids: [...], include_task_records: bool}`。页面入口是任务列表里的「导出选中任务」 |
| POST | `/api/export/all.xlsx` | 全部队列 + 全部任务;超过 200 条截断,概览页写明截断数(接口保留,页面暂无按钮) |
| GET | `/api/export/queues/{id}.xlsx` | 导出单条队列的任务详情,按 `queue_position` 升序。页面入口是队列行「更多 → 导出任务详情」 |

单次上限 200 个任务(「导出全部」超限时截断而非报错)。权限为 `require_operator`
(导出含创建参数)。

Sheet 结构:

| Sheet | 内容 |
|-------|------|
| 导出概览 | 导出时间/范围/任务数/合计成功失败/整体成功率/数据口径/安全说明 |
| 队列汇总 | 一行一队列:任务总数/运行中/排队中/已完成/成功任务数/失败任务数/成功率/并发/长尾跳过 |
| 队列信息 | 仅单队列入口:队列ID、并发上限、有效并发、长尾跳过、描述、任务总数 |
| 任务总览 | 一行一任务,**成功数/失败数** + 成功率等(末行合计) |
| 创建参数-关键项 | 列顺序对齐「任务管理 → 配置信息 → 创建参数」卡片的五个分组(基本/OBS/模型/用户模拟/高级) |
| 任务明细 | 可选,来自 `task_records`;超过 2 万行只留提示行 |

口径与边界:

- 关键项复用 `api/services/create_params.build_create_params`,与详情页是**同一份实现**。
- 成功/失败数取 `task_instances.completed_tasks` / `failed_tasks`(即 output 目录
  `complete.jsonl` / `failed.jsonl` 行数),与 Dashboard 列同源。`total_tasks` 是启动前估算,
  实际 complete+failed 可能超出它,「未完成数」钳到 0,成功率按 `成功 ÷ (成功+失败)` 计算。
- **敏感值只在后端脱敏**:`mask_secret` 处理 API Key(长度 ≤ 8 一律整体打码,比前端
  `maskKey` 更严格)。
- 历史实例无 `create_params` → 关键项 Sheet 整行标「—」,不中断整包。
- 计数按只读路径刷新(`_sync_instance_status(persist=False)`),**不写库**,避免
  read 路径争用 SQLite 写锁。

---

## 五、配置项

`.env` / `.env.example`:

```bash
# 全局沙箱上限已取消（原 MAX_TOTAL_SANDBOXES 已移除）：
# 队列放行只取决于「本队列内是否已有 preparing/running」，不同队列可任意并行。

# preparing 超时(秒)：超过此时间仍未拉到进程的准备中实例判定为卡住，标记 failed 并放行下一个
PREPARE_TIMEOUT_SECONDS=1800

# 后端启动时回收「准备中被重启中断」的孤儿实例的宽限期(秒)。
# 必须 > 0：多 worker 晚启动时不能误伤其它 worker 正在准备的实例。
ORPHAN_PREPARING_GRACE_SECONDS=120

# 空跑模式(仅调试队列用,生产务必关闭)
MOCK_TASK_RUN=0          # 1 = 用「sleep N 秒后写 complete.jsonl」替代真实 hive.py
MOCK_TASK_SECONDS=10
```

> ⚠️ **改 `.env` 必须重启后端**:`config.py` 在 import 时读取,而 `uvicorn --reload`
> 只监听 `.py` 文件,**不会**加载 `.env` 的改动。

---

## 六、涉及文件清单

### 后端

| 文件 | 作用 |
|------|------|
| `api/services/queue_scheduler.py` | **核心调度器**:`schedule_queue` / `_try_claim_next` / `find_schedulable_queues` / `recover_stuck_preparing` / `resume_all_queues` / `OCCUPYING_STATUSES` |
| `api/routers/queues.py` | 队列 CRUD + 启动/停止/取消队列 |
| `api/routers/instances.py` | 创建/启动/停止任务时的队列逻辑;`_prepare_and_launch`;`_refresh_running_once`;`_is_pid_running`;`background_cache_refresher` |
| `api/models/instance.py` | `QueueInfo` / `QueueCreate` / `QueueTaskItem`;`InstanceCreate.queue_id` |
| `api/routers/task_export.py` | 任务详情导出两个端点(`/api/export/instances.xlsx`、`/api/export/queues/{id}.xlsx`) |
| `api/services/task_export.py` | 导出数据收集、敏感值脱敏、Excel 多 Sheet 组装 |
| `api/services/create_params.py` | `build_create_params`:详情页与导出共用的创建参数组装 |
| `api/core/database.py` | `task_queues` 建表 + 迁移(加列,含 `prepare_started_at`) |
| `api/core/config.py` | `PREPARE_TIMEOUT_SECONDS` / `ORPHAN_PREPARING_GRACE_SECONDS` / `MOCK_TASK_RUN` / `MOCK_TASK_SECONDS` |
| `main.py` | 注册 queues router;startup 调 `resume_all_queues()` |
| `test/test_queue_scheduler.py` | 调度器自动化用例 |
| `test/test_task_export.py` | 导出用例:脱敏、`build_create_params` 回填、Sheet 结构 |

### 前端

| 文件 | 作用 |
|------|------|
| `frontend/src/components/QueuePicker.vue` | **加入队列窗口**:选择/新建队列、筛选、队列详情 |
| `frontend/src/views/Queues.vue` | 队列管理页:**队列列表**(列表/分页/筛选/启动/停止/删除)与**任务列表**(任务管理同源,用于筛选与导出)两个并列面板 + 导出操作区 |
| `frontend/src/utils/download.js` | `downloadBlob`:带 Authorization 的二进制下载(覆盖 120s 超时) |
| `frontend/src/views/Dashboard.vue` | 任务实例列表:队列筛选、队列列、**排队名次**列、排队中状态 |
| `frontend/src/views/CreateInstance.vue` | 高级配置:任务队列选择 + 新建队列入口 |
| `frontend/src/views/InstanceDetail.vue` | 队列信息 tab:所属队列、名次、前方等待数、预计开始时间 |
| `frontend/src/views/Layout.vue` / `router/index.js` | 队列管理页入口与路由 |

---

## 七、界面行为

| 位置 | 行为 |
|------|------|
| **创建任务 → 高级配置** | 「任务队列」下拉 + 「新建队列」按钮(打开加入队列窗口) |
| **加入队列窗口** | 两个 tab:选择已有队列(带筛选/队列详情)/ 新建队列(名称去重校验、并发量、长尾跳过、描述) |
| **队列管理页** | 「长尾跳过」列显示 `剩余 10%` 或「关闭」;行操作「更多 → 编辑设置」可随时改开关与阈值 |
| **任务实例列表** | 队列筛选;「任务队列」「队列位置」列;`queued` 显示为**排队中** |
| **列表排序** | 按 `created_at` **倒序**,最新创建的任务在最前 |
| **排队名次** | 显示为该队列内 `queued` 任务的**第几名**(如 `1 (等待中)`),非绝对 `queue_position` |
| **操作** | 启动(排队中则显示「启动(重新加入排队)」)/ 停止(排队中则显示「停止(退出队列)」) |

---

## 八、已知限制

> **TODO(未来扩展)**:`max_concurrent` 仍不参与调度。若要支持 >2 的并行度,
> 需把 `_try_claim_next` 中的常量 `2` 换成 `max(max_concurrent, 2)`,
> 并为多槽位设计更细的放行条件(当前「剩余比例」只适合 2 槽位)。

1. **队列并发受全局封顶**:实际并发 = `min(QUEUE_MAX_CONCURRENT, 队列 max_concurrent)`。
   队列可以把 `max_concurrent` 调小,但无法突破全局值。长尾跳过在此基础上再 +1。
2. **`queue_position` 有空洞**:只增不减,反复重启会跳号。UI 已改用「名次」规避,但原始序号仍不整洁。
3. **并发量仅展示**:队列列表的「当前运行/并发量」中,`max_concurrent` 是用户填的展示值。
4. **预估开始时间仅供参考**:用队列内**正常跑完**(`completed`/`finished`)实例的平均耗时顺推,
   已扣除当前占用者的已运行时长;没有历史数据时不返回预计时间(`null`)。
   被 `stop` 中断的实例不计入平均,`preparing` 阶段的下载耗时也未单独建模。
5. **删除限制**:仅允许删除空队列(无任何任务关联)。

---

## 九、验证要点

自动化用例位于 `test/test_queue_scheduler.py`(unittest,隔离临时 DB,**不碰生产 platform.db**):

```bash
cd platform && python -m unittest discover -s test -p "test_queue_scheduler.py" -v
```

| 场景 | 用例 | 结论 |
|------|------|------|
| 串行阻塞 | `ClaimTest.test_claim_blocked_by_running` | 有 running 时排队任务不放行 |
| `preparing` 占位 | `ClaimTest.test_claim_blocked_by_preparing` | preparing 期间不放行下一个 |
| 入队顺序 | `ClaimTest.test_claim_order_by_position` | 按 `queue_position` 升序放行 |
| 抢占清理 | `ClaimTest.test_claim_clears_stale_fields` | 转 preparing 时清空 pid/started_at,并写 `prepare_started_at` |
| 多进程并发 | `ClaimTest.test_concurrent_claimers_only_one_wins` | 4 线程同时调度,只放行 1 个 |
| 无全局上限 | `ScheduleQueueTest.test_schedule_ignores_concurrent_num` | `concurrent_num` 再大也照常放行 |
| 队列间并行 | `ScheduleQueueTest.test_schedule_cross_queue_parallel` | q1 在跑不影响 q2 放行 |
| 调度兜底扫描 | `SchedulableScanTest.*` | 有 queued 且无占用者的队列被找出;有占用者/无 queued 不选 |
| 看门狗时间基准 | `WatchdogTest.*` | 排队 10h 后进入 preparing 不被误杀;真卡住 2000s 判 `failed`;有 pid 不动 |
| 孤儿回收宽限 | `OrphanRecoveryTest.*` | 刚进入 preparing 的不被误伤;超过宽限期才退回 `created` |
| 重启恢复 | `ResumeTest.*` | 只有含 queued 任务的队列被兜底调度 |
| 终态写库保护 | `StatusWriteGuardTest.*` | 终态落库不覆盖 `stopped` |
| 长尾跳过-关闭 | `TailSkipTest.test_disabled_keeps_strict_serial` | 即使剩余 0 也不放行 |
| 长尾窗口-窗口外 | `TailSkipTest.test_not_in_tail_window` | 并发 100/阈值 5,剩余 6 不放行 |
| 长尾窗口-窗口内 | `TailSkipTest.test_in_tail_window` | 剩余 4 放行 |
| 长尾窗口-严格小于 | `TailSkipTest.test_strictly_less_than` | 剩余正好 5 不放行 |
| 长尾窗口-阈值生效 | `TailSkipTest.test_same_progress_differs_by_percent` | 同一进度 5% 不放行、10% 放行 |
| 长尾窗口-失败计数 | `TailSkipTest.test_failed_tasks_count_toward_progress` | `failed` 也计入已处理 |
| 长尾窗口-分母是并发量 | `TailSkipTest.test_denominator_is_concurrency_not_total` | 总量 10000/剩余 500 不放行(旧口径会放行) |
| 长尾窗口-与总量无关 | `TailSkipTest.test_large_total_small_remainder` | 总量 10000/剩余 4 仍放行 |
| 长尾窗口-小并发兜底 | `TailSkipTest.test_small_concurrency_floor_one` / `..._triggers_at_zero_remaining` | 并发 10/阈值 5 → 剩余 0 才触发 |
| 长尾窗口-并发异常 | `TailSkipTest.test_zero_concurrency_treated_as_one` | 并发 0 按 1 处理 |
| 队列并发=2 | `TailSkipTest.test_queue_max_concurrent_two_allows_two` / `..._blocks_third` | 队列 max_concurrent 决定基础并发 |
| 队列 2 + 长尾 1 | `TailSkipTest.test_tail_skip_adds_one_on_top_of_queue_max` | 窗口内第 3 个放行 |
| 峰值封顶 | `TailSkipTest.test_tail_skip_peak_blocked` | 第 4 个不放行 |
| 窗口外 | `TailSkipTest.test_tail_skip_without_window_uses_queue_max` | 只按队列并发放行 |
| 长尾关闭 | `TailSkipTest.test_tail_skip_disabled_ignores_window` | 窗口内也不额外放行 |
| 队列 1 + 长尾 1 | `TailSkipTest.test_queue_max_one_tail_skip_two` | 峰值 2 |
| 全局封顶队列值 | `TailSkipTest.test_global_cap_limits_queue_max` | 全局 1 时队列 5 也只能跑 1 个 |
| 全局不封顶长尾 | `TailSkipTest.test_global_cap_limits_tail_peak` | 全局 2 + 队列 2 + 长尾 1 = 3 |
| 全局 1 + 长尾 | `TailSkipTest.test_global_cap_one_blocks_even_in_window` | 基础 1，窗口内峰值 2 |
| 长尾回落 | `TailSkipTest.test_after_tail_task_finishes_falls_back` | 长尾任务结束后额度回落 |
| 未知总量 | `TailSkipTest.test_total_tasks_zero_does_not_trigger` | `total_tasks=0` 不触发 |
| 调度链 | `ScheduleQueueTailSkipTest.*` | `schedule_queue` 走同一套规则 |

仍未自动化:完成续跑(`background_cache_refresher` 的完整链路)、`stop_instance` 的进程终止、
`start_instance` 的并发启动(需 HTTP 层桩)、前端展示。

**回归提示**:把 `OCCUPYING_STATUSES` 改回 `("running",)`,上述串行相关用例会失败 4 条 ——
可用于确认测试有鉴别力(已实测)。
