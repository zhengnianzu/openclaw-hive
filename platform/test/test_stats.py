# -*- coding: utf-8 -*-
"""资源看板聚合逻辑测试（unittest，无 pytest 依赖）。

覆盖 api/services/stats.py 的纯函数与 instances 的 OS 派生：
- sandbox_type 派生优先级与 inferred 标记
- used/queued 口径与 concurrent_num 权重
- 用户排行榜、池归属（含未配置池）
- 趋势扫描线重建（重叠区间、缺 stopped_at 的脏数据、分桶）

运行：cd platform && python -m unittest test.test_stats -v
"""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.routers import instances  # noqa: E402
from api.services import export_meta  # noqa: E402
from api.services import proxy  # noqa: E402
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


class SpecDistributionTest(unittest.TestCase):
    """按单沙箱规格（cpu_request × memory_request）聚合，按沙箱数加权。"""

    def test_same_spec_merged(self):
        rows = [
            _inst(cpu_request=2, memory_request=6, concurrent_num=3),
            _inst(cpu_request=2, memory_request=6, concurrent_num=4),
        ]
        out = stats.sandbox_spec_distribution(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["count"], 2)
        self.assertEqual(out[0]["pods"], 7)
        self.assertEqual(out[0]["cpu"], 14.0)
        self.assertEqual(out[0]["memory_gb"], 42.0)

    def test_sorted_by_pods_desc(self):
        rows = [
            _inst(cpu_request=16, memory_request=16, concurrent_num=2),
            _inst(cpu_request=1, memory_request=4, concurrent_num=9),
        ]
        out = stats.sandbox_spec_distribution(rows)
        self.assertEqual([x["cpu_request"] for x in out], [1.0, 16.0])
        self.assertEqual([x["pods"] for x in out], [9, 2])

    def test_queued_excluded(self):
        rows = [_inst(cpu_request=1, memory_request=4, status="queued", concurrent_num=5)]
        self.assertEqual(stats.sandbox_spec_distribution(rows), [])

    def test_label_formatting(self):
        rows = [
            _inst(cpu_request=2, memory_request=6),
            _inst(cpu_request=1.5, memory_request=2.5),
        ]
        out = {x["cpu_request"]: x["label"] for x in stats.sandbox_spec_distribution(rows)}
        self.assertEqual(out[2.0], "2 核 / 6 GiB")     # 整数不带小数尾
        self.assertEqual(out[1.5], "1.5 核 / 2.5 GiB")

    def test_tail_folding_matches_effective_pods(self):
        """与 effective_pods 同口径：尾部只剩少许任务时沙箱数被折下来。"""
        rows = [
            _inst(cpu_request=2, memory_request=6, concurrent_num=8,
                  total_tasks=10, completed_tasks=9),
        ]
        out = stats.sandbox_spec_distribution(rows)
        self.assertEqual(out[0]["pods"], 1)
        self.assertEqual(out[0]["cpu"], 2.0)


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

    def test_groups_and_frames_passed_through(self):
        """机器组明细要透到看板：容量构成按组展开，分母才解释得清。"""
        out = stats.pool_usage([], {"pool-a": {
            "total_cpu": 100, "total_memory_gb": 400, "max_sandboxes": 80,
            "frames": 3,
            "groups": [
                {"name": "group1", "nodes": 2, "total_cpu": 64, "max_sandboxes": 64},
                {"name": "group2", "nodes": 1, "total_cpu": 36, "max_sandboxes": 16},
            ],
        }})
        self.assertEqual(out[0]["frames"], 3.0)
        self.assertEqual([g["name"] for g in out[0]["groups"]], ["group1", "group2"])
        self.assertEqual(out[0]["groups"][0]["nodes"], 2)

    def test_groups_copied_not_aliased(self):
        """返回的 group dict 必须是副本，否则改动会写回 load_pools 的缓存。"""
        spec_group = {"name": "group1", "nodes": 2}
        out = stats.pool_usage([], {"pool-a": {
            "total_cpu": 1, "total_memory_gb": 1, "groups": [spec_group],
        }})
        out[0]["groups"][0]["nodes"] = 999
        self.assertEqual(spec_group["nodes"], 2)

    def test_legacy_pool_has_empty_groups(self):
        """老格式池没有 groups，要给出空列表而不是缺字段。"""
        out = stats.pool_usage([], {"pool-a": {"total_cpu": 8, "total_memory_gb": 32}})
        self.assertEqual(out[0]["groups"], [])
        self.assertEqual(out[0]["frames"], 0.0)

    def test_unconfigured_namespace_has_group_fields(self):
        """未登记的池也要带上新字段，前端不必做存在性判断。"""
        out = stats.pool_usage([_inst(sandbox_namespace="pool-z")], {})
        z = [x for x in out if x["namespace"] == "pool-z"][0]
        self.assertEqual(z["groups"], [])
        self.assertEqual(z["frames"], 0.0)


