# -*- coding: utf-8 -*-
"""资源看板聚合逻辑测试（unittest，无 pytest 依赖）。

覆盖 api/services/stats.py 的纯函数与 instances 的 OS 派生：
- sandbox_type 派生优先级与 inferred 标记
- used/queued 口径与 concurrent_num 权重
- 用户排行榜、池归属（含未配置池）
- 趋势扫描线重建（重叠区间、缺 stopped_at 的脏数据、分桶）

运行：cd platform && python -m unittest test.test_stats -v
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.routers import instances  # noqa: E402
from api.services import stats  # noqa: E402


def _inst(**kw):
    base = {
        "id": "x", "status": "running", "created_by": "alice",
        "concurrent_num": 10, "cpu_request": 2.0, "cpu_limit": 3.0,
        "memory_request": 6.0, "memory_limit": 8.0,
        "harness_type": "openclaw", "sandbox_type": "linux",
        "sandbox_type_inferred": False,
        "sandbox_namespace": "pool-a",
        "started_at": None, "stopped_at": None, "prepare_started_at": None,
    }
    base.update(kw)
    return base


class DeriveSandboxTypeTest(unittest.TestCase):
    def test_known_keys(self):
        self.assertEqual(instances._derive_sandbox_type("x86_cpu", None), ("linux", False))
        self.assertEqual(instances._derive_sandbox_type("windows", None), ("windows", False))

    def test_unknown_key_inferred(self):
        self.assertEqual(instances._derive_sandbox_type("windows_arm", None), ("windows", True))
        self.assertEqual(instances._derive_sandbox_type("riscv", None), ("linux", True))

    def test_config_type_fallback(self):
        self.assertEqual(instances._derive_sandbox_type(None, "windows"), ("windows", False))
        self.assertEqual(instances._derive_sandbox_type(None, "linux"), ("linux", False))

    def test_empty_is_inferred_linux(self):
        self.assertEqual(instances._derive_sandbox_type(None, None), ("linux", True))


class SummaryTest(unittest.TestCase):
    def test_used_excludes_queued(self):
        rows = [
            _inst(status="running", concurrent_num=10),
            _inst(status="preparing", concurrent_num=4),
            _inst(status="queued", concurrent_num=100),
        ]
        s = stats.aggregate_summary(rows)
        self.assertEqual(s["used_cpu"], 28.0)
        self.assertEqual(s["queued_cpu"], 200.0)
        self.assertEqual(s["used_memory_gb"], 84.0)
        self.assertEqual(s["pods"], 14)
        self.assertEqual(s["active"], 2)
        self.assertEqual(s["queued"], 1)

    def test_limit_columns(self):
        s = stats.aggregate_summary([_inst(status="running", concurrent_num=2)])
        self.assertEqual(s["used_cpu_limit"], 6.0)
        self.assertEqual(s["used_memory_gb_limit"], 16.0)

    def test_ignores_terminal(self):
        rows = [_inst(status="completed"), _inst(status="failed")]
        self.assertEqual(stats.aggregate_summary(rows)["used_cpu"], 0.0)


class DistributionTest(unittest.TestCase):
    def test_status_order_and_zero_fill(self):
        out = stats.status_distribution({"running": 2, "weird": 1})
        self.assertEqual([x["status"] for x in out[:8]], stats.STATUS_ORDER)
        self.assertEqual(len(out), 9)
        self.assertEqual(out[0]["count"], 2)
        self.assertEqual(out[1]["count"], 0)
        self.assertEqual(out[-1], {"status": "weird", "label": "weird", "count": 1})

    def test_harness_grouping(self):
        rows = [
            _inst(harness_type="openclaw", concurrent_num=10),
            _inst(harness_type="hermes", concurrent_num=5),
            _inst(harness_type="hermes", status="queued"),
        ]
        out = stats.harness_distribution(rows)
        self.assertEqual([x["harness_type"] for x in out], ["openclaw", "hermes"])
        self.assertEqual(out[1]["pods"], 5)

    def test_os_comparison_fixed_keys(self):
        out = stats.os_resource_comparison([_inst(sandbox_type="windows", cpu_request=16)])
        self.assertEqual(set(out), {"linux", "windows"})
        self.assertEqual(out["windows"]["cpu"], 160.0)
        self.assertEqual(out["linux"]["cpu"], 0.0)


class LeaderboardTest(unittest.TestCase):
    def test_sorted_by_cpu_and_unknown_user(self):
        rows = [
            _inst(created_by="bob", concurrent_num=2),
            _inst(created_by="", concurrent_num=5),
            _inst(created_by="alice", concurrent_num=10),
            _inst(created_by="alice", status="queued"),
        ]
        out = stats.user_leaderboard(rows)
        self.assertEqual([x["user"] for x in out], ["alice", stats.UNKNOWN_USER, "bob"])
        self.assertEqual(out[0]["cpu"], 20.0)
        self.assertEqual(out[0]["queued"], 1)

    def test_per_os_split(self):
        rows = [
            _inst(created_by="alice", sandbox_type="linux", concurrent_num=10),
            _inst(created_by="alice", sandbox_type="windows", concurrent_num=4),
        ]
        alice = stats.user_leaderboard(rows)[0]
        self.assertEqual(alice["linux_cpu"], 20.0)     # 10 × 2
        self.assertEqual(alice["windows_cpu"], 8.0)     # 4 × 2
        self.assertEqual(alice["cpu"], 28.0)
        self.assertEqual(alice["pods"], 14)


class PoolUsageTest(unittest.TestCase):
    def test_configured_pool_zero_still_listed(self):
        out = stats.pool_usage([], {"pool-a": {"total_cpu": 8, "total_memory_gb": 32}})
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["configured"])
        self.assertEqual(out[0]["used_cpu"], 0.0)

    def test_unconfigured_namespace(self):
        out = stats.pool_usage(
            [_inst(sandbox_namespace="pool-z")],
            {"pool-a": {"total_cpu": 8, "total_memory_gb": 32}},
        )
        z = [x for x in out if x["namespace"] == "pool-z"][0]
        self.assertFalse(z["configured"])
        self.assertEqual(z["total_cpu"], 0.0)
        self.assertEqual(z["used_cpu"], 20.0)

    def test_queued_not_counted_as_used(self):
        out = stats.pool_usage(
            [_inst(status="queued")], {"pool-a": {"total_cpu": 8, "total_memory_gb": 32}}
        )
        self.assertEqual(out[0]["used_cpu"], 0.0)
        self.assertEqual(out[0]["queued_cpu"], 20.0)
        self.assertEqual(out[0]["instances"], 0)


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 10, 19, 12, 0, 0).timestamp()
        self.start = self.end - 3600

    def _fmt(self, ts):
        return datetime.fromtimestamp(ts).isoformat()

    def test_overlap_summed(self):
        rows = [
            _inst(started_at=self._fmt(self.start + 600), stopped_at=self._fmt(self.end)),
            _inst(started_at=self._fmt(self.start + 600), stopped_at=self._fmt(self.end)),
        ]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 300)
        middle = pts[6]
        self.assertEqual(middle["used_cpu"], 40.0)
        self.assertEqual(middle["running"], 2)
        self.assertEqual(pts[0]["used_cpu"], 0.0)

    def test_os_split_sums_to_total(self):
        rows = [
            _inst(sandbox_type="linux", started_at=self._fmt(self.start),
                  stopped_at=self._fmt(self.end)),
            _inst(sandbox_type="windows", started_at=self._fmt(self.start),
                  stopped_at=self._fmt(self.end), concurrent_num=5),
        ]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 300)
        mid = pts[len(pts) // 2]
        self.assertEqual(mid["linux_cpu"], 20.0)
        self.assertEqual(mid["windows_cpu"], 10.0)   # 5 × 2
        self.assertEqual(mid["linux_cpu"] + mid["windows_cpu"], mid["used_cpu"])

    def test_running_instance_keeps_last_point(self):
        # 运行中的实例没有停止时间：末桶不能把它算成已结束而掉到 0
        rows = [_inst(status="running", started_at=self._fmt(self.start + 600),
                      stopped_at=None)]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 300)
        self.assertGreater(pts[-1]["used_cpu"], 0)
        self.assertEqual(pts[-1]["used_cpu"], pts[-2]["used_cpu"])

    def test_last_bucket_not_in_the_future(self):
        now = datetime(2026, 10, 19, 12, 7, 51).timestamp()
        pts = stats.reconstruct_trend([], now - 3600, now, 300)
        last = datetime.strptime(pts[-1]["ts"], "%Y-%m-%d %H:%M").timestamp()
        self.assertLessEqual(last, now)

    def test_orphan_interval_truncated(self):
        # 终态但缺 stopped_at 的脏数据：区间只按 +24h 计，不会一直画到 now
        started = self.end - 100 * 3600
        rows = [_inst(status="completed", started_at=self._fmt(started), stopped_at=None)]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 3600)
        self.assertTrue(all(p["used_cpu"] == 0 for p in pts))

        # 同样的截断规则下，仍在 24h 内的孤儿实例应正常计入（裁剪后的结束边界与窗口右端
        # 重合，末点会在该事件上归零，取首点断言）
        started = self.end - 10 * 3600
        rows = [_inst(status="completed", started_at=self._fmt(started), stopped_at=None)]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 3600)
        self.assertEqual(pts[0]["used_cpu"], 20.0)

    def test_no_start_is_skipped(self):
        pts = stats.reconstruct_trend([_inst(status="queued")], self.start, self.end, 3600)
        self.assertTrue(all(p["used_cpu"] == 0 for p in pts))

    def test_short_instance_visible_in_wide_bucket(self):
        # 1 小时桶里只跑了 10 秒的实例：峰值采样必须看得到
        s = self.start + 1800
        rows = [_inst(started_at=self._fmt(s), stopped_at=self._fmt(s + 10))]
        pts = stats.reconstruct_trend(rows, self.start, self.end, 3600)
        self.assertEqual(pts[0]["used_cpu"], 20.0)
        self.assertEqual(pts[1]["used_cpu"], 0.0)

    def test_bucket_count_bounded(self):
        for hours, step in ((1, 60), (24, 300), (72, 900), (168, 3600), (720, 14400)):
            self.assertEqual(stats.bucket_seconds(hours), step)
            self.assertLessEqual(hours * 3600 / step, 360)

    def test_parse_ts_formats(self):
        self.assertIsNotNone(stats.parse_ts("2026-10-19T12:00:00"))
        self.assertIsNotNone(stats.parse_ts("2026-10-19 12:00:00"))
        self.assertIsNone(stats.parse_ts(None))
        self.assertIsNone(stats.parse_ts("nope"))


if __name__ == "__main__":
    unittest.main()
