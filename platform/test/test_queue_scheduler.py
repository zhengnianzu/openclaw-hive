# -*- coding: utf-8 -*-
"""队列调度器测试（unittest，无 pytest 依赖）。

覆盖 doc/README_queue.md「九、验证要点」的场景，全部使用**隔离的临时 DB**，
不碰生产 platform.db。

运行：cd platform && python -m unittest test.test_queue_scheduler -v
"""
import asyncio
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core import config as config_module  # noqa: E402
from api.core import database as db  # noqa: E402
from api.services import queue_scheduler as qs  # noqa: E402


class QueueTestBase(unittest.TestCase):
    """每个用例一个临时 DB：init_db 建表 → 插入队列与任务。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()
        self._old_grace = db.settings.ORPHAN_PREPARING_GRACE_SECONDS
        db.settings.ORPHAN_PREPARING_GRACE_SECONDS = 120
        self._create_queue("q1")

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        db.settings.ORPHAN_PREPARING_GRACE_SECONDS = self._old_grace
        self._tmpdir.cleanup()

    # ---- helpers ----

    def _create_queue(self, queue_id):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_queues (id, name, max_concurrent, description) "
                "VALUES (?, ?, 1, '')",
                (queue_id, queue_id),
            )

    def _add_task(self, task_id, status="queued", queue_id="q1", pos=1,
                  concurrent_num=10, created_at=None):
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO task_instances
                   (id, name, config_path, status, total_tasks, concurrent_num,
                    created_at, queue_id, queue_position)
                   VALUES (?, ?, '/tmp/none.yaml', ?, 0, ?, ?, ?, ?)""",
                (task_id, task_id, status, concurrent_num,
                 created_at or datetime.now().isoformat(), queue_id, pos),
            )

    def _status(self, task_id):
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT status, queue_position FROM task_instances WHERE id=?", (task_id,)
            ).fetchone()
        return row["status"]

    def _row(self, task_id):
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM task_instances WHERE id=?", (task_id,)
            ).fetchone()
        return dict(row)

    def _claim(self, queue_id="q1"):
        return qs._try_claim_next(queue_id)

    def _run_schedule_with_stub(self, queue_id):
        """跑真实的 schedule_queue，但把 _prepare_and_launch 换成不触碰文件系统的桩。

        否则真实流程会去读 /tmp/none.yaml 并把任务置成 failed，测不到「是否放行」。
        """
        import api.routers.instances as inst_mod
        from unittest.mock import patch

        async def _stub(instance_id, inst):
            return None

        with patch.object(inst_mod, "_prepare_and_launch", _stub):
            asyncio.run(qs.schedule_queue(queue_id))
            asyncio.run(asyncio.sleep(0))  # 让 create_task 落地


class ClaimTest(QueueTestBase):
    """串行阻塞 / preparing 占位 / 顺序"""

    def test_claim_first_queued(self):
        self._add_task("a", pos=1)
        self._add_task("b", pos=2)
        claimed = self._claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], "a")
        self.assertEqual(self._status("a"), "preparing")
        self.assertEqual(self._status("b"), "queued")

    def test_claim_blocked_by_running(self):
        self._add_task("a", status="running", pos=1)
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())
        self.assertEqual(self._status("b"), "queued")

    def test_claim_blocked_by_preparing(self):
        """preparing 也占槽位 —— 只认 running 时本用例会失败（回归提示）。"""
        self._add_task("a", status="preparing", pos=1)
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())
        self.assertEqual(self._status("b"), "queued")

    def test_claim_clears_stale_fields(self):
        self._add_task("a", pos=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET pid=12345, started_at=?, error_summary='x' WHERE id='a'",
                (datetime.now().isoformat(),),
            )
        claimed = self._claim()
        self.assertIsNone(claimed["pid"])
        self.assertIsNone(claimed["started_at"])
        self.assertIsNone(claimed["error_summary"])
        self.assertIsNotNone(claimed["prepare_started_at"])

    def test_claim_order_by_position(self):
        self._add_task("late", pos=9)
        self._add_task("early", pos=3)
        self.assertEqual(self._claim()["id"], "early")

    def test_claim_empty_queue(self):
        self.assertIsNone(self._claim())

    def test_only_one_claim_per_occupied_queue(self):
        self._add_task("a", pos=1)
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())
        self.assertIsNone(self._claim())

    def test_concurrent_claimers_only_one_wins(self):
        """模拟多 worker：并发调度同一队列，只能有一个拿到任务。"""
        for i in range(1, 6):
            self._add_task(f"t{i}", pos=i)
        results = []

        def worker():
            results.append(self._claim())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r]
        self.assertEqual(len(winners), 1)
        with db.get_connection() as conn:
            occupying = conn.execute(
                "SELECT COUNT(*) AS c FROM task_instances WHERE status IN ('preparing','running')"
            ).fetchone()["c"]
        self.assertEqual(occupying, 1)