class EffectivePodsTest(unittest.TestCase):
    """尾部折算：running 实例的活沙箱数 = min(并发, 剩余任务数)。

    背景：hive.py 是 concurrent_num 个 worker 各领一个任务，尾部只剩少量沙箱。
    队列的 tail_skip 会在这个窗口里放行下一个任务，若按配置并发统计，两个实例
    的占用都被算满，看板明显高估。
    """

    def test_tail_is_folded(self):
        # 并发 100，剩 49 个任务 → 49 个沙箱，不是 100
        row = _inst(concurrent_num=100, total_tasks=1000, completed_tasks=951, failed_tasks=0)
        self.assertEqual(stats.effective_pods(row), 49)
        self.assertEqual(stats.instance_cpu(row), 98.0)   # 49 × 2.0

    def test_head_is_clamped_to_concurrency(self):
        # 剩 900 个任务，但只有 100 个 worker → 钳到 100
        row = _inst(concurrent_num=100, total_tasks=1000, completed_tasks=100, failed_tasks=0)
        self.assertEqual(stats.effective_pods(row), 100)

    def test_unknown_total_falls_back_to_concurrency(self):
        row = _inst(concurrent_num=100, total_tasks=0, completed_tasks=0, failed_tasks=0)
        self.assertEqual(stats.effective_pods(row), 100)

    def test_all_done_is_zero_not_negative(self):
        row = _inst(concurrent_num=100, total_tasks=50, completed_tasks=40, failed_tasks=20)
        self.assertEqual(stats.effective_pods(row), 0)

    def test_failed_counts_too(self):
        row = _inst(concurrent_num=100, total_tasks=1000, completed_tasks=940, failed_tasks=11)
        self.assertEqual(stats.effective_pods(row), 49)

    def test_preparing_stays_full(self):
        # preparing 还没建沙箱，但队列槽位已占用，维持全额计入
        row = _inst(status="preparing", concurrent_num=100, total_tasks=1000, completed_tasks=999)
        self.assertEqual(stats.effective_pods(row), 100)

    def test_summary_and_pods_share_caliber(self):
        # 已用核数 / 上限 / 沙箱数三者必须同口径，不能一个折算一个不折
        s = stats.aggregate_summary([
            _inst(concurrent_num=100, total_tasks=1000, completed_tasks=951),
        ])
        self.assertEqual(s["used_cpu"], 98.0)          # 49 × 2.0
        self.assertEqual(s["used_cpu_limit"], 147.0)   # 49 × 3.0
        self.assertEqual(s["pods"], 49)

    def test_trend_keeps_configured_concurrency(self):
        # 趋势是历史重建，不知道当时剩多少任务，仍按配置并发估算，不能跟着折算
        end = datetime(2026, 10, 19, 12, 0, 0).timestamp()
        start = end - 3600
        row = _inst(
            total_tasks=1000, completed_tasks=951,
            started_at=datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S"),
            stopped_at=datetime.fromtimestamp(end).strftime("%Y-%m-%d %H:%M:%S"),
        )
        points = stats.reconstruct_trend([row], start, end, 60)
        self.assertGreater(len(points), 0)
        self.assertEqual(points[0]["used_cpu"], 20.0)   # 10 并发 × 2.0，未被折成 5 个沙箱


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


