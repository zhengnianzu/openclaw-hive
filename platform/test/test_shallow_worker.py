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


if __name__ == "__main__":
    unittest.main(verbosity=2)
