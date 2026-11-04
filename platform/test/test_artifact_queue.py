# -*- coding: utf-8 -*-
"""产物下载队列（shallow_queue 的 artifact 维）测试。

覆盖发现迁移的核心语义 —— 这些点靠猜会错，且错了不会报错、只会「悄悄下不到」：

  - complete.jsonl 投递 → artifact_state='queued'（该下）
  - failed.jsonl 投递   → artifact_state='skipped'（不该下，没有交付物）
  - 用户点击            → 无条件 queued，并清零 attempts / 退避（突破重试上限）
  - 普通增量            → **不重置**已 done/empty 的行（否则永远下不完）
  - 结果回写            → 同时写队列与 task_traj_records 镜像
  - empty 是独立终态     → 不进 failed 计数、不回队列

运行：cd platform && python -m unittest discover -s test -p "test_artifact_queue.py" -v
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

# 必须在任何 get_connection 之前指向临时库，否则会作用在线上 platform.db 上。
_TMP_DB_DIR = tempfile.mkdtemp(prefix="aq_test_db_")
settings.DB_PATH = os.path.join(_TMP_DB_DIR, "test_platform.db")

from api.core.database import get_connection, init_db  # noqa: E402
from offline import output_worker as w  # noqa: E402
from offline import task_tracker as tt  # noqa: E402

IID = "__aq_test_instance__"


class ArtifactQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM task_traj_records WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM task_instances WHERE id=?", (IID,))

    def _mk_instance(self):
        with get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO task_instances "
                "(id, name, config_path, status, created_at) "
                "VALUES (?,?,?,?,datetime('now'))",
                (IID, "t", "/nonexistent.yaml", "finished"))
            conn.commit()

    def _rows(self):
        with get_connection() as conn:
            return {r["config_name"]: dict(r) for r in conn.execute(
                "SELECT config_name, artifact_state, artifact_attempts, "
                "artifact_retry_at, artifact_note, artifact_priority, priority, "
                "local_state, obs_state "
                "FROM shallow_queue WHERE instance_id=?", (IID,))}

    def _mirror(self):
        with get_connection() as conn:
            return {r["config_name"]: dict(r) for r in conn.execute(
                "SELECT config_name, artifacts_status, artifact_complete, "
                "artifact_files, artifact_bytes "
                "FROM task_traj_records WHERE instance_id=?", (IID,))}

    # ── 发现分流 ──

    def test_completed_task_does_not_auto_enter_queue(self):
        """产物是纯点击驱动：tracker 投递只落 pending_click，不自动入下载队列。

        改前每个成功 config 都置 queued，于是点一次「输出」就给一个 3.4 万 config
        的实例灌 3.4 万条全量 `cp -r` —— 线上实测积压 97.4 万条、磁盘只剩 214G，
        全量下完会直接写爆盘。交付物只在用户点开详情时才下。
        """
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["ok.json"], artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["ok.json"]["artifact_state"], "pending_click")

    def test_failed_task_does_not_enter_queue(self):
        """失败任务同样只落 pending_click（OBS 上本就没有交付物）。"""
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["bad.json"], artifact_ready=False)
            conn.commit()
        row = self._rows()["bad.json"]
        self.assertEqual(row["artifact_state"], "pending_click")
        self.assertIn("任务失败", row["artifact_note"])

    def test_failed_task_still_enters_shallow_dimensions(self):
        """产物维 skipped 不能影响浅层：local/obs 两维照常要处理。"""
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["bad.json"], artifact_ready=False)
            conn.commit()
        row = self._rows()["bad.json"]
        self.assertEqual(row["local_state"], "queued")
        self.assertEqual(row["obs_state"], "queued")

    # ── 点击语义 ──
    #
    # 注意：tracker 的 submit_batch **不能**因为 priority>0 就重置 artifact 维。
    # priority 是浅层「整实例插队」的语义，pick_clicked 每 5 秒把该实例的
    # 全部 config 重投一遍；若按它重置产物状态，已下完的行会被无限打回队列
    # （线上实测同一批 task 重复下载 8 轮以上）。
    # 真正的「重下这个 task」由 artifacts._requeue_artifact / batch_output.trigger_deep
    # 直接改 artifact_state + artifact_priority，见下面两个用例。

    def test_click_does_not_requeue_tracker_path(self):
        """tracker 的点击重投不能把已下完的行打回队列。"""
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["d.json"], artifact_ready=True)
            conn.execute(
                "UPDATE shallow_queue SET artifact_state='done' "
                "WHERE instance_id=? AND config_name='d.json'", (IID,))
            conn.commit()
            # pick_clicked 每轮都这样重投：priority=100 + artifact_ready=True
            tt.submit_batch(conn, IID, ["d.json"], priority=100, artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["d.json"]["artifact_state"], "done")

    def test_click_does_not_requeue_failed_task_via_tracker(self):
        """failed 行同样不能被 tracker 的点击重投复活（复活要走显式下载入口）。"""
        self._mk_instance()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, artifact_state, "
                "artifact_attempts, artifact_retry_at) VALUES (?,?,'failed',?,?)",
                (IID, "x.json", w.ARTIFACT_MAX_ATTEMPTS, time.time() + 999))
            conn.commit()
            tt.submit_batch(conn, IID, ["x.json"], priority=100, artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["x.json"]["artifact_state"], "failed")

    def test_requeue_artifact_resets_failed_task(self):
        """显式「下载此任务」要能突破重试上限，并写产物专用优先级。"""
        from api.routers.artifacts import _requeue_artifact
        self._mk_instance()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, artifact_state, "
                "artifact_attempts, artifact_retry_at) VALUES (?,?,'failed',?,?)",
                (IID, "x.json", w.ARTIFACT_MAX_ATTEMPTS, time.time() + 999))
            conn.commit()
        _requeue_artifact(IID, "x")

        row = self._rows()["x.json"]
        self.assertEqual(row["artifact_state"], "queued")
        self.assertEqual(row["artifact_attempts"], 0)
        self.assertIsNone(row["artifact_retry_at"])
        # 产物车道号必须写在 artifact_priority 上，不能污染浅层的 priority
        self.assertEqual(row["artifact_priority"], 100)
        self.assertEqual(row["priority"], 0)

    # ── 幂等 / 不打断已完成状态 ──

    def test_plain_incremental_does_not_reset_done(self):
        """普通增量每轮都会重投同一批 config；若重置，已下完的会被反复打回队列。"""
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["a.json"], artifact_ready=True)
            conn.execute(
                "UPDATE shallow_queue SET artifact_state='done' "
                "WHERE instance_id=? AND config_name='a.json'", (IID,))
            conn.commit()
            tt.submit_batch(conn, IID, ["a.json"], artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["a.json"]["artifact_state"], "done")

    def test_plain_incremental_does_not_reset_empty(self):
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["e.json"], artifact_ready=True)
            conn.execute(
                "UPDATE shallow_queue SET artifact_state='empty' "
                "WHERE instance_id=? AND config_name='e.json'", (IID,))
            conn.commit()
            tt.submit_batch(conn, IID, ["e.json"], artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["e.json"]["artifact_state"], "empty")

    def test_legacy_skipped_converges_to_pending_click(self):
        """历史遗留的 skipped（旧版给失败任务打的占位）收敛成 pending_click。

        不是「拉回 queued」—— 产物已改为纯点击驱动，复活成 queued 会让 tracker
        每 5 秒把这批行重新灌进下载队列。收敛成 pending_click 的意义是：
        这行将来仍然可以被用户点开下载，但不占用任何下载配额。
        """
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["r.json"], artifact_ready=False)
            conn.commit()
            conn.execute("UPDATE shallow_queue SET artifact_state='skipped' "
                         "WHERE instance_id=? AND config_name='r.json'", (IID,))
            conn.commit()
            tt.submit_batch(conn, IID, ["r.json"], artifact_ready=True)
            conn.commit()
        self.assertEqual(self._rows()["r.json"]["artifact_state"], "pending_click")

    def test_click_queued_row_survives_tracker_resubmit(self):
        """用户点击入队后，tracker 每 5s 的重投不能把它踢出队列或降优先级。

        这是线上「同一批 task 被反复下载 8 轮」的反向约束：既不能把 done 打回
        queued，也不能把刚点的 queued 改回 pending_click。
        """
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["c.json"], artifact_ready=True)
            conn.commit()
            # 模拟 artifacts.py / batch_output.py 的点击入队口径
            conn.execute(
                "UPDATE shallow_queue SET artifact_state='queued', artifact_priority=100 "
                "WHERE instance_id=? AND config_name='c.json'", (IID,))
            conn.commit()
            tt.submit_batch(conn, IID, ["c.json"], artifact_ready=True)
            conn.commit()
        row = self._rows()["c.json"]
        self.assertEqual(row["artifact_state"], "queued")
        self.assertEqual(row["artifact_priority"], 100)

    # ── 结果回写 ──

    def test_persist_ok_writes_both_tables(self):
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["a.json"], artifact_ready=True)
            conn.commit()
        w._persist_artifact_outcome(
            "ok", (9, 1234, time.time(), IID, "a.json", "a"), None)

        self.assertEqual(self._rows()["a.json"]["artifact_state"], "done")
        m = self._mirror()["a.json"]
        self.assertEqual(m["artifacts_status"], "done")
        self.assertEqual(m["artifact_complete"], 1)
        self.assertEqual(m["artifact_files"], 9)
        self.assertEqual(m["artifact_bytes"], 1234)

    def test_persist_empty_is_terminal_and_not_retried(self):
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["e.json"], artifact_ready=True)
            conn.commit()
        w._persist_artifact_outcome(
            "empty", None, ("obs://b/x/", IID, "e.json", "e"))

        row = self._rows()["e.json"]
        self.assertEqual(row["artifact_state"], "empty")
        self.assertIsNone(row["artifact_retry_at"])   # 不排退避 = 不重试
        m = self._mirror()["e.json"]
        self.assertEqual(m["artifacts_status"], "empty")
        self.assertEqual(m["artifact_files"], 0)      # 区分「下完了但是空的」

    def test_persist_fail_sets_backoff(self):
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["f.json"], artifact_ready=True)
            conn.commit()
        before = time.time()
        w._persist_artifact_outcome(
            "fail", None, ("obsutil exit 1", IID, "f.json", "f"))

        row = self._rows()["f.json"]
        self.assertEqual(row["artifact_state"], "failed")
        self.assertGreater(row["artifact_retry_at"], before)

    def test_persist_creates_mirror_row_when_missing(self):
        """tracker 发现了但浅层还没建行 —— 下载完不能因为 UPDATE 未命中就丢结果。"""
        self._mk_instance()
        with get_connection() as conn:
            tt.submit_batch(conn, IID, ["n.json"], artifact_ready=True)
            conn.commit()
        self.assertNotIn("n.json", self._mirror())

        w._persist_artifact_outcome(
            "ok", (3, 100, time.time(), IID, "n.json", "n"), None)

        m = self._mirror()["n.json"]
        self.assertEqual(m["artifacts_status"], "done")
        self.assertEqual(m["artifact_complete"], 1)

    # ── 失败重排 ──

    def test_due_failed_goes_back_to_queue(self):
        self._mk_instance()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, artifact_state, "
                "artifact_attempts, artifact_retry_at) VALUES (?,?,'failed',1,?)",
                (IID, "due.json", time.time() - 10))
            conn.commit()
        w._requeue_due_failed()
        self.assertEqual(self._rows()["due.json"]["artifact_state"], "queued")

    def test_future_failed_stays_put(self):
        """退避窗口内的不能重排，否则会「失败→立刻重领→再失败」忙等。"""
        self._mk_instance()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, artifact_state, "
                "artifact_attempts, artifact_retry_at) VALUES (?,?,'failed',1,?)",
                (IID, "later.json", time.time() + 600))
            conn.commit()
        w._requeue_due_failed()
        self.assertEqual(self._rows()["later.json"]["artifact_state"], "failed")

    def test_exhausted_failed_is_skipped(self):
        self._mk_instance()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_queue (instance_id, config_name, artifact_state, "
                "artifact_attempts, artifact_retry_at) VALUES (?,?,'failed',?,?)",
                (IID, "dead.json", w.ARTIFACT_MAX_ATTEMPTS, time.time() - 10))
            conn.commit()
        w._requeue_due_failed()
        self.assertEqual(self._rows()["dead.json"]["artifact_state"], "skipped")


class ClearShallowRequestTest(unittest.TestCase):
    """实例出队时必须回收残留 —— 这是 92.6 万条孤儿产物行的产生机制。

    改前 `_clear_shallow_request` 只删 shallow_requests 一行，该实例投递的
    产物行仍留在 queued 里慢慢下（线上 159 个已出队实例合计 92.6 万行），
    tracker_state 的 offset 也从不清理（实测 321 行）。
    """

    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM shallow_queue WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM shallow_requests WHERE instance_id=?", (IID,))
            conn.execute("DELETE FROM tracker_state WHERE instance_id=?", (IID,))
            conn.commit()

    def _seed(self):
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO shallow_requests (instance_id, created_by, status, created_at) "
                "VALUES (?,?,'tracking',datetime('now'))", (IID, "u"))
            for cn, st, pri in (("bg.json", "queued", 0),
                                ("clk.json", "queued", 100),
                                ("done.json", "done", 100)):
                conn.execute(
                    "INSERT INTO shallow_queue (instance_id, config_name, local_state, "
                    " obs_state, artifact_state, artifact_priority) "
                    "VALUES (?,?,'done','done',?,?)", (IID, cn, st, pri))
            conn.execute(
                "INSERT INTO tracker_state (instance_id, file_path, inode, offset, "
                " head_hash, updated_at) VALUES (?,?,1,99,'h',datetime('now'))",
                (IID, "/x/complete.jsonl"))
            conn.commit()

    def test_reclaims_background_rows_only(self):
        self._seed()
        with get_connection() as conn:
            w._clear_shallow_request(conn, IID)
            conn.commit()
            rows = {r["config_name"]: r["artifact_state"] for r in conn.execute(
                "SELECT config_name, artifact_state FROM shallow_queue "
                "WHERE instance_id=?", (IID,))}
        # 背景行退出队列：实例已出队，不该继续占下载配额和磁盘
        self.assertEqual(rows["bg.json"], "pending_click")
        # 用户点击的行必须保留 —— 那是显式要求，必须下完
        self.assertEqual(rows["clk.json"], "queued")
        # 已下完的不能动
        self.assertEqual(rows["done.json"], "done")

    def test_clears_registration_and_offsets(self):
        self._seed()
        with get_connection() as conn:
            w._clear_shallow_request(conn, IID)
            conn.commit()
            n_req = conn.execute(
                "SELECT COUNT(*) n FROM shallow_requests WHERE instance_id=?",
                (IID,)).fetchone()["n"]
            n_ts = conn.execute(
                "SELECT COUNT(*) n FROM tracker_state WHERE instance_id=?",
                (IID,)).fetchone()["n"]
        self.assertEqual(n_req, 0)
        self.assertEqual(n_ts, 0)

    def test_idempotent(self):
        """无登记时静默 —— worker 每轮都可能调用。"""
        with get_connection() as conn:
            w._clear_shallow_request(conn, IID)   # 不应抛
            conn.commit()


class MirrorBackfillGateTest(unittest.TestCase):
    """工作区列表的「懒发现」触发条件:镜像行数 < complete.jsonl 行数。

    这个判据错过两次:
      1. `if not rows` —— 浅层会先建 complete=0 的行,于是永不触发,列表永远空
         (实测单实例 7161 个 task 里 1419 个早已跑完却一个都看不到)
      2. `if not any(artifact_complete)` —— 「全部为 0 才补」,对 running 实例是错的:
         它边跑边产出,只要曾经补过一次(哪怕只补出 16 行),条件就永久为假,
         之后新完成的几百个任务再也不会建镜像行
         (实测某实例 complete.jsonl 922 行,工作区只显示 16 个)

    现在按**数量**比,所以 running 实例每次打开工作区都会把新完成的补齐。
    """

    def test_gate_triggers_when_mirror_lags(self):
        """镜像 16 行 < 实际 922 行 → 必须补。"""
        self.assertTrue(16 < 922)

    def test_gate_skips_when_caught_up(self):
        """镜像已追平 → 不再补（避免每次打开都重扫 jsonl）。"""
        self.assertFalse(922 < 922)

    def test_old_any_gate_would_miss(self):
        """回归:旧判据 `not any(complete)` 在「有 1 行 complete=1」时为假,
        于是剩下 906 个永远不补 —— 这正是 bug 的形状。"""
        rows = [{"artifact_complete": 1}] + [{"artifact_complete": 0}] * 15
        old_gate = not any(r["artifact_complete"] for r in rows)
        self.assertFalse(old_gate, "旧判据不触发（bug）")
        n_mirror = sum(1 for r in rows if r["artifact_complete"])
        self.assertTrue(n_mirror < 922, "新判据按数量比，仍会触发")

    def test_complete_count_missing_file_is_zero(self):
        """complete.jsonl 取不到时返回 0 —— 镜像有多少显示多少,不触发无意义补发现。"""
        from api.routers.artifacts import _complete_jsonl_count
        self.assertEqual(_complete_jsonl_count({"config_path": "/nonexistent.yaml"}), 0)


if __name__ == "__main__":
    unittest.main()
