# 失败类型分级治理(Error Policy / Throttle)设计与实现

本文描述 hive 的**失败分类与自适应并发**机制:错误码按「该怎么办」分级、瞬时失败重试、
集群容量不足时自动降并发/熔断,以及涉及的全部代码位置。

> 核心目标:详情页「配置信息 → 任务执行情况」目前只告诉用户**错了多少**,
> 不告诉用户**该怎么办**。而实测数据表明:不同错误码的正确处置方式**完全相反** ——
> 下载抖动该重试,集群容量不足重试只会加剧,配置写错重试一万次也没用。
>
> 当前状态:分类元数据与并发控制器已落地并通过单测;`hive.py` 的接入**尚未开始**
> (见第七节「落地状态与开关设计」)。

---

## 一、为什么要分级:数据证据

数据来源:`platform/platform.db` + `platform/database/task_data.db`,共 **1063 万条**
`task_records`。全量异常率 **39.3%**(4,177,552 / 10,634,909),近 7 天恶化到 **47.7%**。

### 1.1 C003 与并发强相关

| 并发档 | 任务数 | C003 率 | 成功率 |
|---|---|---|---|
| <100 | 132,784 | 1.28% | 50.75% |
| 100–499 | 1,624,616 | 2.32% | 67.38% |
| 500–999 | 4,119,207 | 3.98% | 63.49% |
| **1000+** | 4,748,164 | **6.12%** | 56.45% |

并发从 <100 升到 1000+,C003 率涨了近 **5 倍**,且单调上升。

### 1.2 C003 与 S008 的「形状」相反 —— 这是分级的根本依据

近 7 天 >200 任务的实例,按该实例的失败率分档:

| 失败率档 | C003 实例数 / 损失任务 | S008 实例数 / 损失任务 |
|---|---|---|
| 70–100% | 11 / 61,773 | 8 / 21,738 |
| 40–70% | 14 / 34,462 | 14 / 76,475 |
| 15–40% | 11 / 35,362 | 70 / 109,601 |
| 5–15% | 6 / 6,242 | 68 / 45,286 |
| <5% | 469 / 4,557 | 351 / 14,838 |

- **C003 是「整实例全军覆没」**:仅 25 个实例(失败率 >40%)吃掉 96,235 个任务。
  最坏个例 `2611022050-...-aa0a` **1688/1688 = 100%**、`2610211021-...-a6a9` 9467/10000 = 94.7%。
- **S008 是「逐任务方差」**:损失平铺在各档位(<5% 档就有 351 个实例)。

**结论**:C003 是**实例级系统性事件**(该降并发/熔断),S008 是**任务级随机损耗**
(降并发没用)。两者必须分开处置。

### 1.3 C003 按天脉冲式爆发

| 日期 | C003 率 | 成功率 |
|---|---|---|
| 2026-10-21 | 37.1% | 48.7% |
| 2026-10-24 | **0.0%** | 63.6% |
| 2026-10-28 | 16.9% | 51.7% |
| 2026-11-01 | **0.0%** | 48.5% |
| 2026-11-04 | 18.7% | **28.7%** |

0.0% ↔ 37.1% 来回跳 —— 这是**集群侧容量事件**,不是代码缺陷。
所以**运行时自适应**比静态调参有效:静态值没法同时适配 0% 和 37% 两种天气。

### 1.4 一个 C003 底下藏着三种原因,处置相反

从 `/mnt/sfs_turbo/.../instances/*/outputs/*/logs/task-*.log` 实采:

| detail 文本 | 含义 | 实测频次 | 该怎么办 |
|---|---|---|---|
| `no_cluster_matches_request` | 集群无可调度资源 | 压倒性多数(单实例 28,401 次) | **降并发/熔断** |
| `[Errno -3] Temporary failure in name resolution` | DNS/控制面不可达 | 单实例 7,617 次 | **重试+退避** |
| `Sandbox is running` / `Sandbox pod sandbox network setup failed: PodInitializing` | 沙箱瞬时态 | 零散 | **重试+退避** |

