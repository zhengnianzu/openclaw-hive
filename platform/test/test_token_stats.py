# -*- coding: utf-8 -*-
"""llm_proxy_lite 用量摘要解析测试（unittest，无 pytest 依赖）。

覆盖：
  - 标准 proxy_summary.txt 的汇总与按模型统计
  - 失败调用行（无输出 tokens）
  - 空文本 / 缺字段
  - collect_instance_tokens 的「无数据返回 None」口径（与 0 区分）

运行：cd platform && /root/.venv/bin/python test/test_token_stats.py -v
"""
import os
import sys
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.services import token_stats as ts  # noqa: E402

SUMMARY = """
================================================================================
API 调用统计摘要
================================================================================

成功调用次数: 3
失败调用次数: 1
总输入 Tokens: 30000
总输出 Tokens: 900
总 Tokens: 30900

按模型统计:
--------------------------------------------------------------------------------
glm-5.3: 调用 2 次, 输入 20000 , 输出 600, 总计 20600 tokens
glm-5.3: 调用 1 次, 输入 10000 , 输出 300, 总计 10300 tokens
glm-5.3-flash: 失败 1 次, 输入 500 tokens
--------------------------------------------------------------------------------
"""


class ParseTest(unittest.TestCase):
    def test_totals(self):
        p = ts.parse_proxy_summary(SUMMARY)
        self.assertEqual(p["ok_calls"], 3)
        self.assertEqual(p["fail_calls"], 1)
        self.assertEqual(p["input_tokens"], 30000)
        self.assertEqual(p["output_tokens"], 900)
        self.assertEqual(p["total_tokens"], 30900)

    def test_per_model(self):
        p = ts.parse_proxy_summary(SUMMARY)
        models = {m["model"]: m for m in p["models"]}
        self.assertIn("glm-5.3", models)
        self.assertIn("glm-5.3-flash", models)
        flash = models["glm-5.3-flash"]
        self.assertEqual(flash["fail"], 1)
        self.assertEqual(flash["ok"], 0)
        self.assertEqual(flash["input_tokens"], 500)

    def test_empty_text(self):
        p = ts.parse_proxy_summary("")
        self.assertEqual(p["ok_calls"], 0)
        self.assertEqual(p["total_tokens"], 0)
        self.assertEqual(p["models"], [])

    def test_total_falls_back_to_sum(self):
        p = ts.parse_proxy_summary("总输入 Tokens: 10\n总输出 Tokens: 5\n")
        self.assertEqual(p["total_tokens"], 15)


class CollectTest(unittest.TestCase):
    def test_not_enabled_returns_none(self):
        self.assertIsNone(ts.collect_instance_tokens({"proxy_enable": False,
                                                      "traj_save_path": "x"}))
        self.assertIsNone(ts.collect_instance_tokens(None))
        self.assertIsNone(ts.collect_instance_tokens("not json"))

    def test_missing_cache_returns_none(self):
        # 开了网关但缓存目录不存在 —— 必须是 None 而不是 0
        self.assertIsNone(ts.collect_instance_tokens({
            "proxy_enable": True,
            "traj_save_path": "nonexistent_traj_dir_for_test",
            "traj_save_bucket": "obs://no-such-bucket",
        }))


if __name__ == "__main__":
    unittest.main()
