# -*- coding: utf-8 -*-
"""GET /api/instances/{id}/task-config（会话列表「任务配置」弹窗）的测试。

重点是**路径安全**：这个接口对所有登录用户开放（含 viewer），而 config_name
来自前端。不校验的话 `?config_name=../../../etc/passwd` 就能越过
task_input_path 读任意文件。

运行：cd platform && python test/test_task_config.py
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.config import settings  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="tcfg_db_")
settings.DB_PATH = os.path.join(_TMP, "test_platform.db")

from fastapi import HTTPException  # noqa: E402
from api.core.database import get_connection, init_db  # noqa: E402
from api.routers.instances import get_task_config  # noqa: E402

IID = "__tcfg_inst__"
USER = {"username": "t"}


class TaskConfigPathSafetyTest(unittest.TestCase):
    """config_name 必须是纯文件名。"""

    def setUp(self):
        init_db()

    def _call(self, name):
        return get_task_config(IID, config_name=name, user=USER)

    def test_rejects_parent_traversal(self):
        with self.assertRaises(HTTPException) as cm:
            self._call("../../../etc/passwd")
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_subdir(self):
        """带目录成分的一律拒 —— basename 削完后与入参不一致。"""
        with self.assertRaises(HTTPException) as cm:
            self._call("sub/dir/x.json")
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_absolute_path(self):
        with self.assertRaises(HTTPException) as cm:
            self._call("/etc/passwd")
        self.assertEqual(cm.exception.status_code, 400)

    def test_rejects_empty(self):
        with self.assertRaises(HTTPException) as cm:
            self._call("")
        self.assertEqual(cm.exception.status_code, 400)

    def test_missing_instance_is_404(self):
        """实例不存在 → 404（不是 500）。"""
        with self.assertRaises(HTTPException) as cm:
            get_task_config("__no_such_instance__", config_name="a.json", user=USER)
        self.assertEqual(cm.exception.status_code, 404)


class TaskConfigReadTest(unittest.TestCase):
    """正常读取：造一个带 task_input_path 的实例。"""

    def setUp(self):
        init_db()
        self.root = tempfile.mkdtemp(prefix="tcfg_inst_")
        self.cfg_dir = os.path.join(self.root, "configs")
        os.makedirs(self.cfg_dir, exist_ok=True)
        with open(os.path.join(self.cfg_dir, "ok.json"), "w", encoding="utf-8") as f:
            f.write('{"harness_type":"openclaw","note":"hello"}')
        # config.yaml 指向上面的输入目录（_task_input_path 读的就是它）
        self.yaml = os.path.join(self.root, "config.yaml")
        with open(self.yaml, "w", encoding="utf-8") as f:
            f.write("run_config:\n  task:\n    task_input_path: %s\n" % self.cfg_dir)
        with get_connection() as conn:
            conn.execute("DELETE FROM task_instances WHERE id=?", (IID,))
            conn.execute(
                "INSERT INTO task_instances (id, name, config_path, status, created_at) "
                "VALUES (?,?,?,'finished',datetime('now'))", (IID, IID, self.yaml))
            conn.commit()

    def tearDown(self):
        with get_connection() as conn:
            conn.execute("DELETE FROM task_instances WHERE id=?", (IID,))
            conn.commit()

    def test_reads_content(self):
        r = get_task_config(IID, config_name="ok.json", user=USER)
        self.assertEqual(r["config_name"], "ok.json")
        self.assertEqual(r["type"], "json")
        self.assertIn("openclaw", r["content"])
        self.assertTrue(r["path"].endswith("ok.json"))

    def test_missing_file_is_404(self):
        with self.assertRaises(HTTPException) as cm:
            get_task_config(IID, config_name="nope.json", user=USER)
        self.assertEqual(cm.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
