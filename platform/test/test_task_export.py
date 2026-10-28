# -*- coding: utf-8 -*-
"""任务详情导出测试（unittest，无 pytest 依赖）。

覆盖三块容易回归的东西：
  1. 脱敏：mask_secret —— 导出文件不能带出原始 key；
  2. build_create_params：从端点抽出的公共函数，字段回填与详情页一致；
  3. build_workbook：Sheet 名称与表头结构。

运行：cd platform && python -m unittest discover -s test -p "test_task_export.py" -v
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
from api.services import task_export as tex  # noqa: E402
from api.services.create_params import build_create_params  # noqa: E402

SNAPSHOT = """global:
  api_key: "omni-env-default-api-key"
sandbox:
  x86_cpu:
    sandbox:
      env:
        - name: SERPER_API_KEY
          value: sk-serper-realkey-123
s3:
  bucket_name: "obs://rl-agentdata"
sandbox_active_key: x86_cpu
sandbox_namespace: omni-env-default-worker
run_config:
  obs:
    traj_save_bucket: "obs://rl-agentdata"
    traj_save_path: "openclaw_trajs/traj_x"
"""


class MaskTest(unittest.TestCase):
    def test_mask_secret_short_value_fully_masked(self):
        # 前端 maskKey 对 <= 8 的原样返回，后端必须更严格
        self.assertEqual(tex.mask_secret("short"), tex.MASK_PLACEHOLDER)
        self.assertEqual(tex.mask_secret("12345678"), tex.MASK_PLACEHOLDER)

    def test_mask_secret_long_value_keeps_edges(self):
        self.assertEqual(tex.mask_secret("sk-1234567890abcdef"), "sk-1***cdef")

    def test_mask_secret_empty(self):
        self.assertEqual(tex.mask_secret(None), "")
        self.assertEqual(tex.mask_secret(""), "")

    def test_mask_secret_idempotent(self):
        once = tex.mask_secret("sk-1234567890abcdef")
        self.assertEqual(tex.mask_secret(once), once)


class BuildCreateParamsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _row(self, **kw):
        base = {
            "id": "inst-1",
            "create_params": json.dumps({
                "task_name": "t1",
                "harness_type": "openclaw",
                "model_api_key": "sk-1234567890abcdef",
            }),
            "config_snapshot": SNAPSHOT,
            "suffix_api_keys": 0,
        }
        base.update(kw)
        return base

    def test_snapshot_backfill(self):
        params, meta = build_create_params(self._row())
        self.assertTrue(meta["has_snapshot"])
        self.assertIsNone(meta["snapshot_error"])
        self.assertEqual(params["obs_bucket"], "obs://rl-agentdata")
        self.assertEqual(params["sandbox_namespace"], "omni-env-default-worker")
        self.assertEqual(params["sandbox_active_key"], "x86_cpu")
        self.assertEqual(params["traj_save_path"], "openclaw_trajs/traj_x")
        self.assertFalse(params["suffix_api_keys"])
        self.assertNotIn("copy_complete_from", params)

    def test_missing_create_params_does_not_raise(self):
        # 导出时一条坏数据不该炸整包 —— 端点会自己 raise 404，服务函数不抛
        params, meta = build_create_params(self._row(create_params=None))
        self.assertEqual(params, {})
        self.assertTrue(meta["has_snapshot"])

    def test_broken_snapshot_records_error(self):
        params, meta = build_create_params(self._row(config_snapshot="a: [unclosed"))
        self.assertIsNotNone(meta["snapshot_error"])
        # create_params 本身仍可用
        self.assertEqual(params["task_name"], "t1")


class WorkbookTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    def _rows(self):
        return [
            {
                "id": "inst-1", "name": "任务A", "queue_id": "q1", "queue_position": 1,
                "harness_type": "openclaw", "status": "completed", "created_by": "u",
                "created_at": "2026-10-01T10:00:00", "concurrent_num": 100,
                "total_tasks": 10, "completed_tasks": 9, "failed_tasks": 1,
                "_params": {"task_name": "t1", "model_api_key": "sk-1234567890abcd",
                            "total_num": 0},
                "_params_meta": {"has_snapshot": True, "snapshot_error": None},
                "config_snapshot": SNAPSHOT,
            },
            {"id": "nope", "missing": True},
        ]

    def test_sheet_names_and_headers(self):
        from openpyxl import load_workbook

        wb = tex.build_workbook(self._rows(), conn=None, include_task_records=False)
        self.assertEqual(
            wb.sheetnames,
            ["导出概览", "任务总览", "创建参数-关键项"],
        )
        self.assertEqual(wb["任务总览"]["A1"].value, "任务名称")
        self.assertEqual(wb["任务总览"]["M1"].value, "成功数")
        self.assertEqual(wb["任务总览"]["N1"].value, "失败数")
        self.assertEqual(wb["创建参数-关键项"]["C1"].value, "基本.任务标识")

        path = os.path.join(self._tmpdir.name, "out.xlsx")
        with open(path, "wb") as f:
            f.write(tex.workbook_bytes(wb))
        ws = load_workbook(path)["任务总览"]
        # 概览末行是合计
        last = [c.value for c in ws[ws.max_row]]
        self.assertEqual(last[0], "合计")
        self.assertEqual(last[12], 9)    # M 列：成功数
        self.assertEqual(last[13], 1)    # N 列：失败数
        # 不存在的任务被保留成占位行，不静默丢弃
        ids = [ws.cell(row=r, column=2).value for r in range(2, ws.max_row + 1)]
        self.assertIn("nope", ids)

    def test_workbook_never_contains_raw_secret(self):
        wb = tex.build_workbook(self._rows(), conn=None, include_task_records=False)
        path = os.path.join(self._tmpdir.name, "out.xlsx")
        with open(path, "wb") as f:
            f.write(tex.workbook_bytes(wb))

        from openpyxl import load_workbook
        wb2 = load_workbook(path)
        for name in wb2.sheetnames:
            for row in wb2[name].iter_rows(values_only=True):
                for cell in row:
                    if not isinstance(cell, str):
                        continue
                    self.assertNotIn("omni-env-default-api-key", cell, f"{name} 泄漏了 global.api_key")
                    self.assertNotIn("sk-serper-realkey-123", cell, f"{name} 泄漏了 env key")

    def test_queue_sheet_prepended(self):
        wb = tex.build_workbook(
            self._rows(), conn=None,
            queue={"id": "q1", "name": "队列1", "max_concurrent": 2,
                   "created_at": "2026-10-01T09:00:00"},
            effective_concurrent=2, task_count=1,
            include_task_records=False,
        )
        self.assertEqual(wb.sheetnames[0], "导出概览")
        self.assertEqual(wb.sheetnames[1], "队列信息")

    def test_overview_has_funnel_columns(self):
        """漏斗列是 append-only：追加在原 18 列之后，既有列位置不变。"""
        funnel = {
            "inst-1": {"L0": 10, "L1": 8, "L1.5": 6, "L2": 4, "L3": 2,
                       "graded": 30, "unevaluated": 5, "failed": 1,
                       "total": 37, "task_done": 21},
        }
        wb = tex.build_workbook(self._rows(), conn=None,
                                include_task_records=False)
        # conn=None 时列仍在，值留空
        headers_no_conn = [c.value for c in wb["任务总览"][1]]
        self.assertEqual(headers_no_conn[18:], [
            "会话L0", "会话L1", "会话L1.5", "会话L2", "会话L3",
            "已分级", "未评估", "分级失败", "会话总数", "完成标记",
        ])

        ws = tex._sheet_overview(wb, self._rows(), funnel=funnel)
        rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2)]
        first = rows[0]
        # 前 18 列保持原样
        self.assertEqual(first[12], 9)    # 成功数
        self.assertEqual(first[13], 1)    # 失败数
        self.assertEqual(first[18:], [10, 8, 6, 4, 2, 30, 5, 1, 37, 21])
        # 合计行同步补齐漏斗列
        last = rows[-1]
        self.assertEqual(last[0], "合计")
        self.assertEqual(last[18:], [10, 8, 6, 4, 2, 30, 5, 1, 37, 21])
        # 不存在的任务：漏斗列留空而不是 0，避免误读成"跑了但全 0"
        self.assertEqual(rows[1][18:], [""] * 10)


class FunnelCountsTest(unittest.TestCase):
    """collect_funnel_counts：口径必须与 batch_output.get_shallow 一致。"""

    def setUp(self):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE task_records (
                instance_id TEXT, config_name TEXT, task_idx INTEGER, traj_level TEXT);
            CREATE TABLE task_traj_records (
                instance_id TEXT, config_name TEXT, task_done INTEGER);
        """)

    def tearDown(self):
        self.conn.close()

    def _add(self, iid, levels, done_configs=()):
        for i, lv in enumerate(levels):
            self.conn.execute(
                "INSERT INTO task_records VALUES (?,?,?,?)",
                (iid, f"c{i}", i, lv))
        for cfg in done_configs:
            self.conn.execute(
                "INSERT INTO task_traj_records VALUES (?,?,1)", (iid, cfg))

    def test_buckets_match_get_shallow(self):
        # L0/L1/L1.5/L2/L3 各 1，2 条 NULL，1 条 failed，1 条 'none' 残差
        self._add("a", ["L0", "L1", "L1.5", "L2", "L3",
                        None, None, "failed", "none"],
                  done_configs=("c0", "c2"))
        out = tex.collect_funnel_counts(self.conn, ["a"])
        self.assertEqual(out["a"], {
            "L0": 1, "L1": 1, "L1.5": 1, "L2": 1, "L3": 1,
            "graded": 5, "unevaluated": 2, "failed": 1,
            "total": 9,      # 'none' 残差只进 total
            "task_done": 2,
        })
        # 关键不变式：graded + unevaluated + failed != total（残差），导出概览已写明
        self.assertEqual(out["a"]["graded"] + out["a"]["unevaluated"]
                         + out["a"]["failed"], 8)

    def test_unknown_instance_and_empty_instance_are_zeroed(self):
        out = tex.collect_funnel_counts(self.conn, ["never-seen", "b", "b"])
        self.assertEqual(set(out), {"never-seen", "b"})   # 去重
        for d in out.values():
            self.assertEqual(d["total"], 0)
            self.assertEqual(d["graded"], 0)
            self.assertEqual(d["L0"], 0)

    def test_empty_input(self):
        self.assertEqual(tex.collect_funnel_counts(self.conn, []), {})


if __name__ == "__main__":
    unittest.main()
