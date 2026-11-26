# -*- coding: utf-8 -*-
"""批量创建时的 key 分配策略测试（unittest，无 pytest 依赖）。

规则：invite_code 是最高优先级 —— 填了它就表示「每个子任务各要一把独立 key」。
已填的 key 不浪费，留给第一个用到该 invite_code 的子任务，其余子任务 key 清空，
由 create_instance 按 invite_code 各自申请。

覆盖：
  - 填 invite_code + 填 key → 第一个子任务沿用原 key，其余为空
  - 填 invite_code + 不填 key → 全部为空（全部走自动申请）
  - 不填 invite_code → 全批共用同一把 key（老行为不变）
  - agents 同口径，且不同 invite_code / base_url 各自独立计「第一把」
  - agents 列表逐子任务重建，不污染后续子任务

运行：cd platform && /root/.venv/bin/python test/test_batch_key_alloc.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core import database as db  # noqa: E402
from api.models.batch_experiment import BatchExperimentCreate  # noqa: E402
from api.routers import batch_experiments as be  # noqa: E402

EXISTING_KEY = "sk-existing-0123456789"
INVITE = "invite-abc"


def _req(models, agents=None, harness_count=3):
    harnesses = [
        {"harness_type": t, "harness_config_id": i + 1,
         "harness_config_version_id": i + 1, "label": f"H{i+1}"}
        for i, t in enumerate(["claude-code", "codex", "opencode"][:harness_count])
    ]
    common = {"agents": agents} if agents is not None else {}
    return BatchExperimentCreate(
        name="t", registration_id=1,
        harnesses=harnesses, models=models, common=common,
    )


class KeyAllocTest(unittest.TestCase):
    """捕获每个子任务实际收到的 payload，断言 key 分配。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO task_registrations (created_by, task_name, task_path_obs, data_total) "
                "VALUES (?,?,?,?)", ("u", "ds", "task_data/x/configs", 3))

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _run(self, req):
        """跑一次创建，返回每个子任务的 InstanceCreate 入参。"""
        import asyncio
        seen = []

        async def _fake_create(req_obj, _user):
            seen.append(req_obj)
            inst = mock.Mock()
            inst.id = f"inst-{req_obj.name}"
            return inst

        with mock.patch("api.routers.instances.create_instance", _fake_create):
            asyncio.run(be.create_experiment(req, {"username": "u", "role": "operator"}))
        return seen

    # ---- 主 Agent 模型 ----

    def test_invite_code_wins_first_key_reused(self):
        seen = self._run(_req([{
            "model_id": "m", "model_base_url": "http://h:8082/v1",
            "model_api_key": EXISTING_KEY, "invite_code": INVITE,
        }]))
        self.assertEqual(len(seen), 3)
        # 第一个沿用现成 key，不浪费
        self.assertEqual(seen[0].model_api_key, EXISTING_KEY)
        # 其余清空 → create_instance 会按 invite_code 各自申请
        self.assertEqual(seen[1].model_api_key, "")
        self.assertEqual(seen[2].model_api_key, "")
        # invite_code 要一路传下去，否则清空后没人去申请
        for s in seen:
            self.assertEqual(s.invite_code, INVITE)

    def test_invite_code_without_key_all_empty(self):
        seen = self._run(_req([{
            "model_id": "m", "model_base_url": "http://h:8082/v1",
            "model_api_key": "", "invite_code": INVITE,
        }]))
        self.assertEqual([s.model_api_key for s in seen], ["", "", ""])

    def test_no_invite_code_shares_one_key(self):
        """不填 invite_code 时保持老行为：全批共用同一把 key。"""
        seen = self._run(_req([{
            "model_id": "m", "model_base_url": "http://h:8082/v1",
            "model_api_key": EXISTING_KEY, "invite_code": "",
        }]))
        self.assertEqual([s.model_api_key for s in seen],
                         [EXISTING_KEY, EXISTING_KEY, EXISTING_KEY])

    def test_different_models_have_separate_first_key(self):
        """两个模型各自的 invite_code 分桶，各有各的「第一把」。"""
        seen = self._run(_req([
            {"model_id": "m1", "model_base_url": "http://h:8082/v1",
             "model_api_key": "sk-k1-0123456789", "invite_code": "code-1"},
            {"model_id": "m2", "model_base_url": "http://h:8082/v1",
             "model_api_key": "sk-k2-0123456789", "invite_code": "code-2"},
        ], harness_count=2))
        by_model = {}
        for s in seen:
            by_model.setdefault(s.model_id, []).append(s.model_api_key)
        self.assertEqual(by_model["m1"], ["sk-k1-0123456789", ""])
        self.assertEqual(by_model["m2"], ["sk-k2-0123456789", ""])

    # ---- 用户模拟 agents ----

    def test_agents_follow_same_rule(self):
        agents = [{
            "name": "user_simulator", "model": "x",
            "base_url": "http://h:8082/v1",
            "api_key": EXISTING_KEY, "invite_code": INVITE,
        }]
        seen = self._run(_req(
            [{"model_id": "m", "model_base_url": "http://h:8082/v1"}],
            agents=agents))
        keys = [s.agents[0].api_key for s in seen]
        self.assertEqual(keys, [EXISTING_KEY, "", ""])
        # invite_code 必须保留，否则后两个子任务无从申请
        for s in seen:
            self.assertEqual(s.agents[0].invite_code, INVITE)

    def test_agents_list_not_mutated_across_subtasks(self):
        """agents 若被就地改，第一个子任务之后全都会被污染。"""
        agents = [{
            "name": "a1", "base_url": "http://h:8082/v1",
            "api_key": EXISTING_KEY, "invite_code": INVITE,
        }]
        self._run(_req([{"model_id": "m"}], agents=agents))
        # 调用方传进来的原始 dict 不能被改动
        self.assertEqual(agents[0]["api_key"], EXISTING_KEY)

    def test_two_agents_different_invite_codes(self):
        agents = [
            {"name": "sim", "base_url": "http://h:8082/v1",
             "api_key": "sk-sim-0123456789", "invite_code": "code-sim"},
            {"name": "eval", "base_url": "http://h:8082/v1",
             "api_key": "sk-eval-0123456789", "invite_code": "code-eval"},
        ]
        seen = self._run(_req(
            [{"model_id": "m", "model_base_url": "http://h:8082/v1"}],
            agents=agents, harness_count=2))
        self.assertEqual([s.agents[0].api_key for s in seen], ["sk-sim-0123456789", ""])
        self.assertEqual([s.agents[1].api_key for s in seen], ["sk-eval-0123456789", ""])


if __name__ == "__main__":
    unittest.main()
