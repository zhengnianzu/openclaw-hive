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
import sys
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
    def test_commit_layer_uses_short_sha(self):
        repo = _git_repo()
        src, tar = crs.repo_dirs(repo)
        base = os.path.join(settings.HIVE_ROOT, "platform", "code")
        self.assertEqual(src, os.path.join(base, "src", "openclaw-task", "main", SHA[:12]))
        self.assertEqual(tar, os.path.join(base, "tar", "openclaw-task", "main", SHA[:12]))

    def test_explicit_commit_overrides_row(self):
        """同步流程要按新拉到的 sha 算目录，此时库里还是旧值。"""
        other = "f" * 40
        src, _ = crs.repo_dirs(_git_repo(), commit=other)
        self.assertTrue(src.endswith(os.path.join("main", other[:12])))

    def test_distinct_commits_distinct_dirs(self):
        """每个 commit 一个目录 —— 这是实例可复现性的基础。"""
        a = crs.repo_tar_path(_git_repo(), commit="1" * 40)
        b = crs.repo_tar_path(_git_repo(), commit="2" * 40)
        self.assertNotEqual(a, b)

    def test_unsynced_repo_raises_409(self):
        """没有 commit 就无法确定目录，必须显式报错而不是猜一个。"""
        for bad in ("", None):
            with self.assertRaises(HTTPException) as ctx:
                crs.repo_dirs(_git_repo(resolved_commit=bad))
            self.assertEqual(ctx.exception.status_code, 409)

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
    def test_unsynced_git_repo_reports_empty(self):
        """未同步的 git 仓查状态不该抛 409，而是报告「都没有」。"""
        st = crs.repo_status(_git_repo(resolved_commit=""))
        self.assertFalse(st["downloaded"])
        self.assertFalse(st["packaged"])
        self.assertIsNone(st["tar_path"])
        self.assertEqual(st["commit"], "")

    def test_obs_repo_status_keys(self):
        st = crs.repo_status(_obs_repo())
        self.assertEqual(
            set(st),
            {"downloaded", "packaged", "tar_path", "src_path", "commit"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
