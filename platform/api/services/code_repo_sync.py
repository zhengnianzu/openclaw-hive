# -*- coding: utf-8 -*-
"""代码仓的路径解析与拉取打包（OBS / Git 两种来源的唯一实现）。

抽出本模块之前，「下载到 code/src/{name}/{version} 再 tar 成 openclaw-task.tar」
这件事有两份几乎相同的实现（code_repos.py 的 download_code_repo 与 instances.py
准备阶段的代码仓分支），路径 join 则散落 6 处。加 Git 来源要给 git 仓多插一层
commit 目录，散着改必然漏，所以先在这里收口：

    repo_dirs / repo_tar_path  —— 路径怎么拼，只有这里说得上话
    ensure_repo_ready          —— 拉取 + 打包，router 和 instances 共用
    resolve_remote_head        —— 只读探测远端 HEAD，给「有新版本」提示用

目录规则（兼容存量是硬要求）：
    obs: code/{src,tar}/{name}/{version}/              —— 与改动前逐字一致
    git: code/{src,tar}/{name}/{version}/{sha[:12]}/   —— 多一层短 sha

git 仓按 commit 分目录带来的性质：实例 config.yaml 里的 tar 路径一旦写定，
就钉在当时那个 commit 上。之后仓库同步到新 commit 只会新增目录，不会改写
已有实例实际跑的代码——这正是按 commit 隔离想换来的可复现性。
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime

from fastapi import HTTPException

from ..core.config import settings
from ..core.database import get_connection
from .secrets import mask_secret

TAR_NAME = "openclaw-task.tar"

# 与改动前的超时口径保持一致：OBS/clone 600s，打包 120s。
# PREPARE_TIMEOUT_SECONDS(1800) 的注释正是按「两次 600s 下载 + 120s 打包」算的，
# 改这里要连带复核那个阈值。
FETCH_TIMEOUT = 600
TAR_TIMEOUT = 120

SOURCE_OBS = "obs"
SOURCE_GIT = "git"


def _code_dir() -> str:
    return os.path.join(settings.HIVE_ROOT, "platform", "code")


def source_type(repo: dict) -> str:
    """存量行没有这列 / 值为空时一律按 obs 处理。"""
    return (repo.get("source_type") or SOURCE_OBS).strip().lower()


def is_git(repo: dict) -> bool:
    return source_type(repo) == SOURCE_GIT


def _obs_dir_name(obs_path: str) -> str:
    return obs_path.rstrip("/").split("/")[-1]


def content_dir_name(repo: dict) -> str:
    """tar 内唯一的顶层目录名（也是 src_dir 下那层实际内容目录）。

    obs 仓取 obs_path 末段（obsutil cp -r 会在目标下重建该目录）；
    git 仓取 subdir 末段，subdir 为空时退回仓库名。沙箱侧按
    `Path(main_code_tar).stem` 之外的约定找这个目录，所以 git 仓也必须
    打出一个带名字的顶层目录，不能把文件平铺进 tar 根。
    """
    if is_git(repo):
        subdir = (repo.get("subdir") or "").strip().strip("/")
        if subdir:
            return subdir.split("/")[-1]
        url = (repo.get("repo_url") or "").strip().rstrip("/")
        name = url.split("/")[-1] if url else ""
        if name.endswith(".git"):
            name = name[:-4]
        return name or repo["name"]
    return _obs_dir_name(repo.get("obs_path") or "")


def repo_dirs(repo: dict, commit: str | None = None) -> tuple[str, str]:
    """(src_dir, tar_dir)。git 仓多一层 commit 短 sha。

    commit 显式传入时优先于 repo['resolved_commit']——同步流程要先按新拉到的
    sha 算目录，而此时库里存的还是旧值。
    """
    parts = [repo["name"], repo["version"]]
    if is_git(repo):
        sha = (commit if commit is not None else repo.get("resolved_commit") or "").strip()
        if not sha:
            raise HTTPException(
                status_code=409,
                detail=f"代码仓 {repo['name']}/{repo['version']} 尚未同步，无法确定 commit 目录，请先执行同步",
            )
        parts.append(sha[:12])
    base = _code_dir()
    return os.path.join(base, "src", *parts), os.path.join(base, "tar", *parts)


def repo_tar_path(repo: dict, commit: str | None = None) -> str:
    return os.path.join(repo_dirs(repo, commit)[1], TAR_NAME)


def content_path(repo: dict, commit: str | None = None) -> str:
    """源码实际落地的目录（src_dir 下那层内容目录）。"""
    return os.path.join(repo_dirs(repo, commit)[0], content_dir_name(repo))


# ---------------------------------------------------------------------------
# 子进程
# ---------------------------------------------------------------------------

async def _run(cmd: list[str], *, cwd: str | None = None, timeout: int = FETCH_TIMEOUT,
               what: str = "命令") -> tuple[int, str, str]:
    """跑子进程并返回 (rc, stdout, stderr)。超时直接抛 HTTPException。

    与 instances.py 的 _async_subprocess_run 同款；本模块自带一份是为了不从
    service 反向 import router（那会把两个模块焊在一起）。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=500, detail=f"{what}超时({timeout}s)")
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _safe(text: str, limit: int = 500) -> str:
    """报错文本脱敏：把 token 原文换成掩码再截断。

    clone 失败时 git 会把完整远端 URL 回显到 stderr，而我们往 URL 里注入过
    token。不先替换就直接塞进 HTTPException.detail，token 会一路出现在
    接口响应和前端提示里。
    """
    token = (settings.GITHUB_TOKEN or "").strip()
    if token:
        text = text.replace(token, mask_secret(token))
    return text[:limit]


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git_url(repo: dict) -> str:
    """带凭据的 clone URL。只在内存里用，绝不入库 / 不落日志。"""
    url = (repo.get("repo_url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Git 代码仓缺少仓库地址")
    token = (settings.GITHUB_TOKEN or "").strip()
    if token and url.startswith("https://"):
        return "https://x-access-token:" + token + "@" + url[len("https://"):]
    return url


def _branch(repo: dict) -> str:
    return (repo.get("branch") or "").strip() or "main"


async def resolve_remote_head(repo: dict) -> str:
    """远端分支当前 sha（只读，不落盘）。"""
    branch = _branch(repo)
    rc, out, err = await _run(
        ["git", "ls-remote", _git_url(repo), f"refs/heads/{branch}"],
        timeout=60,
        what="探测远端分支",
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"探测远端分支失败: {_safe(err)}")
    line = out.strip().split("\n")[0].strip() if out.strip() else ""
    sha = line.split("\t")[0].strip() if line else ""
    if not sha:
        raise HTTPException(
            status_code=400,
            detail=f"远端不存在分支 {branch}（仓库 {repo.get('repo_url')}）",
        )
    return sha


async def _git_fetch(repo: dict, commit: str) -> str:
    """把 commit 对应的源码落到 content_path 并返回该路径。

    每个 commit 独占目录，所以这里永远是「干净目录里新 clone」，不需要 fetch
    合并既有 checkout。clone 到临时目录再整体 rename：中途失败不会留下一个
    半拉子的 content_path 被后续调用误判成「已下载」。
    """
    src_dir, _ = repo_dirs(repo, commit)
    dest = os.path.join(src_dir, content_dir_name(repo))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(src_dir, exist_ok=True)

    tmp = os.path.join(src_dir, f".clone-{commit[:12]}.tmp")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)

    branch = _branch(repo)
    # --depth 1 只能配 --branch 取分支头。如果远端在我们探测 sha 之后又推了新
    # commit，这里 clone 到的会是更新的那个；下面用实际 sha 校正，避免目录名
    # 与内容对不上。
    rc, _, err = await _run(
        ["git", "clone", "--depth", "1", "--single-branch", "--branch", branch,
         _git_url(repo), tmp],
        timeout=FETCH_TIMEOUT,
        what="代码仓克隆",
    )
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"代码仓克隆失败: {_safe(err)}")

    rc, out, err = await _run(["git", "rev-parse", "HEAD"], cwd=tmp, timeout=30, what="读取 commit")
    actual = out.strip() if rc == 0 else ""
    if rc != 0 or not actual:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"读取 commit 失败: {_safe(err)}")

    if actual != commit:
        # 远端在探测与 clone 之间前进了。按实际 sha 重新定位目标目录，
        # 否则 sha 目录里装的是另一个 commit 的代码。
        src_dir, _ = repo_dirs(repo, actual)
        dest = os.path.join(src_dir, content_dir_name(repo))
        os.makedirs(src_dir, exist_ok=True)
        if os.path.isdir(dest):
            shutil.rmtree(dest)

    # .git 不进 tar：沙箱里用不到，白占体积，且含远端 URL。
    shutil.rmtree(os.path.join(tmp, ".git"), ignore_errors=True)

    subdir = (repo.get("subdir") or "").strip().strip("/")
    if subdir:
        inner = os.path.join(tmp, subdir)
        if not os.path.isdir(inner):
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"仓库内不存在子目录 {subdir}")
        os.replace(inner, dest)
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        os.replace(tmp, dest)

    return actual


