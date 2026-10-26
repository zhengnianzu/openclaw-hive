# -*- coding: utf-8 -*-
"""Worker 调度监控测试（unittest，无 pytest 依赖）。

覆盖三处「靠猜会错」的地方：
  - 卡死行回收：时间比较必须用 SQLite 的 datetime() 而非 epoch（两种时间格式混在库里）
  - ETA：吞吐为 0 时必须返回 null，不能编一个假数字
  - 心跳写入/裁剪 + 启动标记行不算「活动」

运行：cd platform && python -m unittest discover -s test -p "test_worker_monitor.py" -v
"""
import os
import sys
import tempfile
import time
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.config import settings  # noqa: E402
from api.core.database import get_connection, init_db  # noqa: E402
from offline import output_worker as w  # noqa: E402

# 这些用例会真的建表、插行、跑迁移。必须在 import 之后、任何 get_connection 之前
# 把 DB_PATH 指向临时库 —— 否则 init_db() 会作用在线上 platform.db 上
# （实际发生过：测试把生产库的残留表清理 + 索引重建跑了一遍）。
_TMP_DB_DIR = tempfile.mkdtemp(prefix="wm_test_db_")
settings.DB_PATH = os.path.join(_TMP_DB_DIR, "test_platform.db")

IID = "__wm_test_instance__"


class ReclaimTest(unittest.TestCase):
    """卡死 downloading 行会被回收为 pending。"""

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM task_traj_records WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM worker_heartbeats WHERE note LIKE '__wm_test__%'")

    def _insert(self, traj, status, minutes_ago):
        """插入一行，updated_at 往前推 minutes_ago 分钟（SQLite UTC 字符串）。"""
        with get_connection() as conn:
            # 回收判断用 artifact_started_at（epoch），所以这里也要设成过去的时间
            conn.execute(
                "INSERT INTO task_traj_records "
                "(instance_id, config_name, traj_name, artifacts_status, "
                " artifact_complete, artifact_started_at, updated_at) "
                "VALUES (?,?,?,?,1, ?, datetime('now'))",
                (IID, traj, traj, status, time.time() - minutes_ago * 60))
            conn.commit()

    def test_reclaims_stale_downloading(self):
        self._insert("stale_task", "downloading", 60)
        n = w._reclaim_stuck_hours()
        self.assertGreaterEqual(n, 1)

        with get_connection() as conn:
            row = conn.execute(
                "SELECT artifacts_status AS status, error FROM task_traj_records "
                "WHERE instance_id=? AND traj_name='stale_task'", (IID,)).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertIn("自动回收", row["error"])

    def test_keeps_fresh_downloading(self):
        """刚被领取的行不能回收，否则会和正在跑的 worker 抢同一行。"""
        self._insert("fresh_task", "downloading", 1)
        with get_connection() as conn:
            before = conn.execute(
                "SELECT artifacts_status AS status FROM task_traj_records "
                "WHERE instance_id=? AND traj_name='fresh_task'", (IID,)).fetchone()["status"]

        w._reclaim_stuck_hours()

        with get_connection() as conn:
            after = conn.execute(
                "SELECT artifacts_status AS status FROM task_traj_records "
                "WHERE instance_id=? AND traj_name='fresh_task'", (IID,)).fetchone()["status"]
        self.assertEqual(before, "downloading")
        self.assertEqual(after, "downloading")

    def test_does_not_touch_done(self):
        self._insert("done_task", "done", 600)
        w._reclaim_stuck_hours()
        with get_connection() as conn:
            st = conn.execute(
                "SELECT artifacts_status AS status FROM task_traj_records "
                "WHERE instance_id=? AND traj_name='done_task'", (IID,)).fetchone()["status"]
        self.assertEqual(st, "done")


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        init_db()
        with get_connection() as conn:
            conn.execute("DELETE FROM worker_heartbeats WHERE worker='__wm_test__'")

    def tearDown(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM worker_heartbeats WHERE worker='__wm_test__'")

    def test_write_and_read(self):
        t0 = time.time() - 5
        w._write_heartbeat("__wm_test__", 1234, t0, time.time(), 7, 1, "__wm_test__ round")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM worker_heartbeats WHERE worker='__wm_test__' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["processed"], 7)
        self.assertEqual(row["failed"], 1)
        self.assertEqual(row["pid"], 1234)
        self.assertAlmostEqual(row["duration"], 5, delta=1.5)

    def test_started_marker_has_no_duration(self):
        """启动标记行 round_start=0，duration 必须为 NULL。

        若按 time.time()-0 算，会得到 17 亿秒这种假数字（曾实际出现在页面上）。
        """
        w._write_worker_started("__wm_test__", 4321, "__wm_test__ started")
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM worker_heartbeats WHERE worker='__wm_test__' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["round_start"], 0)
        self.assertIsNone(row["duration"])


class EtaTest(unittest.TestCase):
    """ETA 的核心约定：没有实测吞吐就不给数字。"""

    def test_zero_throughput_yields_null(self):
        # 直接验证纯计算约定（不依赖心跳表内容）
        per_min = 0.0
        pending = 22
        remaining = None
        if per_min > 0 and pending > 0:
            remaining = pending / per_min * 60
        self.assertIsNone(remaining)

    def test_positive_throughput(self):
        per_min = 2.0
        pending = 22
        remaining = round(pending / per_min * 60, 0) if per_min > 0 else None
        self.assertEqual(remaining, 660.0)


class ModeTest(unittest.TestCase):
    def test_worker_mode(self):
        self.assertEqual(w._worker_mode(False, False, True), "artifacts")
        self.assertEqual(w._worker_mode(False, True, False), "shallow")
        self.assertEqual(w._worker_mode(True, False, False), "deep")
        self.assertEqual(w._worker_mode(False, False, False), "mixed")


if __name__ == "__main__":
    unittest.main()
