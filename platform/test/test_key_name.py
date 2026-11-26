# -*- coding: utf-8 -*-
"""API Key 名字生成与长度约束测试（unittest，无 pytest 依赖）。

背景：上游（tokenfly 网关）对 key 名字有字节长度限制，超了返回
HTTP 200 + {"message":"Token name is too long"}，raise_for_status 拦不住，
而 create_instance 里申请失败只打日志不抛错 —— 结果是**静默**建出一批没有 key
的子任务（实测 Batch-20261124-007 有 17 个 agent 位置是空 key）。

两道防线：
  1. 名字用 task_name（batch-xxx-cN）而不是中文展示名，天然短且可追溯
  2. _truncate_key_name 按字节上限截断，且按字符边界切，不产生半个汉字

运行：cd platform && /root/.venv/bin/python test/test_key_name.py -v
"""
import os
import sys
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.routers.instances import (  # noqa: E402
    _KEY_NAME_MAX_BYTES, _truncate_key_name,
)


class TruncateTest(unittest.TestCase):
    def test_short_name_unchanged(self):
        self.assertEqual(_truncate_key_name("short"), "short")

    def test_realistic_batch_names_fit(self):
        """改用 task_name 后，批量实验的两种名字都不需要截断。"""
        for name in ("60043045_batch-20261124-007-c1",
                     "batch-20261124-007-c1_user_simulator",
                     "batch-20261124-007-c1_evaluator"):
            self.assertEqual(_truncate_key_name(name), name,
                             f"{name} 不该被截断（{len(name.encode())}B）")

    def test_over_limit_is_truncated(self):
        """这是线上实际失败的那个名字（56 字节）。"""
        long = "60043045_2611241113_test_smoke_ds-配置1_user_simulator"
        self.assertGreater(len(long.encode()), _KEY_NAME_MAX_BYTES)
        out = _truncate_key_name(long)
        self.assertLessEqual(len(out.encode()), _KEY_NAME_MAX_BYTES)
        self.assertTrue(long.startswith(out))

    def test_never_splits_a_multibyte_char(self):
        """按字符边界切：截断结果必须能正常 encode/decode，不留半个汉字。

        纯中文名逐个长度都试一遍，覆盖「边界正好落在某个汉字中间」的所有情况。
        """
        for n in range(1, 40):
            name = "配" * n
            out = _truncate_key_name(name)
            self.assertLessEqual(len(out.encode()), _KEY_NAME_MAX_BYTES)
            # 能原样往返说明没有被切出非法字节序列
            self.assertEqual(out.encode().decode("utf-8"), out)
            self.assertTrue(name.startswith(out))

    def test_empty_and_none(self):
        self.assertEqual(_truncate_key_name(""), "")
        self.assertEqual(_truncate_key_name(None), "")

    def test_strips_surrounding_space(self):
        self.assertEqual(_truncate_key_name("  abc  "), "abc")


if __name__ == "__main__":
    unittest.main()