class ScheduleQueueTest(QueueTestBase):
    """schedule_queue：占用时不动、无占用者时放行（不再受全局容量限制）"""

    def test_schedule_noop_when_occupied(self):
        self._add_task("a", status="running", pos=1)
        self._add_task("b", pos=2)
        asyncio.run(qs.schedule_queue("q1"))
        self.assertEqual(self._status("b"), "queued")

    def test_schedule_ignores_concurrent_num(self):
        """全局沙箱上限已取消：concurrent_num 再大也不该阻碍放行。"""
        self._add_task("b", pos=1, concurrent_num=10 ** 6)
        self._run_schedule_with_stub("q1")
        self.assertEqual(self._status("b"), "preparing")

    def test_schedule_cross_queue_parallel(self):
        """不同队列互不影响：q1 在跑不影响 q2 放行。"""
        self._create_queue("q2")
        self._add_task("a", status="running", queue_id="q1", pos=1)
        self._add_task("b", queue_id="q2", pos=1)
        self._run_schedule_with_stub("q2")
        self.assertEqual(self._status("b"), "preparing")


class SchedulableScanTest(QueueTestBase):
    """兜底扫描：有排队任务但无占用者的队列要被找出来"""

    def test_finds_idle_queue_with_queued(self):
        self._add_task("a", pos=1)
        self.assertEqual(qs.find_schedulable_queues(), ["q1"])

    def test_skips_occupied_queue(self):
        self._add_task("a", status="running", pos=1)
        self._add_task("b", pos=2)
        self.assertEqual(qs.find_schedulable_queues(), [])

    def test_skips_queue_without_queued(self):
        self._add_task("a", status="created", pos=1)
        self.assertEqual(qs.find_schedulable_queues(), [])


class WatchdogTest(QueueTestBase):
    """recover_stuck_preparing 的时间基准"""

    def test_long_queued_task_not_killed_after_prepare_starts(self):
        """排队很久的任务被领进 preparing 后不应立即被判超时。

        旧实现用 created_at（创建时间，且是本地时间）比较，本用例会失败。
        """
        old = (datetime.now() - timedelta(hours=10)).isoformat()
        self._add_task("a", pos=1, created_at=old)
        claimed = self._claim()
        self.assertIsNotNone(claimed)
        # prepare_started_at 是刚写入的 UTC 时间，不应命中 1800s 阈值
        killed = qs.recover_stuck_preparing(timeout_seconds=1800)
        self.assertEqual(killed, set())
        self.assertEqual(self._status("a"), "preparing")

    def test_stuck_preparing_is_recovered(self):
        self._add_task("a", status="preparing", pos=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET prepare_started_at=datetime('now','-2000 seconds'), pid=NULL "
                "WHERE id='a'"
            )
        killed = qs.recover_stuck_preparing(timeout_seconds=1800)
        self.assertEqual(killed, {"q1"})
        self.assertEqual(self._status("a"), "failed")

    def test_preparing_with_pid_not_recovered(self):
        self._add_task("a", status="preparing", pos=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET prepare_started_at=datetime('now','-2000 seconds'), pid=999999 "
                "WHERE id='a'"
            )
        self.assertEqual(qs.recover_stuck_preparing(timeout_seconds=1800), set())
        self.assertEqual(self._status("a"), "preparing")

    def test_legacy_row_without_prepare_started_at_uses_created_at(self):
        old = (datetime.now() - timedelta(hours=10)).isoformat()
        self._add_task("a", status="preparing", pos=1, created_at=old)
        with db.get_connection() as conn:
            conn.execute("UPDATE task_instances SET prepare_started_at=NULL WHERE id='a'")
        killed = qs.recover_stuck_preparing(timeout_seconds=1800)
        self.assertEqual(killed, {"q1"})


class ResumeTest(QueueTestBase):
    """后端重启兜底"""

    def test_resume_all_queues_schedules_queued_only(self):
        self._add_task("a", pos=1)
        self._add_task("b", status="created", pos=2)
        n = asyncio.run(qs.resume_all_queues())
        self.assertEqual(n, 1)


class OrphanRecoveryTest(QueueTestBase):
    """recover_orphan_preparing 必须带时间阈值，不能误伤刚进入 preparing 的实例"""

    def _orphan_count(self):
        with db.get_connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM task_instances WHERE status='created'"
            ).fetchone()[0]

    def test_just_started_preparing_not_recovered(self):
        """另一个 worker 刚进入 preparing 的实例不能被晚启动的 worker 打回 created。"""
        from api.routers import instances as inst_mod
        self._add_task("a", status="preparing", pos=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET prepare_started_at=datetime('now'), pid=NULL, started_at=NULL "
                "WHERE id='a'"
            )
        self.assertEqual(inst_mod.recover_orphan_preparing(), 0)
        self.assertEqual(self._status("a"), "preparing")

    def test_stale_orphan_is_recovered(self):
        from api.routers import instances as inst_mod
        self._add_task("a", status="preparing", pos=1)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET prepare_started_at=datetime('now','-600 seconds'), "
                "pid=NULL, started_at=NULL WHERE id='a'"
            )
        self.assertEqual(inst_mod.recover_orphan_preparing(), 1)
        self.assertEqual(self._status("a"), "created")


class StatusWriteGuardTest(QueueTestBase):
    """终态落库不能覆盖 stopped（stop/restart 竞态）"""

    def test_terminal_write_does_not_override_stopped(self):
        from api.routers import instances as inst_mod
        self._add_task("a", status="running", pos=1, concurrent_num=10)
        inst = self._row("a")
        inst["total_tasks"] = 0
        # 读快照之后用户点了停止
        with db.get_connection() as conn:
            conn.execute("UPDATE task_instances SET status='stopped' WHERE id='a'")
        inst_mod._sync_instance_status(inst, persist=True)
        self.assertEqual(self._status("a"), "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
