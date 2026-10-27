# -*- coding: utf-8 -*-
"""沙箱资源解析与资源池归属测试（unittest，无 pytest 依赖）。

覆盖首页看板「当前资源使用情况」依赖的两条逻辑：
- _parse_resources() 对三种 config 形态的解析
- resource_pools.default_namespace() 的降级顺序
以及沙箱配置的 sandbox_namespace 校验（必须已在 resource_pools.yaml 登记）。

运行：cd platform && python -m unittest test.test_sandbox_resources -v
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.routers import instances, resource_pools, sandbox_configs  # noqa: E402


class ParseResourcesTest(unittest.TestCase):
    """单沙箱资源解析：三种历史形态 + 兜底。"""

    def test_linux_flat(self):
        cfg = {
            "sandbox_active_key": "x86_cpu",
            "sandbox": {"x86_cpu": {"sandbox": {"resources": {
                "cpu_request": "2", "cpu_limit": "3",
                "memory_request": "6Gi", "memory_limit": "8Gi",
            }}}},
            "sandbox_namespace": "ns-linux",
        }
        self.assertEqual(instances._parse_resources(cfg), {
            "cpu_request": 2.0, "cpu_limit": 3.0,
            "memory_request": 6.0, "memory_limit": 8.0,
        })

    def test_windows_nested(self):
        cfg = {
            "sandbox_active_key": "windows",
            "sandbox": {"windows": {"sandbox": {"resources": {
                "requests": {"cpu": "16", "memory": "32Gi"},
                "limits": {"cpu": "16", "memory": "32Gi"},
            }}}},
        }
        self.assertEqual(instances._parse_resources(cfg), {
            "cpu_request": 16.0, "cpu_limit": 16.0,
            "memory_request": 32.0, "memory_limit": 32.0,
        })

    def test_legacy_env_make(self):
        cfg = {"env_make": {"args": {"resources": {
            "cpu_request": "1", "cpu_limit": "2",
            "memory_request": "5Gi", "memory_limit": "8Gi",
        }}}}
        self.assertEqual(instances._parse_resources(cfg), {
            "cpu_request": 1.0, "cpu_limit": 2.0,
            "memory_request": 5.0, "memory_limit": 8.0,
        })

    def test_empty_falls_back(self):
        self.assertEqual(instances._parse_resources({}), instances._RES_FALLBACK)

    def test_memory_units(self):
        # Gi=1024Mi、G=1000M，统一归一成 GiB
        self.assertEqual(instances._gib("1536Mi"), 1.5)
        self.assertEqual(instances._gib("1Gi"), 1.0)
        self.assertAlmostEqual(instances._gib("2G"), 1.9531, places=4)


class DefaultNamespaceTest(unittest.TestCase):
    """default_namespace() 的降级顺序：default → 第一个池 → 常量。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_pools_file = resource_pools.POOLS_FILE
        self._old_default = resource_pools.DEFAULT_NAMESPACE

    def tearDown(self):
        resource_pools.POOLS_FILE = self._old_pools_file
        resource_pools.DEFAULT_NAMESPACE = self._old_default
        self._tmpdir.cleanup()

    def _write(self, content):
        path = os.path.join(self._tmpdir.name, "resource_pools.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        resource_pools.POOLS_FILE = path

    def test_explicit_default(self):
        self._write("default: pool-b\npools:\n  pool-a: {total_cpu: 1}\n  pool-b: {total_cpu: 2}\n")
        self.assertEqual(resource_pools.default_namespace(), "pool-b")

    def test_first_pool_when_default_missing(self):
        self._write("pools:\n  pool-a: {total_cpu: 1}\n  pool-b: {total_cpu: 2}\n")
        self.assertEqual(resource_pools.default_namespace(), "pool-a")

    def test_constant_when_file_missing(self):
        resource_pools.POOLS_FILE = os.path.join(self._tmpdir.name, "nope.yaml")
        self.assertEqual(resource_pools.default_namespace(), resource_pools.DEFAULT_NAMESPACE)


class NamespaceValidationTest(unittest.TestCase):
    """资源池必须已在 resource_pools.yaml 登记。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_pools_file = resource_pools.POOLS_FILE
        path = os.path.join(self._tmpdir.name, "resource_pools.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("default: registered-pool\npools:\n  registered-pool: {total_cpu: 8}\n")
        resource_pools.POOLS_FILE = path

    def tearDown(self):
        resource_pools.POOLS_FILE = self._old_pools_file
        self._tmpdir.cleanup()

    def test_registered_passes(self):
        self.assertEqual(sandbox_configs._validate_namespace("registered-pool"), "registered-pool")

    def test_unregistered_rejected(self):
        with self.assertRaises(Exception) as ctx:
            sandbox_configs._validate_namespace("not-registered")
        self.assertIn("未在", str(ctx.exception))

    def test_empty_rejected(self):
        with self.assertRaises(Exception):
            sandbox_configs._validate_namespace("  ")

    def test_bad_syntax_rejected(self):
        for bad in ("UPPER", "-leading", "trailing-", "with space"):
            with self.assertRaises(Exception, msg=bad):
                sandbox_configs._validate_namespace(bad)


class PoolGroupsTest(unittest.TestCase):
    """按机器组派生池容量：总量 = Σ 节点数×单机值，单机沙箱数受核数压制。"""

    # 线上 omni-env-default-worker 的五个机器组
    GROUPS = [
        {"name": "group1", "nodes": 38, "cpu_per_node": 384,
         "memory_gb_per_node": 1536, "max_sandboxes_per_node": 256},
        {"name": "group2", "nodes": 30, "cpu_per_node": 384,
         "memory_gb_per_node": 1536, "max_sandboxes_per_node": 256},
        {"name": "group3", "nodes": 150, "cpu_per_node": 64,
         "memory_gb_per_node": 256, "max_sandboxes_per_node": 256},
        {"name": "group4", "nodes": 300, "cpu_per_node": 32,
         "memory_gb_per_node": 128, "max_sandboxes_per_node": 256},
        {"name": "group5", "nodes": 10, "cpu_per_node": 64,
         "memory_gb_per_node": 256, "max_sandboxes_per_node": 256},
    ]

    def test_totals(self):
        d = resource_pools.derive_groups(self.GROUPS)
        self.assertEqual(d["total_cpu"], 45952)
        self.assertEqual(d["total_memory_gb"], 183808)
        self.assertEqual(d["max_sandboxes"], 37248)
        self.assertEqual(d["frames"], 528)

    def test_single_node_sandboxes_capped_by_cpu(self):
        """声明 256 只是上限：384 核的组能用满 256，64/32 核的组被核数压住。"""
        d = resource_pools.derive_groups(self.GROUPS)
        per_node = {g["name"]: g["usable_sandboxes_per_node"] for g in d["groups"]}
        self.assertEqual(per_node, {
            "group1": 256, "group2": 256,
            "group3": 64, "group4": 32, "group5": 64,
        })

    def test_missing_cap_falls_back_to_cpu(self):
        self.assertEqual(
            resource_pools.usable_sandboxes_per_node(cpu_per_node=64,
                                                     max_sandboxes_per_node=None),
            64)

    def test_group_name_defaulted(self):
        d = resource_pools.derive_groups([{"nodes": 2, "cpu_per_node": 8}])
        self.assertEqual(d["groups"][0]["name"], "group1")

    def test_empty_groups(self):
        d = resource_pools.derive_groups([])
        self.assertEqual((d["total_cpu"], d["total_memory_gb"],
                          d["max_sandboxes"], d["frames"]), (0, 0, 0, 0))

    def test_legacy_scalar_format_still_works(self):
        """老格式没有 groups：总量照读，沙箱并发未知（0），不算「未配置」。"""
        spec = resource_pools._derive_pool({"total_cpu": 256, "total_memory_gb": 1024})
        self.assertEqual(spec["total_cpu"], 256)
        self.assertEqual(spec["total_memory_gb"], 1024)
        self.assertEqual(spec["max_sandboxes"], 0)
        self.assertTrue(spec["configured"])
        self.assertFalse(spec["grouped"])


class PoolUsageTest(unittest.TestCase):
    """pool_usage() 的池内沙箱数与 configured 标记。"""

    def test_pods_counted_per_pool(self):
        from api.services import stats as stats_service
        pools = {
            "pool-a": {"total_cpu": 100, "total_memory_gb": 200, "max_sandboxes": 50},
        }
        rows = [
            # 尾部：并发 8 但要跑完 3 个任务，只剩 1 个沙箱活着
            {"status": "running", "sandbox_namespace": "pool-a", "concurrent_num": 8,
             "cpu_request": 2, "memory_request": 6, "total_tasks": 10,
             "completed_tasks": 9},
            # 排队的不计沙箱
            {"status": "queued", "sandbox_namespace": "pool-a", "concurrent_num": 4,
             "cpu_request": 2, "memory_request": 6},
        ]
        out = {p["namespace"]: p for p in stats_service.pool_usage(rows, pools)}
        self.assertEqual(out["pool-a"]["pods"], 1)
        self.assertEqual(out["pool-a"]["max_sandboxes"], 50)
        self.assertEqual(out["pool-a"]["used_cpu"], 2)


if __name__ == "__main__":
    unittest.main()
