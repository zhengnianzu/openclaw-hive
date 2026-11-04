# -*- coding: utf-8 -*-
"""shallow_worker（按场景分车道的浅层 worker）测试。

覆盖四处「靠猜会错」的地方 —— 都是这次改造的核心契约：
  - 车道隔离：realtime 只领 priority>=100，auto 只领 priority<100，两边不交叉
  - auto 最新优先：ORDER BY id DESC（积压几十万条时新批次要先出结果）
  - 阶段串联：阶段2 必须等阶段1 写出 task_done 才能领（否则会读到不完整的 tsr）
  - 已定级行不再被领：obs_state 残留 'queued' 但 traj_level 已有值的行要跳过
    （线上有 2.5 万行这种，不跳过会让看板报出「181 天」的假积压）

运行：cd platform && python test/test_shallow_worker.py
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.config import settings  # noqa: E402

# 必须在任何 get_connection 之前指向临时库 —— 否则 init_db() 会作用在线上
# platform.db 上（test_worker_monitor 的注释里记着这事真的发生过）。
_TMP_DB_DIR = tempfile.mkdtemp(prefix="sw_test_db_")
settings.DB_PATH = os.path.join(_TMP_DB_DIR, "test_platform.db")

from api.core.database import get_connection, init_db  # noqa: E402
from offline import shallow_local as sl  # noqa: E402
from offline import shallow_obs as so  # noqa: E402
from offline import task_tracker as tt  # noqa: E402
from offline import auto_tracker as at  # noqa: E402
from offline import output_worker as ow  # noqa: E402

IID = "__sw_test_instance__"


class LaneTestBase(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM task_records WHERE instance_id=?", (IID,))
            conn.commit()

    def _seed(self, rows):
        """rows: [(config_name, priority, task_done, traj_level)]，按给定顺序插入。

        插入顺序决定 id 顺序 —— auto 的「最新优先」就是按 id DESC 取，
        所以这个顺序是测试的一部分。
        """
        with get_connection() as conn:
            for i, (cn, pri, done, lvl) in enumerate(rows):
                conn.execute(
                    "INSERT INTO shallow_queue "
                    "(instance_id, config_name, task_idx, priority, "
                    " local_state, obs_state, artifact_state) "
                    "VALUES (?,?,?,?,'queued','queued','pending_click')",
                    (IID, cn, i, pri))
                conn.execute(
                    "INSERT INTO task_records "
                    "(instance_id, config_name, task_idx, status, task_done, traj_level) "
                    "VALUES (?,?,?,'任务成功',?,?)",
                    (IID, cn, i, done, lvl))
            conn.commit()

    def _reset_claims(self):
        """把两维都放回 queued，便于在同一个用例里连续测两条车道。"""
        with get_connection() as conn:
            conn.execute(
                "UPDATE shallow_queue SET local_state='queued', obs_state='queued', "
                "claimed_by=NULL, claimed_at=NULL, obs_claimed_by=NULL, "
                "obs_claimed_at=NULL WHERE instance_id=?", (IID,))
            conn.commit()


class Stage1LaneTest(LaneTestBase):
    """阶段1（本地 DONE 判定）的车道隔离与排序。"""

    def test_realtime_only_claims_clicked_rows(self):
        self._seed([("clk1.json", 100, None, None), ("bg1.json", 0, None, None)])
        with get_connection() as conn:
            got = sl.claim(conn, 10, lane="realtime")
        self.assertEqual([r["config_name"] for r in got], ["clk1.json"])

    def test_auto_only_claims_background_rows(self):
        self._seed([("clk1.json", 100, None, None), ("bg1.json", 0, None, None)])
        with get_connection() as conn:
            got = sl.claim(conn, 10, lane="auto")
        self.assertEqual([r["config_name"] for r in got], ["bg1.json"])

    def test_auto_is_newest_first(self):
        """auto 按 id DESC：最后投递的先处理。

        积压几十万条时，新跑完的批次必须先出结果，而不是排在历史行后面等几天。
        """
        self._seed([("old.json", 0, None, None),
                    ("mid.json", 0, None, None),
                    ("new.json", 0, None, None)])
        with get_connection() as conn:
            got = sl.claim(conn, 10, lane="auto")
        self.assertEqual([r["config_name"] for r in got],
                         ["new.json", "mid.json", "old.json"])

    def test_realtime_is_oldest_first(self):
        """realtime 按 id 升序：同一实例内保持投递序，用户看到的顺序稳定。"""
        self._seed([("a.json", 100, None, None), ("b.json", 100, None, None)])
        with get_connection() as conn:
            got = sl.claim(conn, 10, lane="realtime")
        self.assertEqual([r["config_name"] for r in got], ["a.json", "b.json"])

    def test_lanes_do_not_overlap(self):
        """两条车道各跑一遍，加起来正好覆盖全部行且互不重复。"""
        self._seed([("clk1.json", 100, None, None), ("clk2.json", 100, None, None),
                    ("bg1.json", 0, None, None), ("bg2.json", 0, None, None)])
        with get_connection() as conn:
            rt = {r["config_name"] for r in sl.claim(conn, 10, lane="realtime")}
            au = {r["config_name"] for r in sl.claim(conn, 10, lane="auto")}
        self.assertEqual(rt, {"clk1.json", "clk2.json"})
        self.assertEqual(au, {"bg1.json", "bg2.json"})
        self.assertEqual(rt & au, set())


class Stage2ChainTest(LaneTestBase):
    """阶段2（OBS 定级）的串联依赖与车道隔离。"""

    def test_requires_stage1_task_done(self):
        """task_done IS NULL（阶段1 还没跑完）→ 阶段2 不得领取。

        提前定级会拿到不完整的 tsr。这是两阶段合进一个进程后仍必须保留的顺序约束。
        """
        self._seed([("x.json", 100, None, None)])
        with get_connection() as conn:
            got = so.claim(conn, 10, lane="realtime")
        self.assertEqual(got, [])

    def test_claims_after_stage1_done(self):
        self._seed([("x.json", 100, 1, None)])
        with get_connection() as conn:
            got = so.claim(conn, 10, lane="realtime")
        self.assertEqual([r["config_name"] for r in got], ["x.json"])

    def test_skips_already_graded_rows(self):
        """traj_level 已有值的行不再领 —— 哪怕 obs_state 还残留 'queued'。

        线上有 2.5 万行是这样（旧架构定级成功但队列那一维没推进）。
        不跳过的话 worker 会白领一轮，看板也会把它们算进积压。
        """
        self._seed([("graded.json", 100, 1, "L2")])
        with get_connection() as conn:
            got = so.claim(conn, 10, lane="realtime")
        self.assertEqual(got, [])

    def test_auto_newest_first(self):
        self._seed([("old.json", 0, 1, None), ("new.json", 0, 1, None)])
        with get_connection() as conn:
            got = so.claim(conn, 10, lane="auto")
        self.assertEqual([r["config_name"] for r in got], ["new.json", "old.json"])

    def test_lane_isolation(self):
        self._seed([("clk.json", 100, 1, None), ("bg.json", 0, 1, None)])
        with get_connection() as conn:
            rt = [r["config_name"] for r in so.claim(conn, 10, lane="realtime")]
        self._reset_claims()
        with get_connection() as conn:
            au = [r["config_name"] for r in so.claim(conn, 10, lane="auto")]
        self.assertEqual(rt, ["clk.json"])
        self.assertEqual(au, ["bg.json"])


class NoLaneBackCompatTest(LaneTestBase):
    """lane=None 时保持旧行为（两个旧 CLI 仍按原样工作）。"""

    def test_stage1_no_lane_claims_both(self):
        self._seed([("clk.json", 100, None, None), ("bg.json", 0, None, None)])
        with get_connection() as conn:
            got = {r["config_name"] for r in sl.claim(conn, 10)}
        self.assertEqual(got, {"clk.json", "bg.json"})

    def test_stage1_no_lane_prioritises_clicks(self):
        """不分车道时仍按 priority DESC —— 点击的行排在背景行前面。"""
        self._seed([("bg.json", 0, None, None), ("clk.json", 100, None, None)])
        with get_connection() as conn:
            got = [r["config_name"] for r in sl.claim(conn, 10)]
        self.assertEqual(got[0], "clk.json")

    def test_stage2_no_lane_claims_both(self):
        self._seed([("clk.json", 100, 1, None), ("bg.json", 0, 1, None)])
        with get_connection() as conn:
            got = {r["config_name"] for r in so.claim(conn, 10)}
        self.assertEqual(got, {"clk.json", "bg.json"})


class TrackerOrderTest(unittest.TestCase):
    """tracker 的实例投递顺序：先未扫过的，再按登记时间倒序（最新点击优先）。

    原来 pick_clicked 没有 ORDER BY，SQLite 按 shallow_requests 主键
    （instance_id 字符串）返回 —— 实测最老的登记先扫、最新的最后扫，
    与 shallow-auto 的「最新优先」口径正好相反。
    """

    IIDS = ("__tk_old__", "__tk_mid__", "__tk_new__", "__tk_fresh__")

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            for i in self.IIDS:
                conn.execute("DELETE FROM shallow_requests WHERE instance_id=?", (i,))
                conn.execute("DELETE FROM task_instances WHERE id=?", (i,))
            conn.commit()

    def _seed(self):
        """造 4 个登记：3 个 tracking（时间递增）+ 1 个 queued（最老时间）。

        queued 那条**故意给最老的登记时间**：它必须靠 status 排到最前，
        以此区分「按 status 优先」与「单纯按时间倒序」。
        """
        rows = [
            ("__tk_old__",   "tracking", "2026-11-01 00:00:00"),
            ("__tk_mid__",   "tracking", "2026-11-02 00:00:00"),
            ("__tk_new__",   "tracking", "2026-11-03 00:00:00"),
            ("__tk_fresh__", "queued",   "2026-10-01 00:00:00"),
        ]
        with get_connection() as conn:
            for iid, st, ts in rows:
                conn.execute(
                    "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                    "VALUES (?,?,?,'finished',?)", (iid, iid, "/nonexistent.yaml", ts))
                conn.execute(
                    "INSERT INTO shallow_requests (instance_id, created_by, status, created_at) "
                    "VALUES (?,'t',?,?)", (iid, st, ts))
            conn.commit()

    def test_queued_first_then_newest(self):
        self._seed()
        with get_connection() as conn:
            got = [r["instance_id"] for r in tt.pick_clicked(conn)
                   if r["instance_id"] in self.IIDS]
        # queued 的排最前（哪怕它登记时间最老），其余按时间倒序
        self.assertEqual(got, ["__tk_fresh__", "__tk_new__", "__tk_mid__", "__tk_old__"])

    def test_running_auto_tracked_without_click(self):
        """running 实例不需要点击就被跟踪 —— 实例还在产出,结果随时在变。

        实测 26 个 running 实例只有 6 个被点过,另外 20 个正在产出却零分析。
        """
        iid = "__tk_autorun__"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                "VALUES (?,?,?,'running',datetime('now'))",
                (iid, iid, "/nonexistent.yaml"))
            conn.commit()
            got = {r["instance_id"]: dict(r) for r in tt.pick_clicked(conn)}
            conn.execute("DELETE FROM task_instances WHERE id=?", (iid,))
            conn.commit()
        self.assertIn(iid, got, "running 实例必须被自动跟踪")
        self.assertEqual(got[iid]["req_status"], "tracking")
        self.assertEqual(got[iid]["src"], 1, "src=1 表示自动跟踪（非点击）")

    def test_clicked_before_auto_tracked(self):
        """点击的排在自动跟踪的前面 —— 用户在等。"""
        self._seed()
        auto = "__tk_autorun2__"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                "VALUES (?,?,?,'running',datetime('now'))",
                (auto, auto, "/nonexistent.yaml"))
            conn.commit()
            rows = tt.pick_clicked(conn)
            conn.execute("DELETE FROM task_instances WHERE id=?", (auto,))
            conn.commit()
        srcs = [r["src"] for r in rows]
        self.assertEqual(srcs, sorted(srcs), "src=0(点击) 必须全部排在 src=1(自动) 之前")

    def test_ended_unclicked_not_tracked(self):
        """已结束且没点过的实例不在这里处理 —— 交给 auto_tracker 逐个补全。"""
        iid = "__tk_endedonly__"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                "VALUES (?,?,?,'finished',datetime('now'))",
                (iid, iid, "/nonexistent.yaml"))
            conn.commit()
            got = {r["instance_id"] for r in tt.pick_clicked(conn)}
            conn.execute("DELETE FROM task_instances WHERE id=?", (iid,))
            conn.commit()
        self.assertNotIn(iid, got)

    def test_newest_tracking_before_oldest(self):
        """只有 tracking 时，最新登记的必须排在最老的前面。"""
        self._seed()
        with get_connection() as conn:
            conn.execute("UPDATE shallow_requests SET status='tracking' "
                         "WHERE instance_id='__tk_fresh__'")
            conn.commit()
            got = [r["instance_id"] for r in tt.pick_clicked(conn)
                   if r["instance_id"] in self.IIDS]
        self.assertEqual(got[0], "__tk_new__")
        self.assertEqual(got[-1], "__tk_fresh__")   # 登记时间最老 → 最后


class LanePriorityTest(unittest.TestCase):
    """tracker 按「实例是否还在跑」分车道(_lane_priority)。

    改前写死 priority=100,而 tracker 只扫点击过的实例 → **所有行都进实时车道**,
    shallow-auto 永远分不到活(实测 30 分钟 realtime 处理 8986、auto 只有 266,
    而 auto 的 64 并发全程闲着)。
    """

    def test_active_goes_realtime(self):
        for s in ("running", "preparing"):
            self.assertEqual(tt._lane_priority(s), tt.LANE_REALTIME, s)

    def test_ended_goes_auto(self):
        for s in ("finished", "stopped", "completed", "queued", "created"):
            self.assertEqual(tt._lane_priority(s), tt.LANE_AUTO, s)

    def test_unknown_goes_auto(self):
        """取不到 status 时走背景车道 —— 宁可慢,不要占实时车道。"""
        self.assertEqual(tt._lane_priority(None), tt.LANE_AUTO)
        self.assertEqual(tt._lane_priority("某个没见过的态"), tt.LANE_AUTO)


class SubmitResetTest(unittest.TestCase):
    """submit_batch 的 reset 语义。

    判据必须是显式的 `reset` 参数,**不能用 `excluded.priority > 0`** ——
    priority 现在是车道号:已结束实例的行走背景车道、priority=0,但它们同样是
    用户点「输出」才投进来的,同样需要从 done/skipped 复活。
    """

    IID = "__reset_test__"

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (self.IID,))
            conn.commit()

    def _state(self, conn, cn="a.json"):
        r = conn.execute(
            "SELECT local_state, obs_state, priority FROM shallow_queue "
            "WHERE instance_id=? AND config_name=?", (self.IID, cn)).fetchone()
        return (r["local_state"], r["obs_state"], r["priority"])

    def test_reset_revives_background_lane_rows(self):
        """priority=0(背景车道)的行也必须能被 reset 复活 —— 这是改动的要害。"""
        with get_connection() as conn:
            tt.submit_batch(conn, self.IID, ["a.json"], priority=0, reset=True)
            conn.execute("UPDATE shallow_queue SET local_state='done', obs_state='done' "
                         "WHERE instance_id=?", (self.IID,))
            conn.commit()
            tt.submit_batch(conn, self.IID, ["a.json"], priority=0, reset=True)
            conn.commit()
            self.assertEqual(self._state(conn)[:2], ("queued", "queued"))

    def test_no_reset_keeps_done(self):
        """reset=False(懒发现)不得把已分析完的行打回队列。"""
        with get_connection() as conn:
            tt.submit_batch(conn, self.IID, ["a.json"], priority=0, reset=True)
            conn.execute("UPDATE shallow_queue SET local_state='done', obs_state='done' "
                         "WHERE instance_id=?", (self.IID,))
            conn.commit()
            tt.submit_batch(conn, self.IID, ["a.json"], priority=0, reset=False)
            conn.commit()
            self.assertEqual(self._state(conn)[:2], ("done", "done"))

    def test_priority_never_downgrades(self):
        """车道号取 MAX:实例从 running 变 finished 后,老行保持 100 不回退。

        这是可接受的 —— 那些行在 running 期间已经分析完了,留在实时车道不占活。
        新增的行会按当前 status 走背景车道。
        """
        with get_connection() as conn:
            tt.submit_batch(conn, self.IID, ["a.json"], priority=100, reset=True)
            tt.submit_batch(conn, self.IID, ["a.json"], priority=0, reset=True)
            conn.commit()
            self.assertEqual(self._state(conn)[2], 100)


class AutoTrackerPickTest(unittest.TestCase):
    """auto_tracker 的实例挑选：已结束 + 有缺口 + 没被点过 + 没投过。

    存在意义:task_tracker 纯点击驱动,没点过的实例一行都不投 ——
    实测全库 2126 个已结束实例只有 16 个被跟踪,243 个实例共 301 万行
    永远等不到分析,而 shallow-auto 的 64 并发全程闲着。
    """

    IIDS = ("__at_fin__", "__at_run__", "__at_clicked__",
            "__at_nogap__", "__at_done__")

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            at._ensure_state_table(conn)
            for i in self.IIDS:
                conn.execute("DELETE FROM task_instances WHERE id=?", (i,))
                conn.execute("DELETE FROM task_records WHERE instance_id=?", (i,))
                conn.execute("DELETE FROM shallow_requests WHERE instance_id=?", (i,))
                conn.execute("DELETE FROM auto_tracker_state WHERE instance_id=?", (i,))
            conn.commit()

    def _mk(self, iid, status, gap_rows=3, clicked=False, submitted=False):
        with get_connection() as conn:
            at._ensure_state_table(conn)
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (iid, iid, "/nonexistent.yaml", status))
            for i in range(gap_rows):
                conn.execute(
                    "INSERT INTO task_records (instance_id, config_name, task_idx, "
                    " status, traj_level) VALUES (?,?,?,'任务成功',NULL)",
                    (iid, f"{iid}_{i}.json", i))
            if clicked:
                conn.execute(
                    "INSERT INTO shallow_requests (instance_id, created_by, status) "
                    "VALUES (?,'t','tracking')", (iid,))
            if submitted:
                at._mark_done(conn, iid, 1, "test")
            conn.commit()

    def _pick(self, min_gap=1):
        with get_connection() as conn:
            got = at.pick_one_instance(conn, min_gap)
            conn.commit()
        return got["id"] if got else None

    def test_picks_ended_instance_with_gap(self):
        self._mk("__at_fin__", "finished", gap_rows=5)
        self.assertEqual(self._pick(), "__at_fin__")

    def test_skips_running(self):
        """running 由 task_tracker 负责（还在产出,点击驱动的增量更合适）。"""
        self._mk("__at_run__", "running", gap_rows=5)
        self.assertIsNone(self._pick())

    def test_skips_clicked(self):
        """已登记的交给 task_tracker,本进程不重复投。"""
        self._mk("__at_clicked__", "finished", gap_rows=5, clicked=True)
        self.assertIsNone(self._pick())

    def test_skips_already_submitted(self):
        """投过的不重复选 —— 缺口要等 shallow-auto 慢慢消化,期间 traj_level 仍是 NULL,
        不记状态会每轮重新选到它、每轮整扫一遍 jsonl。"""
        self._mk("__at_done__", "finished", gap_rows=5, submitted=True)
        self.assertIsNone(self._pick())

    def test_skips_no_gap(self):
        """全部已定级的实例没有缺口,不该被选。"""
        self._mk("__at_nogap__", "finished", gap_rows=2)
        with get_connection() as conn:
            conn.execute("UPDATE task_records SET traj_level='L2' WHERE instance_id=?",
                         ("__at_nogap__",))
            conn.commit()
        self.assertIsNone(self._pick())

    def test_min_gap_filters_and_marks(self):
        """缺口小于 min_gap 时跳过,并记一笔 —— 否则每轮都会重新选到它。"""
        self._mk("__at_fin__", "finished", gap_rows=2)
        self.assertIsNone(self._pick(min_gap=100))
        with get_connection() as conn:
            row = conn.execute(
                "SELECT note FROM auto_tracker_state WHERE instance_id=?",
                ("__at_fin__",)).fetchone()
        self.assertIsNotNone(row, "跳过的实例必须记状态,否则会被反复选中")

    def test_lane_is_background(self):
        """本进程恒投背景车道 —— 历史补全是吞吐活,不该占实时车道。"""
        self.assertEqual(at.LANE_AUTO, 0)

    def test_newest_first(self):
        """最近结束的实例先补 —— 刚跑完的自动插队到历史积压前面。"""
        with get_connection() as conn:
            at._ensure_state_table(conn)
            for iid, ts in (("__at_fin__", "2026-07-01 00:00:00"),
                            ("__at_run__", "2026-11-03 00:00:00")):
                conn.execute(
                    "INSERT INTO task_instances (id, name, config_path, status, "
                    " created_at, stopped_at) VALUES (?,?,?,'finished',?,?)",
                    (iid, iid, "/nonexistent.yaml", ts, ts))
                conn.execute(
                    "INSERT INTO task_records (instance_id, config_name, task_idx, "
                    " status, traj_level) VALUES (?,?,0,'任务成功',NULL)",
                    (iid, f"{iid}.json"))
            conn.commit()
        # 11-03 的那个必须先被选中，而不是 7 月的
        self.assertEqual(self._pick(), "__at_run__")


class AutoLanePendingTest(unittest.TestCase):
    """水位控制:背景车道待办超过上限就不投新实例。

    没有水位时本进程是「60s 无条件投一个实例」,而历史实例缺口差异极大
    (实测最大 61,808 行、最小 3 行) —— 连投 5 个大实例就是 20 万行涌入。
    """

    IID = "__wl_test__"

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (self.IID,))
            conn.execute("DELETE FROM task_records WHERE instance_id=?", (self.IID,))
            conn.commit()

    def test_counts_stage1_queued(self):
        with get_connection() as conn:
            before = at.auto_lane_pending(conn)
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, priority, "
                " local_state, obs_state, artifact_state) "
                "VALUES (?,'a.json',0,'queued','done','pending_click')", (self.IID,))
            conn.commit()
            self.assertEqual(at.auto_lane_pending(conn), before + 1)

    def test_ignores_realtime_lane(self):
        """实时车道的行不算进背景水位。"""
        with get_connection() as conn:
            before = at.auto_lane_pending(conn)
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, priority, "
                " local_state, obs_state, artifact_state) "
                "VALUES (?,'b.json',100,'queued','done','pending_click')", (self.IID,))
            conn.commit()
            self.assertEqual(at.auto_lane_pending(conn), before)

    def test_ignores_already_graded_obs_rows(self):
        """obs_state='queued' 但 traj_level 已写好的遗留行不算水位。

        线上有 2.5 万行这种,worker 根本不会领;算进水位会让本进程
        误以为队列很满、永远不投新实例。
        """
        with get_connection() as conn:
            before = at.auto_lane_pending(conn)
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, priority, "
                " local_state, obs_state, artifact_state) "
                "VALUES (?,'c.json',0,'done','queued','pending_click')", (self.IID,))
            conn.execute(
                "INSERT INTO task_records (instance_id, config_name, task_idx, "
                " status, task_done, traj_level) "
                "VALUES (?,'c.json',0,'任务成功',1,'L2')", (self.IID,))
            conn.commit()
            self.assertEqual(at.auto_lane_pending(conn), before)

    def test_run_once_skips_when_full(self):
        """水位满时 run_once 不选实例(也就不读 jsonl)。"""
        st = at.run_once(min_gap=1, max_pending=1)
        self.assertIsNone(st["instance"])
        self.assertEqual(st["submitted"], 0)


class TsrLoggingTest(unittest.TestCase):
    """tsr 拉取的日志降噪:rc=6(对象不存在)只计数,真异常仍逐条打。

    为什么:2026-08 及更早的批次 harness 不写 traj_stats_result.json
    (OBS 上只有 logs/<task>.log + gateway.log),拉不到会自动走
    log_fallback_grade 读本地日志判等级 —— **分析并没有失败**。
    而原来每个任务打一行「OBS 拉 tsr 失败 rc=6」,补历史时一轮几千行,
    把真正的异常彻底淹没。
    """

    def setUp(self):
        ow.tsr_round_summary()      # 清零

    def tearDown(self):
        ow.tsr_round_summary()

    def test_counts_are_summarised(self):
        ow._tsr_count("missing")
        ow._tsr_count("missing")
        ow._tsr_count("fetched")
        s = ow.tsr_round_summary()
        self.assertIn("无tsr", s)
        self.assertIn("=2", s)
        self.assertIn("拉取=1", s)

    def test_summary_resets(self):
        ow._tsr_count("missing")
        self.assertTrue(ow.tsr_round_summary())
        self.assertEqual(ow.tsr_round_summary(), "", "取过一次就该清零")

    def test_summary_empty_when_untouched(self):
        """本轮没碰 tsr 时返回空串 —— 调用方据此不打这一行。"""
        self.assertEqual(ow.tsr_round_summary(), "")

    def test_thread_safe(self):
        """跑在 16~64 线程池里,dict 的 += 不是原子操作,必须加锁。"""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=32) as pool:
            for _ in range(2000):
                pool.submit(ow._tsr_count, "missing")
        self.assertIn("=2000", ow.tsr_round_summary())


class DoneFailedUpsertTest(unittest.TestCase):
    """「任务异常」的行必须 UPSERT task_records,不能只 UPDATE。

    task_records 的行平时由在线 _sync_task_records 写,但实例跑太快结束 /
    在线同步漏掉的 config 在 apply_results 里是**没有行**的 —— 纯 UPDATE
    命中 0 行却把队列置成 done,这个任务就永久消失:页面上既不算已定级、
    也不算未评估,连 total 都不包含它。

    实测全库 23,516 行是这种孤儿;某实例 4,161 个任务里 1,739 个因此丢失,
    页面显示成「未执行 1739」(而其实它们全部跑过且失败了)。
    """

    IID = "__df_test__"

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (self.IID,))
            conn.execute("DELETE FROM task_records WHERE instance_id=?", (self.IID,))
            conn.commit()

    def _queue_row(self, cn, idx=None):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, task_idx, "
                " priority, local_state, obs_state, artifact_state) "
                "VALUES (?,?,?,0,'claimed','queued','pending_click')",
                (self.IID, cn, idx))
            conn.commit()
            return conn.execute(
                "SELECT id FROM shallow_queue WHERE instance_id=? AND config_name=?",
                (self.IID, cn)).fetchone()["id"]

    def test_creates_row_when_missing(self):
        qid = self._queue_row("a.json", 7)
        sl.apply_results([({"id": qid, "instance_id": self.IID,
                            "config_name": "a.json", "task_idx": 7},
                           "done_failed", 0, "status=任务异常")])
        with get_connection() as conn:
            tr = conn.execute(
                "SELECT task_idx, status, traj_level FROM task_records "
                "WHERE instance_id=? AND config_name='a.json'", (self.IID,)).fetchone()
        self.assertIsNotNone(tr, "task_records 没行时必须建出来")
        self.assertEqual(tr["traj_level"], "failed")
        self.assertEqual(tr["status"], "任务异常")
        self.assertEqual(tr["task_idx"], 7)

    def test_does_not_overwrite_existing_grade(self):
        """已定级的行不被覆盖 —— 那可能是别的路径判出的真实等级。"""
        qid = self._queue_row("b.json", 3)
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO task_records (instance_id, config_name, task_idx, "
                " status, traj_level) VALUES (?,?,3,'任务成功','L2')",
                (self.IID, "b.json"))
            conn.commit()
        sl.apply_results([({"id": qid, "instance_id": self.IID,
                            "config_name": "b.json", "task_idx": 3},
                           "done_failed", 0, "x")])
        with get_connection() as conn:
            tr = conn.execute(
                "SELECT status, traj_level FROM task_records "
                "WHERE instance_id=? AND config_name='b.json'", (self.IID,)).fetchone()
        self.assertEqual(tr["traj_level"], "L2")
        self.assertEqual(tr["status"], "任务成功")

    def test_queue_advanced(self):
        """建行的同时两个浅层维都要收掉,不留在队列里空转。"""
        qid = self._queue_row("c.json")
        sl.apply_results([({"id": qid, "instance_id": self.IID,
                            "config_name": "c.json", "task_idx": None},
                           "done_failed", 0, "x")])
        with get_connection() as conn:
            q = conn.execute(
                "SELECT local_state, obs_state FROM shallow_queue WHERE id=?",
                (qid,)).fetchone()
        self.assertEqual(q["local_state"], "done")
        self.assertEqual(q["obs_state"], "done")

    def test_null_task_idx_ok(self):
        """task_idx 为 NULL 也要能建行（tracker 投递时 task_records 还没建行）。"""
        qid = self._queue_row("d.json")
        sl.apply_results([({"id": qid, "instance_id": self.IID,
                            "config_name": "d.json", "task_idx": None},
                           "done_failed", 0, "x")])
        with get_connection() as conn:
            tr = conn.execute(
                "SELECT traj_level FROM task_records "
                "WHERE instance_id=? AND config_name='d.json'", (self.IID,)).fetchone()
        self.assertEqual(tr["traj_level"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
