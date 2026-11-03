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


if __name__ == "__main__":
    unittest.main(verbosity=2)
