# -*- coding: utf-8 -*-
"""会话列表（GET /shallow/tasks）的两个新筛选项。

  1. 任务编号搜索：keyword 写 `task-5921` 或 `5921` → 按 task_idx 精确匹配
  2. 任务配置正文检索：content_keyword → grep task_input_path 下的 config json

运行：cd platform && python test/test_shallow_task_search.py
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.config import settings  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="sts_db_")
settings.DB_PATH = os.path.join(_TMP, "test_platform.db")

from api.core.database import get_connection, init_db  # noqa: E402
from api.core.database import ensure_traj_extra_columns  # noqa: E402
from api.routers import batch_output as bo  # noqa: E402
from offline import shallow_query as sq  # noqa: E402

IID = "__sts_inst__"


class TaskIdxKeywordTest(unittest.TestCase):
    """keyword 的 `task-N` / 纯数字写法。

    列表上万行时用户看到的是 task-N,让他必须去搜哈希名不合理。
    """

    def test_matches_task_dash_number(self):
        import re
        for kw, want in (("task-5921", 5921), ("TASK-100", 100), ("5921", 5921)):
            m = re.fullmatch(r"(?:task-)?(\d+)", kw, re.I)
            self.assertIsNotNone(m, kw)
            self.assertEqual(int(m.group(1)), want)

    def test_non_numeric_is_rejected(self):
        """非「task-数字」形态不匹配 —— 编号框是独立的,不会回退到名称模糊搜。

        接口对这种输入返回空（WHERE 1=0）而不是忽略该条件:忽略会让用户
        以为「搜 task-abc 查出全部」是匹配上了。
        """
        import re
        for kw in ("92ee900a", "workspace_builder", "task-abc", "画"):
            self.assertIsNone(re.fullmatch(r"(?:task-)?(\d+)", kw, re.I), kw)


class ExtractQueryTextTest(unittest.TestCase):
    """extract_query_text：从 config json 取任务描述。"""

    def test_queries_text(self):
        self.assertEqual(
            sq.extract_query_text({"queries": [{"text": "帮我画个花"}]}), "帮我画个花")

    def test_multi_queries_joined(self):
        """多轮任务把所有 text 拼起来一起搜。"""
        got = sq.extract_query_text({"queries": [{"text": "第一句"}, {"text": "第二句"}]})
        self.assertIn("第一句", got)
        self.assertIn("第二句", got)

    def test_string_list_format(self):
        """queries 是字符串列表的旧格式也认。"""
        self.assertEqual(sq.extract_query_text({"queries": ["直接一句"]}), "直接一句")

    def test_no_fallback_to_whole_json(self):
        """结构不符预期返回空串，**不回退到整个 json** ——
        否则搜「画」会被 MAP_Linux.json 之类的路径串误命中。"""
        self.assertEqual(sq.extract_query_text({"gateway_ws_url": "ws://x/画"}), "")
        self.assertEqual(sq.extract_query_text({"queries": "不是列表"}), "")
        self.assertEqual(sq.extract_query_text({}), "")


class LruEvictTest(unittest.TestCase):
    """按实例 LRU 只保留最近 N 个的 query_text。

    搜索是交互行为,只发生在用户正在看的实例上:全库 1092 万行里
    running + 点击过的只占 3%。超出 LRU 的置 NULL（行保留,可重建）。
    """

    def setUp(self):
        init_db()
        ensure_traj_extra_columns()
        with get_connection() as conn:
            conn.execute("DELETE FROM task_records WHERE instance_id LIKE '__lru%'")
            conn.execute("CREATE TABLE IF NOT EXISTS shallow_query_lru ("
                         "  instance_id TEXT PRIMARY KEY, touched_at REAL NOT NULL)")
            conn.execute("DELETE FROM shallow_query_lru")
            conn.commit()

    def test_evicts_oldest(self):
        import time as _t
        with get_connection() as conn:
            for i in range(8):
                conn.execute(
                    "INSERT INTO task_records (instance_id, config_name, task_idx, "
                    " status, query_text) VALUES (?,?,0,'任务成功','文本')",
                    (f"__lru{i:02d}", f"a{i}.json"))
                sq.touch_instance(conn, f"__lru{i:02d}")
                _t.sleep(0.002)      # 保证 touched_at 有序
            conn.commit()
        self.assertEqual(sq.evict_lru(5), 3)
        with get_connection() as conn:
            kept = conn.execute(
                "SELECT COUNT(*) n FROM task_records "
                " WHERE instance_id LIKE '__lru%' AND query_text IS NOT NULL"
            ).fetchone()["n"]
            cleared = [r["instance_id"] for r in conn.execute(
                "SELECT instance_id FROM task_records "
                " WHERE instance_id LIKE '__lru%' AND query_text IS NULL "
                " ORDER BY instance_id")]
        self.assertEqual(kept, 5)
        self.assertEqual(cleared, ["__lru00", "__lru01", "__lru02"],
                         "清掉的必须是最久没被搜的")

    def test_noop_when_under_limit(self):
        with get_connection() as conn:
            sq.touch_instance(conn, "__lru00")
            conn.commit()
        self.assertEqual(sq.evict_lru(20), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
