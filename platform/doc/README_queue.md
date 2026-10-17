# 任务队列串联执行(Queue)设计与实现

本文描述 openclaw-hive platform 的**队列串联执行**功能:排队规则、调度流程、数据结构与涉及的全部代码位置。

> 设计稿:`platform/queque.png`
> 核心目标:多个任务实例归属同一个队列后,按加入顺序**依次串行执行**,前一个结束后一个自动开始。

---

## 一、排队规则

### 1.1 基本规则

| 规则 | 说明 |
|------|------|
| **队列内串行** | 同一队列同一时刻**只有一个任务在跑**,后面的任务排队等待 |
| **队列间并行** | 不同队列互不影响,各自独立调度 |
| **全局沙箱上限** | 所有运行中任务的沙箱总量不得超过 `MAX_TOTAL_SANDBOXES`(环境变量配置) |
| **顺序依据** | 队列内按 `queue_position` **升序**放行(数字越小越先跑) |
| **入队顺序** | 创建任务并选队列时,`queue_position = MAX(同队列 position) + 1`(排到队尾) |

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

### 1.4 启动 / 停止的语义

| 操作 | 行为 |
|------|------|
| **启动**(单个) | 检查队列是否有任务占槽位:有 → 转 `queued` 排队;无 → 直接 `preparing` 启动。<br>**同时把该任务重新排到队尾**(`queue_position` 重算为 `MAX+1`) |
| **启动队列** | 把队列内 `created` / `stopped` 的任务**全部**转 `queued`,交给调度器按位置依次放行 |
| **停止**(单个) | 杀掉进程,状态转 `stopped`;**保留 `queue_id`**(保住队列归属,下次启动不必重选队列),但不再占用槽位、不参与排队 |
| **停止队列** | 把队列内 `queued` 的任务重置为 `created`;正在运行的任务**不受影响** |

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
   ├─ 1. check_global_sandbox_capacity()
   │      可用 = MAX_TOTAL_SANDBOXES - SUM(concurrent_num of preparing/running)
   │
   ├─ 2. _try_claim_next(queue_id)   ← 单条 SQL,原子
   │      条件:队首 queued 存在 且 队列内无 preparing/running
   │      动作:该任务 status → preparing, RETURNING *
   │      抢不到 → 直接 return(队列被占 / 已空)
   │
   ├─ 3. 容量不足? 需要 > 可用 → 退回 queued,return
   │
   └─ 4. 交给 _prepare_and_launch(task_id, row)
          走与「单个启动」完全相同的真实路径
          (OBS 下载 / 统计任务数 / 记录 pid / 队列续跑)
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

---

## 三、数据结构

### 3.1 队列表 `task_queues`

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT PK | 队列 ID(用户输入,全局唯一,即队列名) |
| `name` | TEXT | 显示名(当前与 id 相同) |
| `max_concurrent` | INTEGER | 并发量,**默认 1,当前仅作展示**(调度仍严格串行) |
| `description` | TEXT | 描述 |
| `created_by` | TEXT | 创建者 |
| `created_at` / `updated_at` | TIMESTAMP | 时间戳 |

### 3.2 `task_instances` 扩展字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `queue_id` | TEXT | 所属队列;NULL = 未入队 |
| `queue_position` | INTEGER | 入队序号,`MAX+1` 递增,**只增不减(会有空洞)** |

索引:`idx_task_queue (queue_id, queue_position)`

> ⚠️ **`queue_position` 不是「队伍里排第几」**,而是只增不减的序号。
> 因为「启动」会重新入队(+1),反复重启后会出现 `1,2,3,7,8,9` 这样的空洞。
> **UI 展示「排队名次」时不能直接用它**,必须按同队列 `queued` 任务重新计算名次。

### 3.3 状态枚举

`created` / `queued` / `preparing` / `running` / `completed` / `finished` / `stopped`

---

## 四、API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/queues` | 队列列表(含 `active_tasks` / `queued_tasks` / `task_count`) |
| GET | `/api/queues/check-id/{id}` | 队列名去重校验,返回 `{available: bool}` |
| POST | `/api/queues` | 新建队列(重名返回 400) |
| GET | `/api/queues/{id}/tasks` | 队列详情:按 `queue_position` 排序,含**排队名次**与预计开始时间 |
| POST | `/api/queues/{id}/start-all` | 启动队列:`created`/`stopped` → `queued` |
| POST | `/api/queues/{id}/stop-all` | 停止队列:`queued` → `created`(运行中的不动) |
| POST | `/api/queues/{id}/cancel-all` | (保留兼容)`queued` → `stopped` |
| DELETE | `/api/queues/{id}` | 删除队列,**仅允许删除空队列** |

任务实例接口新增:`POST /api/instances` 支持 `queue_id`;`GET /api/instances` 返回 `queue_id` / `queue_position`。

