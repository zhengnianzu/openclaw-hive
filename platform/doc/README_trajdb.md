# 轨迹数据库（traj DB）说明

从 OBS 上的 **workspace** 原始轨迹，下载 → 解析 → 统计 → 可视化查看的完整链路说明。
配套代码：[traj_pipeline/download_workspace_and_run.py](traj_pipeline/download_workspace_and_run.py)、
[data_viewer/server.py](data_viewer/server.py)、[traj_stats.py](traj_stats.py)。

> platform 侧精简迁移版见 `openclaw-hive/platform/src/traj_pipeline.py`（只含 workspace 快路径的枚举 → 下 stats → 聚合，不含慢路径/详情/Hermes 下载）。
>
> 文档里的路径与口径均以两个**真实 OBS 批次**核对过（openclaw：`小艺claw-任务生产-ds-v4-flash-行业办公-0819-2121`；hermes：`0809_no_rubrics_glm_hermes_1_tokenfly_0810_0930`），与 `traj_pipeline/README_workspace.md` 的部分描述有出入：hermes 轨迹不是 `query*.json`，openclaw 批次没有 `traj_stats_result.json`，详见下文标注。

---

## 一、workspace 格式的处理流程

系统跑的是「AI Agent 蒸馏任务」：每个 task 目录里有两个 Agent —— **assistant**（干活）与 **evaluator**（质检打分）。
workspace 格式指 OBS 上 `openclaw_trajs/<batch>/` 结构，其下每个子目录是一个 task，task 内同时含 assistant 与 evaluator 的原始轨迹。

处理分三阶段：

### 表格设计
数据库：hive_output.db

-----“任务表” tasks-----
字段	来源
任务ID / 任务名称	task_registrations.id / task_name
OBS路径	task_registrations.traj_path
状态 running	后台 worker 计算时写
轨迹条数	obsutil ls -d 数一级子目录（建议缓存，翻页要处理 Next marker，见 list_task_dirs download_workspace_and_run.py:133-165）
轨迹名称列表	同上 ls -d 结果


-----“轨迹”表格字段设计------

单条轨迹（session = task 目录名）在表格 / 详情里的字段：

| 字段 | 来源 / 说明 | 现状 |
|---|---|---|
| **任务 ID** | 任务实例 id（`tasks.json`） | ✅ 已有 |
| **轨迹 ID** | `per_session[].session`（即 task 目录名） | ✅ 已有 |
| **轨迹名字** | 同 session（无独立命名字段），详情页 `session` 展示 | ⚠️ 复用目录名 |
| **轨迹标签（统一漏斗口径）** | `_get_session_level` 算出 L0/L1/L1.5/L2/L3（L1 门槛按 harness 分叉，见「二」）；`level_filter` 多选、`has_eval`、`completion_filter`(ge05/eq1/no_eval) 筛选 | ✅ 已有 |
| **工具等详细信息** | assistant 轨迹解析后每条消息含 `toolCall`/`toolResult`（名称/参数/输出）；`tool_calls`/`plain_rounds` 计数 | ✅ 已有 |
| **任务日志（本地缓存路径）** | 统一为 `<task_dir>/workdir/run.log`（最全/最新正源，见「路径规定」）；`_ensure_workspace_log` 按需下载，页面只回传尾部 2MB | ✅ 已有 |
| **gateway 日志（本地缓存路径）** | **openclaw 特有**：`<task_dir>/workdir/gateway.log`（gateway 运行时服务日志：启动/插件/ws/tool-policy/长会话诊断，与 run.log 互补，非轨迹内容）；hermes 无（进程内模式）。当前 data_viewer / traj_pipeline **未下载也未展示** | ❌ 待补（新需求，仅 openclaw） |
| **评估日志（本地缓存路径）** | `logs/evaluator_use.log`（openclaw/hermes 均存在）；verdict 优先从主 log 解析，评估日志作回退 | ⚠️ 路径已确认，回退链路待补 |
| **assistant 轨迹** | openclaw：`agents/{assistant1,main}/sessions/*.jsonl`（排除 trajectory，取最大文件）；hermes：`profiles/{assistant1,main}/sessions/session_*.json`（单个 JSON dict） | ✅ 路径已按 harness 实测 |
| **evaluator 轨迹** | openclaw：`agents/evaluator/sessions/*.jsonl`；hermes：`profiles/evaluator/sessions/session_*.json`（对称推断，未实测）。**层级 2 懒加载**（点开 Evaluator 标签才拉），默认不下载 | ✅ 路径一致（hermes 待实测） |
| **轨迹状态** | `logs/traj_stats_result.json`：**oc_memory 有且有效、小艺claw 没有、hermes 陈旧**（`task_level:"none"`）。层级 1 快路径下载后**校验有效性**，无效回退慢路径 | ⚠️ 详见「快路径校验」 |
| **task_done（bool）** | 主 log 是否含「【Task_Done】」标记；无统计来源时的降级 L1 近似 | ✅ 已有 |
| **has_eval（bool）** | 是否拿到数值 completion | ✅ 已有 |
| **completion（float）** | evaluator 首轮数值完成度，如 0.75；[0,1]，无则 null | ✅ 已有 |
| **evaluator 评分** | `completion` / `inclination` / `reason` / `rubric_checks` 四件套，详情页 rubric 面板 | ✅ 已有 |