> **这是降并发信号准确性的前提**:不做二次分类,DNS 抖动会被误判成容量不足而白降并发;
> 反之容量不足被误判成可重试,会把已经过载的集群继续打满。

`no_cluster_matches_request` 实测 **6.4s 就返回**(日志 `elapsed=6.4s`),失败极快,
几乎不消耗沙箱配额 —— 所以退避等待是安全的,窗口也能迅速填满让状态机及时反应。

### 1.5 S005 的 100% 实例是配置错误,不是抖动

实例 `2610311421-c008450182610311420-5648`:**10000/10000** 全 S005,日志显示
`daily-tech-broadcast`、`content-compliance`、`excel-analysis`、`diagram-builder`
—— **所有** skill 均 fetch 失败,即 `skill_dir` / OBS 路径配错。

这类必须**快速失败**并提示改配置,重试 10000 次只会放大伤害。

---

## 二、分类体系:按「该怎么办」而非按「来源」

`ERROR_CATALOG`([prepare_config.py:755](../prepare_config.py#L755))按**来源**分
C/S/T 前缀,一眼可辨故障出自哪一层。但来源相同的错,处置方式可能完全相反
(S005 下载抖动该重试,S003 镜像配错重试无意义)。

所以**另立一张按处置分类的表** `ERROR_POLICY`([prepare_config.py:807](../prepare_config.py#L807)),
两张表并存、各司其职:

| policy | 含义 | 动作 |
|---|---|---|
| `retry` | 瞬时故障,重试即可挽回 | 按 `max_retries` 退避重试 |
| `throttle` | 容量压力,重试只会加剧 | **不重试**,喂给 `ThrottleController` 降并发/熔断 |
| `fail_fast` | 配置/环境错误,重试无意义 | **不重试**,尽早让用户去改配置 |
| `task_side` | 任务侧正常损耗,非基础设施问题 | **不重试**,也**不该触发降并发** |

### 2.1 A 类:可重试(`retry`)

| 码 | 含义 | max_retries | 备注 |
|---|---|---|---|
| S009 | upload traj to OBS failed | 3 | **已实现**(见 2.5) |
| S005 | skill download failed | 3 | 整实例失败率 >80% 时转 C 类,见 2.6 |
| S006 | user profile download failed | 3 | 同上 |
| S007 | agents download failed | 3 | 同上 |
| C002 | upload file failed | 3 | |
| S004 | sandbox port-update failed | 3 | |
| T007 | TimeoutError | **1** | agent 已跑很久,重试成本高、收益低 |
| C003(DNS/PodInit) | 见 1.4 | 3 | 由 `classify_c003` 判定 |

### 2.2 B 类:应降并发/熔断(`throttle`)

| 码 | 含义 | 近 7 天 |
|---|---|---|
| C003(`no_cluster_matches_request`) | 集群无可调度资源 | 142,483 |
| S001 | gateway start failed | 4,824 |
| S002 | gateway startup timeout | 1,255 |

S001/S002 归此类的理由:gateway 起不来常是沙箱资源被挤占的征兆,是并发压力的伴生信号。

### 2.3 C 类:应快速失败(`fail_fast`)

| 码 | 含义 | 提示用户检查 |
|---|---|---|
| C001 | extract code failed | 代码包 |
| C004 | agent config write failed | agent 配置 |
| S003 | gateway unexpected output | harness / 镜像 |

### 2.4 D 类:任务侧正常损耗(`task_side`)

T001/T002/T003/T004/T005/T006/T008/T009/T010、X999,以及 **S008**。

> **S008 为什么归任务侧**:它虽是 S 前缀,但 `_run_main_script`
> ([hive.py:903-906](../hive.py#L903-L906))的语义是「非零退出且 stdout 里**没有**
> 可识别的业务失败线索」,分类优先级为 `具体T码 > T010 > S008` —— 即 S008 是
> **任务侧兜底**,不是沙箱故障。其损失平铺的形状(1.2 节)也印证了这点。
>
> 但它是近 7 天**第一大失败项**(268,589),所以虽不重试、不降并发,
> 前端仍应单独提示「建议排查主脚本/镜像」。

### 2.5 复用已有的重试实现

全仓**唯一**已存在的重试是 S009 OBS 上传([hive.py:1316-1356](../hive.py#L1316-L1356)):
3 次重试 + `random.uniform(0, 60)` 随机退避(注释写明「随机错开重试,均分 obsutil 流量」)。

这是**已在生产验证过**的模式,接入时应把它抽成通用
`_retry_with_backoff(fn, retries, label)`,S009 改为调用它,避免两套逻辑分叉。

> **重试的额外压力需要注意**:S005 重试 3 次意味着下载流量 ×3,大并发下可能反而打挂
> OBS。所以必须带抖动(沿用 S009 的 `random.uniform` 思路),不能同步重试。

### 2.6 待定:S005 转 fail_fast 的判定位置

`ERROR_POLICY` 里 S005/S006/S007 带了 `config_error_ratio: 0.8`,语义是
「整实例失败率 >80% 时判为配置错误」。但**判定位置尚未确定**:

| 方案 | 优点 | 缺点 |
|---|---|---|
| hive 进程内累计 | 不依赖平台,实时 | 每个 task 看不到全局,需在 `run_tasks` 层维护计数 |
| 平台侧判定 | 有全量 `task_records`,口径准 | 只能事后告警,救不回已经烧掉的任务 |

**倾向 hive 进程内**:`ThrottleController` 已经在维护滑动窗口,加一个
「同码连续失败计数」成本很低,且能在前几百个任务内就停下来。

---

## 三、自适应并发:为什么需要,以及为什么是这个形态

### 3.1 当前任务是如何 spawn 出来的

```
用户点「启动」 / 队列调度器放行
  └─ instances.start_instance (platform/api/routers/instances.py:1790)
       └─ 原子抢占队列槽位(UPDATE ... WHERE 槽位条件,保证同队列串行)
            └─ status: queued → preparing
                 └─ _prepare_instance(OBS 下载 configs、统计任务数)
                      └─ subprocess.Popen (instances.py:1960)   ★ 一个实例 = 一个 OS 进程
                           ["python", HIVE_ROOT/hive.py, "--config", config_path]
                           start_new_session=True   (独立进程组,便于 killpg)
                           └─ status: preparing → running,记录 proc.pid
```

进程内:

```
main() (hive.py:1845)
  └─ concurrent_num = run_cfg.concurrent_num     ★ 只在启动时读一次
  └─ asyncio.run(run_tasks(..., concurrent_num=concurrent_num))
       └─ run_tasks (hive.py:1648)
            ├─ task_queue = asyncio.Queue(),塞入 [task_start : task_start+task_num]
            └─ workers = [_worker(i, task_queue, config) for i in range(concurrent_num)]
               await asyncio.gather(*workers)    ★ 固定 N 个协程,一次性创建
                    └─ _worker (hive.py:1616) 循环:
                         task_queue.get_nowait() → 队空就 break 退出
                         └─ OpenClawDistillationTask(config)   ← 每任务新建对象
                              └─ task.run(config_file, task_idx) (hive.py:1434)
                                   ├─ await asyncio.sleep(random.uniform(1,10))  ← 已有启动错峰
                                   ├─ make(request)   → ★ 创建沙箱,失败即 C003
                                   └─ _execute_task(gateway → 主脚本 → 传 OBS)
```

### 3.2 三个关键事实

| 事实 | 含义 |
|---|---|
| **一个实例 = 一个 OS 进程** | 并发 2000 不是 2000 个进程,而是进程内 2000 个 asyncio 协程,每协程同时持 1 个沙箱。所以「有效并发」≈ 在飞沙箱数 |
| **`concurrent_num` 启动时读一次就固定** | 平台改数据库里的 `concurrent_num`,对**正在跑的**实例完全无效 |
| **YAML 每个任务都会被重读一次** | `OpenClawDistillationTask.__init__` 里 `load_yaml_config(self.config.run_config_file)`([hive.py:371](../hive.py#L371))在**每个任务**创建时执行 —— 这是现成的、无需新增 IPC 的**活控制通道** |

### 3.3 为什么不是「减少 worker 数量」

worker 协程在 `asyncio.gather` 里一次性创建,且 `_worker` 的退出条件是**队列为空**
(`QueueEmpty` → break)。中途砍协程要么得 `cancel` task(**会中断正在跑的任务、丢掉沙箱**),
要么重构整个 gather 结构。都不划算。

### 3.4 实际方案:worker 数不变,收紧「在飞沙箱数」

worker 协程数保持 `concurrent_num` 不变,在真正占用沙箱的那段外面加一道**闸门**。
降并发 = 缩小闸门许可数,多余的 worker 自然阻塞在闸门上等待,**不中断任何正在跑的任务**。

```python
async with throttle.slot():          # 闸门:降并发在这里生效
    result = await task.run(...)
throttle.record(result.status, result.error_code, result.detail)
```

> **为什么不用 `asyncio.Semaphore`**:标准 Semaphore **不支持动态改容量**
> (改 `_value` 是私有行为,且唤醒语义不对)。所以用 `asyncio.Condition` 守一对
> `(limit, in_flight)` 自己实现,见 [utils/throttle.py:61](../utils/throttle.py#L61)。

---

## 四、三档状态机

信号 = **滑动窗口内 `throttle` 类失败占比**。注意 C003 必须先经 `classify_c003`
按 detail 二次分类(见 1.4),否则 DNS 抖动会被误判。

| 状态 | 触发条件 | 动作 |
|---|---|---|
| `normal` | 占比 < `recover_ratio`(0.15)且连续成功 ≥ `recover_streak`(20) | 阶梯升回 ×1.25,**不超过配置并发** |
| `shrunk` | 占比 ≥ `shrink_ratio`(0.30) | `limit = max(floor, limit // 2)`,2000→1000→…→50,**带冷却期** |
| `paused` | 占比 ≥ `pause_ratio`(0.80) | `limit` 冻结,每 `probe_interval`(60s)**探测性放行 1 个** |

熔断这一档正是防住 `1688/1688 全损` 的关键:100% C003 的实例在当前实现下会一路烧完
全部任务,加上熔断后会在前几百个任务内停下来等集群恢复。

### 4.1 参数一览

全部可被 `config.yaml` 的 `run_config.throttle` 覆盖,默认值见
[utils/throttle.py](../utils/throttle.py#L44):

| 参数 | 默认 | 含义 |
|---|---|---|
| `window` | 200 | 滑动窗口:最近多少个任务结果参与判定 |
| `min_samples` | 30 | 样本不足时不做任何判定,避免开跑头几个任务就误降 |
| `shrink_ratio` | 0.30 | 降并发阈值 |
| `pause_ratio` | 0.80 | 熔断阈值 |
| `recover_ratio` | 0.15 | 升回阈值 |
| `floor` | 50 | 降并发下限(会被 `min(floor, concurrent_num)` 夹住,见 4.4) |
| `cooldown` | 120s | 两次降档之间的冷却,**防连续腰斩**(见 4.2) |
| `probe_interval` | 60s | 熔断期间探测放行的间隔 |
| `grow_factor` | 1.25 | 升回步长 |
| `recover_streak` | 20 | 需连续多少个非 throttle 结果才升回 |
| `enabled` | — | 设 `false` 时阈值置为不可达,等同于关掉这套机制 |

### 4.2 一次决策消耗掉它所依据的样本(重要)

降档的依据是「这一批样本里容量类失败占比过高」,档位降下来之后这批样本就**用过了**。
否则窗口里的旧失败会在后续每个 `record()` 上反复命中阈值 —— 实测 **20 个失败就能把
2000 一路腰斩到 floor(50)**。

所以 `_reconcile` 在每次真正做出决策后都会清空窗口
([`_consume_window`](../utils/throttle.py#L244))。

> `cooldown` 是**承重参数**,不是可选优化:它拦住了「清空窗口后立刻又攒满同样比例的
> 失败、再降一档」这条路。实测默认 120s 下,500 个任务持续 40% 容量失败只降一档
> (2000→1000),行为正确;而 `cooldown=0` 时会一路降到 floor。

### 4.3 熔断恢复回到「熔断前的一半」,不是回到 floor

并发 2000 的实例因一次集群抖动熔断后,若恢复时直接跳回 `floor`(50),
剩下几万个任务会以 **1/40** 的速度爬完 —— 比降并发本身的代价大得多。

所以 `_pause` 会记下 `_pre_pause_limit`,`_grow` 从熔断态恢复时回到
`max(floor, _pre_pause_limit // 2)`。取一半而不是原值:集群刚缓过来,
立刻打回满并发很容易再次打挂。

### 4.4 `floor` 不得高于配置并发

用户故意设 `concurrent_num=10` 时,默认 `floor=50` 不该把并发**反向抬到** 50。
构造时用 `max(1, min(floor, initial))` 夹住。

### 4.5 熔断档必须带超时等待(否则永久挂死)

熔断档的放行条件是**时间到了**(`probe_interval`),而 `asyncio.Condition.wait()`
**只在被 notify 时醒**。若无限等:探测任务跑完后 `release` 的 notify 会因
「间隔还没到」被 `_can_admit` 挡回去,之后**再没有任何事件能唤醒等待者** ——
实例就永久挂死。

所以 `acquire` 在熔断档用 `asyncio.wait_for(self._cond.wait(), timeout)` 轮询,
超时秒数由 [`_wait_timeout`](../utils/throttle.py#L169) 算出「距下次可探测还差多久」
(下限 0.01s 避免 busy-loop)。

---

## 五、两个控制入口

| 入口 | 机制 |
|---|---|
| **自动**(主路径) | 进程内状态机按第四节自调,无需人工干预 |
| **手动** | 用户在平台改并发 → 写回 `config.yaml` 的 `run_config.concurrent_num` → 下一个任务 `__init__` 重读 YAML([hive.py:371](../hive.py#L371))时拿到新值 → `throttle.set_ceiling(新值)` |

手动值作为**上界**:自动降并发仍可在其下继续收紧,否则用户一改配置就把熔断/降档的
保护抹掉了。且**调大时不直接放开闸门**(`set_ceiling` 只抬上界,不动 `limit`),
交给状态机按窗口逐步升回 —— 避免一键打满再次打挂集群。

> 不需要新的 IPC/信号,完全复用已有的「每任务重读配置」行为。

---

## 六、可观测性

| 产物 | 内容 |
|---|---|
| `main.log` | 每次 shrink/pause/grow 写一行,含当时占比、新 limit、原因。便于事后复盘「这批任务为什么只跑了 300 个」 |
| `outputs/<cfg>/_throttle.json` | `snapshot()` 的原子落盘(`os.replace`),含 `state` / `limit` / `ceiling` / `floor` / `in_flight` / `throttle_ratio`。供平台详情页显示「有效并发 500 / 配置 2000(集群容量不足已自动降档)」。写失败不影响主流程 |

前端侧规划(尚未实现):
- `error_tree` 节点([instances.py:2390-2405](../platform/api/routers/instances.py#L2390-L2405))
  每个 code 带 `policy` + `advice` 文案;
- 实例级聚合结论,如「本实例 94.7% 失败为集群容量不足,已自动降并发;建议降低并发后重跑失败任务」;
- [InstanceDetail.vue:105-111](../platform/frontend/src/views/InstanceDetail.vue#L105-L111)
  的 `breakdown-row` 加 policy 标签,沿用现有 `el-tag` + `error-tree` 折叠结构与配色约定
  (状态语义色、不靠色相单独承载含义)。

> 「重跑失败」已存在(继承 `complete.jsonl` 机制,
> [instances.py:1511-1519](../platform/api/routers/instances.py#L1511-L1519)),
> A 类占比高时在建议文案里引导用户用它,**不新做重跑入口**。

---

## 七、落地状态与开关设计

### 7.1 已完成

| 文件 | 内容 | 状态 |
|---|---|---|
| [prepare_config.py](../prepare_config.py#L786) | `ERROR_POLICY`、`classify_c003`、`error_policy`、`error_max_retries` | ✅ **纯新增 120 行,0 删除** |
| [utils/throttle.py](../utils/throttle.py) | `ThrottleController` + `build_from_config` | ✅ 新文件 |
| [test/test_error_policy.py](../test/test_error_policy.py) | 7 项,24 个错误码全覆盖 | ✅ 通过 |
| [test/test_throttle.py](../test/test_throttle.py) | 13 项 | ✅ 通过 |

**对运行中的任务零影响**,已验证三条:
1. 运行中进程持有的 `.py` 文件句柄数 = **0** —— Python 启动时已把模块编译进内存,之后不再读盘;
2. 全仓**没有任何** `importlib.reload` / `importlib.import_module` / `__import__`;
3. 新增符号在 `hive.py` 里**无人调用**,当前是死代码。

### 7.2 单测中发现并修掉的 3 个真 bug

这几个若直接上线都会出事:

| bug | 症状 | 修法 |
|---|---|---|
| 反复腰斩 | 同一批失败证据在每次 `record()` 上重复命中阈值,20 个失败就把 2000 降到 50 | 一次决策消耗掉其依据的样本(4.2) |
| 熔断恢复掉到 floor | 2000 并发因一次抖动熔断,恢复跳回 50,剩余任务以 1/40 速度爬完 | 回到熔断前的一半(4.3) |
| **熔断永久挂死** | 探测任务跑完后无任何事件能唤醒等待者,实例永久卡住 | 熔断档带超时轮询(4.5) |

### 7.3 未完成:`hive.py` 接入

**`hive.py` 一行未动** —— 当前有 **26 个进程在跑**(最老 25 小时)、**25 个实例排队待启动**。
排队的那些一旦放行就会用磁盘上的代码,这是真正的暴露面。

所以 `hive.py` 的改动必须包在**默认关闭的开关**后面。推荐**环境变量**方案:

```python
# hive.py 顶部
THROTTLE_ENABLED = os.environ.get("HIVE_THROTTLE", "").lower() in ("1", "true", "on")
```

| 开关状态 | 行为 |
|---|---|
| 关闭(默认) | `throttle = None`,`_worker` 走原来的裸调用路径,`make()` 不包重试 —— 行为与现在**逐字节一致** |
| 打开 | 平台启动 hive 时注入 `HIVE_THROTTLE=1` |

注入通道**已经存在**:`subprocess.Popen` 已在传 `env`
(见 [instances.py:1949](../platform/api/routers/instances.py#L1949) 的
`env["RLXF_CLEAN_LOG_PATH"]`),加一个键即可,不碰 config 模板。

灰度路径:平台加「启用自适应并发」勾选框 → 只对勾选的新实例注入 →
先拿 1 个 cc=10 实例跑通 → 再 cc=600 → 最后 cc=2000。

### 7.4 接入时必须真机验证的点

| # | 项 | 为什么 |
|---|---|---|
| 1 | **`task.run` 加返回值** | 它现在返回 `None`,`_worker` 没人接。改成返回 `(status, error_code, detail)` 后要确认 `except Exception` / `finally: task_queue.task_done()` 不受影响,且 `complete.jsonl` / `failed.jsonl` 写入时机不变 —— 这两个文件的**行数**被平台用来算进度,曾出过「进度 212%」的坑 |
| 2 | **`make()` 包重试** | 重试会延长单任务耗时,可能撞上 `wait_timeout_seconds: 1200` 或 `max_ttl_second: 14400`。需确认 退避总时长 + 单任务耗时 < 沙箱 TTL |
| 3 | **闸门串进 `_worker`** | 单测已证「降并发不中断在跑的任务」,真机还要确认 limit 充足时 `async with` 近乎零开销 |
| 4 | **重试对 OBS 的额外压力** | S005 重试 3 次 = 下载流量 ×3,大并发下可能打挂 OBS,必须带抖动 |

---

## 八、代码位置索引

| 功能 | 位置 |
|---|---|
| 错误码目录(按来源) | [prepare_config.py:755](../prepare_config.py#L755) `ERROR_CATALOG` |
| 处置策略表(按动作) | [prepare_config.py:807](../prepare_config.py#L807) `ERROR_POLICY` |
| C003 特征串 | [prepare_config.py:851](../prepare_config.py#L851) `_C003_THROTTLE_MARKERS` / `_C003_RETRY_MARKERS` |
| C003 二次分类 | [prepare_config.py:865](../prepare_config.py#L865) `classify_c003` |
| 策略查询 | [prepare_config.py:887](../prepare_config.py#L887) `error_policy` / [:901](../prepare_config.py#L901) `error_max_retries` |
| 并发控制器 | [utils/throttle.py:61](../utils/throttle.py#L61) `ThrottleController` |
| 闸门 | [utils/throttle.py:140](../utils/throttle.py#L140) `slot` / [:148](../utils/throttle.py#L148) `acquire` |
| 熔断超时等待 | [utils/throttle.py:169](../utils/throttle.py#L169) `_wait_timeout` |
| 记账入口 | [utils/throttle.py:196](../utils/throttle.py#L196) `record` |
| 状态机 | [utils/throttle.py:218](../utils/throttle.py#L218) `_reconcile` |
| 手动调整 | [utils/throttle.py:305](../utils/throttle.py#L305) `set_ceiling` |
| 按配置构造 | [utils/throttle.py:375](../utils/throttle.py#L375) `build_from_config` |
| 已有的 S009 重试(待抽象复用) | [hive.py:1316-1356](../hive.py#L1316-L1356) |
| C003 抛出点 | [hive.py:1455-1464](../hive.py#L1455-L1464) |
| S008 分类优先级 | [hive.py:892-906](../hive.py#L892-L906) |
| worker 循环 | [hive.py:1616](../hive.py#L1616) |
| 并发读取点 | [hive.py:1869](../hive.py#L1869) |
| 每任务重读 YAML(活控制通道) | [hive.py:371](../hive.py#L371) |

---

## 九、附:复核数据的 SQL

```sql
-- 近 7 天错误码分布
ATTACH 'platform/database/task_data.db' AS td;
SELECT error_code, COUNT(*) FROM td.task_records
 WHERE status='任务异常' AND updated_at >= datetime('now','-7 days')
 GROUP BY error_code ORDER BY 2 DESC;

-- C003 与并发的相关性(需 ATTACH platform.db 取 concurrent_num)
SELECT CASE WHEN i.concurrent_num>=1000 THEN '1000+'
            WHEN i.concurrent_num>=500  THEN '500-999'
            WHEN i.concurrent_num>=100  THEN '100-499'
            ELSE '<100' END AS bucket,
       COUNT(*) tasks,
       ROUND(100.0*SUM(r.error_code='C003')/COUNT(*),2) c003_pct,
       ROUND(100.0*SUM(r.status='任务成功')/COUNT(*),2) ok_pct
  FROM td.task_records r JOIN task_instances i ON i.id=r.instance_id
 GROUP BY bucket;

-- 找「整实例全军覆没」的个例
SELECT r.instance_id, i.concurrent_num cc, COUNT(*) n,
       SUM(r.error_code='C003') c003,
       ROUND(100.0*SUM(r.error_code='C003')/COUNT(*),1) pct
  FROM td.task_records r JOIN task_instances i ON i.id=r.instance_id
 GROUP BY r.instance_id HAVING n>200 AND pct>20
 ORDER BY pct DESC LIMIT 15;
```

> 服务重启由用户自行执行(本仓既定约定),验证步骤不代为重启 worker/平台进程。
