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
        # 默认让全局上限足够高，队列自己的 max_concurrent 决定并发；
        # 验证全局封顶的用例会自己把 QUEUE_MAX_CONCURRENT 调低。
        self._old_global_max = db.settings.QUEUE_MAX_CONCURRENT
        db.settings.QUEUE_MAX_CONCURRENT = 10
        self._create_queue("q1")

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        db.settings.ORPHAN_PREPARING_GRACE_SECONDS = self._old_grace
        db.settings.QUEUE_MAX_CONCURRENT = self._old_global_max
        self._tmpdir.cleanup()

    # ---- helpers ----

    def _create_queue(self, queue_id, max_concurrent=1):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_queues (id, name, max_concurrent, description) "
                "VALUES (?, ?, ?, '')",
                (queue_id, queue_id, max_concurrent),
            )

    def _add_task(self, task_id, status="queued", queue_id="q1", pos=1,
                  concurrent_num=10, created_at=None, total_tasks=0,
                  completed_tasks=0, failed_tasks=0):
        with db.get_connection() as conn:
            conn.execute(
                """INSERT INTO task_instances
                   (id, name, config_path, status, total_tasks, completed_tasks, failed_tasks,
                    concurrent_num, created_at, queue_id, queue_position)
                   VALUES (?, ?, '/tmp/none.yaml', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, task_id, status, total_tasks, completed_tasks, failed_tasks,
                 concurrent_num, created_at or datetime.now().isoformat(), queue_id, pos),
            )

    def _set_queue_max(self, n, queue_id="q1"):
        with db.get_connection() as conn:
            conn.execute("UPDATE task_queues SET max_concurrent=? WHERE id=?", (n, queue_id))

    def _set_tail_skip(self, enabled=True, percent=10, queue_id="q1"):
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_queues SET tail_skip_enabled=?, tail_skip_percent=? WHERE id=?",
                (1 if enabled else 0, percent, queue_id),
            )

    def _set_progress(self, task_id, total, completed, failed=0):
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET total_tasks=?, completed_tasks=?, failed_tasks=? WHERE id=?",
                (total, completed, failed, task_id),
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


class TailSkipTest(QueueTestBase):
    """长尾跳过：剩余任务数 < 沙箱并发量 × 阈值% 时提前放行下一个

    判定口径是「剩余工作量喂不满一轮并发」，分母是该任务的 concurrent_num，
    不是任务总数。
    """

    def _running(self, completed, total=100, concurrent=100):
        self._add_task("a", status="running", pos=1, concurrent_num=concurrent)
        self._set_progress("a", total=total, completed=completed)

    def test_disabled_keeps_strict_serial(self):
        """未开长尾跳过：即使剩余为 0 也不放行。"""
        self._running(completed=100)
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_not_in_tail_window(self):
        """并发 100、阈值 5 → 阈值 5；剩余 6 不放行。"""
        self._set_tail_skip(True, 5)
        self._running(completed=94)   # 剩余 6
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_in_tail_window(self):
        """并发 100、阈值 5 → 阈值 5；剩余 4 放行。"""
        self._set_tail_skip(True, 5)
        self._running(completed=96)   # 剩余 4
        self._add_task("b", pos=2)
        claimed = self._claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], "b")

    def test_strictly_less_than(self):
        """剩余正好等于阈值（5）时不放行——用户给定公式是严格小于。"""
        self._set_tail_skip(True, 5)
        self._running(completed=95)   # 剩余 5
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_custom_percent_respected(self):
        """阈值 10 → 阈值 10；剩余 9 放行。"""
        self._set_tail_skip(True, 10)
        self._running(completed=91)   # 剩余 9
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())

    def test_same_progress_differs_by_percent(self):
        """同一进度，阈值 10 放行、阈值 5 不放行。"""
        self._running(completed=91)   # 剩余 9
        self._add_task("b", pos=2)
        self._set_tail_skip(True, 5)
        self.assertIsNone(self._claim())
        self._set_tail_skip(True, 10)
        self.assertIsNotNone(self._claim())

    def test_failed_tasks_count_toward_progress(self):
        """失败的任务同样算「已处理」，与终态判定口径一致。"""
        self._set_tail_skip(True, 5)
        self._add_task("a", status="running", pos=1, concurrent_num=100)
        self._set_progress("a", total=100, completed=93, failed=4)  # 剩余 3 < 5
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())

    def test_denominator_is_concurrency_not_total(self):
        """总量 10000 时，剩余 500 在旧口径（5% of total）会放行；新口径不放行。"""
        self._set_tail_skip(True, 5)
        self._running(completed=9500, total=10000, concurrent=100)
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_large_total_small_remainder(self):
        """总量很大也不影响触发点：剩余 4 < 5 就放行。"""
        self._set_tail_skip(True, 5)
        self._running(completed=9996, total=10000, concurrent=100)
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())

    def test_small_concurrency_floor_one(self):
        """并发 10、阈值 5 → 乘积 0.5，兜底下限为 1：剩余 1 不放行、剩余 0 放行。"""
        self._set_tail_skip(True, 5)
        self._running(completed=9, total=10, concurrent=10)   # 剩余 1
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_small_concurrency_triggers_at_zero_remaining(self):
        self._set_tail_skip(True, 5)
        self._running(completed=10, total=10, concurrent=10)  # 剩余 0
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())

    def test_zero_concurrency_treated_as_one(self):
        """并发量异常为 0 时按 1 处理，剩余 0 才触发。"""
        self._set_tail_skip(True, 5)
        self._running(completed=10, total=10, concurrent=0)
        self._add_task("b", pos=2)
        self.assertIsNotNone(self._claim())

    def test_queue_max_concurrent_two_allows_two(self):
        """队列 max_concurrent=2：平时就能同时跑 2 个。"""
        self._set_queue_max(2)
        self._add_task("a", status="running", pos=1)
        self._add_task("b", pos=2)
        self.assertEqual(self._claim()["id"], "b")

    def test_queue_max_concurrent_two_blocks_third(self):
        self._set_queue_max(2)
        self._add_task("a", status="running", pos=1)
        self._add_task("b", status="running", pos=2)
        self._add_task("c", pos=3)
        self.assertIsNone(self._claim())

    def test_tail_skip_adds_one_on_top_of_queue_max(self):
        """队列 2 + 长尾 1 = 峰值 3；窗口内第 3 个放行。"""
        self._set_queue_max(2)
        self._set_tail_skip(True, 10)
        self._running(completed=96)   # 剩余 4 < 100×10%
        self._add_task("b", status="running", pos=2)
        self._add_task("c", pos=3)
        self.assertEqual(self._claim()["id"], "c")

    def test_tail_skip_peak_blocked(self):
        """队列 2 + 长尾 1 = 3，第 4 个不放行。"""
        self._set_queue_max(2)
        self._set_tail_skip(True, 10)
        self._running(completed=96)
        self._add_task("b", status="running", pos=2)
        self._add_task("c", status="running", pos=3)
        self._add_task("d", pos=4)
        self.assertIsNone(self._claim())

    def test_tail_skip_without_window_uses_queue_max(self):
        """窗口外只允许队列并发 2，第 3 个不放行。"""
        self._set_queue_max(2)
        self._set_tail_skip(True, 10)
        self._running(completed=10)  # 剩余 90，远不在窗口
        self._add_task("b", status="running", pos=2)
        self._add_task("c", pos=3)
        self.assertIsNone(self._claim())

    def test_tail_skip_disabled_ignores_window(self):
        """关闭长尾跳过时，即使进入尾部窗口也只按队列并发放行。"""
        self._set_queue_max(2)
        self._set_tail_skip(False, 10)
        self._running(completed=96)
        self._add_task("b", status="running", pos=2)
        self._add_task("c", pos=3)
        self.assertIsNone(self._claim())

    def test_queue_max_one_tail_skip_two(self):
        """队列 1 + 长尾 1 = 峰值 2。"""
        self._set_queue_max(1)
        self._set_tail_skip(True, 10)
        self._running(completed=96)
        self._add_task("b", pos=2)
        self.assertEqual(self._claim()["id"], "b")
        self._add_task("c", pos=3)
        self.assertIsNone(self._claim())

    def test_global_cap_limits_queue_max(self):
        """全局 QUEUE_MAX_CONCURRENT=1 时，队列 max_concurrent=5 也只能跑 1 个。"""
        db.settings.QUEUE_MAX_CONCURRENT = 1
        self._set_queue_max(5)
        self._add_task("a", status="running", pos=1)
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_global_cap_limits_tail_peak(self):
        """全局上限 2 + 队列 2 + 长尾 1 → 基础 min(2,2)=2，峰值 3；全局不封顶长尾。"""
        db.settings.QUEUE_MAX_CONCURRENT = 2
        self._set_queue_max(2)
        self._set_tail_skip(True, 10)
        self._running(completed=96)
        self._add_task("b", status="running", pos=2)
        self._add_task("c", pos=3)
        self.assertEqual(self._claim()["id"], "c")

    def test_global_cap_one_blocks_even_in_window(self):
        """全局上限 1 + 长尾 1 → 基础 1，窗口内峰值 2。"""
        db.settings.QUEUE_MAX_CONCURRENT = 1
        self._set_queue_max(5)
        self._set_tail_skip(True, 10)
        self._running(completed=96)
        self._add_task("b", pos=2)
        self.assertEqual(self._claim()["id"], "b")
        self._add_task("c", pos=3)
        self.assertIsNone(self._claim())

    def test_total_tasks_zero_does_not_trigger(self):
        """总数未知（0）时无法计算比例，长尾窗口不成立，只按队列并发放行。"""
        self._set_queue_max(1)
        self._set_tail_skip(True, 10)
        self._add_task("a", status="running", pos=1)  # total_tasks=0
        self._add_task("b", pos=2)
        self.assertIsNone(self._claim())

    def test_after_tail_task_finishes_falls_back(self):
        """长尾补位任务结束后，额度回落到队列并发。"""
        self._set_queue_max(2)
        self._set_tail_skip(True, 10)
        self._running(completed=96)   # 剩余 4 < 10
        self._add_task("b", status="running", pos=2)
        self._add_task("c", status="running", pos=3)
        self._add_task("d", pos=4)
        # a 结束，b/c 都还在跑（剩余 100%）→ 额度回落为 2，但已有 2 个占用者，d 不放行
        with db.get_connection() as conn:
            conn.execute("UPDATE task_instances SET status='completed' WHERE id='a'")
        self.assertIsNone(self._claim())


class ScheduleQueueTailSkipTest(QueueTestBase):
    """schedule_queue 走的是同一套抢占规则"""

    def test_schedule_releases_next_in_tail_window(self):
        self._set_tail_skip(True, 10)
        self._add_task("a", status="running", pos=1, concurrent_num=100)
        self._set_progress("a", total=100, completed=96)
        self._add_task("b", pos=2)
        self._run_schedule_with_stub("q1")
        self.assertEqual(self._status("b"), "preparing")


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