### 日志/轨迹路径规定

云上 `<task_dir>` = OBS 上的 task 前缀；本地缓存 `origin/<task_dir>/`。按 harness 分两套：

> ⚠️ **主任务日志统一为 `workdir/run.log`**（实测 3 个批次都有，且均为最全/最新正源）：
> - oc_memory：`workdir/run.log` 与 `logs/<task>.log` **字节完全相同**（同一文件副本）
> - 小艺claw：`workdir/run.log`(291 行) vs `logs/harness_automation.log`(290 行)，run.log 含后者 269/290 行 + 21 行独有 → run.log 更新更全
> - hermes：`workdir/run.log`(423 行完整对话) vs `logs/<task>.log`(117 行截断) → run.log 是正源，`logs/<task>.log` 丢失部分 assistant 内容
>
> **结论：解析一律用 `workdir/run.log`**。`logs/<task>.log` / `logs/harness_automation.log` 均为 run.log 的截断/派生副本，不作解析源。
>
> **`workdir/gateway.log` = openclaw gateway 运行时服务日志（启动时序/插件/ws 请求/tool-policy 工具策略/长会话诊断），与 run.log 内容不同（互补），非轨迹内容**。实测：oc_memory ✅(2153B/22 行)、小艺claw ✅(12607B/73 行)、hermes ❌(**workdir/ 只有 run.log**——hermes 是进程内 AIAgent 模式，无独立 gateway 服务)。「gateway 日志」字段仅 openclaw 有，深层/懒加载，仅排障用。

**openclaw**（`agents/*/sessions/*.jsonl` 布局，实测 `小艺claw-...-0819-2121`、`oc_memory_..._0820_1947`）：

| 文件 | OBS 路径 | 本地缓存 |
|---|---|---|
| 任务日志 | `<task_dir>/workdir/run.log`（**唯一解析源**） | `origin/<task_dir>/workdir/` |
| 评估日志 | `<task_dir>/logs/evaluator_use.log` | 同左 |
| gateway 日志 | `<task_dir>/workdir/gateway.log`（oc_memory/小艺claw 实测存在；gateway 运行时日志，非轨迹内容） | 同左 |
| assistant 轨迹 | `<task_dir>/agents/{assistant1,main}/sessions/*.jsonl`（排除 `.trajectory.jsonl`，取最大文件） | 同左 |
| evaluator 轨迹 | `<task_dir>/agents/evaluator/sessions/*.jsonl`（oc_memory 有 `sessions.json`；懒加载） | 同左（懒加载） |
| 轨迹状态 | `<task_dir>/logs/traj_stats_result.json`：**小艺claw 批次没有；oc_memory 批次有且有效**（实测 `tool_calls=5, plain_rounds=1, task_level="L1"`） | 同左 |

**hermes**（`profiles/*/sessions/session_*.json` 布局，实测 `0809_no_rubrics_glm_hermes_1_tokenfly_0810_0930`）：

