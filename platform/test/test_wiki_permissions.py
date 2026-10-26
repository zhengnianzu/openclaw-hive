# -*- coding: utf-8 -*-
"""文档中心（Wiki）权限测试（unittest，无 pytest 依赖）。

覆盖本次放开的边界：
  - operator 能新建文档，且 author 自动记为本人
  - operator 能改/删自己的，改不了别人的
  - admin 通吃
  - viewer 一律拒绝
  - 分类管理 / 排序 / 发布通知仍然只有 admin

测试在临时目录里造一个空 wiki，不碰真实 wiki/md/。

运行：cd platform && python -m unittest test.test_wiki_permissions -v
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.core.config import settings  # noqa: E402
from api.core.security import get_current_user  # noqa: E402
from api.routers import wiki  # noqa: E402

_ADMIN = {"id": 1, "username": "boss", "role": "admin", "is_active": 1}
_OPS = {"id": 2, "username": "alice", "role": "operator", "is_active": 1}
_OP_OTHER = {"id": 3, "username": "bob", "role": "operator", "is_active": 1}
_VIEWER = {"id": 4, "username": "viewer1", "role": "viewer", "is_active": 1}

_ACTIVE = None   # 当前请求扮演谁


def _fake_current_user():
    return _ACTIVE


class WikiPermissionTest(unittest.TestCase):
    def setUp(self):
        global _ACTIVE
        self._tmp = tempfile.TemporaryDirectory()
        # 整个 wiki 根（md/ + index.db + assets/）都指向临时目录。
        # 只 patch md_dir 不够：get_wiki_connection 读的是 settings.WIKI_DB_PATH，
        # 不换掉的话测试会往真实 wiki/index.db 里写分类和文档行 —— 实际发生过一次，
        # 生产库里留下了 `临时` 分类和 public-doc 行。
        self._old_wiki_dir = settings.WIKI_DIR
        self._old_wiki_db = settings.WIKI_DB_PATH
        settings.WIKI_DIR = self._tmp.name
        settings.WIKI_DB_PATH = os.path.join(self._tmp.name, "index.db")

        app = FastAPI()
        app.include_router(wiki.router)
        app.dependency_overrides[get_current_user] = _fake_current_user
        self.client = TestClient(app)
        _ACTIVE = _OPS

    def tearDown(self):
        settings.WIKI_DIR = self._old_wiki_dir
        settings.WIKI_DB_PATH = self._old_wiki_db
        self.client.app.dependency_overrides.clear()
        self._tmp.cleanup()

    # ---- 小工具 ----

    def _as(self, who):
        global _ACTIVE
        _ACTIVE = who

    def _create(self, title="我的文档", slug=None):
        body = {"title": title, "content_md": "# 正文", "tags": "", "summary": ""}
        if slug:
            body["slug"] = slug
        return self.client.post("/api/wiki/documents", json=body)

    def _seed(self, title, slug, author):
        """绕过接口直接落一篇文档，用来造「别人写的」数据。"""
        from datetime import datetime
        now = datetime.now().isoformat()
        wiki.wiki_store.write_file(slug, {
            "title": title, "category": "", "tags": "", "summary": "",
            "linked_entity_type": "", "linked_entity_key": "",
            "is_pinned": 0, "sort_order": 100, "author": author,
            "created_at": now, "updated_at": now,
        }, "# 正文")

    # ---- 新建 ----

    def test_operator_can_create_and_becomes_author(self):
        r = self._create("运行者的笔记", slug="ops-note")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["author"], "alice")

    def test_viewer_cannot_create(self):
        self._as(_VIEWER)
        self.assertEqual(self._create(slug="by-viewer").status_code, 403)

    def test_admin_can_create(self):
        self._as(_ADMIN)
        self.assertEqual(self._create(slug="by-admin").status_code, 200)

    # ---- 改 ----

    def test_operator_can_update_own(self):
        self._create(slug="mine")
        r = self.client.put("/api/wiki/documents/mine", json={"title": "改过的标题"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["title"], "改过的标题")

    def test_operator_cannot_update_others(self):
        self._seed("别人的", "not-mine", "bob")
        r = self.client.put("/api/wiki/documents/not-mine", json={"title": "篡改"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("只能修改自己创建的文档", r.json()["detail"])

    def test_admin_can_update_others(self):
        self._seed("别人的", "not-mine", "bob")
        self._as(_ADMIN)
        r = self.client.put("/api/wiki/documents/not-mine", json={"title": "管理员改的"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_update_records_updated_by(self):
        """updated_by 原来从未写入，详情页「最后由 X 更新」永远显示作者本人。"""
        self._create(slug="track")
        self.client.put("/api/wiki/documents/track", json={"title": "改过"})
        rec = wiki.wiki_store.parse_file("track")
        self.assertEqual(rec["updated_by"], "alice")

    def test_update_missing_doc_is_404(self):
        self.assertEqual(
            self.client.put("/api/wiki/documents/nope", json={"title": "x"}).status_code, 404)

    # ---- 删 ----

    def test_operator_can_delete_own(self):
        self._create(slug="to-delete")
        self.assertEqual(self.client.delete("/api/wiki/documents/to-delete").status_code, 200)

    def test_operator_cannot_delete_others(self):
        self._seed("别人的", "keep-me", "bob")
        # 文件在 <WIKI_DIR>/md/ 下（wiki_store.md_dir），不是 WIKI_DIR 根目录
        path = os.path.join(self._tmp.name, "md", "keep-me.md")
        self.assertTrue(os.path.isfile(path), "种子文档没写成功，后续断言无意义")
        r = self.client.delete("/api/wiki/documents/keep-me")
        self.assertEqual(r.status_code, 403)
        # 403 之后文件必须还在
        self.assertTrue(os.path.isfile(path))

    def test_admin_can_delete_others(self):
        self._seed("别人的", "admin-del", "bob")
        self._as(_ADMIN)
        self.assertEqual(self.client.delete("/api/wiki/documents/admin-del").status_code, 200)

    # ---- 全局操作仍限 admin ----

    def test_operator_cannot_reorder(self):
        self._create(slug="a")
        r = self.client.post("/api/wiki/documents/reorder", json={"slugs": ["a"]})
        self.assertEqual(r.status_code, 403)

    def test_operator_cannot_manage_categories(self):
        self.assertEqual(
            self.client.post("/api/wiki/categories", json={"name": "新分类"}).status_code, 403)

    def test_operator_cannot_publish(self):
        self._create(slug="pub")
        r = self.client.post("/api/wiki/documents/pub/publish", json={"push": True})
        self.assertEqual(r.status_code, 403)

    def test_operator_cannot_delete_category(self):
        # 先由 admin 建一个分类，再确认 operator 删不掉
        self._as(_ADMIN)
        cid = self.client.post("/api/wiki/categories", json={"name": "临时"}).json()["id"]
        self._as(_OPS)
        self.assertEqual(self.client.delete(f"/api/wiki/categories/{cid}").status_code, 403)

    # ---- 读不受影响 ----

    def test_viewer_can_still_read(self):
        self._seed("公开文档", "public-doc", "boss")
        self._as(_VIEWER)
        self.assertEqual(self.client.get("/api/wiki/documents/public-doc").status_code, 200)
        self.assertEqual(self.client.get("/api/wiki/documents").status_code, 200)


if __name__ == "__main__":
    unittest.main()
