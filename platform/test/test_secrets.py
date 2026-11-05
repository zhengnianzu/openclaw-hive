# -*- coding: utf-8 -*-
"""API Key 脱敏测试（unittest，无 pytest 依赖）。

覆盖实例详情两条脱敏路径的核心函数：
  1. mask_create_params —— /create-params 下发前遮三处 key 字段，且不改入参；
  2. collect_secret_values —— 按长度降序，短值不参与替换；
  3. redact_text —— 「配置文件」tab 按原文替换，各 harness 格式一视同仁。

运行：cd platform && python -m unittest discover -s test -p "test_secrets.py" -v
"""
import json
import os
import sys
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.services import secrets as sec  # noqa: E402

# 真实形态：库里 1239 个 key 全是 sk- 开头、长度 51
KEY_MAIN = "sk-" + "a" * 48
KEY_SIM = "sk-" + "b" * 48
KEY_EVAL = "sk-" + "c" * 48


def _params():
    return {
        "model_api_key": KEY_MAIN,
        "model_base_url": "http://115.120.62.121:8082/v1",
        "user_proxy_api_key": KEY_SIM,
        "agents": [
            {"name": "user_simulator", "api_key": KEY_SIM, "model": "m1"},
            {"name": "evaluator", "api_key": KEY_EVAL, "model": "m2"},
        ],
    }


class TestMaskSecret(unittest.TestCase):
    def test_long_value_keeps_edges(self):
        self.assertEqual(sec.mask_secret("sk-1234567890abcdef"), "sk-1***cdef")

    def test_short_value_fully_masked(self):
        self.assertEqual(sec.mask_secret("short"), sec.MASK_PLACEHOLDER)
        self.assertEqual(sec.mask_secret("12345678"), sec.MASK_PLACEHOLDER)

    def test_empty(self):
        self.assertEqual(sec.mask_secret(None), "")
        self.assertEqual(sec.mask_secret(""), "")

    def test_idempotent(self):
        once = sec.mask_secret(KEY_MAIN)
        self.assertEqual(sec.mask_secret(once), once)


class TestMaskCreateParams(unittest.TestCase):
    def test_masks_all_three_fields(self):
        out = sec.mask_create_params(_params())
        self.assertEqual(out["model_api_key"], sec.mask_secret(KEY_MAIN))
        self.assertEqual(out["user_proxy_api_key"], sec.mask_secret(KEY_SIM))
        self.assertEqual(out["agents"][0]["api_key"], sec.mask_secret(KEY_SIM))
        self.assertEqual(out["agents"][1]["api_key"], sec.mask_secret(KEY_EVAL))

    def test_no_raw_key_survives_serialization(self):
        blob = json.dumps(sec.mask_create_params(_params()))
        for raw in (KEY_MAIN, KEY_SIM, KEY_EVAL):
            self.assertNotIn(raw, blob)

    def test_does_not_mutate_input(self):
        """导出和复制实例还要用原文，脱敏绝不能回写调用方的 dict。"""
        p = _params()
        sec.mask_create_params(p)
        self.assertEqual(p["model_api_key"], KEY_MAIN)
        self.assertEqual(p["user_proxy_api_key"], KEY_SIM)
        self.assertEqual(p["agents"][0]["api_key"], KEY_SIM)
        self.assertEqual(p["agents"][1]["api_key"], KEY_EVAL)

    def test_keeps_non_secret_fields(self):
        out = sec.mask_create_params(_params())
        self.assertEqual(out["model_base_url"], "http://115.120.62.121:8082/v1")
        self.assertEqual(out["agents"][0]["name"], "user_simulator")

    def test_tolerates_missing_and_odd_shapes(self):
        self.assertEqual(sec.mask_create_params({}), {})
        self.assertEqual(sec.mask_create_params({"agents": None}), {"agents": None})
        # agents 里混入非 dict 不该炸
        out = sec.mask_create_params({"agents": ["x", {"api_key": KEY_MAIN}]})
        self.assertEqual(out["agents"][0], "x")
        self.assertEqual(out["agents"][1]["api_key"], sec.mask_secret(KEY_MAIN))


