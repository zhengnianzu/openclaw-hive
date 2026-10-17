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


if __name__ == "__main__":
    unittest.main()
