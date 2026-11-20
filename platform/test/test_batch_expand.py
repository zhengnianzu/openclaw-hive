# -*- coding: utf-8 -*-
"""批量实验：Harness × 模型 正交展开测试（unittest，无 pytest 依赖）。

覆盖：
  - 2 Harness × 2 模型 → 4 个臂，label 带 Harness 名，模型参数原样带入
  - 模型的 base_url / api_type 不做任何 /v1 规整（由各 harness 在创建时解析）
  - Harness 的镜像 / 沙箱配置按 Harness 走，不串到别的臂
  - models 为空 → 拒绝创建（400）
  - 旧形状（blocks 内嵌 models / 直接给 arms）仍按原规则展开
  - legacy: blocks 内嵌模型为空 → 退化成吃 common 兜底模型

全部使用隔离的临时 DB。

运行：cd platform && /root/.venv/bin/python test/test_batch_expand.py -v
"""
import json
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core import database as db  # noqa: E402
from api.models.batch_experiment import BatchExperimentCreate  # noqa: E402
from api.routers import batch_experiments as be  # noqa: E402


def _req(**over):
    base = dict(
        name="t",
        registration_id=1,
        harnesses=[
            {"harness_type": "claude-code", "harness_config_id": 3,
             "harness_config_version_id": 3, "label": "CC默认",
             "image_name": "img-cc", "sandbox_config_id": None},
            {"harness_type": "opencode", "harness_config_id": 16,
             "harness_config_version_id": 9, "label": "OC默认",
             "image_name": "img-oc", "sandbox_config_id": 4},
        ],
        models=[
            {"model_id": "glm-5.3", "model_base_url": "http://h:8082/v1",
             "model_api_key": "sk-a", "model_api_type": "anthropic-messages"},
            {"model_id": "ds-v4", "model_base_url": "http://h:8082",
             "model_api_key": "sk-b", "model_api_type": "openai-completions"},
        ],
    )
    base.update(over)
    return BatchExperimentCreate(**base)


class ExpandTest(unittest.TestCase):
    def test_cross_product(self):
        arms = be._expand_arms(_req())
        self.assertEqual(len(arms), 4)
        pairs = {(a.harness_type, a.model_id) for a in arms}
        self.assertEqual(pairs, {
            ("claude-code", "glm-5.3"), ("claude-code", "ds-v4"),
            ("opencode", "glm-5.3"), ("opencode", "ds-v4"),
        })

    def test_labels_include_harness_and_model(self):
        arms = be._expand_arms(_req())
        self.assertEqual({a.label for a in arms}, {
            "CC默认 · glm-5.3", "CC默认 · ds-v4",
            "OC默认 · glm-5.3", "OC默认 · ds-v4",
        })

    def test_model_params_passed_through_verbatim(self):
        """base_url 不做 /v1 补/剥 —— 各 harness 在 create_instance 里自己解析。"""
        arms = be._expand_arms(_req())
        cc_ds = next(a for a in arms
                     if a.harness_type == "claude-code" and a.model_id == "ds-v4")
        self.assertEqual(cc_ds.model_base_url, "http://h:8082")
        oc_glm = next(a for a in arms
                      if a.harness_type == "opencode" and a.model_id == "glm-5.3")
        self.assertEqual(oc_glm.model_base_url, "http://h:8082/v1")
        self.assertEqual(oc_glm.model_api_type, "anthropic-messages")
        self.assertEqual(oc_glm.model_api_key, "sk-a")

    def test_harness_runtime_follows_harness(self):
        arms = be._expand_arms(_req())
        cc = [a for a in arms if a.harness_type == "claude-code"]
        oc = [a for a in arms if a.harness_type == "opencode"]
        self.assertEqual({a.image_name for a in cc}, {"img-cc"})
        self.assertEqual({a.image_name for a in oc}, {"img-oc"})
        self.assertEqual({a.sandbox_config_id for a in cc}, {None})
        self.assertEqual({a.sandbox_config_id for a in oc}, {4})

    # ---- 旧形状回归 ----
    def test_legacy_blocks_with_models(self):
        req = BatchExperimentCreate(
            name="t", registration_id=1,
            blocks=[{"harness_type": "openclaw", "harness_config_id": 1,
                     "harness_config_version_id": 150,
                     "models": [{"model_id": "m1"}, {"model_id": "m2"}]}],
        )
        arms = be._expand_arms(req)
        self.assertEqual([a.model_id for a in arms], ["m1", "m2"])

    def test_legacy_block_without_models_uses_fallback(self):
        req = BatchExperimentCreate(
            name="t", registration_id=1,
            blocks=[{"harness_type": "openclaw", "harness_config_id": 1,
                     "harness_config_version_id": 150, "label": "H"}],
        )
        arms = be._expand_arms(req)
        self.assertEqual(len(arms), 1)
        self.assertEqual(arms[0].label, "H")
        self.assertEqual(arms[0].model_id, "")

    def test_legacy_arms_passthrough(self):
        req = BatchExperimentCreate(
            name="t", registration_id=1,
            arms=[{"harness_type": "openclaw", "model_id": "m1"}],
        )
        arms = be._expand_arms(req)
        self.assertEqual(len(arms), 1)
        self.assertEqual(arms[0].model_id, "m1")


class CreateValidationTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO task_registrations (created_by, task_name, task_path_obs, data_total) "
                "VALUES (?,?,?,?)",
                ("u", "ds", "task_data/x/configs", 3),
            )

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _call(self, req, tmpdir):
        """create_experiment 会真的建子任务 —— 这里只验证能被拦住的那条路径。

        400 在 any create_instance 之前抛出（校验在最前），所以不会写文件。
        """
        import asyncio

        class _NoCreate:
            pass

        # 跳过 require_operator：直接调被包装的函数
        return asyncio.run(be.create_experiment(req, {"username": "u", "role": "operator"}))

    def test_empty_models_rejected(self):
        req = _req(models=[])
        with self.assertRaises(Exception) as ctx:
            self._call(req, self._tmpdir)
        self.assertIn("至少配置一个模型", str(ctx.exception))

    def test_empty_harnesses_rejected(self):
        req = _req(harnesses=[])
        with self.assertRaises(Exception) as ctx:
            self._call(req, self._tmpdir)
        self.assertIn("至少选择一个 Harness", str(ctx.exception))

    def test_create_persists_both_axes_and_masks_keys(self):
        """接口级：2 Harness × 1 模型 → 2 个子任务，params_json 存两条轴且 key 已掩码。

        create_instance 被替换成桩函数 —— 本用例只验证实验行、子任务行、参数快照，
        不碰真实实例目录 / OBS。
        """
        import asyncio
        from unittest import mock

        req = _req(models=[{"model_id": "glm-5.3",
                            "model_base_url": "http://h:8082/v1",
                            "model_api_key": "sk-plain-0123456789",
                            "model_api_type": "anthropic-messages"}])

        async def _fake_create(_req_obj, _user):
            inst = mock.Mock()
            inst.id = f"inst-{_req_obj.name}"
            return inst

        with mock.patch("api.routers.instances.create_instance", _fake_create):
            res = asyncio.run(be.create_experiment(
                req, {"username": "u", "role": "operator"}))

        self.assertEqual(res["created"], 2)
        batch_id = res["experiment"].id

        with db.get_connection() as conn:
            items = conn.execute(
                "SELECT arm_label FROM batch_experiment_items WHERE batch_id = ? "
                "ORDER BY id", (batch_id,)).fetchall()
            row = conn.execute(
                "SELECT params_json FROM batch_experiments WHERE id = ?",
                (batch_id,)).fetchone()

        self.assertEqual([i["arm_label"] for i in items],
                         ["CC默认 · glm-5.3", "OC默认 · glm-5.3"])
        saved = json.loads(row["params_json"])
        self.assertEqual(len(saved["harnesses"]), 2)
        self.assertEqual(len(saved["models"]), 1)
        self.assertNotEqual(saved["models"][0]["model_api_key"], "sk-plain-0123456789")
        # 摊平后的 arms 也要带上，且 key 同样已掩码
        self.assertEqual(len(saved["arms"]), 2)
        for a in saved["arms"]:
            self.assertNotEqual(a["model_api_key"], "sk-plain-0123456789")

    def test_total_num_defaults_to_sample_total_and_can_be_capped(self):
        """common.total_num=0 → 用登记样本数；>0 → 按它截断（不是「不限制」）。"""
        import asyncio
        from unittest import mock

        seen = []

        async def _fake_create(req_obj, _user):
            seen.append(req_obj.total_num)
            inst = mock.Mock()
            inst.id = f"inst-{req_obj.name}"
            return inst

        def _run(**common_over):
            seen.clear()
            req = _req(models=[{"model_id": "glm-5.3"}],
                       common=common_over or {})
            with mock.patch("api.routers.instances.create_instance", _fake_create):
                return asyncio.run(be.create_experiment(
                    req, {"username": "u", "role": "operator"}))

        # 登记 data_total = 3 → 每个子任务跑 3 条
        _run()
        self.assertEqual(seen, [3, 3])

        # 显式任务总数 1 → 截断到 1 条
        _run(total_num=1)
        self.assertEqual(seen, [1, 1])


if __name__ == "__main__":
    unittest.main()
