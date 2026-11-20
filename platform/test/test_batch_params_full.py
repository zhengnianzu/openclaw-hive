# -*- coding: utf-8 -*-
"""接口级验证：GET /api/batch-experiments/{id}/params/full

在临时库里造实验 + 子任务，用 FastAPI TestClient 打真实路由：
  - operator/admin → 200，返回的 blocks[].models[].model_api_key 是明文
  - viewer        → 403
  - 普通详情接口 GET /{id} 仍只有掩码

运行：cd platform && /root/.venv/bin/python test/test_batch_params_full.py
"""
import json
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.core import database as db  # noqa: E402
from api.core.config import settings  # noqa: E402
from api.core.security import create_access_token  # noqa: E402
from api.routers import batch_experiments as be  # noqa: E402

MODEL_KEY = "sk-live-MODELKEY-0123456789"
AGENT_KEY = "sk-live-AGENTKEY-0123456789"
BATCH = "Batch-20261120-900"
INST = "2611200000-batch-c900-c1-aaaa"


class ParamsFullEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()

        app = FastAPI()
        app.include_router(be.router)
        self.client = TestClient(app)

        masked = be._masked_params({
            "model_id": "glm-5.3",
            "model_api_key": MODEL_KEY,
            "agents": [{"name": "user_simulator", "api_key": AGENT_KEY}],
            "arms": [{
                "harness_type": "openclaw", "harness_config_id": 1,
                "harness_config_version_id": 150, "model_id": "glm-5.3",
                "model_base_url": "http://10.0.0.1:8082/v1",
                "model_api_key": MODEL_KEY,
            }],
            "blocks": [{
                "harness_type": "openclaw", "harness_config_id": 1,
                "harness_config_version_id": 150,
                "models": [{
                    "model_id": "glm-5.3",
                    "model_base_url": "http://10.0.0.1:8082/v1",
                    "model_api_key": MODEL_KEY,
                }],
            }],
        })
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                ("op1", "x", "operator"),
            )
            conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                ("vw1", "x", "viewer"),
            )
            conn.execute(
                "INSERT INTO task_queues (id, name, max_concurrent) VALUES (?,?,?)",
                ("batch-20261120-900", "t", 10),
            )
            conn.execute(
                "INSERT INTO batch_experiments "
                "(id, name, registration_id, params_json, created_by, queue_id) "
                "VALUES (?,?,?,?,?,?)",
                (BATCH, "t", 1, json.dumps(masked, ensure_ascii=False), "op1",
                 "batch-20261120-900"),
            )
            conn.execute(
                "INSERT INTO batch_experiment_items "
                "(batch_id, instance_id, harness_type, harness_config_id, "
                " harness_config_version_id, arm_label, sim_label, sim_idx, repeat_idx) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (BATCH, INST, "openclaw", 1, 150, "glm-5.3", "", 1, 1),
            )
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, create_params) "
                "VALUES (?,?,?,?)",
                (INST, "t", "/tmp/config.yaml", json.dumps({
                    "model_id": "glm-5.3",
                    "model_base_url": "http://10.0.0.1:8082/v1",
                    "model_api_key": MODEL_KEY,
                    "agents": [{"name": "user_simulator", "api_key": AGENT_KEY}],
                })),
            )

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _get(self, username, path):
        token = create_access_token({"sub": username})
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def test_operator_gets_plaintext(self):
        r = self._get("op1", f"/api/batch-experiments/{BATCH}/params/full")
        self.assertEqual(r.status_code, 200, r.text)
        params = r.json()["params"]
        self.assertEqual(params["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(params["arms"][0]["model_api_key"], MODEL_KEY)
        self.assertEqual(params["agents"][0]["api_key"], AGENT_KEY)

    def test_viewer_is_forbidden(self):
        r = self._get("vw1", f"/api/batch-experiments/{BATCH}/params/full")
        self.assertEqual(r.status_code, 403, r.text)

    def test_plain_detail_still_masked(self):
        r = self._get("op1", f"/api/batch-experiments/{BATCH}")
        self.assertEqual(r.status_code, 200, r.text)
        saved = json.loads(r.json()["params_json"])
        self.assertNotEqual(saved["blocks"][0]["models"][0]["model_api_key"], MODEL_KEY)
        self.assertNotEqual(saved["arms"][0]["model_api_key"], MODEL_KEY)

    def test_unknown_batch_404(self):
        r = self._get("op1", "/api/batch-experiments/Batch-nope/params/full")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