| 文件 | OBS 路径 | 本地缓存 |
|---|---|---|
| 任务日志 | `<task_dir>/workdir/run.log`（**唯一解析源**，423 行完整；`logs/<task>.log` 为 117 行截断副本） | `origin/<task_dir>/workdir/` |
| 评估日志 | `<task_dir>/logs/evaluator_use.log` | 同左 |
| gateway 日志 | **hermes 无**（进程内 AIAgent 模式，workdir/ 只有 run.log，没有 gateway.log） | — |
| assistant 轨迹 | `<task_dir>/profiles/{assistant1,main}/sessions/session_*.json`（**单个 JSON dict，`messages[]` OpenAI-chat 格式：role=user/assistant/tool，assistant 工具调用在顶层 `tool_calls[]`，content 为纯字符串，另有 `reasoning_content`**） | 同左 |
| evaluator 轨迹 | `<task_dir>/profiles/evaluator/sessions/session_*.json`（对称推断，未实测） | 同左（懒加载） |
| 轨迹状态 | `<task_dir>/logs/traj_stats_result.json`（存在但**内容陈旧**，见「快路径校验」） | 同左（懒加载） |

> ⚠️ hermes 轨迹**不是** `traj_pipeline/README_workspace.md` 说的 `logs/trajectories/*/query*.json`——该批次根本没有此路径。`_render_query1_trajectory` / `_query1_verdict` 等依赖 query1 的代码在真实数据上拿不到输入，hermes 的 completion 多数为 None（L1.5+ 预期较少）。

### 加载分层：展示信息 ↔ 所需文件（最小集）

原则：**只下「当前层要展示的信息」所需的最小文件集**，不预下载、不整包拉取；同一文件被多层复用（如 run.log 打标 + 详情 Log 共用一份缓存）。按 harness 分叉。

**层级 0 · 任务列表**（任务表格行）
| 展示信息 | 所需文件 | 体积 | 触发 |
|---|---|---|---|
| 任务 ID / 轨迹 ID / 轨迹条数 / 名称列表 | **0 个文件**（仅 `obsutil ls -d` 枚举，metadata 不落盘） | 0 | 任务列表页加载 |
| 任务状态 running | 0（worker 计算时写库） | 0 | — |

**层级 1 · 统计打标**（轨迹表格行：L0-L3、tool_calls、plain_rounds、completion、has_eval）
| 展示信息 | 所需文件 | 体积 | 路径 |
|---|---|---|---|
| 快路径（默认） | **openclaw：`logs/traj_stats_result.json`**；**hermes：`workdir/run.log`**（她的 stats 文件陈旧无效） | ~1KB / ~几十KB | `_fetch_one_task_stats` → 有效即用 |
| 慢路径（stats 无效/缺失回退） | **openclaw/oc：`agents/main/sessions/*.jsonl`（取最大）+ `workdir/run.log`（task_done 降级）** | jsonl 几十KB~几MB | `_fetch_task_done_marker` + `_load_workspace_assistant` |
| 轨迹状态（任务状态列） | 复用上面已下载文件，不新增 | 0 | — |

> 小艺claw 无 `traj_stats_result.json`、hermes 内容陈旧 → 这两批走慢路径（多下 assistant 轨迹文件）；oc_memory 快路径 1 文件即出打标。

**层级 2 · 详情查看**（点开单任务，渐进式）
| 展示信息 | 所需文件 | 体积 | 触发 |
|---|---|---|---|
| Assistant 轨迹 | openclaw：`agents/main/sessions/*.jsonl`（取最大）；hermes：`profiles/main/sessions/session_*.json` | jsonl 几十KB~几MB / ~170KB | 点开「Assistant 轨迹」标签 |
| Evaluator 轨迹 | openclaw：`agents/evaluator/sessions/*.jsonl`；hermes：`profiles/evaluator/sessions/session_*.json` | 懒加载 | 点开「Evaluator 轨迹」标签（本地无则禁用） |
| 任务 Log | `workdir/run.log`（打标时已缓存则复用，不重复下载） | 尾部 2MB 回传 | 点开「任务 Log」标签 |
| gateway 日志 | **openclaw：`workdir/gateway.log`**（hermes 无） | ~2-12KB | 深层排障，懒加载 |
| rubric 裁决面板 | 来自已下载 run.log（evaluator 块），不新增 | 0 | 详情页加载 |