class TestCollectSecretValues(unittest.TestCase):
    def test_dedups_and_sorts_desc_by_length(self):
        vals = sec.collect_secret_values(_params())
        # user_proxy_api_key 与 agents[0] 同值，只应出现一次
        self.assertEqual(len(vals), 3)
        self.assertEqual(sorted(vals), sorted({KEY_MAIN, KEY_SIM, KEY_EVAL}))
        self.assertEqual(vals, sorted(vals, key=len, reverse=True))

    def test_prefix_shadowing_replaces_longest_first(self):
        """短 key 是长 key 前缀时，必须先替长的，否则长 key 会留下原文尾巴。"""
        long_key = "sk-" + "z" * 48
        short_key = long_key[:20]
        vals = sec.collect_secret_values(
            {"model_api_key": short_key, "user_proxy_api_key": long_key}
        )
        self.assertEqual(vals[0], long_key)
        text = f"token={long_key}"
        self.assertNotIn(long_key, sec.redact_text(text, {
            "model_api_key": short_key, "user_proxy_api_key": long_key,
        }))

    def test_skips_short_values(self):
        # 短值不能当替换目标：会在正常内容里误命中
        self.assertEqual(sec.collect_secret_values({"model_api_key": "sk-123"}), [])

    def test_empty_params(self):
        self.assertEqual(sec.collect_secret_values({}), [])
        self.assertEqual(sec.collect_secret_values(None), [])


class TestRedactText(unittest.TestCase):
    """各 harness 的 key 字段名/格式都不同，replace 对它们一视同仁。"""

    def test_claude_code_settings_json(self):
        text = json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "http://x/v1",
            "ANTHROPIC_AUTH_TOKEN": KEY_MAIN,
        }}, indent=2)
        out = sec.redact_text(text, _params())
        self.assertNotIn(KEY_MAIN, out)
        self.assertIn(sec.mask_secret(KEY_MAIN), out)
        self.assertIn("ANTHROPIC_BASE_URL", out)  # 其余内容未被破坏

    def test_codex_toml_and_dsh_dynamic_env_name(self):
        text = (
            '[model_providers.custom]\n'
            f'experimental_bearer_token = "{KEY_MAIN}"\n'
            'credentials:\n'
            f'  CUSTOM_API_KEY: {KEY_EVAL}\n'
        )
        out = sec.redact_text(text, _params())
        self.assertNotIn(KEY_MAIN, out)
        self.assertNotIn(KEY_EVAL, out)

    def test_openclaw_json_nested(self):
        text = json.dumps({"models": {"providers": {"local": {
            "apiKey": KEY_MAIN, "baseUrl": "http://x",
        }}}})
        self.assertNotIn(KEY_MAIN, sec.redact_text(text, _params()))

    def test_multiple_occurrences_all_replaced(self):
        text = f"{KEY_SIM}\nagain: {KEY_SIM}\n"
        out = sec.redact_text(text, _params())
        self.assertNotIn(KEY_SIM, out)
        self.assertEqual(out.count(sec.mask_secret(KEY_SIM)), 2)

    def test_no_params_returns_content_unchanged(self):
        """历史实例可能没有 create_params —— 原样返回，不该 500。"""
        text = f"apiKey: {KEY_MAIN}"
        self.assertEqual(sec.redact_text(text, {}), text)
        self.assertEqual(sec.redact_text(text, None), text)

    def test_empty_content(self):
        self.assertEqual(sec.redact_text("", _params()), "")

    def test_config_yaml_default_api_key_untouched(self):
        """config.yaml 的 global.api_key 是非敏感默认值，不在 key 清单里，不该被动。"""
        text = "global:\n  api_key: omni-env-default-api-key\n"
        self.assertEqual(sec.redact_text(text, _params()), text)


# ---------------------------------------------------------------------------
# 沙箱配置脱敏（viewer 看不到 controlplane_api_key 与 env 里的 key）
# ---------------------------------------------------------------------------

CP_KEY = "cp-" + "d" * 30          # global.api_key
ENV_KEY = "serper-" + "e" * 26     # env 里的 SERPER_API_KEY


def _sandbox_cfg():
    return {
        "global": {"api_key": CP_KEY, "controlplane_base_url": "http://x:1/v1"},
        "sandbox": {
            "x86_cpu": {
                "sandbox": {
                    "image": "swr.example.com/t:1",
                    "env": [
                        {"name": "SERPER_API_KEY", "value": ENV_KEY},
                        {"name": "MCP_PLAYWRIGHT", "value": "off"},
                        {"name": "DSH_MAX_TOKENS", "value": "131072"},
                    ],
                }
            }
        },
    }


