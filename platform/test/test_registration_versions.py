# -*- coding: utf-8 -*-
"""数据集版本功能：/api/registrations 的自动版本 + 恢复。

覆盖几个「写错了不会立刻报错、但会毁掉审计价值」的点：

  1. 创建即 v1，change_json 为空（没有上一版可比）
  2. 空保存（字段原样提交）不产生新版本 —— 否则用户点开编辑框再保存一次就多一版
  3. 改一个字段 → v2，change_json 精确记录 field/old/new
  4. restore 追加 v3 并改写主行，历史不被删除
  5. api_key 不进快照（历史表不能成为第二份明文密钥库）
  6. ensure_versions 幂等，存量行只回填一条 v1

运行：cd platform && python3 -m unittest test.test_registration_versions -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.core import database as db  # noqa: E402
from api.core.security import create_access_token  # noqa: E402
from api.routers import registrations as reg  # noqa: E402


class RegistrationVersionTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.init_db()

        app = FastAPI()
        app.include_router(reg.router)
        self.client = TestClient(app)

        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                ("adm1", "x", "admin"),
            )
            conn.execute(
                "INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                ("vw1", "x", "viewer"),
            )

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        self._tmpdir.cleanup()

    # ---- helpers ----
    def _h(self, username):
        return {"Authorization": f"Bearer {create_access_token({'sub': username})}"}

    def _create(self, **over):
        body = {
            "task_name": "web-260520",
            "requester": "alice",
            "task_path_obs": "obs://bucket/task_data/260520/web_configs",
            "skill_dir_obs": "skills/lib",
            "agent_dir_obs": "task_data/agents",
            "default_skills": "find-skills",
            "remark": "初始",
            "api_key": "sk-SECRET-should-not-be-snapshotted",
        }
        body.update(over)
        r = self.client.post("/api/registrations", json=body, headers=self._h("adm1"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _versions(self, reg_id):
        r = self.client.get(f"/api/registrations/{reg_id}/versions", headers=self._h("adm1"))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ---- tests ----
    def test_create_makes_v1(self):
        info = self._create()
        self.assertEqual(info["current_version"], 1)
        self.assertEqual(info["version_count"], 1)
        self.assertEqual(info["latest_change"], "初始创建")

        vs = self._versions(info["id"])
        self.assertEqual(len(vs), 1)
        self.assertEqual(vs[0]["version"], 1)
        self.assertEqual(json.loads(vs[0]["change_json"]), [])

    def test_empty_save_does_not_bump_version(self):
        info = self._create()
        # 原样提交所有字段 = 没有实质变更
        payload = {k: info[k] for k in reg.SNAPSHOT_FIELDS}
        r = self.client.put(f"/api/registrations/{info['id']}", json=payload, headers=self._h("adm1"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["current_version"], 1)
        self.assertEqual(len(self._versions(info["id"])), 1)

    def test_field_change_makes_v2_with_diff(self):
        info = self._create()
        r = self.client.put(
            f"/api/registrations/{info['id']}",
            json={"task_path_obs": "obs://bucket/task_data/260601/web_configs"},
            headers=self._h("adm1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["current_version"], 2)
        # 没填 change_summary → 按改动字段自动生成
        self.assertIn("用户Config目录", r.json()["latest_change"])

        vs = self._versions(info["id"])
        self.assertEqual(len(vs), 2)
        changes = json.loads(vs[0]["change_json"])
        self.assertEqual(changes, [{
            "field": "task_path_obs",
            "old": "obs://bucket/task_data/260520/web_configs",
            "new": "obs://bucket/task_data/260601/web_configs",
        }])

    def test_custom_summary_is_stored(self):
        info = self._create()
        self.client.put(
            f"/api/registrations/{info['id']}",
            json={"remark": "换了备注", "change_summary": "同步上游更新"},
            headers=self._h("adm1"),
        )
        vs = self._versions(info["id"])
        self.assertEqual(vs[0]["summary"], "同步上游更新")

    def test_restore_appends_v3_and_rewrites_row(self):
        info = self._create()
        orig = info["task_path_obs"]
        self.client.put(
            f"/api/registrations/{info['id']}",
            json={"task_path_obs": "obs://bucket/changed"},
            headers=self._h("adm1"),
        )
        v1 = [v for v in self._versions(info["id"]) if v["version"] == 1][0]

        r = self.client.post(
            f"/api/registrations/{info['id']}/versions/{v1['id']}/restore",
            headers=self._h("adm1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["current_version"], 3)
        self.assertEqual(r.json()["task_path_obs"], orig)

        vs = self._versions(info["id"])
        self.assertEqual([v["version"] for v in vs], [3, 2, 1])  # 历史只追加
        self.assertEqual(vs[0]["summary"], "恢复自 v1")

    def test_api_key_never_in_snapshot(self):
        info = self._create()
        vs = self._versions(info["id"])
        snap = json.loads(vs[0]["snapshot_json"])
        self.assertNotIn("api_key", snap)
        self.assertNotIn("eval_config_api_key", snap)
        self.assertNotIn("SECRET", vs[0]["snapshot_json"])

    def test_diff_endpoint_reports_both_directions(self):
        info = self._create()
        self.client.put(
            f"/api/registrations/{info['id']}",
            json={"remark": "v2 的备注"},
            headers=self._h("adm1"),
        )
        self.client.put(
            f"/api/registrations/{info['id']}",
            json={"remark": "v3 的备注"},
            headers=self._h("adm1"),
        )
        v2 = [v for v in self._versions(info["id"]) if v["version"] == 2][0]
        r = self.client.get(
            f"/api/registrations/{info['id']}/versions/{v2['id']}/diff",
            headers=self._h("adm1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["previous"], [{"field": "remark", "old": "初始", "new": "v2 的备注"}])
        self.assertEqual(d["current"], [{"field": "remark", "old": "v2 的备注", "new": "v3 的备注"}])
        self.assertEqual(d["labels"]["task_path_obs"], "用户Config目录")

    def test_viewer_cannot_write(self):
        info = self._create()
        r = self.client.put(f"/api/registrations/{info['id']}", json={"remark": "x"},
                            headers=self._h("vw1"))
        self.assertEqual(r.status_code, 403, r.text)
        r = self.client.get(f"/api/registrations/{info['id']}/versions", headers=self._h("vw1"))
        self.assertEqual(r.status_code, 200, r.text)  # 读开放

    def test_delete_cascades_versions(self):
        info = self._create()
        self.client.delete(f"/api/registrations/{info['id']}", headers=self._h("adm1"))
        with db.get_connection() as conn:
            left = conn.execute(
                "SELECT COUNT(*) AS c FROM task_registration_versions WHERE registration_id = ?",
                (info["id"],),
            ).fetchone()["c"]
        self.assertEqual(left, 0)

    def test_ensure_versions_backfills_once(self):
        # 模拟存量行：直接插库，绕过 API（current_version = 0）
        with db.get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO task_registrations (created_by, task_name, task_path_obs, remark) "
                "VALUES (?,?,?,?)",
                ("legacy", "老数据集", "obs://bucket/old", "老备注"),
            )
            legacy_id = cur.lastrowid

        reg.ensure_versions()
        reg.ensure_versions()  # 幂等

        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM task_registration_versions WHERE registration_id = ?",
                (legacy_id,),
            ).fetchone()["c"]
            row = conn.execute(
                "SELECT current_version FROM task_registrations WHERE id = ?", (legacy_id,)
            ).fetchone()
        self.assertEqual(n, 1)
        self.assertEqual(row["current_version"], 1)

    def test_ensure_versions_realigns_stale_counter(self):
        """表里有版本但冗余列没同步（上次迁移中断）→ 对齐回最大版本号，不新建行。"""
        info = self._create()
        self.client.put(f"/api/registrations/{info['id']}", json={"remark": "改了"},
                        headers=self._h("adm1"))
        with db.get_connection() as conn:
            conn.execute("UPDATE task_registrations SET current_version = 0 WHERE id = ?",
                         (info["id"],))
        reg.ensure_versions()
        with db.get_connection() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM task_registration_versions WHERE registration_id = ?",
                (info["id"],),
            ).fetchone()["c"]
            cur = conn.execute("SELECT current_version FROM task_registrations WHERE id = ?",
                               (info["id"],)).fetchone()["current_version"]
        self.assertEqual(n, 2)
        self.assertEqual(cur, 2)


class DatasetFileTest(unittest.TestCase):
    """数据集文件：读优先本地缓存、写走 OBS 并回写缓存。

    上传本身不在这里真打 OBS —— 那是外部副作用。用一个假进程替换
    asyncio.create_subprocess_exec，只验证「什么时候会去调 obsutil、调了什么」。
    """

    ROOT_REL = "task_data/260520/web_configs"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._old_db_path = db.settings.DB_PATH
        self._old_cache = getattr(db.settings, "OUTPUT_CACHE", "")
        db.settings.DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        db.settings.OUTPUT_CACHE = os.path.join(self._tmpdir.name, "cache")
        db.init_db()

        app = FastAPI()
        app.include_router(reg.router)
        self.client = TestClient(app)

        with db.get_connection() as conn:
            conn.execute("INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                         ("adm1", "x", "admin"))
            conn.execute("INSERT INTO users (username, hashed_password, role) VALUES (?,?,?)",
                         ("vw1", "x", "viewer"))
            cur = conn.execute(
                "INSERT INTO task_registrations (created_by, task_name, task_path_obs) "
                "VALUES (?,?,?)",
                ("adm1", "web", self.ROOT_REL),
            )
            self.reg_id = cur.lastrowid

        self.obj = (f"obs://{db.settings.OBS_BUCKET.split('//')[-1]}/"
                    f"{self.ROOT_REL}/case001/config.json")

    def tearDown(self):
        db.settings.DB_PATH = self._old_db_path
        db.settings.OUTPUT_CACHE = self._old_cache
        self._tmpdir.cleanup()

    def _h(self, u="adm1"):
        return {"Authorization": f"Bearer {create_access_token({'sub': u})}"}

    def _seed_cache(self, body: str):
        """按 dataset_cache_dir 的规则把文件放进本地缓存（含 obsutil cp 造成的嵌套层）。"""
        d = os.path.join(reg.dataset_cache_dir(self.ROOT_REL), "web_configs", "case001")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    # ---- 读 ----
    def test_read_prefers_local_cache(self):
        self._seed_cache('{"from":"cache"}')
        r = self.client.get(f"/api/registrations/{self.reg_id}/file",
                            params={"path": self.obj}, headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["source"], "local")
        self.assertIn('"cache"', r.json()["content"])

    def test_read_missing_local_is_409(self):
        r = self.client.get(f"/api/registrations/{self.reg_id}/file",
                            params={"path": self.obj}, headers=self._h())
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("重新下载", r.json()["detail"])

    def test_read_rejects_object_outside_dataset(self):
        sibling = (f"obs://{db.settings.OBS_BUCKET.split('//')[-1]}/"
                   f"task_data/260520/web_configsX/a.json")
        r = self.client.get(f"/api/registrations/{self.reg_id}/file",
                            params={"path": sibling}, headers=self._h())
        self.assertEqual(r.status_code, 403, r.text)

    def test_read_rejects_traversal(self):
        evil = self.obj.replace("case001/config.json", "case001/../../../etc/passwd")
        r = self.client.get(f"/api/registrations/{self.reg_id}/file",
                            params={"path": evil}, headers=self._h())
        self.assertEqual(r.status_code, 400, r.text)

    # ---- 写：只写本地，标 modified；云端交给 sync_worker ----
    def test_save_rejects_invalid_json(self):
        self._seed_cache('{"old":1}')
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": "{not json"}, headers=self._h())
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("JSON", r.json()["detail"])

    def test_save_rejects_viewer(self):
        self._seed_cache('{}')
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": "{}"}, headers=self._h("vw1"))
        self.assertEqual(r.status_code, 403, r.text)

    def test_save_is_local_only_and_marks_modified(self):
        """保存不碰 OBS —— 这里故意不 mock subprocess，真调了就会失败/变慢。"""
        local = self._seed_cache('{"old":1}')
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": '{"new":2}'},
                            headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "modified")
        self.assertEqual(body["modified_count"], 1)
        self.assertIn("等待同步", body["detail"])
        # 本地文件确实被改了
        with open(local, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"new":2}')

    def test_save_without_local_copy_is_409(self):
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": '{"a":1}'},
                            headers=self._h())
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("重新下载", r.json()["detail"])

    def test_save_same_content_stays_clean(self):
        """内容没变 → 不制造一个「待同步」的假改动。"""
        from api.services import dataset_files as dfs
        self._seed_cache('{"same":1}')
        with db.get_connection() as conn:
            dfs.scan_and_store(conn, self.reg_id, self.ROOT_REL)   # 建基线
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": '{"same":1}'},
                            headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "clean")

    def test_save_without_baseline_marks_modified(self):
        """没有基线行 = 云端那版未知，必须传一次才能确定一致。"""
        self._seed_cache('{"x":1}')
        r = self.client.put(f"/api/registrations/{self.reg_id}/file",
                            json={"path": self.obj, "content": '{"x":1}'},
                            headers=self._h())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "modified")

    # ---- 下载 + 哈希基线 ----
    def test_scan_and_store_builds_baseline(self):
        from api.services import dataset_files as dfs
        self._seed_cache('{"a":1}')
        with db.get_connection() as conn:
            out = dfs.scan_and_store(conn, self.reg_id, self.ROOT_REL)
            rows = conn.execute(
                "SELECT rel_path, sha256, synced_sha256, status FROM task_registration_files "
                "WHERE registration_id = ?", (self.reg_id,)).fetchall()
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["new"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "clean")
        # 首次下载：当前哈希即基线
        self.assertEqual(rows[0]["sha256"], rows[0]["synced_sha256"])

    def test_scan_detects_local_drift(self):
        from api.services import dataset_files as dfs
        local = self._seed_cache('{"a":1}')
        with db.get_connection() as conn:
            dfs.scan_and_store(conn, self.reg_id, self.ROOT_REL)   # 建基线
        with open(local, "w", encoding="utf-8") as f:
            f.write('{"a":2}')                                      # 本地改
        with db.get_connection() as conn:
            dfs.scan_and_store(conn, self.reg_id, self.ROOT_REL)
            row = conn.execute(
                "SELECT status FROM task_registration_files WHERE registration_id = ?",
                (self.reg_id,)).fetchone()
        self.assertEqual(row["status"], "modified")

    # ---- sync_worker ----
    def test_sync_worker_uploads_and_clears_modified(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sync_worker", os.path.join(PLATFORM_DIR, "offline", "sync_worker.py"))
        sw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sw)

        self._seed_cache('{"v":1}')
        self.client.put(f"/api/registrations/{self.reg_id}/file",
                        json={"path": self.obj, "content": '{"v":2}'}, headers=self._h())

        calls = []

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        old_run = subprocess.run
        sw.subprocess.run = lambda cmd, **kw: (calls.append(cmd), R())[1]
        try:
            stats = sw.sync_once(self.reg_id)
        finally:
            sw.subprocess.run = old_run

        self.assertEqual(stats["ok"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.obj, calls[0])
        self.assertIn("-f", calls[0])
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT status, sha256, synced_sha256 FROM task_registration_files "
                "WHERE registration_id = ?", (self.reg_id,)).fetchone()
            mc = conn.execute("SELECT modified_count FROM task_registrations WHERE id = ?",
                              (self.reg_id,)).fetchone()["modified_count"]
        self.assertEqual(row["status"], "clean")
        self.assertEqual(row["sha256"], row["synced_sha256"])
        self.assertEqual(mc, 0)

    def test_sync_worker_keeps_modified_on_upload_failure(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sync_worker", os.path.join(PLATFORM_DIR, "offline", "sync_worker.py"))
        sw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sw)

        self._seed_cache('{"v":1}')
        self.client.put(f"/api/registrations/{self.reg_id}/file",
                        json={"path": self.obj, "content": '{"v":2}'}, headers=self._h())

        class R:
            returncode = 1
            stdout = ""
            stderr = "AccessDenied"

        old_run = subprocess.run
        sw.subprocess.run = lambda cmd, **kw: R()
        try:
            stats = sw.sync_once(self.reg_id)
        finally:
            sw.subprocess.run = old_run

        self.assertEqual(stats["failed"], 1)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM task_registration_files WHERE registration_id = ?",
                (self.reg_id,)).fetchone()
        self.assertEqual(row["status"], "modified")   # 下一轮重试

    def test_redownload_refuses_when_modified(self):
        self._seed_cache('{"v":1}')
        self.client.put(f"/api/registrations/{self.reg_id}/file",
                        json={"path": self.obj, "content": '{"v":2}'}, headers=self._h())
        r = self.client.post(f"/api/registrations/{self.reg_id}/redownload", headers=self._h())
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("尚未同步", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