**下载顺序约定**：层级 0 → 1 → 2 渐进；层级 2 内「Assistant 轨迹 → Evaluator 轨迹 → gateway」按需，永远最后拉 evaluator。同一文件多层复用（run.log 打标 + 详情 Log 共用本地缓存，不重复 `obsutil cp`）。


### 阶段一：登记任务 + 触发采集

1. 从 hive_platform.db 中找到相关任务
2. 触发采集：`run_pipeline()` 拼命令调 `download_workspace_and_run.py`
3. **harness 判定**：不能看 `harness_home`（hermes 批次的 traj_stats_result.json 里 `harness_home` 也写 `/home/ma-user/.openclaw`）。按文件布局判定：
   - 有 `profiles/*/sessions/*.json` → **hermes**
   - 有 `agents/*/sessions/*.jsonl` → **openclaw**
   判定后选对应的 include/exclude 下载清单（任务名或 OBS 路径含 `hermes` 仅作辅助提示，最终以布局为准）。

### 阶段二：下载 + 统计（层级 1：统计打标）

选择要执行的任务 -> 登记到 "hive_output.db任务表格"
触发补充"轨迹表格"

```
枚举 task：obsutil ls -d 列 workspace 下所有 task 子目录（处理 Next marker 翻页）  --> 轨迹表格
    │
    ├─ 快路径（默认，文件最小集）：并发下载每个 task 的 stats 文件
    │    openclaw：logs/traj_stats_result.json（~1KB）
    │    hermes：  workdir/run.log（其 stats 文件陈旧无效）
    │    ✅ 存在且有效 → 直接聚合成 filter_stats.json，不下载原始轨迹
    │       （实测 oc_memory 批次：tool_calls=5, plain_rounds=1, task_level="L1"）
    │    ⚠️ 存在但内容陈旧（task_level=="none" 或 agents[].has_trajectory==false，
    │       hermes 批次实测如此）→ 判为无效，回退慢路径
    │    ❌ 文件不存在（小艺claw 批次实测没有）→ 回退慢路径
    │
    └─ 慢路径（层级 1 最小集）：下载 assistant 轨迹 + 主 log（workdir/run.log）
          openclaw：agents/main/sessions/*.jsonl（取最大） + workdir/run.log
          hermes：  profiles/main/sessions/session_*.json + workdir/run.log
          走 traj_stats.process_root 口径重算：
          tool_calls / plain_rounds / 首轮 completion / 打标。
          task_done（主 log 含【Task_Done】）作为无统计来源时的降级 L1 近似。
```

将"轨迹标签"（L0-L3）、`task_done`（bool）、`completion`（float）、`has_eval`（bool）写入轨迹表格。

> 快路径可用性**按批次而定**（见「日志/轨迹路径规定」）：oc_memory 批次可用；小艺claw 无该文件、hermes 内容陈旧 → 这两批实际走慢路径。
> 文件数最小集：快路径 oc=1（stats json）、hermes=1（run.log）；慢路径 oc=2（jsonl+run.log）、hermes=2（session+run.log）。

### 阶段三：详情查看（层级 2：深层文件，按需懒加载）

用户点开某个 task，`/api/tasks/{id}/session-detail/{session}` 分 openclaw / hermes 两条渲染路径：
- openclaw：`agents/main/sessions/*.jsonl` 事件流 → 消息数组（`_load_workspace_jsonl_trajectory`）
- hermes：`profiles/main/sessions/session_*.json` 单个 dict 的 `messages[]`（OpenAI-chat；工具调用读顶层 `tool_calls[]`）

渐进式下载（对应「加载分层」层级 2）：总览只花层级 1 的最小集拿统计，详情才按需拉单个 session 的轨迹文件（Assistant → Evaluator → gateway），evaluator 轨迹永远最后才拉；run.log 若在打标时已缓存则详情 Log 直接复用，不重复下载。

---

## 二、统一漏斗口径（L0 → L3）

