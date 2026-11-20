# -*- coding: utf-8 -*-
"""批量实验复制时继承 key 的测试（unittest，无 pytest 依赖）。

覆盖：
  - _masked_params 对 blocks/arms/common/agents/sim_arms 五处 key 全部掩码，
    且不修改入参
  - _restore_params_secrets 按 harness 三件套 + model_id 把明文填回 blocks / arms，
    按 agent 名字填回 agents；查不到的位置留空；坏 JSON 的子任务跳过不抛

全部使用隔离的临时 DB，不碰生产 platform.db。

运行：cd platform && python -m unittest test.test_batch_copy -v
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
from api.routers import batch_experiments as be  # noqa: E402

MODEL_KEY = "sk-live-MODELKEY-0123456789"
AGENT_KEY = "sk-live-AGENTKEY-0123456789"


class MaskedParamsTest(unittest.TestCase):
    def _params(self):
        return {
            "model_api_key": MODEL_KEY,
            "agents": [{"name": "user_simulator", "api_key": AGENT_KEY}],
            "models": [{"model_id": "glm-5.3", "model_api_key": MODEL_KEY}],
            "arms": [{"harness_type": "openclaw", "model_api_key": MODEL_KEY}],
            "blocks": [{
                "harness_type": "openclaw",
                "models": [{"model_id": "glm-5.3", "model_api_key": MODEL_KEY}],
            }],
            "sim_arms": [{"agents": [{"name": "evaluator", "api_key": AGENT_KEY}]}],
        }

    def test_all_key_slots_are_masked(self):
        masked = be._masked_params(self._params())
        self.assertNotEqual(masked["model_api_key"], MODEL_KEY)
        self.assertNotEqual(masked["agents"][0]["api_key"], AGENT_KEY)
        # 模型轴：与 Harness 解耦后单独的列表，漏脱敏同样是明文落库
        self.assertNotEqual(masked["models"][0]["model_api_key"], MODEL_KEY)
        self.assertNotEqual(masked["arms"][0]["model_api_key"], MODEL_KEY)
        # 这条是本 bug 的回归点：blocks 曾经漏脱敏，明文直接入库
        self.assertNotEqual(masked["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertNotEqual(masked["sim_arms"][0]["agents"][0]["api_key"], AGENT_KEY)

    def test_masking_does_not_mutate_input(self):
        src = self._params()
        be._masked_params(src)
        self.assertEqual(src["model_api_key"], MODEL_KEY)
        self.assertEqual(src["models"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(src["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(src["agents"][0]["api_key"], AGENT_KEY)

    def test_missing_or_empty_keys_are_left_alone(self):
        masked = be._masked_params({
            "model_api_key": "",
            "agents": [{"name": "x"}, "not-a-dict"],
            "blocks": ["not-a-dict", {"models": [{"model_id": "m"}]}],
        })
        self.assertEqual(masked["model_api_key"], "")
        self.assertNotIn("api_key", masked["agents"][0])
        self.assertEqual(masked["blocks"][1]["models"][0], {"model_id": "m"})


class RestoreParamsTest(unittest.TestCase):
    """每个用例一个临时 DB：建表 → 造实验 / 子任务 / create_params。"""

    BATCH = "Batch-20261120-001"
    INST = "2611200000-batch-c1-abcd"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _insert(self, create_params):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO batch_experiment_items "
                "(batch_id, instance_id, harness_type, harness_config_id, "
                " harness_config_version_id, arm_label, sim_label, sim_idx, repeat_idx) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (self.BATCH, self.INST, "openclaw", 1, 150, "glm-5.3", "", 1, 1),
            )
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, create_params) "
                "VALUES (?,?,?,?)",
                (self.INST, "t", "/tmp/config.yaml",
                 create_params if create_params is None else json.dumps(create_params)),
            )

    def _masked_input(self):
        return {
            "model_api_key": be._mask_secret(MODEL_KEY),
            "model_id": "glm-5.3",
            "agents": [{"name": "user_simulator", "api_key": be._mask_secret(AGENT_KEY)}],
            "models": [{
                "model_id": "glm-5.3",
                "model_base_url": "http://10.0.0.1:8082/v1",
                "model_api_key": be._mask_secret(MODEL_KEY),
            }],
            "arms": [{
                "harness_type": "openclaw", "harness_config_id": 1,
                "harness_config_version_id": 150, "model_id": "glm-5.3",
                "model_base_url": "http://10.0.0.1:8082/v1",
                "model_api_key": be._mask_secret(MODEL_KEY),
            }],
            "blocks": [{
                "harness_type": "openclaw", "harness_config_id": 1,
                "harness_config_version_id": 150,
                "models": [{
                    "model_id": "glm-5.3",
                    "model_base_url": "http://10.0.0.1:8082/v1",
                    "model_api_key": be._mask_secret(MODEL_KEY),
                }],
            }],
        }

    def _instance_params(self, **over):
        cp = {
            "model_id": "glm-5.3",
            "model_base_url": "http://10.0.0.1:8082/v1",
            "model_api_key": MODEL_KEY,
            "agents": [{"name": "user_simulator", "api_key": AGENT_KEY}],
        }
        cp.update(over)
        return cp

    def test_restores_blocks_arms_common_and_agents(self):
        self._insert(self._instance_params())
        out = be._restore_params_secrets(self.BATCH, self._masked_input())
        self.assertEqual(out["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(out["arms"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(out["model_api_key"], MODEL_KEY)
        self.assertEqual(out["agents"][0]["api_key"], AGENT_KEY)

    def test_restores_model_axis_across_harnesses(self):
        """模型轴与 Harness 解耦：key 从任意一个子任务取回即可。

        这里源实例的 harness 是 openclaw，而 models 轴没有任何 harness 信息 ——
        查找必须按 (model_id, base_url) 做，不能依赖 harness 三件套。
        """
        self._insert(self._instance_params())
        out = be._restore_params_secrets(self.BATCH, self._masked_input())
        self.assertEqual(out["models"][0]["model_api_key"], MODEL_KEY)

    def test_does_not_mutate_input(self):
        self._insert(self._instance_params())
        src = self._masked_input()
        be._restore_params_secrets(self.BATCH, src)
        self.assertNotEqual(src["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)

    def test_base_url_form_difference_still_matches(self):
        # 源实例存的是带尾斜杠 / 不同写法，块内是不带的 —— 归一化后应撞上
        self._insert(self._instance_params(model_base_url="http://10.0.0.1:8082/v1/"))
        out = be._restore_params_secrets(self.BATCH, self._masked_input())
        self.assertEqual(out["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)

    def test_falls_back_to_harness_trio_when_base_url_differs(self):
        # base_url 被改写过（老实验）时退回「同 harness 三件套 + 同 model_id」
        self._insert(self._instance_params(model_base_url="http://other-host:8082"))
        out = be._restore_params_secrets(self.BATCH, self._masked_input())
        self.assertEqual(out["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)

    def test_unknown_config_stays_empty(self):
        self._insert(self._instance_params())
        params = self._masked_input()
        params["blocks"][0]["models"][0]["model_id"] = "unknown-model"
        params["blocks"][0]["models"][0]["model_base_url"] = "http://nowhere"
        params["arms"] = []
        out = be._restore_params_secrets(self.BATCH, params)
        self.assertNotEqual(out["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertFalse(out["blocks"][0]["models"][0]["model_api_key"])

    def test_broken_create_params_is_skipped(self):
        self._insert(None)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE task_instances SET create_params = ? WHERE id = ?",
                ("{not json", self.INST),
            )
        params = self._masked_input()
        out = be._restore_params_secrets(self.BATCH, params)  # 不应抛异常
        self.assertFalse(out["blocks"][0]["models"][0]["model_api_key"])

    def test_no_items_at_all(self):
        out = be._restore_params_secrets("Batch-nope", self._masked_input())
        self.assertFalse(out["blocks"][0]["models"][0]["model_api_key"])

    def test_empty_params(self):
        self._insert(self._instance_params())
        self.assertEqual(be._restore_params_secrets(self.BATCH, {}), {})


if __name__ == "__main__":
    unittest.main()