---

## 五、配置项

`.env` / `.env.example`:

```bash
# 全局最大沙箱总数(队列调度资源检查)
MAX_TOTAL_SANDBOXES=1000

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
| `api/services/queue_scheduler.py` | **核心调度器**:`schedule_queue` / `_try_claim_next` / `check_global_sandbox_capacity` / `resume_all_queues` / `OCCUPYING_STATUSES` |
| `api/routers/queues.py` | 队列 CRUD + 启动/停止/取消队列 |
| `api/routers/instances.py` | 创建/启动/停止任务时的队列逻辑;`_prepare_and_launch`;`_refresh_running_once`;`_is_pid_running`;`background_cache_refresher` |
| `api/models/instance.py` | `QueueInfo` / `QueueCreate` / `QueueTaskItem`;`InstanceCreate.queue_id` |
| `api/core/database.py` | `task_queues` 建表 + 迁移(加列) |
| `api/core/config.py` | `MAX_TOTAL_SANDBOXES` / `MOCK_TASK_RUN` / `MOCK_TASK_SECONDS` |
| `main.py` | 注册 queues router;startup 调 `resume_all_queues()` |

### 前端

| 文件 | 作用 |
|------|------|
| `frontend/src/components/QueuePicker.vue` | **加入队列窗口**:选择/新建队列、筛选、队列详情 |
| `frontend/src/views/Queues.vue` | 队列管理页:列表、分页、筛选、启动/停止/删除队列 |
| `frontend/src/views/Dashboard.vue` | 任务实例列表:队列筛选、队列列、**排队名次**列、排队中状态 |
| `frontend/src/views/CreateInstance.vue` | 高级配置:任务队列选择 + 新建队列入口 |
| `frontend/src/views/InstanceDetail.vue` | 队列信息 tab:所属队列、名次、前方等待数、预计开始时间 |
| `frontend/src/views/Layout.vue` / `router/index.js` | 队列管理页入口与路由 |

---

## 七、界面行为

| 位置 | 行为 |
|------|------|
| **创建任务 → 高级配置** | 「任务队列」下拉 + 「新建队列」按钮(打开加入队列窗口) |
| **加入队列窗口** | 两个 tab:选择已有队列(带筛选/队列详情)/ 新建队列(名称去重校验、并发量、描述) |
| **任务实例列表** | 队列筛选;「任务队列」「队列位置」列;`queued` 显示为**排队中** |
| **列表排序** | 按 `created_at` **倒序**,最新创建的任务在最前 |
| **排队名次** | 显示为该队列内 `queued` 任务的**第几名**(如 `1 (等待中)`),非绝对 `queue_position` |
| **操作** | 启动(排队中则显示「启动(重新加入排队)」)/ 停止(排队中则显示「停止(退出队列)」) |

---

## 八、已知限制

> **TODO(未来扩展)**:下述第 1、3 条为同一件事 —— 让 `max_concurrent` 真正参与调度
> (多槽位放行、容量按队列粒度核算)。实现时需同步更新本文「一、排队规则」与「二、调度流程」
> 两章:`_try_claim_next` 的 `NOT EXISTS` 条件要从「无占用者」改为「占用数 < max_concurrent」,
> 且需考虑多 worker 下按队列批量抢占的原子性。

1. **仅串行**:`max_concurrent` 字段已存在但**不参与调度**,填 >1 也仍是串行。多槽位调度未实现。
2. **`queue_position` 有空洞**:只增不减,反复重启会跳号。UI 已改用「名次」规避,但原始序号仍不整洁。
3. **并发量仅展示**:队列列表的「当前运行/并发量」中,`max_concurrent` 是用户填的展示值。
4. **预估开始时间粗略**:用队列内已完成任务的**平均耗时**顺推,没有历史数据时不给预计时间。
5. **删除限制**:仅允许删除空队列(无任何任务关联)。

---

## 九、验证要点

已覆盖的验证场景(均为隔离临时 DB + 启动桩/空跑):

| 场景 | 结论 |
|------|------|
| 串行阻塞 | 有 running/preparing 时排队任务不放行 |
| 完成续跑 | A 完成后只放行 B,C/D 仍排队 |
| `preparing` 占位 | preparing 期间不放行下一个 |
| 全局容量 | 可用 = 上限 − Σ(preparing+running 的 concurrent_num) |
| 多进程并发 | 4 进程同时调度,仍只放行 1 个、不重复启动 |
| 重启恢复 | 整队 queued 时重启,任务被正确拾起 |
| 停止/启动语义 | 停止退出排队并让出槽位;启动重新排到队尾 |
| 排队名次 | 位置有空洞时正确显示 1,2,3 |

**回归提示**:只需把 `OCCUPYING_STATUSES` 改回 `("running",)`,串行相关用例即失败 —— 可用于确认测试有鉴别力。