两条路径最终都归一化到同一套逐层嵌套的漏斗（每档都是上一档子集）。

### 命名统一
- `task_done`（bool）：主 log 是否含「【Task_Done】」标记
- `has_eval`（bool）：是否拿到数值 completion
- `completion`（float [0,1]）：evaluator 首轮数值完成度分（无则 null）

### 轨迹标签数据源优先级
1. **快路径**：优先采用 `traj_stats_result.json`（存在且有效，见「快路径校验」）
2. **慢路径**：本地解析 assistant 轨迹 + 主 log，走 `_get_session_level` 口径（与 traj_stats.py:609 同一门槛）

### L1 门槛（按 harness 分叉，必须保留）
| harness | L1 门槛 | 依据 |
|---|---|---|
| openclaw | `tool_calls >= 3 且 plain_rounds > 0` | traj_stats.py:609 |
| hermes | `plain_rounds > 0`（有产出即可，不强制工具数；hermes turn 粒度粗，单轮对话任务 tool_calls=0 但有完整答复） | traj_stats.py:644/677 |

> 无任何统计来源（无 traj_stats_result.json、轨迹无法解析）时，降级以 `task_done == True` 作 L1 近似。

### 各档定义（快路径 / 慢路径有缓存）
| 档次 | 含义 | filter_stats.json 字段 |
|---|---|---|
| **L0** | 总轨迹数（所有 assistant 轨迹） | `filtered_count` + `dropped_count` |
| **L1** | 过门槛（见上表，按 harness 分叉）；不过门槛计入 `dropped_count` | `filtered_count` |
| **L1.5** | L1 内有数值 completion（`has_eval=True`，不含 null） | `with_eval_count` |
| **L2** | L1.5 内 completion ≥ 0.5 | `completion_ge_0.5` |
| **L3** | L1.5 内 completion == 1 | `completion_eq_1` |

completion 取自 evaluator **首轮**（编号最小的 turn，不死守 turn=1）裁决。蒸馏最终要的就是 L3 那批满分轨迹。

### 慢路径且无缓存（降级口径）
无任何统计来源时（快路径无效 + 轨迹无法解析），只用主 log：

| 档次 | 含义 |
|---|---|
| **L0** | 总轨迹数（所有 assistant 轨迹） |
| **L1** | `task_done == True`（主 log 含【Task_Done】）——降级近似，**与正常 L1 口径不同，结果不可跨模式比较** |
| **L1.5** | L1 内有数值 completion（含 null 一律不算） |
| **L2** | L1.5 内 completion ≥ 0.5 |
| **L3** | L1.5 内 completion == 1 |

> 前端按 Lx 过滤时，需标记降级打标的任务，避免与正常打标混比。

---

## 三、针对任务的函数清单（按依赖文件分组）

> 以「依赖文件」为一行（同一文件是唯一数据源，所有消费它的函数合并在一起）；列：**依赖文件 → 功能 → 函数 → 输出**。路径引用「日志/轨迹路径规定」，层级对应「加载分层」；同一文件多层复用（run.log 打标 + 详情 Log 共用缓存，不重复下载）。

### 层级 0 · 任务枚举

| 依赖文件 | 功能 | 函数 | 输出 |
|---|---|---|---|
| 无（仅 `obsutil ls -d` metadata，不落盘） | 枚举 workspace 下所有 task 子目录，Next marker 翻页 | `list_task_dirs` | `[task_dir_name, ...]`（轨迹 ID 列表）；轨迹条数 = len |

### 层级 1 · 统计打标

