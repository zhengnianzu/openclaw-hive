# -*- coding: utf-8 -*-
"""task_tracker：浅层分析的「发现与投递」进程（纯点击驱动）。

## 职责

**只投递用户点过「输出」的实例**（shallow_requests 登记的），不自动扫描历史。
每 5 秒一轮，把这些实例里**新完成的 config** 投进 shallow_queue，供 shallow-local /
shallow-obs 领取。它不分析、不算分级、不碰 OBS，永远是毫秒级的一小步。

没点过的任务不会触发浅层分析 —— 这是刻意的：几十万条自动积压会把用户刚点的
实例挤到队尾，且每 5 秒扫一遍历史实例目录纯属浪费。要分析就点一下。

## 增量读（点击后只投新增）

点击登记后，按 offset 增量读该实例的 `complete.jsonl` / `failed.jsonl`：
  · 首轮（shallow_requests.status='queued'）：清零 offset 从头整扫一遍已完成的
    config，随后把 status 置 'tracking'
  · 之后（status='tracking'）：只读新增行，运行中实例新完成的 config 逐轮带进来，
    结束后文件不再增长、自然安静
tracker_state 表记录 (instance_id, file_path) → (inode, offset, head_hash)。
实例跑到后期有几万行、几 MB，全读一遍越来越慢；记 offset 只读新增部分。

关键：**必须用 inode + 文件头指纹检测文件被替换**。hive 重跑时会把 failed.jsonl
原地 truncate 重写（NFSv3 不支持 rename，inode 不变），offset 若不清零，前面的行
会被整段跳过。inode 变或头指纹变 → offset 归零。

## 投递去重

`INSERT ... ON CONFLICT(instance_id, config_name) DO UPDATE`，靠 shallow_queue 的
UNIQUE 约束在 DB 层去重。tracker 的 offset 只保证「不重复读」，不保证「不重复投递」
（offset 更新失败时会重读），所以去重必须落在 DB。

用法：
    python offline/task_tracker.py                 # 常驻，5s 一轮
    python offline/task_tracker.py --once          # 跑一轮即退出（调试）
    python offline/task_tracker.py --interval 5
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import get_connection, ensure_shallow_extra_columns  # noqa: E402
from offline.heartbeat import write_heartbeat, write_worker_started  # noqa: E402

WORKER_NAME = "tracker"
DEFAULT_INTERVAL = 5
# 实时车道水位上限：待办超过这个行数就**暂停投递自动跟踪的实例**（点击的照投）。
#
# 为什么需要：一个 running 实例可能 10 分钟产出近万个任务（实测某实例
# total=10000，10 分钟跑完 9,924 个），而 tracker 每 5 秒投一批、
# shallow-realtime 实测 727 行/分 —— 产出快过消化，队列无界堆积
# （实测积压 26,650 条 ≈ 37 分钟延迟，新点的实例全被挤在后面）。
#
# 有水位后：队列深则本轮只投点击的（用户在等），自动跟踪的下轮再说。
# 实例的 jsonl 不会丢数据 —— offset 没推进，下次照旧从那里接着读。
#
# 取 30000 的依据：shallow-realtime 提到 48 并发后约 2000+ 行/分，
# 3 万行约 15 分钟消化完；再小会让大实例迟迟投不进去。
DEFAULT_MAX_PENDING = int(os.environ.get("TRACKER_MAX_PENDING", "30000"))
DEFAULT_LIMIT = 100          # 只盯最近 N 个有任务的实例
RECORDS = ("complete.jsonl", "failed.jsonl")


def resolve_output_dir(config_path: str) -> str | None:
    """实例的输出根目录。

    优先级与 offline_worker._local_complete_records 一致：
    config.yaml 的 run_config.task.task_output_path 优先（建实例时写死的绝对路径），
    取不到再回退 <inst_dir>/outputs。搬迁过的实例前者会失效，必须能回退。
    """
    out_dir = None
    try:
        from omegaconf import OmegaConf
        cfg = OmegaConf.load(config_path)
        out_dir = str(cfg.run_config.task.task_output_path or "")
    except Exception:
        out_dir = None
    if out_dir and os.path.isdir(out_dir):
        return out_dir
    inst_dir = os.path.dirname(config_path)
    fallback = os.path.join(inst_dir, "outputs")
    return fallback if os.path.isdir(fallback) else None


def find_records(out_dir: str, name: str) -> str | None:
    """在 out_dir 下找 complete.jsonl / failed.jsonl。

    实际布局有两种：<out_dir>/<stem>/<name> 和 <out_dir>/<name>（task_output_path
    是否已经带了 batch 段取决于建实例时的配置）。只下钻两层，避免遍历巨大的 outputs 树。
    """
    direct = os.path.join(out_dir, name)
    if os.path.isfile(direct):
        return direct
    try:
        for entry in os.scandir(out_dir):
            if not entry.is_dir():
                continue
            p = os.path.join(entry.path, name)
            if os.path.isfile(p):
                return p
    except OSError:
        pass
    return None


def _head_hash(path: str, n: int = 128) -> str:
    """文件头 n 字节的指纹。用来检测「原地重写」。

    inode 检测不够：hive 的 _rewrite_failed_record 是**原地 truncate 重写**
    （NFSv3 不支持 rename），inode 不变。如果只靠 inode，重写后文件变长时
    旧 offset 会指向新内容的中间，前面的行被整段跳过。
    """
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read(n)).hexdigest()
    except OSError:
        return ""


def read_new_lines(conn, instance_id: str, path: str,
                   state_table: str = "tracker_state") -> list[str]:
    """从上次的 offset 开始读新增行，返回非空行列表；同时把新 offset 落库。

    调用方负责 commit（本函数不提交），以便和投递放进同一个事务。

    重置 offset 的两个条件（任一成立）：
      1. inode 变了 —— 文件被删除后重建（complete.jsonl 不会，但失败重跑时会）
      2. 文件头指纹变了 —— 原地 truncate 重写（failed.jsonl 每轮结束都会重写）

    `state_table` 让不同的跟踪进程各记各的偏移。表结构必须与 tracker_state 一致。
    offset 表没有「属主」列，两个进程共用一张表会互相推进对方的偏移，
    各自只能读到一部分行 —— success_tracker 因此传自己的表名。
    """
    try:
        st = os.stat(path)
    except OSError:
        return []

    row = conn.execute(
        f"SELECT inode, offset, head_hash FROM {state_table} "
        "WHERE instance_id=? AND file_path=?",
        (instance_id, path),
    ).fetchone()
    prev_inode = row["inode"] if row else None
    prev_offset = row["offset"] if row else 0
    prev_head = row["head_hash"] if row else None

    cur_head = _head_hash(path)
    replaced = prev_inode != st.st_ino or (prev_head and prev_head != cur_head)
    offset = 0 if replaced else min(prev_offset, st.st_size)

    lines: list[str] = []
    if st.st_size > offset:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(st.st_size - offset)
        except OSError:
            return []
        text = chunk.decode("utf-8", "replace")
        # 最后一行可能只写了一半（append 进行中），此时要保留在 offset 里下轮再读，
        # 否则会把半个 config 名当成完整名字投递出去。
        cut = text.rfind("\n")
        if cut == -1:
            return []                      # 连一个完整行都没有
        complete_part = text[: cut + 1]
        consumed = len(complete_part.encode("utf-8"))
        lines = [ln.strip() for ln in complete_part.splitlines() if ln.strip()]
        offset += consumed

    conn.execute(
        f"INSERT INTO {state_table} (instance_id, file_path, inode, offset, head_hash, updated_at) "
        "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(instance_id, file_path) DO UPDATE SET "
        "  inode=excluded.inode, offset=excluded.offset, "
        "  head_hash=excluded.head_hash, updated_at=CURRENT_TIMESTAMP",
        (instance_id, path, st.st_ino, offset, cur_head),
    )
    return lines


# submit_batch 的 SQL 片段：reset=True 时把两个浅层维打回 queued。
# 抽成常量只为让上面那条 f-string 不必内嵌引号转义。
_LOCAL_RESET = "'queued'"
_OBS_RESET = "'queued'"


def submit_batch(conn, instance_id: str, config_names: list[str],
                 priority: int = 0, artifact_ready: bool = True,
                 reset: bool = True) -> int:
    """把 config 投进 shallow_queue，返回新插入的行数。

    task_idx 从 task_records 带过来（在线同步时已写入）：shallow-local 要用它拼
    task-<idx>.log 的路径。取不到时留 NULL，由 shallow-local 回退到输入目录下标。

    priority 是**浅层车道号**（不是「是否用户点击」）：
      100 = 实时车道，实例还在跑（running/preparing），用户在等新完成的任务冒出来
        0 = 背景车道，实例已结束，剩下的是批量补全，交给 64 并发吃满
    由 `_lane_priority(实例status)` 决定，领取端见 shallow_worker 的 claim 过滤。

    reset: 冲突时是否把两个浅层维打回 queued（复活已 done/skipped 的行）。
      tracker 投递要 True —— 用户点「输出」就是要重新看这个实例的结果。
      artifacts.py 的懒发现要 False —— 它只为把列表显示出来而补建行，
      打回 queued 会让两个 worker 白跑几千行已分析完的任务。

    artifact_ready: 该 config 是不是「任务成功」（来自 complete.jsonl）。
      **只用于备注文案**：产物维现在恒为 'pending_click'（见下），不再按成功/失败
      决定入队与否 —— 交付物只在用户点开详情时下载。
    """
    if not config_names:
        return 0
    idx_map = {}
    try:
        rows = conn.execute(
            "SELECT config_name, task_idx FROM task_records WHERE instance_id=?",
            (instance_id,),
        ).fetchall()
        idx_map = {r["config_name"]: r["task_idx"] for r in rows}
    except Exception:
        pass

    # 产物维的初始值：**恒为 'pending_click'**（不被任何 worker 领取的静默态）。
    #
    # 交付物是「点开某个 task 详情才需要」的东西：整个 OBS 前缀 `cp -r` 下来动辄
    # 数十 MB，而浅层分级只读 1KB 的 traj_stats_result.json + 本地日志，**根本不需要
    # 交付物**。此前这里对每个成功 config 都置 'queued'，于是点一次「输出」就给
    # 一个 3.4 万 config 的实例灌 3.4 万条全量下载任务 —— 线上实测积压 97.4 万条
    # （其中 92.6 万属于已出队实例），按 210 条/分要 3.2 天，而磁盘只剩 214G，
    # 全量下完会直接写爆盘。
    #
    # 现在产物只由用户点击入队（artifacts.py 的 _requeue_artifact / batch_output.py
    # 的 trigger_deep，两者都置 artifact_priority=100 进实时车道）。
    art_init = "pending_click"
    before = conn.total_changes
    conn.executemany(
        "INSERT INTO shallow_queue (instance_id, config_name, task_idx, priority, "
        "       local_state, obs_state, artifact_state, artifact_note) "
        "VALUES (?,?,?,?,'queued','queued',?,?) "
        "ON CONFLICT(instance_id, config_name) DO UPDATE SET "
        "  priority=MAX(shallow_queue.priority, excluded.priority), "
        # 点击触发时把各维都重置回 queued —— 否则用户点了「立即分析」，
        # 但这条早已是 done/skipped（历史遗留或上次失败），worker 不会再处理，
        # 界面看起来「点了没反应」。
        #
        # 判据是 `reset`（调用方显式传入），**不能用 `excluded.priority > 0`** ——
        # priority 现在是「车道号」而不是「是否用户点击」：已结束实例的行走背景
        # 车道、priority=0，但它们同样是用户点「输出」才投进来的，同样需要复活。
        # 用 priority>0 判断会让这些行永远停在 done/skipped，表现为「点了没反应」。
        f"  local_state={_LOCAL_RESET if reset else 'shallow_queue.local_state'}, "
        f"  obs_state={_OBS_RESET if reset else 'shallow_queue.obs_state'}, "
        # 产物维**完全不看 priority**，且本函数永不把它推进到 queued。
        #
        # priority 是浅层「用户点了输出，整个实例插队」的语义：一个实例被点过一次，
        # 它的全部 config 都带 priority=100，而 pick_clicked 每 5 秒把这批 config
        # 整批重投一遍。如果这里按 priority 重置 artifact_state，已下完的行会被
        # 无限打回队列 —— 实测线上同一批 task 被反复下载 8 轮以上。
        #
        # 用户要下某个 task 的交付物，走 artifacts.py 的 _requeue_artifact /
        # batch_output.py 的 trigger_deep（直接置 artifact_state='queued' +
        # artifact_priority=100），不依赖这里。
        #
        # 已是终态（done/empty/claimed/queued）的行一律不动：只有历史遗留的
        # 'skipped'（旧版给失败任务打的占位）收敛成 pending_click，让它将来可被点开。
        "  artifact_state=CASE "
        "      WHEN shallow_queue.artifact_state='skipped' THEN 'pending_click' "
        "      ELSE shallow_queue.artifact_state END, "
        "  state=CASE WHEN shallow_queue.state IN ('done','skipped') "
        "             THEN 'queued' ELSE shallow_queue.state END",
        [(instance_id, cn, idx_map.get(cn), priority, art_init,
          "" if artifact_ready else "任务失败，无交付物") for cn in config_names],
    )
    return conn.total_changes - before


def pick_clicked(conn) -> list[dict]:
    """用户点击「输出」登记的实例（shallow_requests 表）。

    POST /api/instances/{id}/shallow 只写这张表；旧架构的 --shallow-only 靠它决定
    处理哪些实例，但新架构三个进程都不读它 —— 所以点击必须在这里接上，
    否则用户点完什么都不会发生。

    **除了点击登记的，还自动带上「正在跑（running/preparing）」的实例** ——
    实例还在产出就说明结果随时在变，用户点开输出页时应该已经有数据，而不是
    「点一下、等 5 秒、才开始分析」。实测 26 个 running 实例里只有 6 个被点过，
    另外 20 个正在产出却零分析（complete.jsonl 已攒到 230KB）。
    自动跟踪的实例**不写 shallow_requests** —— 那张表的语义是「用户显式要求」，
    污染它会带偏 _clear_shallow_request 的出队逻辑和 output_status 门控。

    status 两态（仅对点击登记的有意义）：
      'queued'   —— 刚点，还没扫过。首轮要从头(offset=0)整扫一遍，之后转 'tracking'
      'tracking' —— 已在增量跟踪。运行中实例每轮只读新增行，结束后自然安静

    **顺序：先未扫过的(queued)，再按登记时间倒序(最新点击优先)**。

    两层排序各有原因：
      1. `req_status='queued'` 排最前 —— 这是「用户刚点、一行都还没投」的实例，
         它在界面上是完全空白的。已 tracking 的实例每轮只增量读几行、早就有结果可看，
         让它们排在后面不影响体感。
      2. 同组内按 `created_at DESC` —— 用户最后点的那个通常就是他正盯着的。
         原来没有 ORDER BY，SQLite 按 shallow_requests 的主键(instance_id 字符串)
         返回，实测结果是**最老的登记(10-31)先扫、最新的(11-03 08:09)最后扫**，
         与 shallow-auto 的「最新优先」(ORDER BY id DESC)口径正好相反。

    注意这只影响「同一轮内先读哪个实例的 jsonl」。tracker 一轮通常 0.2~0.3s
    就把全部实例扫完（增量读只有几行），所以顺序在实例数少时无感；
    但实例数多、或某实例首轮要整扫几十万行时，顺序就决定了谁先拿到结果。
    """
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    rows = conn.execute(
        # 外层包一次：复合查询（UNION ALL）的 ORDER BY 只能引用结果列的位置或
        # 第一个 SELECT 的列名，CASE 表达式里引用别名会报
        # 「2nd ORDER BY term does not match any column」。
        "SELECT * FROM ( "
        # ① 点击登记的（任意状态）
        "SELECT sr.instance_id AS instance_id, sr.status AS req_status, "
        "       ti.config_path AS config_path, ti.status AS status, "
        "       0 AS src, sr.created_at AS ord_at "
        "  FROM shallow_requests sr "
        "  JOIN task_instances ti ON ti.id = sr.instance_id "
        "UNION ALL "
        # ② **正在跑但没点过**的实例：自动跟踪，不需要点击。
        #
        # 实例还在产出就说明结果随时在变，用户点开输出页时应该已经有数据，
        # 而不是「点一下、等 5 秒、才开始分析」。实测 26 个 running 实例里
        # 只有 6 个被点过 —— 另外 20 个正在产出却零分析，complete.jsonl 已经
        # 攒到几十上百 KB（单实例实测 230,571 字节）。
        #
        # req_status 固定 'tracking'：不需要「首轮清零整扫」那套 —— 那是给
        # 「用户点击一个早已跑过的实例」用的（offset 可能已到文件尾）。
        # running 实例是边跑边读，offset 自然从 0 开始递增。
        # 也不写 shallow_requests：那张表的语义是「用户显式要求」，
        # 自动跟踪不该污染它（否则实例结束后 _clear_shallow_request 的
        # 出队逻辑、以及 output_status 门控都会被带偏）。
        f"SELECT ti.id, 'tracking', ti.config_path, ti.status, "
        f"       1 AS src, COALESCE(ti.started_at, ti.created_at) "
        f"  FROM task_instances ti "
        f" WHERE ti.status IN ({marks}) "
        f"   AND NOT EXISTS(SELECT 1 FROM shallow_requests sr2 "
        f"                   WHERE sr2.instance_id = ti.id) "
        ") "
        # 排序：点击的优先于自动跟踪的（用户在等）；组内未扫过的优先、再按时间倒序
        "ORDER BY src, "
        "         CASE WHEN req_status='queued' THEN 0 ELSE 1 END, "
        "         ord_at DESC",
        ACTIVE_STATUSES,
    ).fetchall()
    return [dict(r) for r in rows]


# 「实例还在跑」的状态。与 output_worker._pick_running_instances、
# queue_scheduler.OCCUPYING_STATUSES 同口径，别各写一套。
ACTIVE_STATUSES = ("running", "preparing")
# 浅层车道号：实时 = 用户正盯着结果的，背景 = 批量补全。
# 与 shallow_worker 的 claim 过滤（priority >= / < 100）对齐。
LANE_CLICK = 100      # 用户点了「输出」—— 他正盯着这个实例，最高优先
LANE_TRACK = 90       # running/preparing 自动跟踪 —— 实时车道，但让位于点击
LANE_AUTO = 0         # 已结束实例的历史补全 —— 背景车道
# 实时车道的下界：>= 这个值走 shallow-realtime，< 走 shallow-auto。
# 取 90 而不是 100，是为了让「自动跟踪」也享受实时车道的低延迟，
# 同时点击（100）在同一车道内仍能插到它前面（claim 按 priority DESC 排序）。
LANE_REALTIME_MIN = LANE_TRACK
# 兼容旧名（外部可能 import）
LANE_REALTIME = LANE_CLICK


def _short_iid(instance_id: str) -> str:
    """instance_id 缩写，用于日志。

    id 的形状是 `<MMDDHHmm>-<用户><时间戳>-<4位后缀>`，例如
    `2611031754-wanyi26110317530533-70b2`。中段的用户名+时间戳最长且信息重复
    （前段已有时间），所以只留**首段时间 + 末段后缀**：`2611031754-70b2`。
    这两段合起来足够在日志里唯一定位一个实例，且长度固定 15 字符。

    不用 `name`：实测同一批次的 name 只差末尾几个字（「…lego」/「…lego-copy」/
    「…lego-copy-copy」），截断后完全分不出谁是谁；而且 name 最长 50+ 字符、
    含中文，对齐很难看。
    """
    parts = instance_id.split("-")
    if len(parts) >= 3:
        return f"{parts[0]}-{parts[-1]}"
    return instance_id[:16]


def realtime_lane_pending(conn) -> int:
    """实时车道当前待办行数（两阶段任一维还在排队）。

    阶段 2 必须带 EXISTS 子查询，与 shallow_obs.claim 的条件完全一致 ——
    线上有大量 `obs_state='queued'` 但 `traj_level` 早已写好的遗留行，
    worker 根本不会领，算进水位会让 tracker 误以为队列很满、永远不投新行。
    """
    return conn.execute(
        "SELECT COUNT(*) FROM shallow_queue sq "
        " WHERE sq.priority >= ? AND ("
        "   sq.local_state='queued' OR ("
        "     sq.obs_state='queued' AND EXISTS("
        "       SELECT 1 FROM task_records tr "
        "        WHERE tr.instance_id=sq.instance_id "
        "          AND tr.config_name=sq.config_name "
        "          AND tr.traj_level IS NULL AND tr.task_done IS NOT NULL)))",
        (LANE_REALTIME_MIN,),
    ).fetchone()[0]


def _lane_priority(inst_status: str | None, clicked: bool = False) -> int:
    """决定浅层车道号。三档：

    | 来源 | priority | 车道 | 理由 |
    |------|----------|------|------|
    | 用户点了「输出」 | 100 (LANE_CLICK) | 实时 | 他正盯着这个实例,最高优先 |
    | running/preparing 自动跟踪 | 90 (LANE_TRACK) | 实时 | 实例还在产出,要秒级增量;但让位于点击 |
    | 已结束实例的历史补全 | 0 (LANE_AUTO) | 背景 | 吞吐活,交给 64 并发吃满 |

    **点击优先于自动跟踪**是关键：两者都在实时车道（claim 条件
    `priority >= LANE_REALTIME_MIN`），而 claim 内部按 `priority DESC` 排序，
    所以用户随时点一下就能插到自动跟踪的几万行前面。
    改前两者都是 100，无法区分，用户点击只能和自动跟踪的行一起排队
    （实测实时车道积压 26,650 条，新点的实例要等 37 分钟）。

    **点击的已结束实例也走 100**：用户显式要求看结果，不该因为实例已结束就被
    降到背景车道去排几十万条历史的队。所以 clicked=True 时不看 status。
    """
    if clicked:
        return LANE_CLICK
    return LANE_TRACK if inst_status in ACTIVE_STATUSES else LANE_AUTO


def _reset_offsets(conn, instance_id: str) -> None:
    """把某实例的 tracker_state 偏移清零，让下一次 read_new_lines 从头整扫。

    用于「刚点击」那一轮：实例此前可能被（旧版自动发现）跟踪过、offset 已到文件尾，
    若不清零，点击后 read_new_lines 读不到任何「新增」行，等于点了没反应。
    清零后首轮把已完成的 config 整批投一遍，之后恢复增量。
    """
    conn.execute("DELETE FROM tracker_state WHERE instance_id=?", (instance_id,))


def _submit_incremental(conn, instance_id: str, out_dir: str, priority: int) -> tuple[int, int]:
    """按 offset 增量读该实例的 complete/failed.jsonl，投递新增 config。

    返回 (新增行数, 新投递数)。成功/失败仍分开投，但**只影响备注文案** ——
    产物维恒为 'pending_click'（交付物只在用户点开详情时下载），不再按成功/失败
    决定是否入下载队列。
    """
    done_names: list[str] = []
    failed_names: list[str] = []
    for rec in RECORDS:
        path = find_records(out_dir, rec)
        if not path:
            continue
        try:
            got = read_new_lines(conn, instance_id, path)
        except Exception as e:
            print(f"  [warn] {instance_id} 读 {rec} 失败: {e}", flush=True)
            continue
        (done_names if rec == "complete.jsonl" else failed_names).extend(got)

    n_lines = len(done_names) + len(failed_names)
    # 同一条 config 可能同时出现在 complete 和 failed（重跑后先失败又成功）。
    # 成功优先：在 complete.jsonl 里的从 failed 名单摘掉，否则后投的 failed 会把它
    # 覆盖成 skipped，永远下不到交付物。
    done_uniq = list(dict.fromkeys(os.path.basename(n) for n in done_names))
    done_set = set(done_uniq)
    failed_uniq = [n for n in dict.fromkeys(
        os.path.basename(x) for x in failed_names) if n not in done_set]

    n_sub = 0
    if done_uniq:
        n_sub += submit_batch(conn, instance_id, done_uniq,
                              priority=priority, artifact_ready=True)
    if failed_uniq:
        # 点击是无条件的：失败也要把日志/轨迹拉回来，所以 priority>0 时产物也放行
        n_sub += submit_batch(conn, instance_id, failed_uniq,
                              priority=priority, artifact_ready=(priority > 0))
    return n_lines, n_sub


def run_once(limit: int, max_pending: int = DEFAULT_MAX_PENDING) -> dict:
    """投递「点击过的」+「正在跑的」实例，且**只投新增 config**。

    点击驱动 + running 自动跟踪：已结束且没点过的实例不在这里处理
    （交给 offline/auto_tracker.py 按实例逐个补全）。
    点击后按 offset 增量读 jsonl（tracker_state 记录偏移），不每轮全量重拉：
      · 首轮（status='queued'）：清零 offset → 从头整扫已完成的 config → 转 'tracking'
      · 之后（status='tracking'）：只读新增行，运行中实例新完成的 config 逐轮带进来
    `limit` 参数保留仅为兼容 CLI，不再用于发现范围。
    """
    stats = {"instances": 0, "lines": 0, "submitted": 0, "clicked": 0,
             "pending": 0, "throttled": 0}
    with get_connection() as conn:
        # 水位：实时车道积压过深时，本轮**只投点击的实例**（用户在等），
        # 自动跟踪的跳过、下轮再说。offset 不推进，数据不会丢。
        pending = realtime_lane_pending(conn)
        stats["pending"] = pending
        throttle = max_pending > 0 and pending >= max_pending

        for inst in pick_clicked(conn):
            iid = inst["instance_id"]
            clicked = inst.get("src") == 0
            if throttle and not clicked:
                stats["throttled"] += 1
                continue
            out_dir = resolve_output_dir(inst["config_path"])
            if not out_dir:
                continue
            stats["instances"] += 1

            # 刚点那一轮：清零偏移，保证从头整扫一次（否则旧 offset 会把已完成的
            # config 当成「已读」跳过），随后标记为 tracking 进入增量模式。
            if inst.get("req_status") == "queued":
                _reset_offsets(conn, iid)
                conn.execute(
                    "UPDATE shallow_requests SET status='tracking' WHERE instance_id=?",
                    (iid,))

            # src=0 是点击登记的（pick_clicked 的 UNION 第一段），src=1 是自动跟踪。
            # 点击走 100、自动跟踪走 90，两者同在实时车道但点击能插队。
            n_lines, n_sub = _submit_incremental(
                conn, iid, out_dir,
                priority=_lane_priority(inst.get("status"), clicked=clicked))
            stats["lines"] += n_lines
            stats["submitted"] += n_sub
            stats["clicked"] += n_sub
        conn.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="浅层分析 task_tracker：点击驱动，5s 增量投递")
    ap.add_argument("--once", action="store_true", help="跑一轮即退出")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"轮询间隔秒（默认 {DEFAULT_INTERVAL}）")
    ap.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING,
                    help=f"实时车道水位上限，超过就暂停投递自动跟踪的实例"
                         f"（默认 {DEFAULT_MAX_PENDING}，0=不限）")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="已废弃：纯点击驱动不再按范围发现，保留仅为兼容")
    a = ap.parse_args()

    print(f"[tracker] 启动: 点击驱动 interval={a.interval}s", flush=True)
    # 自愈补列：shallow_queue 的 artifact 维（产物下载队列）也由本进程写入，
    # 若只改了代码没重启 uvicorn，SQL 会 no such column，症状只是「投递=0」。
    ensure_shallow_extra_columns()
    write_worker_started(WORKER_NAME, os.getpid(),
                         f"started interval={a.interval}s limit={a.limit}")
    while True:
        t0 = time.time()
        st: dict = {}
        try:
            st = run_once(a.limit, a.max_pending)
            if st["submitted"] or st["lines"]:
                print(f"[tracker] 跟踪={st['instances']} 新行={st['lines']} "
                      f"投递={st['submitted']} 水位={st['pending']:,} "
                      f"{time.time()-t0:.2f}s", flush=True)
            elif st.get("throttled"):
                # 水位满时每轮打一行，否则看起来像 tracker 挂了
                print(f"[tracker] 水位={st['pending']:,} ≥ {a.max_pending:,}，"
                      f"暂停 {st['throttled']} 个自动跟踪实例（点击的不受限）", flush=True)
            # 每轮都写心跳（哪怕本轮没有新行）：监控页靠最后心跳时间判断进程存活。
            # processed 记「新投递数」，这才是 tracker 的产出；scanned 放 note 里。
            write_heartbeat(
                WORKER_NAME, os.getpid(), t0, time.time(),
                st.get("submitted", 0), 0,
                f"实例={st.get('instances',0)} 新行={st.get('lines',0)}",
            )
        except KeyboardInterrupt:
            print("[tracker] 收到中断，退出", flush=True)
            return
        except Exception as e:
            print(f"[tracker] 本轮异常: {e}", flush=True)
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        if a.once:
            break
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    main()