class TestSensitiveEnvName(unittest.TestCase):
    def test_hits(self):
        for n in ("SERPER_API_KEY", "VL_API_KEY", "api_key", "MY_TOKEN",
                  "CLIENT_SECRET", "DB_PASSWORD"):
            self.assertTrue(sec.is_sensitive_env_name(n), n)

    def test_misses(self):
        for n in ("MCP_PLAYWRIGHT", "PIP_INDEX_URL", "IMAGE_READING", ""):
            self.assertFalse(sec.is_sensitive_env_name(n), n)

    def test_none(self):
        self.assertFalse(sec.is_sensitive_env_name(None))

    def test_dsh_max_tokens_is_not_sensitive(self):
        """名字里带 TOKENS 但不是凭据 —— 当前按子串判定会命中，记录这个已知偏保守行为。"""
        self.assertTrue(sec.is_sensitive_env_name("DSH_MAX_TOKENS"))


class TestMaskEnvList(unittest.TestCase):
    def test_masks_only_sensitive(self):
        env = _sandbox_cfg()["sandbox"]["x86_cpu"]["sandbox"]["env"]
        out = sec.mask_env_list(env)
        self.assertEqual(out[0]["value"], sec.mask_secret(ENV_KEY))
        self.assertEqual(out[1]["value"], "off")          # 非敏感名，原样
        self.assertNotIn(ENV_KEY, json.dumps(out))

    def test_does_not_mutate_input(self):
        env = [{"name": "SERPER_API_KEY", "value": ENV_KEY}]
        sec.mask_env_list(env)
        self.assertEqual(env[0]["value"], ENV_KEY)

    def test_degrades_on_bad_shape(self):
        self.assertEqual(sec.mask_env_list(None), None)
        self.assertEqual(sec.mask_env_list("x"), "x")
        self.assertEqual(sec.mask_env_list(["a", 1]), ["a", 1])

    def test_empty_value_left_alone(self):
        out = sec.mask_env_list([{"name": "API_KEY", "value": ""}])
        self.assertEqual(out[0]["value"], "")


class TestCollectSandboxSecrets(unittest.TestCase):
    def test_collects_both_sources(self):
        vals = sec.collect_sandbox_secret_values(_sandbox_cfg())
        self.assertIn(CP_KEY, vals)
        self.assertIn(ENV_KEY, vals)

    def test_skips_non_sensitive_env(self):
        self.assertNotIn("131072", sec.collect_sandbox_secret_values(_sandbox_cfg()))
        self.assertNotIn("off", sec.collect_sandbox_secret_values(_sandbox_cfg()))

    def test_sorted_desc_by_length(self):
        vals = sec.collect_sandbox_secret_values(_sandbox_cfg())
        self.assertEqual(vals, sorted(vals, key=len, reverse=True))

    def test_short_values_skipped(self):
        cfg = {"global": {"api_key": "sk-xxx"}}
        self.assertEqual(sec.collect_sandbox_secret_values(cfg), [])

    def test_degrades_on_bad_shape(self):
        self.assertEqual(sec.collect_sandbox_secret_values(None), [])
        self.assertEqual(sec.collect_sandbox_secret_values({}), [])
        self.assertEqual(sec.collect_sandbox_secret_values({"sandbox": "x"}), [])
        self.assertEqual(sec.collect_sandbox_secret_values({"global": "x"}), [])
        self.assertEqual(sec.collect_sandbox_secret_values({"sandbox": {"a": "x"}}), [])
        self.assertEqual(
            sec.collect_sandbox_secret_values({"sandbox": {"a": {"sandbox": {"env": "x"}}}}), []
        )


class TestRedactSandboxYaml(unittest.TestCase):
    def test_removes_both_keys(self):
        text = f"global:\n  api_key: {CP_KEY}\nenv:\n  - value: {ENV_KEY}\n"
        out = sec.redact_sandbox_yaml(text, _sandbox_cfg())
        self.assertNotIn(CP_KEY, out)
        self.assertNotIn(ENV_KEY, out)
        self.assertIn(sec.mask_secret(CP_KEY), out)

    def test_keeps_structure(self):
        text = f"global:\n  api_key: {CP_KEY}\n  controlplane_base_url: http://x:1/v1\n"
        out = sec.redact_sandbox_yaml(text, _sandbox_cfg())
        self.assertIn("controlplane_base_url: http://x:1/v1", out)

    def test_empty_and_bad_cfg(self):
        self.assertEqual(sec.redact_sandbox_yaml("", _sandbox_cfg()), "")
        text = f"api_key: {CP_KEY}"
        self.assertEqual(sec.redact_sandbox_yaml(text, {}), text)


if __name__ == "__main__":
    unittest.main()