| 依赖文件 | 功能 | 函数 | 输出 |
|---|---|---|---|
| openclaw：`logs/traj_stats_result.json` | 快路径打标（沙箱预生成统计） | `_fetch_one_task_stats`、`fetch_per_task_stats_files`、`harness_tsr_to_entries` | `per_task entry`：`{task, harness, tool_calls, assistant_rounds, plain_rounds, has_ge3_toolcalls, has_plain_round, passed_gate, has_eval, evaluator_completion, verdict_source, level}` |
| hermes：`workdir/run.log` | 快路径打标（hermes 的 stats 文件陈旧无效 → 用 run.log 重算 tool_calls/plain_rounds） | `_fetch_one_task_stats`（harness=hermes 分支） | 同上 `per_task entry`（tool_calls 读 messages 顶层 `tool_calls[]`，plain_rounds 计无工具轮） |
| openclaw：`agents/main/sessions/*.jsonl` + `workdir/run.log` | 慢路径重算打标（stats 缺失/无效回退） | `build_platform_stats`、`analyze_trajectory`（traj_stats.py:101）、`_fetch_task_done_marker` | 同上 `per_task entry` + `task_done`（bool，run.log 含【Task_Done】）、`char_len`、`tokens` |
| hermes：`profiles/main/sessions/session_*.json` + `workdir/run.log` | 慢路径重算打标 | `build_platform_stats`、`analyze_hermes_messages`（新，读顶层 tool_calls[]）、`_fetch_task_done_marker` | 同上 `per_task entry` + `task_done`、`char_len` |
| 无（输入 entry 列表，纯聚合） | 漏斗计数 + token/char_len 均值 | `stats_from_per_task` | `filter_stats.json`：`{total, L0, L1, L1_5, L2, L3 各档 count, dropped, ge3, plain, has_eval, ge05, eq1, task_done_count, token/char_len 均值}` |

### 层级 2 · 详情查看

| 依赖文件 | 功能 | 函数 | 输出 |
|---|---|---|---|
| openclaw：`agents/main/sessions/*.jsonl` | assistant 轨迹渲染 + char_len 补算 | `_ensure_workspace_session_files`、`_load_workspace_assistant`、`_load_workspace_jsonl_trajectory`、`_backfill_session_char_len`/`_recompute_char_len_stats` | `messages[]`：`[{role, part_type: thinking/text/toolCall/toolResult, content, tool_name, args, isError, exitCode, details}]`（字段截断） |
| hermes：`profiles/main/sessions/session_*.json` | assistant 轨迹渲染（messages[] OpenAI-chat） | `_ensure_workspace_session_files`、`_render_hermes_session`/`_load_hermes_profile_session` | `messages[]` 同上归一化（顶层 `tool_calls[]`→toolCall、`reasoning_content`→thinking、纯字符串 content→text） |
| openclaw：`agents/evaluator/sessions/*.jsonl` | evaluator 轨迹（懒加载，点开才拉） | `_ensure_workspace_evaluator` | `messages[]`（evaluator 侧） |
| hermes：`profiles/evaluator/sessions/session_*.json` | evaluator 轨迹（懒加载，对称推断未实测） | `_ensure_hermes_evaluator` | `messages[]`（推断） |
| `workdir/run.log`（层级1+2 复用） | 任务 Log 显示 + rubric 裁决面板 | `_load_session_log_content`/`_ensure_workspace_log`、`_extract_workspace_verdict`、`_extract_verdict_from_log`、`traj_stats.extract_first_evaluator_obj` | Log 尾部 2MB；`verdict`：`{completion, inclination, reason, rubric_checks:[{name, gate_or_reward, passed, evidence}]}` |
| openclaw：`workdir/gateway.log` | gateway 运行时日志（排障；hermes 无） | （待补，未实现） | gateway log 内容（全文/尾部） |
| `logs/evaluator_use.log` | 评估日志（verdict 回退源；openclaw/hermes 均有） | （verdict 回退分支，无独立函数） | `verdict`（回退） |
| hermes：`logs/trajectories/*/query*.json` | query1 轨迹/裁决（README 假设；**该批次不存在**） | `_find_query1_json`、`_query1_verdict` | None（该批次恒 None，待有 query1 的批次再用） |
| hermes：`profiles/*/state.db` | token 用量（慢路径 hermes） | `_read_hermes_state_db` | `{input_tokens, output_tokens, reasoning_tokens, total_tokens}` |

### 无文件依赖（纯计算 / 调度 / 入口）