# ---------------------------------------------------------------------------
# OBS
# ---------------------------------------------------------------------------

async def _obs_fetch(repo: dict) -> None:
    src_dir, _ = repo_dirs(repo)
    obs_path = (repo.get("obs_path") or "").strip()
    if not obs_path:
        raise HTTPException(status_code=400, detail="OBS 代码仓缺少 OBS 路径")
    os.makedirs(src_dir, exist_ok=True)
    if not obs_path.endswith("/"):
        obs_path += "/"
    rc, _, err = await _run(
        [settings.OBSUTIL_PATH, "cp", obs_path, src_dir, "-r", "-f"],
        timeout=FETCH_TIMEOUT,
        what="OBS下载",
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"OBS下载失败: {_safe(err)}")


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------

async def _pack(repo: dict, commit: str | None = None) -> str:
    src_dir, tar_dir = repo_dirs(repo, commit)
    dir_name = content_dir_name(repo)
    tar_path = os.path.join(tar_dir, TAR_NAME)
    os.makedirs(tar_dir, exist_ok=True)
    rc, _, err = await _run(
        ["tar", "cf", tar_path, dir_name],
        cwd=src_dir,
        timeout=TAR_TIMEOUT,
        what="打包",
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"打包失败: {_safe(err)}")
    return tar_path


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _record_sync(repo_id: int, commit: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE code_repos SET resolved_commit = ?, synced_at = ? WHERE id = ?",
            (commit, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), repo_id),
        )


