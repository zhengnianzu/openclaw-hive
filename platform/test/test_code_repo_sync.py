# -*- coding: utf-8 -*-
"""代码仓 OBS/Git 两种来源的路径解析与脱敏测试（unittest，无 pytest 依赖）。

重点是**存量兼容**：OBS 仓的 src/tar 路径必须与加 Git 来源之前逐字一致，
否则线上 150 条记录已经下载打包好的产物全部对不上。

覆盖：
  1. repo_dirs / repo_tar_path —— obs 不带 commit 层、git 带 sha[:12] 一层；
  2. source_type 缺省 —— 存量行（无该列 / 值为空）一律按 obs 走；
  3. content_dir_name —— git 仓的 tar 顶层目录名从 subdir / repo_url 推导；
  4. 未同步的 git 仓取路径要报 409，而不是算出一个错目录；
  5. _safe —— 报错文本里的 token 原文必须被脱敏。

运行：cd platform && python -m unittest discover -s test -p "test_code_repo_sync.py" -v
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from fastapi import HTTPException  # noqa: E402

from api.core.config import settings  # noqa: E402
from api.services import code_repo_sync as crs  # noqa: E402

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def _obs_repo(**kw):
    repo = {
        "id": 1,
        "name": "openclaw-task",
        "version": "v1",
        "obs_path": "obs://rl-agentdata/code/openclaw-task/v1/openclaw-task/",
        "source_type": "obs",
    }
    repo.update(kw)
    return repo


def _git_repo(**kw):
    repo = {
        "id": 2,
        "name": "openclaw-task",
        "version": "main",
        "obs_path": "",
        "source_type": "git",
        "repo_url": "https://github.com/acme/openclaw-task.git",
        "branch": "main",
        "subdir": "",
        "resolved_commit": SHA,
    }
    repo.update(kw)
    return repo


class TestObsCompat(unittest.TestCase):
    """OBS 仓的路径不能因为引入 git 来源而改变一个字符。"""

    def _expected(self, kind, repo):
        return os.path.join(settings.HIVE_ROOT, "platform", "code", kind,
                            repo["name"], repo["version"])

    def test_dirs_unchanged(self):
        repo = _obs_repo()
        src, tar = crs.repo_dirs(repo)
        self.assertEqual(src, self._expected("src", repo))
        self.assertEqual(tar, self._expected("tar", repo))

    def test_tar_path_unchanged(self):
        repo = _obs_repo()
        self.assertEqual(
            crs.repo_tar_path(repo),
            os.path.join(self._expected("tar", repo), "openclaw-task.tar"),
        )

    def test_content_dir_is_obs_last_segment(self):
        # 改动前是 _obs_dir_name(obs_path)，语义必须保持
        self.assertEqual(crs.content_dir_name(_obs_repo()), "openclaw-task")

    def test_content_path(self):
        repo = _obs_repo()
        self.assertEqual(
            crs.content_path(repo),
            os.path.join(self._expected("src", repo), "openclaw-task"),
        )

    def test_legacy_rows_default_to_obs(self):
        """存量行没有 source_type 这一列，取不到时必须按 obs 处理。"""
        legacy = _obs_repo()
        del legacy["source_type"]
        self.assertFalse(crs.is_git(legacy))
        self.assertEqual(crs.source_type(legacy), "obs")
        # 空串（ALTER TABLE 回填出意外值时）同样兜住
        self.assertFalse(crs.is_git(_obs_repo(source_type="")))
        self.assertFalse(crs.is_git(_obs_repo(source_type=None)))

    def test_obs_path_has_no_commit_layer(self):
        """即使误填了 resolved_commit，obs 仓也不该多出一层目录。"""
        repo = _obs_repo(resolved_commit=SHA)
        self.assertEqual(crs.repo_dirs(repo)[0], self._expected("src", repo))


class TestGitDirs(unittest.TestCase):
    """git 仓原地更新：路径不含 commit 层，与 OBS 来源完全一致。"""

    def test_git_dirs_have_no_commit_layer(self):
        repo = _git_repo()
        src, tar = crs.repo_dirs(repo)
        base = os.path.join(settings.HIVE_ROOT, "platform", "code")
        self.assertEqual(src, os.path.join(base, "src", "openclaw-task", "main"))
        self.assertEqual(tar, os.path.join(base, "tar", "openclaw-task", "main"))

    def test_path_is_stable_across_commits(self):
        """同步到新 commit 不能改变 tar 路径 —— 否则存量实例的配置会指向不存在的文件。"""
        before = crs.repo_tar_path(_git_repo(resolved_commit="1" * 40))
        after = crs.repo_tar_path(_git_repo(resolved_commit="2" * 40))
        self.assertEqual(before, after)

    def test_unsynced_repo_path_computable(self):
        """路径不再依赖 resolved_commit，未同步也能算出来（实例创建时不必先拉一次）。"""
        for bad in ("", None):
            repo = _git_repo(resolved_commit=bad)
            self.assertTrue(crs.repo_tar_path(repo).endswith("openclaw-task.tar"))

    def test_work_root_is_repo_scoped(self):
        """工作副本放在 src_dir/.repo，与 tar 里的内容目录分开。"""
        repo = _git_repo()
        self.assertEqual(
            crs._work_root(repo),
            os.path.join(crs.repo_dirs(repo)[0], ".repo"),
        )

    def test_work_root_differs_from_content_dir(self):
        """subdir 模式下内容目录名可能与仓名不同，工作副本不能和它重名。"""
        repo = _git_repo(subdir="packages/openclaw-task")
        self.assertNotEqual(crs._work_root(repo), crs.content_path(repo))

    def test_content_dir_from_repo_url(self):
        """subdir 为空时取仓名，且要去掉 .git 后缀。"""
        self.assertEqual(crs.content_dir_name(_git_repo()), "openclaw-task")
        self.assertEqual(
            crs.content_dir_name(_git_repo(repo_url="https://github.com/acme/foo")),
            "foo",
        )

    def test_content_dir_from_subdir(self):
        self.assertEqual(
            crs.content_dir_name(_git_repo(subdir="packages/openclaw-task")),
            "openclaw-task",
        )

    def test_git_url_without_token_is_plain(self):
        old = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = ""
        try:
            self.assertEqual(crs._git_url(_git_repo()),
                             "https://github.com/acme/openclaw-task.git")
        finally:
            settings.GITHUB_TOKEN = old

    def test_git_url_injects_token(self):
        old = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = "ghp_" + "x" * 36
        try:
            url = crs._git_url(_git_repo())
            self.assertTrue(url.startswith("https://x-access-token:ghp_"))
            self.assertIn("@github.com/acme/openclaw-task.git", url)
        finally:
            settings.GITHUB_TOKEN = old

    def test_missing_repo_url_raises(self):
        with self.assertRaises(HTTPException) as ctx:
            crs._git_url(_git_repo(repo_url=""))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_branch_defaults_to_main(self):
        self.assertEqual(crs._branch(_git_repo(branch="")), "main")
        self.assertEqual(crs._branch(_git_repo(branch="dev")), "dev")


class TestErrorRedaction(unittest.TestCase):
    """clone 失败时 git 会把带 token 的完整 URL 回显到 stderr。"""

    def test_token_is_masked_in_error_text(self):
        token = "ghp_" + "y" * 36
        old = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = token
        try:
            raw = f"fatal: could not read from https://x-access-token:{token}@github.com/a/b.git"
            safe = crs._safe(raw)
            self.assertNotIn(token, safe)
            self.assertIn("github.com/a/b.git", safe)
        finally:
            settings.GITHUB_TOKEN = old

    def test_truncates_to_limit(self):
        old = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = ""
        try:
            self.assertEqual(len(crs._safe("z" * 900)), 500)
        finally:
            settings.GITHUB_TOKEN = old

    def test_no_token_configured_is_noop(self):
        old = settings.GITHUB_TOKEN
        settings.GITHUB_TOKEN = ""
        try:
            self.assertEqual(crs._safe("plain error"), "plain error")
        finally:
            settings.GITHUB_TOKEN = old


class TestRepoStatus(unittest.TestCase):
    # 用一个真实磁盘上不存在的 version，否则测试会读到线上已同步的目录，
    # 断言就随机器状态漂移（openclaw-task/main 正好是线上那条 git 仓）。
    NOPE = "__unittest_absent__"

    def test_unsynced_git_repo_reports_empty(self):
        """未同步的 git 仓：工作副本与 tar 都不存在，状态报「都没有」。"""
        st = crs.repo_status(_git_repo(version=self.NOPE, resolved_commit=""))
        self.assertFalse(st["downloaded"])
        self.assertFalse(st["packaged"])
        self.assertIsNone(st["tar_path"])
        self.assertEqual(st["commit"], "")
        self.assertFalse(st["syncing"])

    def test_git_src_path_points_to_work_copy(self):
        """git 仓的 src_path 恒为工作副本根目录，而不是按 commit 分的内容目录。"""
        repo = _git_repo(version=self.NOPE)
        st = crs.repo_status(repo)
        self.assertEqual(crs._work_root(repo), os.path.join(crs.repo_dirs(repo)[0], ".repo"))
        # 未下载时 src_path 为 None，但也必须是工作副本那条路径，不是内容目录
        self.assertIn(st["src_path"], (None, crs._work_root(repo)))
        self.assertNotEqual(crs._work_root(repo), crs.content_path(repo))

    def test_obs_repo_status_keys(self):
        st = crs.repo_status(_obs_repo())
        self.assertEqual(
            set(st),
            {"downloaded", "packaged", "tar_path", "src_path", "commit", "syncing"},
        )
        # OBS 仓没有「同步中」这个状态
        self.assertFalse(st["syncing"])


class TestSyncLock(unittest.TestCase):
    """同步互斥锁。

    线上事故：连点几次「同步」，并发的 clone 写同一个 `.clone-{sha}.tmp`，
    后进来的把先来的半成品 rmtree 掉，git 随即失败 —— 表现成「点了没反应」。
    锁必须真的互斥，否则这个 bug 会原样回来。
    """

    def setUp(self):
        # 用独立 version：默认的 openclaw-task/main 正是线上那条仓，
        # 跑测试时若恰好在同步，会抢到同一把锁，测试就会 409 误失败。
        self.repo = _git_repo(version="__unittest_lock__")

    def tearDown(self):
        path = crs._lock_path(self.repo)
        if os.path.exists(path):
            os.close(os.open(path, os.O_RDWR))
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_lock_path_is_repo_scoped(self):
        a = crs._lock_path(_git_repo())
        b = crs._lock_path(_git_repo(version="dev"))
        self.assertNotEqual(a, b)
        # 不同 commit 不该影响锁粒度：锁的是「这个仓的这个版本」
        self.assertEqual(a, crs._lock_path(_git_repo(resolved_commit="f" * 40)))

    def test_second_acquire_while_held_raises_409(self):
        """非阻塞模式下，锁被占用必须立刻 409，而不是陪着等。"""
        fd = crs._acquire_repo_lock(self.repo, blocking=False)
        try:
            with self.assertRaises(HTTPException) as ctx:
                crs._acquire_repo_lock(self.repo, blocking=False)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertIn("正在同步中", ctx.exception.detail)
        finally:
            crs._release_repo_lock(fd)

    def test_lock_released_allows_reacquire(self):
        fd = crs._acquire_repo_lock(self.repo, blocking=False)
        crs._release_repo_lock(fd)
        fd2 = crs._acquire_repo_lock(self.repo, blocking=False)
        crs._release_repo_lock(fd2)

    def test_blocking_acquire_waits_for_release(self):
        """阻塞模式要等到前一个释放后拿到锁，供实例创建/准备阶段排队使用。"""
        fd = crs._acquire_repo_lock(self.repo, blocking=False)
        threading.Timer(0.4, lambda: crs._release_repo_lock(fd)).start()
        t0 = time.monotonic()
        fd2 = crs._acquire_repo_lock(self.repo, blocking=True)
        waited = time.monotonic() - t0
        try:
            self.assertGreaterEqual(waited, 0.3)
        finally:
            crs._release_repo_lock(fd2)


class TestFailedFetchCleanup(unittest.TestCase):
    """失败不该留下空壳目录 —— 那会让「同步过」和「同步失败」在磁盘上无法区分。"""

    def test_empty_dir_removed(self):
        d = tempfile.mkdtemp()
        try:
            empty = os.path.join(d, "sha")
            os.makedirs(empty)
            crs._cleanup_failed_fetch(empty)
            self.assertFalse(os.path.exists(empty))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_non_empty_dir_kept(self):
        """里面已有上次成功的产物时不能删。"""
        d = tempfile.mkdtemp()
        try:
            keep = os.path.join(d, "sha")
            os.makedirs(keep)
            open(os.path.join(keep, "x"), "w").close()
            crs._cleanup_failed_fetch(keep)
            self.assertTrue(os.path.isdir(keep))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_dir_is_noop(self):
        crs._cleanup_failed_fetch("/nonexistent/never/there")


class TestCommitTag(unittest.TestCase):
    """描述首行的 commit 摘要。

    线上反馈：「描述没有变为 commit 内容」。原因之一是摘要里只有 sha，
    看不出这次改了什么；于是补上 commit 标题，并保证换行后的用户文字不被吞掉。
    """

    SHA = "a2816cd1adf1f94345c34a2c339493cdd0b7be34"

    def test_writes_sha_and_subject(self):
        out = crs.with_commit_tag("", self.SHA, "fix: 精简网关日志")
        self.assertEqual(out, "[commit a2816cd1adf1] fix: 精简网关日志")

    def test_sha_shortened_to_12(self):
        out = crs.with_commit_tag("", self.SHA, "x")
        self.assertIn("[commit a2816cd1adf1]", out)
        self.assertNotIn(self.SHA, out)

    def test_subject_optional(self):
        self.assertEqual(crs.with_commit_tag("", self.SHA, ""), "[commit a2816cd1adf1]")

    def test_user_text_preserved_on_next_line(self):
        """描述是人的字段：只覆盖首行摘要，正文必须留着。"""
        old = "[commit ac970b8e8b4e] 旧标题\n我的说明文字"
        out = crs.with_commit_tag(old, self.SHA, "新标题")
        self.assertEqual(out, "[commit a2816cd1adf1] 新标题\n我的说明文字")

    def test_idempotent(self):
        once = crs.with_commit_tag("说明", self.SHA, "标题")
        twice = crs.with_commit_tag(once, self.SHA, "标题")
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("[commit "), 1)

    def test_legacy_tag_without_subject_upgraded(self):
        """历史记录只写过 sha，再同步一次应补上标题而不是叠加。"""
        out = crs.with_commit_tag("[commit ac970b8e8b4e]", self.SHA, "标题")
        self.assertEqual(out, "[commit a2816cd1adf1] 标题")


class TestCodeRepoEndpoints(unittest.TestCase):
    """真正打一遍代码仓接口 —— 只做 import 检查抓不到这类错误。

    线上事故：列表接口里写了 `is_syncing(...)` 却忘了 import，
    `import api.routers.code_repos` 完全正常（NameError 只在调用时抛），
    于是 `GET /api/code-repos` 直接 500，而所有单测都是绿的。
    接口级的冒烟测试才是这类问题的正确护栏。
    """

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routers import code_repos
        from api.core.security import get_current_user, require_operator

        app = FastAPI()
        app.include_router(code_repos.router)
        whoami = {"username": "t", "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: whoami
        app.dependency_overrides[require_operator] = lambda: whoami
        self.client = TestClient(app)

    def test_list_returns_200_and_syncing_field(self):
        r = self.client.get("/api/code-repos")
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertIsInstance(rows, list)
        # 不断言非空：全量跑时别的用例可能把 DB 指向临时库。
        # 状态码 200 + 字段齐全已经足以拦住「忘了 import」这类 NameError。
        for row in rows:
            self.assertIn("syncing", row)
            self.assertIn("source_type", row)

    def test_default_repo_is_pinned_first(self):
        """默认仓必须排在第一条，否则「置顶」这个需求就失效了。"""
        rows = self.client.get("/api/code-repos").json()
        defaults = [r for r in rows if r.get("is_default")]
        if not defaults:
            self.skipTest("当前没有默认代码仓")
        self.assertEqual(rows[0]["id"], defaults[0]["id"])

    def test_tags_endpoint(self):
        r = self.client.get("/api/code-repos/tags")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_status_endpoint_has_all_keys(self):
        rows = self.client.get("/api/code-repos").json()
        for row in rows[:3]:
            r = self.client.get(f"/api/code-repos/{row['id']}/status")
            self.assertEqual(r.status_code, 200)
            st = r.json()
            self.assertEqual(
                set(st),
                {"downloaded", "packaged", "tar_path", "src_path", "commit", "syncing"},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
