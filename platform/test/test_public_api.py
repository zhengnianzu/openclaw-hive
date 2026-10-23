# -*- coding: utf-8 -*-
"""对外 API（/openapi/v1）测试（unittest，无 pytest 依赖）。

覆盖：
  - AK 鉴权：缺 key / 错 key / 未配置 key（503）三种拒绝路径
  - q1 定位：未命中 404；同一 q1 命中多实例时 409 并给出候选；加 instance_id 收敛后 200
  - 输出路径与 hive.py:1106 的规则一致（bucket/traj_save_path/config 文件名去后缀）

测试在临时目录里造一套最小实例（configs + config.yaml），不依赖真实 instances/。

运行：cd platform && python -m unittest test.test_public_api -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.core.config import settings  # noqa: E402
from api.routers import public_api  # noqa: E402

API_KEY = "test-key-123"
Q1_TEXT = "中国的首都在哪里？"
URL = "/openapi/v1/task-output-path"

CONFIG_YAML = """\
run_config:
  obs:
    traj_save_bucket: "obs://rl-agentdata"
    traj_save_path: "openclaw_trajs/traj_demo"
sandbox_id_prefix: demo
"""


def _write_task(instance_root: str, rel: str, first_text: str) -> None:
    path = os.path.join(instance_root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "agents": [{"name": "main"}],
            "queries": [
                {"agent_name": "main", "text": first_text, "session_name": "query1"},
                {"agent_name": "main", "text": "这是评审用的第二条 query", "session_name": "verify1"},
            ],
        }, f, ensure_ascii=False)


def _make_instance(root: str, instance_id: str) -> str:
    inst = os.path.join(root, instance_id)
    os.makedirs(inst, exist_ok=True)
    with open(os.path.join(inst, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(CONFIG_YAML)
    return inst


class PublicApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # 两个实例跑同一道题（模拟真实数据里同一批语料重复跑多次）
        _write_task(_make_instance(self.root, "inst-a"), "configs/demo/001.json", Q1_TEXT)
        _write_task(_make_instance(self.root, "inst-b"), "configs/demo/001.json", Q1_TEXT)

        self._old_instance_dir = settings.INSTANCE_DIR
        self._old_keys = settings.PUBLIC_API_KEYS
        settings.INSTANCE_DIR = self.root
        settings.PUBLIC_API_KEYS = API_KEY
        # 索引缓存是模块级全局，换目录后必须清掉
        public_api._reset_caches()

        app = FastAPI()
        app.include_router(public_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        settings.INSTANCE_DIR = self._old_instance_dir
        settings.PUBLIC_API_KEYS = self._old_keys
        public_api._reset_caches()
        self._tmp.cleanup()

    def _get(self, headers=None, **params):
        return self.client.get(URL, params=params, headers=headers or {})

    # ---- 鉴权 ----

    def test_missing_key_rejected(self):
        self.assertEqual(self._get(q1=Q1_TEXT).status_code, 401)

    def test_wrong_key_rejected(self):
        self.assertEqual(self._get(headers={"X-API-Key": "nope"}, q1=Q1_TEXT).status_code, 401)

    def test_unconfigured_keys_returns_503(self):
        settings.PUBLIC_API_KEYS = ""
        self.assertEqual(self._get(headers={"X-API-Key": API_KEY}, q1=Q1_TEXT).status_code, 503)

    # ---- 定位 ----

    def test_unknown_q1_returns_404(self):
        r = self._get(headers={"X-API-Key": API_KEY}, q1="不存在的问题")
        self.assertEqual(r.status_code, 404)

    def test_ambiguous_q1_returns_409_with_candidates(self):
        r = self._get(headers={"X-API-Key": API_KEY}, q1=Q1_TEXT)
        self.assertEqual(r.status_code, 409)
        detail = r.json()["detail"]
        self.assertEqual(len(detail["candidates"]), 2)
        self.assertEqual(
            {c["instance_id"] for c in detail["candidates"]}, {"inst-a", "inst-b"},
        )

    def test_instance_id_narrows_to_one(self):
        r = self._get(headers={"X-API-Key": API_KEY}, q1=Q1_TEXT, instance_id="inst-b")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {
            "task": "001",
            "output_path": "obs://rl-agentdata/openclaw_trajs/traj_demo/001",
        })

    def test_matches_only_first_query(self):
        """q1 只认 queries[0]，第二条（评审 query）不应命中。"""
        r = self._get(headers={"X-API-Key": API_KEY}, q1="这是评审用的第二条 query")
        self.assertEqual(r.status_code, 404)


class ListKeyTasksTest(unittest.TestCase):
    """列表端点：给定 key 展示所有 q1。"""

    URL = "/openapi/v1/list_key_tasks"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        # 实例 A：configs 是普通目录
        _write_task(_make_instance(self.root, "inst-a"), "configs/demo/001.json", Q1_TEXT)
        # 实例 B：configs 是指向实例目录之外 downloads/ 的符号链接（真实数据大量如此）
        shared_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, shared_root, True)
        shared = os.path.join(shared_root, "downloads", "configs", "shared")
        _write_task(shared, "demo/002.json", "另一道题")
        inst_b = _make_instance(self.root, "inst-b")
        os.symlink(shared, os.path.join(inst_b, "configs"))

        self._old_instance_dir = settings.INSTANCE_DIR
        self._old_keys = settings.PUBLIC_API_KEYS
        settings.INSTANCE_DIR = self.root
        settings.PUBLIC_API_KEYS = API_KEY
        public_api._reset_caches()

        app = FastAPI()
        app.include_router(public_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        settings.INSTANCE_DIR = self._old_instance_dir
        settings.PUBLIC_API_KEYS = self._old_keys
        public_api._reset_caches()
        self._tmp.cleanup()

    def test_lists_all_q1(self):
        r = self.client.get(self.URL, headers={"X-API-Key": API_KEY})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(
            [(i["instance_id"], i["task"], i["q1"]) for i in body["items"]],
            [("inst-a", "001", Q1_TEXT), ("inst-b", "002", "另一道题")],
        )

    def test_symlinked_configs_resolves_to_own_instance(self):
        """configs 是符号链接时，路径必须落回 instances/<该实例>/config.yaml。"""
        body = self.client.get(self.URL, headers={"X-API-Key": API_KEY}).json()
        by_inst = {i["instance_id"]: i for i in body["items"]}
        self.assertEqual(
            by_inst["inst-b"]["output_path"],
            "obs://rl-agentdata/openclaw_trajs/traj_demo/002",
        )

    def test_trailing_slash_and_filter(self):
        headers = {"X-API-Key": API_KEY}
        self.assertEqual(self.client.get(self.URL + "/", headers=headers).status_code, 200)
        body = self.client.get(self.URL, params={"instance_id": "inst-a"}, headers=headers).json()
        self.assertEqual(body["total"], 1)

    def test_requires_key(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)

    def test_bad_instance_does_not_break_whole_list(self):
        """某个实例缺 config.yaml 时，只有那一行报错，不能整个列表 500。"""
        bad = _make_instance(self.root, "inst-c")
        os.remove(os.path.join(bad, "config.yaml"))
        _write_task(bad, "configs/demo/003.json", "第三道题")
        public_api._reset_caches()

        r = self.client.get(self.URL, headers={"X-API-Key": API_KEY})
        self.assertEqual(r.status_code, 200)
        row = next(i for i in r.json()["items"] if i["instance_id"] == "inst-c")
        self.assertIsNone(row["output_path"])
        self.assertIn("error", row)


class ListQ1TruncationTest(unittest.TestCase):
    """列表里 q1 默认截到 200 字；截断后仍可用 task + instance_id 查详情。"""

    URL = "/openapi/v1/list_key_tasks"
    LONG_Q1 = "长" * 500

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        _write_task(_make_instance(self.root, "inst-long"), "configs/demo/009.json", self.LONG_Q1)

        self._old_instance_dir = settings.INSTANCE_DIR
        self._old_keys = settings.PUBLIC_API_KEYS
        settings.INSTANCE_DIR = self.root
        settings.PUBLIC_API_KEYS = API_KEY
        public_api._reset_caches()

        app = FastAPI()
        app.include_router(public_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        settings.INSTANCE_DIR = self._old_instance_dir
        settings.PUBLIC_API_KEYS = self._old_keys
        public_api._reset_caches()
        self._tmp.cleanup()

    def _item(self, **params):
        r = self.client.get(self.URL, params=params, headers={"X-API-Key": API_KEY})
        self.assertEqual(r.status_code, 200)
        return r.json()["items"][0]

    def test_default_truncates_to_200(self):
        item = self._item()
        self.assertTrue(item["q1_truncated"])
        self.assertEqual(item["q1"], "长" * 200 + "…")

    def test_can_disable_truncation(self):
        item = self._item(q1_max_chars=0)
        self.assertFalse(item["q1_truncated"])
        self.assertEqual(item["q1"], self.LONG_Q1)

    def test_truncated_q1_still_resolvable_by_task(self):
        """截断后的文本不能当 q1 用，但 task + instance_id 必须能查回同一条路径。"""
        item = self._item()
        headers = {"X-API-Key": API_KEY}

        r = self.client.get(
            "/openapi/v1/task-output-path",
            params={"q1": item["q1"], "instance_id": item["instance_id"]},
            headers=headers,
        )
        self.assertEqual(r.status_code, 404)

        r = self.client.get(
            "/openapi/v1/task-output-path",
            params={"task": item["task"], "instance_id": item["instance_id"]},
            headers=headers,
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["output_path"], item["output_path"])

    def test_full_q1_still_matches(self):
        """全文 q1 照常匹配——截断只影响列表回显，不影响索引。"""
        r = self.client.get(
            "/openapi/v1/task-output-path",
            params={"q1": self.LONG_Q1, "instance_id": "inst-long"},
            headers={"X-API-Key": API_KEY},
        )
        self.assertEqual(r.status_code, 200)

    def test_requires_q1_or_task(self):
        r = self.client.get("/openapi/v1/task-output-path", headers={"X-API-Key": API_KEY})
        self.assertEqual(r.status_code, 400)


class NormalizeTest(unittest.TestCase):
    def test_nfkc_and_whitespace(self):
        self.assertEqual(public_api._normalize("  ＡＢＣ\n\n 中文 \t"), "ABC 中文")

    def test_crlf_equivalent(self):
        self.assertEqual(public_api._normalize("a\r\nb"), public_api._normalize("a\nb"))


if __name__ == "__main__":
    unittest.main()
