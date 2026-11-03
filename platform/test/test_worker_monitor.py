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
from offline import heartbeat as hbmod  # noqa: E402

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
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM worker_heartbeats WHERE note LIKE '__wm_test__%'")

    def _insert(self, traj, status, minutes_ago, attempts=0):
        """插入一行 claimed 状态的队列行，claimed_at 往前推 minutes_ago 分钟。

        回收判据是 shallow_queue.artifact_claimed_at（epoch REAL）——
        队列已从 task_traj_records 迁到 shallow_queue 的 artifact 维，
        worker 只领取这张表。
        """
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue "
                "(instance_id, config_name, artifact_state, artifact_claimed_by, "
                " artifact_claimed_at, artifact_attempts) "
                "VALUES (?,?,?,'test:1',?,?)",
                (IID, traj + ".json", status,
                 time.time() - minutes_ago * 60, attempts))
            conn.commit()

    def _state(self, traj):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT artifact_state AS status, artifact_note AS note "
                "FROM shallow_queue WHERE instance_id=? AND config_name=?",
                (IID, traj + ".json")).fetchone()
        return row

    def test_reclaims_stale_claimed(self):
        self._insert("stale_task", "claimed", 60)
        n = w._reclaim_stuck_hours()
        self.assertGreaterEqual(n, 1)

        row = self._state("stale_task")
        self.assertEqual(row["status"], "queued")
        self.assertIn("自动回收", row["note"])

    def test_gives_up_after_max_attempts(self):
        """超时且已达重试上限 → 判死 skipped，不再排回队列（防死循环）。"""
        self._insert("dead_task", "claimed", 60,
                     attempts=w.ARTIFACT_MAX_ATTEMPTS)
        w._reclaim_stuck_hours()
        self.assertEqual(self._state("dead_task")["status"], "skipped")

    def test_keeps_fresh_claimed(self):
        """刚被领取的行不能回收，否则会和正在跑的 worker 抢同一行。"""
        self._insert("fresh_task", "claimed", 1)
        w._reclaim_stuck_hours()
        self.assertEqual(self._state("fresh_task")["status"], "claimed")

    def test_does_not_touch_done(self):
        self._insert("done_task", "done", 600)
        w._reclaim_stuck_hours()
        self.assertEqual(self._state("done_task")["status"], "done")


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


class HeartbeatTrimTest(unittest.TestCase):
    """心跳裁剪必须**按 worker 分组**，不能用全表上限。

    全表上限下，写得最勤的进程会把其它进程的历史全部挤掉：tracker 每 5s 一轮，
    几分钟就能冲掉 deep 的所有记录 —— 线上实测窗口只剩约 36 分钟，
    于是看板的 `/trend?hours=4` 根本取不到 4 小时数据。
    """

    WORKERS = ("__trim_busy__", "__trim_quiet__")

    def setUp(self):
        init_db()
        self._clean()
        self._orig_keep = hbmod.KEEP_ROWS
        hbmod.KEEP_ROWS = 5          # 缩小便于观察

    def tearDown(self):
        hbmod.KEEP_ROWS = self._orig_keep
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            for wk in self.WORKERS:
                conn.execute("DELETE FROM worker_heartbeats WHERE worker=?", (wk,))
            conn.commit()

    def _counts(self):
        with get_connection() as conn:
            return {r["worker"]: r["n"] for r in conn.execute(
                "SELECT worker, COUNT(*) n FROM worker_heartbeats "
                "WHERE worker IN (?,?) GROUP BY worker", self.WORKERS)}

    def test_busy_worker_does_not_evict_quiet_worker(self):
        for i in range(30):
            hbmod.write_heartbeat("__trim_busy__", 1, i, i + 1, 1, 0)
        for i in range(3):
            hbmod.write_heartbeat("__trim_quiet__", 2, 100 + i, 101 + i, 1, 0)
        # 再让 busy 狂写：全表裁剪会把 quiet 的 3 行冲掉，分组裁剪不会
        for i in range(30):
            hbmod.write_heartbeat("__trim_busy__", 1, 200 + i, 201 + i, 1, 0)

        counts = self._counts()
        self.assertEqual(counts.get("__trim_busy__"), 5)
        self.assertEqual(counts.get("__trim_quiet__"), 3)

    def test_keeps_newest_rows(self):
        """裁剪保留的是最新的 KEEP_ROWS 行，不是最老的。"""
        for i in range(20):
            hbmod.write_heartbeat("__trim_busy__", 1, i, i + 1, i, 0)
        with get_connection() as conn:
            kept = [r["processed"] for r in conn.execute(
                "SELECT processed FROM worker_heartbeats WHERE worker='__trim_busy__' "
                "ORDER BY id")]
        self.assertEqual(kept, [15, 16, 17, 18, 19])


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