class ProxyNormalizeTest(unittest.TestCase):
    def test_strip_trailing_slash_and_v1(self):
        self.assertEqual(proxy.normalize_base_url("http://1.2.3.4:8082/"), "http://1.2.3.4:8082")
        self.assertEqual(proxy.normalize_base_url("http://1.2.3.4:8082/v1"), "http://1.2.3.4:8082")
        self.assertEqual(proxy.normalize_base_url("http://1.2.3.4:8082/v1/"), "http://1.2.3.4:8082")
        self.assertEqual(proxy.normalize_base_url("http://1.2.3.4:8082/V1"), "http://1.2.3.4:8082")
        self.assertEqual(proxy.normalize_base_url(""), "")
        self.assertEqual(proxy.normalize_base_url(None), "")

    def test_identity_maps_ip(self):
        m = {"1.2.3.4": "jumper001"}
        got = proxy.proxy_identity("http://1.2.3.4:8082/v1", m)
        self.assertEqual(got["host"], "1.2.3.4")
        self.assertEqual(got["proxy_name"], "jumper001")
        self.assertEqual(got["baseurl"], "http://1.2.3.4:8082")

    def test_identity_unknown_ip_is_blank(self):
        got = proxy.proxy_identity("http://9.9.9.9:8082", {"1.2.3.4": "jumper001"})
        self.assertEqual(got["host"], "9.9.9.9")
        self.assertEqual(got["proxy_name"], "")

    def test_identity_non_ip_host_falls_back(self):
        got = proxy.proxy_identity("http://gw.example.com:8082/v1", {})
        self.assertEqual(got["host"], "gw.example.com")


class ProxyLoadTest(unittest.TestCase):
    MAP = {"1.2.3.4": "jumper001", "5.6.7.8": "jumper002"}

    def _params(self, main_url="", agents=None):
        return json.dumps({
            "model_base_url": main_url,
            "agents": agents or [],
        })

    def test_role_split_and_load(self):
        rows = [
            _inst(status="running", concurrent_num=100, create_params=self._params(
                "http://1.2.3.4:8082",
                [{"name": "user_simulator", "base_url": "http://1.2.3.4:8082/v1"},
                 {"name": "evaluator", "base_url": "http://5.6.7.8:8082"}]))
        ]
        out = stats.proxy_load(rows, self.MAP)
        by = {(x["role"], x["host"]): x for x in out}
        self.assertEqual(by[("主Agent", "1.2.3.4")]["pods"], 100)
        self.assertEqual(by[("用户模拟", "1.2.3.4")]["pods"], 100)
        self.assertEqual(by[("Evaluator", "5.6.7.8")]["proxy_name"], "jumper002")

    def test_v1_and_plain_merge_into_one_row(self):
        rows = [
            _inst(concurrent_num=10, create_params=self._params("http://1.2.3.4:8082")),
            _inst(concurrent_num=10, create_params=self._params("http://1.2.3.4:8082/v1")),
            _inst(concurrent_num=10, create_params=self._params("http://1.2.3.4:8082/")),
        ]
        out = [x for x in stats.proxy_load(rows, self.MAP) if x["role"] == "主Agent"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pods"], 30)
        self.assertEqual(out[0]["instances"], 3)

    def test_only_running_counted(self):
        rows = [
            _inst(status="running", concurrent_num=10, create_params=self._params("http://1.2.3.4:8082")),
            _inst(status="queued", concurrent_num=999, create_params=self._params("http://1.2.3.4:8082")),
            _inst(status="preparing", concurrent_num=999, create_params=self._params("http://1.2.3.4:8082")),
        ]
        out = stats.proxy_load(rows, self.MAP)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["pods"], 10)

    def test_missing_create_params_shows_unconfigured(self):
        rows = [_inst(status="running", concurrent_num=5, create_params=None)]
        out = stats.proxy_load(rows, self.MAP)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["host"], "未配置")
        self.assertEqual(out[0]["role"], "主Agent")

    def test_bad_json_does_not_raise(self):
        rows = [_inst(status="running", create_params="{not json")]
        out = stats.proxy_load(rows, self.MAP)
        self.assertEqual(out[0]["host"], "未配置")

    def test_sorted_by_role_then_pods(self):
        rows = [
            _inst(concurrent_num=5, create_params=self._params(
                "http://1.2.3.4:8082",
                [{"name": "user_simulator", "base_url": "http://5.6.7.8:8082/v1"}])),
            _inst(concurrent_num=50, create_params=self._params(
                "http://1.2.3.4:8082",
                [{"name": "user_simulator", "base_url": "http://1.2.3.4:8082/v1"}])),
        ]
        out = stats.proxy_load(rows, self.MAP)
        roles = [x["role"] for x in out]
        self.assertEqual(roles, ["主Agent", "用户模拟", "用户模拟"])
        # 同角色内按 pods 降序
        self.assertEqual(out[1]["pods"], 50)
        self.assertEqual(out[2]["pods"], 5)