async def ensure_repo_ready(repo: dict, *, force: bool = False) -> dict:
    """保证该代码仓的 tar 就位，返回 {tar_path, src_path, commit, already_exists, updated}。

    惰性语义与改动前一致：tar 已存在且 force=False 就直接返回，不重复拉取。
    git 仓在 force 时会先探测远端 HEAD；若与本地 resolved_commit 相同且 tar 已在，
    同样跳过——「同步」按钮点第二次不该白跑一次 clone。

    repo 是 code_repos 的一行（dict）。调用方不必关心来源类型。
    """
    if not is_git(repo):
        src_dir, _ = repo_dirs(repo)
        tar_path = os.path.join(repo_dirs(repo)[1], TAR_NAME)
        if os.path.isfile(tar_path) and not force:
            return {
                "tar_path": tar_path,
                "src_path": content_path(repo),
                "commit": "",
                "already_exists": True,
                "updated": False,
            }
        if force or not (os.path.isdir(src_dir) and os.listdir(src_dir)):
            await _obs_fetch(repo)
        tar_path = await _pack(repo)
        return {
            "tar_path": tar_path,
            "src_path": content_path(repo),
            "commit": "",
            "already_exists": False,
            "updated": True,
        }

    local = (repo.get("resolved_commit") or "").strip()

    # 非强制且本地已有打包产物：直接用，不碰网络。实例准备阶段走的就是这条，
    # 不然每次创建实例都要等一次 ls-remote。
    if local and not force:
        tar_path = repo_tar_path(repo, local)
        if os.path.isfile(tar_path):
            return {
                "tar_path": tar_path,
                "src_path": content_path(repo, local),
                "commit": local,
                "already_exists": True,
                "updated": False,
            }

    target = await resolve_remote_head(repo)

    # 远端没动且产物还在 —— 包括 force 的情形：重复点「同步」不该重拉。
    if target == local and os.path.isfile(repo_tar_path(repo, target)):
        return {
            "tar_path": repo_tar_path(repo, target),
            "src_path": content_path(repo, target),
            "commit": target,
            "already_exists": True,
            "updated": False,
        }

    actual = await _git_fetch(repo, target)
    tar_path = await _pack(repo, actual)
    _record_sync(repo["id"], actual)
    return {
        "tar_path": tar_path,
        "src_path": content_path(repo, actual),
        "commit": actual,
        "already_exists": False,
        "updated": actual != local,
    }


def repo_status(repo: dict) -> dict:
    """只读磁盘，判断该仓是否已下载 / 已打包。"""
    commit = (repo.get("resolved_commit") or "").strip()
    if is_git(repo) and not commit:
        return {
            "downloaded": False,
            "packaged": False,
            "tar_path": None,
            "src_path": None,
            "commit": "",
        }
    tar_path = repo_tar_path(repo, commit or None)
    src_path = content_path(repo, commit or None)
    tar_exists = os.path.isfile(tar_path)
    src_exists = os.path.isdir(src_path)
    return {
        "downloaded": src_exists,
        "packaged": tar_exists,
        "tar_path": tar_path if tar_exists else None,
        "src_path": src_path if src_exists else None,
        "commit": commit,
    }