| 依赖文件 | 功能 | 函数 | 输出 |
|---|---|---|---|
| 无 | 下载器：按 harness 选 include/exclude 清单 | `download_task` | 落盘 `origin/<leaf>/`，无返回值 |
| 无 | rubric 排序：gate 项排前面 | `_sort_rubric_gate_first` | 排序后 `rubric_checks`（gate 在前） |
| 无 | 采集调度：队列串行执行下载 + 统计 | `run_pipeline` / `_enqueue_pipeline` / `_pipeline_worker` | 触发层级1 函数；任务状态写 running |
| 无 | 工具调用失败统计（需全量轨迹，后台线程） | `_run_tool_failure_analysis` / `api_analyze_tool_failures` | tool failure 统计 |
| 无 | CLI 入口：快路径探测 → 命中即输出，未命中回退慢路径 | `main` | 输出 `filter_stats.json` |
| 无 | 定位 `origin/<session>/`（session = task 目录名） | `_workspace_task_dir` | 本地路径 str |

---

## 四、阶段性测试案例（offline 先用真实批次跑通，再进平台）

两个真实批次（已实测可访问）：
- **openclaw**：`obs://s3-asset-b-hd-cce-aifm-nlp-exp/openclaw_trajs/小艺claw-任务生产-ds-v4-flash-行业办公-0819-2121/`
- **hermes**：`obs://s3-asset-b-hd-cce-aifm-nlp-exp/openclaw_trajs/0809_no_rubrics_glm_hermes_1_tokenfly_0810_0930/`

| # | 阶段 | 测什么 | 怎么验 / 预期 |
|---|---|---|---|
| S1 | 枚举 | `list_task_dirs` 两个批次列全任务、Next marker 翻页不漏 | 各批次列出的 task 数与 obsutil 直接 `ls -d` 一致 |
| S2 | 快路径 | `_fetch_one_task_stats` 读 hermes 的 traj_stats_result.json | 能读到，但须识别**内容陈旧**（`task_level:"none"`、`has_trajectory:false`）→ 回退慢路径 |
| S3 | 打标(openclaw) | openclaw 任务 tool_calls / plain_rounds | 与手工数 jsonl 一致；`compute_level` 出 L1（≥3 工具调用且有纯轮） |
| S4 | 打标(hermes) | **新解析器** `analyze_hermes_messages` 读顶层 `tool_calls[]` | 实测 `000001_pub_g_公共服务_投诉路径选择_3c6f3fde_q1` 应得 `tool_calls=24, plain_rounds=1`；旧 `analyze_hermes_session` 错得 0/25，**不得复用** |
| S5 | 漏斗 | 慢路径重算对整批输出 L0-L3 计数，与 data_viewer filter_stats.json（同批次历史结果）一致 | **oc_memory 批次快路径可用**（traj_stats_result.json 有效）→ 快/慢两条口径对比；小艺claw 无该文件、hermes 陈旧 → 快路径判无效回退慢路径 |
| S6 | 详情 | 单任务 assistant 轨迹渲染（openclaw jsonl / hermes messages[]） | 4 标签页各点一遍；日志只回传尾部 2MB；evaluator 标签点开才拉 |
| S7 | 回归 | 平台现有 logs.py 的 eval-stats / traj-stats 仍正常 | 跑一遍旧接口不报错 |
| S8 | 层间文件最小集 | 逐层验证「展示信息 → 所需文件」最小集（见「加载分层」） | 层级0：0 文件；层级1 快路径 oc=1(tsr)/hermes=1(run.log)、慢路径 oc=2/hermes=2；层级2 详情只多拉点开标签对应文件；run.log 复用不重复下载；hermes 无 gateway.log |

### 离线交互函数（人工核查 / 排障用，不起前端）

> 与 S1-S8 互补：S 系列是脚本化断言（自动化验收），这组是**交互式核查工具**——给一个输入、看一个输出，共用同一批解析函数。输入里的批次/任务目录同时接受 OBS 路径与本地 `origin/` 缓存路径。