class ExportMetaTest(unittest.TestCase):
    """导出服务元数据：远端列表解析 + 不可达时的本地兜底。

    不打真实网络：统一 mock export_meta 的取数层。
    """

    def setUp(self):
        export_meta._cache.clear()

    tearDown = setUp  # 别把缓存留给后面的用例

    def test_proxy_map_expands_ips(self):
        got = export_meta.proxy_map_from_proxies([
            {"source": "jumper-002", "ips": ["115.33.106.42", "10.0.0.1"]},
            {"source": "proxy8", "ips": ["115.120.50.8"]},
        ])
        self.assertEqual(got, {
            "115.33.106.42": "jumper-002",
            "10.0.0.1": "jumper-002",
            "115.120.50.8": "proxy8",
        })

    def test_proxy_map_skips_incomplete_entries(self):
        got = export_meta.proxy_map_from_proxies([
            {"source": "", "ips": ["1.1.1.1"]},     # 无名字
            {"source": "no-ip", "ips": []},          # 无 IP
            {"source": "blank-ip", "ips": ["  "]},   # 空白 IP
            {"source": "ok", "ips": ["2.2.2.2"]},
        ])
        self.assertEqual(got, {"2.2.2.2": "ok"})

    def test_list_modes_filters_non_public(self):
        payload = {"modes": [
            {"id": "export", "mode_label": "导出", "public": True},
            {"id": "secret", "mode_label": "内部", "public": False},
            {"id": "legacy", "mode_label": "老版本无 public 字段"},
            {"mode_label": "没有 id 的脏数据"},
        ]}
        with mock.patch.object(export_meta, "_get_json",
                               mock.AsyncMock(return_value=payload)):
            modes = asyncio.run(export_meta.list_modes())
        self.assertEqual([m["id"] for m in modes], ["export", "legacy"])

    def test_mode_options_prefers_mode_label(self):
        got = export_meta.mode_options([
            {"id": "export", "mode_label": "导出", "label": "导出原始文件（三元组）"},
            {"id": "bare"},  # 两个 label 都缺 → 退回 id
        ])
        self.assertEqual(got[0], {"value": "export", "label": "导出",
                                  "desc": "导出原始文件（三元组）"})
        self.assertEqual(got[1], {"value": "bare", "label": "bare", "desc": ""})

    def test_list_modes_empty_when_unconfigured(self):
        """未配 EXPORT_BASE 时直接返回空，不打网络、不写缓存。"""
        with mock.patch.object(export_meta, "_configured", return_value=False), \
             mock.patch.object(export_meta, "_get_json",
                               mock.AsyncMock(side_effect=AssertionError("不该打网络"))):
            self.assertEqual(asyncio.run(export_meta.list_modes()), [])
        self.assertEqual(export_meta._cache, {})

    def test_remote_failure_is_negative_cached(self):
        """远端报错只打一次，30s 内复用空结果（看板每 30s 轮询，不能每次吃超时）。"""
        fetch = mock.AsyncMock(side_effect=RuntimeError("导出服务不可达"))
        with mock.patch.object(export_meta, "_configured", return_value=True), \
             mock.patch.object(export_meta, "_get_json", fetch):
            self.assertEqual(asyncio.run(export_meta.list_modes()), [])
            self.assertEqual(asyncio.run(export_meta.list_modes()), [])
        self.assertEqual(fetch.await_count, 1)

    def test_proxy_map_async_prefers_remote(self):
        remote = [{"source": "jumper-002", "ips": ["1.2.3.4"]}]
        with mock.patch.object(export_meta, "list_proxies",
                               mock.AsyncMock(return_value=remote)):
            got = asyncio.run(proxy.load_proxy_map_async())
        self.assertEqual(got, {"1.2.3.4": "jumper-002"})

    def test_proxy_map_async_falls_back_to_local_file(self):
        with mock.patch.object(export_meta, "list_proxies",
                               mock.AsyncMock(return_value=[])), \
             mock.patch.object(proxy, "load_proxy_map",
                               return_value={"9.9.9.9": "local-only"}) as local:
            got = asyncio.run(proxy.load_proxy_map_async())
        self.assertEqual(got, {"9.9.9.9": "local-only"})
        local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