| 函数 | 输入 | 功能 | 输出（约定） | 载体 |
|---|---|---|---|---|
| `show_lx_summary` | 批次目录（openclaw_trajs 下某个 batch） | 对整批跑 L0→L3 漏斗（快路径优先，无效自动回退慢路径），输出各档数量与占比 | `{total, L0, L1, L1_5, L2, L3, dropped, task_done_count}` + `ratio`：L1/L0 过闸率，L1_5/L1、L2/L1_5、L3/L2 逐级通过率；附 token/char_len 均值 | test/task_level.py |
| `list_task_trajectories` | task 目录（或 task 名） | 列出该任务全部轨迹文件（assistant1/main/evaluator；openclaw jsonl / hermes session；排除 `.trajectory.jsonl`） | `[{path, harness, role, kind(jsonl/json), size, mtime, messages?}]`；openclaw 标出解析实际取哪个（最大 jsonl） | test/obs_traj.py（复用 `find_assistant_trajectories` / `find_hermes_sessions`） |
| `print_trajectory` | task 目录 + 轨迹名（缺省 main） | 选定轨迹**逐块 JSON 打印** assistant 消息流（thinking/text/toolCall/toolResult），每块一个 JSON、带序号 | `block[i] = {role, part_type, content, tool_name, args, isError, exitCode}`（hermes 归一化：`reasoning_content`→thinking、顶层 `tool_calls[]`→toolCall）；`--json` 一次输出整条 | test/obs_traj.py（复用 `analyze_trajectory` / `analyze_hermes_messages`） |
| `verify_api_and_cache` | task 名（+ data_viewer 服务地址） | (a) 直接调详情 API（轨迹 / Log / verdict）不走前端；(b) 校验本地缓存 `origin/<leaf>/` 与 OBS 远端一致性 | 核查报告 `[{file, remote_exists, remote_size, local_exists, local_size, match, api_endpoint, api_matches_cache}]`，不一致项标 MISMATCH / MISSING | test/task_status.py + curl data_viewer |

验收断言（写进脚本）：
- S4：hermes session 解析 `tool_calls==24 且 plain_rounds==1`
- S3：openclaw task 打标 L1 成立且 `passed_gate==True`
- S2：陈旧 traj_stats_result.json 被判定无效并回退慢路径
- harness 判定：两个批次各抽 task，`detect_harness` 结果正确（不受 `harness_home` 干扰）

---

## 五、前端显示效果（详情页）

详情页 4 个标签页：

1. **Assistant 轨迹** —— 消息流：文本、思考（reasoning_content）、工具调用块（工具名 + 参数）、toolResult（工具名 / isError / exitCode / details 折叠）；hermes 的 `tool_calls[]` 与 `reasoning_content` 归一化到同一结构
2. **Evaluator 轨迹** —— 只有点开时才拉（显示「…加载中」），本地没有时标签禁用
3. **任务 Log** —— 主日志，顶部嵌 rubric 裁决面板（Completion 按值着色 / 裁决倾向 / reason 全文 / 逐条 rubric，gate/reward 分色，`passed=false` 红色高亮 + evidence）
4. rubric 面板结构在各标签间复用

---

## 六、依赖文件清单

| 文件 | 阶段 | 作用 |
|---|---|---|
| [traj_pipeline/download_workspace_and_run.py](traj_pipeline/download_workspace_and_run.py) | 二 | 枚举 task、快慢路径下载、`stats_from_per_task` 聚合 |
| [traj_stats.py](traj_stats.py) | 二/三 | `process_root` 逐 task 分析；`extract_first_evaluator_obj` 从 log 解析首轮裁决 |
| [data_viewer/server.py](data_viewer/server.py) | 三 | 全部详情 API：懒加载、jsonl 解析、verdict 归一化、state.db 读取 |
| [data_viewer/static/index.html](data_viewer/static/index.html) | 显示 | 单页前端：漏斗总览 + 详情 4 标签 + rubric 面板 |
| [data_viewer/config.json](data_viewer/config.json) | 全 | `output_base_dir` / `obsutil_path` / `port` |
| offline/（新增） | 二 | 离线脚本（task_level / task_status）：真实批次先行验证打标与状态表，S1-S7 的载体；另含交互核查函数 `show_lx_summary` / `list_task_trajectories` / `print_trajectory` / `verify_api_and_cache`（见「离线交互函数」） |
| obsutil | 二/三 | 所有 OBS 拉取（`ls -d` 枚举、`cp -r -f` 下载） |