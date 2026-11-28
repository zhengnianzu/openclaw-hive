# -*- coding: utf-8 -*-
"""代码仓的路径解析与拉取打包（OBS / Git 两种来源的唯一实现）。

抽出本模块之前，「下载到 code/src/{name}/{version} 再 tar 成 openclaw-task.tar」
这件事有两份几乎相同的实现（code_repos.py 的 download_code_repo 与 instances.py
准备阶段的代码仓分支），路径 join 则散落 6 处。加 Git 来源要给 git 仓多插一层
commit 目录，散着改必然漏，所以先在这里收口：

    repo_dirs / repo_tar_path  —— 路径怎么拼，只有这里说得上话
    ensure_repo_ready          —— 拉取 + 打包，router 和 instances 共用
    resolve_remote_head        —— 只读探测远端 HEAD，给「有新版本」提示用

目录规则（两种来源一致，也与 OBS 改动前逐字一致）：
    code/{src,tar}/{name}/{version}/

git 仓的更新方式是**原地 pull**：工作副本长期保留在 src 目录里，同步时
fetch + reset 到远端分支最新，然后重新打包 tar。不按 commit 分目录 ——
历史 sha 的目录没有保留价值，只会让磁盘无限增长。

由此带来的取舍：实例 config.yaml 里的 main_code_tar 路径是固定的，仓库同步后
**新启动的实例会拿到新代码**，而已经在跑的实例因为 tar 已上传进沙箱、不再回读，
不受影响。重复启动同一实例时则会读到新 tar。
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import re
import shutil
import time
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

# Git 网络的健壮性参数。
#
# 实测（2026-11-28）：同一分钟里 `curl` 请求 git 的 /info/refs 只要 0.5s，
# 而 `git ls-remote` 能挂满 60s、`git clone` 卡在 124K 十分钟不动 —— 链路到
# GitHub 有间歇性卡顿。git 默认对「连接建立后不再前进」的传输不设上限，
# 会一路等到我们的 FETCH_TIMEOUT(600s)，于是用户看到的就是「同步没反应」。
#
# 两个措施：
#   1) lowSpeedLimit/Time：30 秒内平均速度低于 1KB/s 就主动放弃这条连接，
#      把一个卡死的十分钟缩短到半分钟；
#   2) 失败/超时后重开一条新连接重试 —— 卡顿多是某条路径的问题，重连经常就通了。
# http.version=HTTP/1.1：部分出口链路上 HTTP/2 的多路复用会卡住（GitHub 官方
# 也建议在网络不稳时降级），降到 1.1 能避开这一类卡顿。这不是根治 —— 根治在链路，
# 但它把「偶发卡死」的频率压低了一个已知来源。
GIT_NET_OPTS = (
    "-c", "http.version=HTTP/1.1",
    "-c", "http.lowSpeedLimit=1000",
    "-c", "http.lowSpeedTime=30",
)
GIT_ATTEMPTS = 3


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


def repo_dirs(repo: dict) -> tuple[str, str]:
    """(src_dir, tar_dir)。两种来源同一套路径，不含 commit 层。

    路径不再依赖 resolved_commit：git 仓原地更新，所以「还没同步过」也能
    直接算出 tar 路径，实例创建时不需要先拉一次。
    """
    parts = [repo["name"], repo["version"]]
    base = _code_dir()
    return os.path.join(base, "src", *parts), os.path.join(base, "tar", *parts)


def repo_tar_path(repo: dict) -> str:
    return os.path.join(repo_dirs(repo)[1], TAR_NAME)


def content_path(repo: dict) -> str:
    """源码实际落地的目录（src_dir 下那层内容目录）。

    git 仓这个目录本身就是工作副本，内含 .git（打包时排除）。
    """
    return os.path.join(repo_dirs(repo)[0], content_dir_name(repo))


# ---------------------------------------------------------------------------
# 同步互斥锁
#
# 为什么必须有：一次同步会 clone 到 `.clone-{sha}.tmp` 再改名。两个同步并发跑
# 时目标 tmp 路径完全相同（同一 commit），后进来的那个会把先来的半成品目录
# rmtree 掉，git 随即失败 —— 现象就是「点了同步没反应 / 卡住」，而且每多点一次
# 就多一个并发的破坏者。
#
# 用 flock 而不是 asyncio.Lock：uvicorn 是 --workers 4，多个 worker 是**不同进程**，
# 进程内的锁互相看不见。这也与本仓库 db_backup.py / worker_monitor.py 的既有做法一致。
# ---------------------------------------------------------------------------

# 等锁上限：一次同步最坏 `ls-remote 60s + clone 600s + tar 120s`，留点余量。
LOCK_WAIT_SECONDS = 900
LOCK_POLL_SECONDS = 0.5


def _lock_path(repo: dict) -> str:
    """锁文件路径。name/version 已被用作目录名，直接拿来拼是安全的。"""
    return os.path.join(_code_dir(), ".sync-locks", f"{repo['name']}__{repo['version']}.lock")


def _acquire_repo_lock(repo: dict, *, blocking: bool) -> int:
    """拿该代码仓的排他锁，返回持锁的 fd（调用方负责释放）。

    blocking=False 时若已被占用立即抛 409：给「同步」按钮快速反馈，
    而不是让请求挂在那里让用户以为卡死。
    """
    path = _lock_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                os.close(fd)
                raise
            if not blocking:
                os.close(fd)
                raise HTTPException(
                    status_code=409,
                    detail=f"代码仓 {repo['name']}/{repo['version']} 正在同步中，请稍后再试",
                )
            if time.monotonic() >= deadline:
                os.close(fd)
                raise HTTPException(
                    status_code=500,
                    detail=f"等待代码仓 {repo['name']}/{repo['version']} 的同步锁超时({LOCK_WAIT_SECONDS}s)",
                )
            time.sleep(LOCK_POLL_SECONDS)


def _release_repo_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_syncing(repo: dict) -> bool:
    """该代码仓此刻是否有同步在跑（只读探测，不占锁）。

    直接复用同步锁本身作为状态来源：锁被别的进程拿着 = 正在同步。
    这样不需要额外维护一张「同步中」的表，也不会出现锁与状态不一致
    （进程崩了锁自动释放，状态也跟着消失）。

    同进程内若自己正持锁再调用会误报为 True，故只给「别的请求」用，
    不要在同一次同步的调用栈里调它。
    """
    path = _lock_path(repo)
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


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


async def _git_net(args: list[str], *, cwd: str | None = None, timeout: int = FETCH_TIMEOUT,
                   what: str = "Git 操作") -> tuple[int, str, str]:
    """跑一条**走网络**的 git 命令，带低速熔断与重试。

    只用于需要联网的子命令（ls-remote / clone）。rev-parse 这类纯本地操作
    仍走 _run，不该因为网络参数多一层重试。

    重试对两种失败都生效：非零退出，以及 _run 抛出的超时。前者常见于
    「卡顿超时后 git 自己报 early EOF」，后者是链路彻底不动。
    """
    last = ""
    for attempt in range(1, GIT_ATTEMPTS + 1):
        try:
            rc, out, err = await _run(
                ["git", *GIT_NET_OPTS, *args], cwd=cwd, timeout=timeout, what=what,
            )
        except HTTPException as e:
            last = str(e.detail)
        else:
            if rc == 0:
                return rc, out, err
            last = _safe(err)
        if attempt < GIT_ATTEMPTS:
            # 递增退避：卡顿通常是短暂的路由抖动，隔一下再试换到别的路径。
            await asyncio.sleep(1.5 * attempt)
    raise HTTPException(
        status_code=500,
        detail=f"{what}失败（已重试 {GIT_ATTEMPTS} 次）: {last}",
    )


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
    rc, out, err = await _git_net(
        ["ls-remote", _git_url(repo), f"refs/heads/{branch}"],
        timeout=60,
        what="探测远端分支",
    )
    line = out.strip().split("\n")[0].strip() if out.strip() else ""
    sha = line.split("\t")[0].strip() if line else ""
    if not sha:
        raise HTTPException(
            status_code=400,
            detail=f"远端不存在分支 {branch}（仓库 {repo.get('repo_url')}）",
        )
    return sha


def _cleanup_failed_fetch(src_dir: str) -> None:
    """拉取失败后清掉空壳目录。

    失败会留下空目录，肉眼看起来像「同步过但内容没了」，排查时极易误判。
    只删空目录：里面已有上一次成功的产物时不能动。
    """
    try:
        if os.path.isdir(src_dir) and not os.listdir(src_dir):
            os.rmdir(src_dir)
    except OSError:
        pass


def _work_root(repo: dict) -> str:
    """git 工作副本的根目录。整个仓库一份，长期保留，同步时原地更新。

    放在 src_dir 下的 `.repo`：与 tar 里那层内容目录（content_dir_name）分开，
    subdir 模式下也不会与它重名。
    """
    return os.path.join(repo_dirs(repo)[0], ".repo")


async def _git_pull(repo: dict) -> tuple[str, str]:
    """把工作副本更新到远端分支最新，返回 (实际 sha, commit 标题)。

    首次 clone，之后 fetch + reset --hard —— 即「原地 pull」。不直接调
    `git pull` 的原因：浅克隆上 pull 会带 rebase 相关的告警，且工作区任何脏
    改动都会导致它失败；reset --hard 到 FETCH_HEAD 的结果确定，正是我们要的
    「无条件跟随远端分支」。

    调用方必须已持有该仓库的锁。
    """
    work = _work_root(repo)
    src_dir = repo_dirs(repo)[0]
    os.makedirs(src_dir, exist_ok=True)
    branch = _branch(repo)
    url = _git_url(repo)

    if not os.path.isdir(os.path.join(work, ".git")):
        # 首次：clone 到临时目录再整体 rename，失败不会留下半成品被误判成可用。
        tmp = work + ".tmp"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        try:
            await _git_net(
                ["clone", "--depth", "1", "--single-branch", "--branch", branch, url, tmp],
                timeout=FETCH_TIMEOUT, what="代码仓克隆",
            )
        except HTTPException:
            shutil.rmtree(tmp, ignore_errors=True)
            _cleanup_failed_fetch(src_dir)
            raise
        if os.path.isdir(work):
            shutil.rmtree(work)
        os.replace(tmp, work)
    else:
        # 之后：只拉增量。--depth 1 让浅克隆保持浅，不会越同步越厚。
        await _git_net(
            ["-C", work, "fetch", "--depth", "1", "origin", branch],
            timeout=FETCH_TIMEOUT, what="代码仓拉取",
        )
        rc, _, err = await _run(
            ["git", "-C", work, "reset", "--hard", "FETCH_HEAD"],
            timeout=60, what="更新工作副本",
        )
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"更新工作副本失败: {_safe(err)}")

    rc, out, err = await _run(
        ["git", "-C", work, "rev-parse", "HEAD"], timeout=30, what="读取 commit",
    )
    sha = out.strip() if rc == 0 else ""
    if not sha:
        raise HTTPException(status_code=500, detail=f"读取 commit 失败: {_safe(err)}")

    # 标题取不到不算失败：描述里少一行说明，比让整次同步失败要好得多。
    rc, out, _ = await _run(
        ["git", "-C", work, "log", "-1", "--format=%s"], timeout=30, what="读取 commit 标题",
    )
    subject = out.strip() if rc == 0 else ""
    return sha, subject


async def _pack_git(repo: dict) -> str:
    """用 git archive 直接产出 tar。

    用 archive 而不是 tar 打包工作副本：
      - .git 天然不进包，不必拼 --exclude；
      - 只含受版本控制的文件，工作区里误留的 __pycache__/日志不会混进沙箱；
      - 顶层目录名由 --prefix 指定，与沙箱侧 `Path(main_code_tar).stem`
        推出的工程目录（code_stem）一致。
    subdir 非空时用 `HEAD:<subdir>` 只导出该子树，前缀仍用叶子目录名。
    """
    work = _work_root(repo)
    tar_dir = repo_dirs(repo)[1]
    tar_path = repo_tar_path(repo)
    os.makedirs(tar_dir, exist_ok=True)
    subdir = (repo.get("subdir") or "").strip().strip("/")
    source = f"HEAD:{subdir}" if subdir else "HEAD"
    rc, _, err = await _run(
        ["git", "-C", work, "archive", "--format=tar",
         f"--prefix={content_dir_name(repo)}/", "--output", tar_path, source],
        timeout=TAR_TIMEOUT,
        what="打包",
    )
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"打包失败: {_safe(err)}")
    return tar_path


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

async def _pack_obs(repo: dict) -> str:
    """OBS 来源打包：tar 掉 obsutil 拉到的那层目录。"""
    src_dir, tar_dir = repo_dirs(repo)
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

# 描述首行是自动维护的 commit 摘要，形如：
#   [commit a2816cd1adf1] 校核：精简网关日志与任务状态处理
# 换行之后的才是用户自己写的内容，同步时原样保留 —— 描述是人的字段，
# 只该被「当前 commit 是哪次」这一行覆盖，不能把说明冲掉。
_COMMIT_TAG_RE = re.compile(r"^\[commit [0-9a-f]{7,40}\][^\n]*\n?")


def with_commit_tag(desc: str, commit: str, subject: str = "") -> str:
    """把描述首行的 commit 摘要换成这次的 sha + 标题，保留换行后的用户内容。

    幂等：连着同步两次不会堆出两行标记。
    """
    rest = _COMMIT_TAG_RE.sub("", desc or "").lstrip("\n").strip()
    head = f"[commit {commit[:12]}]"
    if subject:
        head = f"{head} {subject}"
    return f"{head}\n{rest}" if rest else head


def _record_sync(repo_id: int, commit: str, subject: str = "") -> None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT description FROM code_repos WHERE id = ?", (repo_id,)
        ).fetchone()
        desc = with_commit_tag(row["description"] if row else "", commit, subject)
        conn.execute(
            "UPDATE code_repos SET resolved_commit = ?, synced_at = ?, description = ? WHERE id = ?",
            (commit, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), desc, repo_id),
        )


async def ensure_repo_ready(repo: dict, *, force: bool = False, wait_lock: bool = True) -> dict:
    """保证该代码仓的 tar 就位，返回 {tar_path, src_path, commit, already_exists, updated}。

    惰性语义与改动前一致：tar 已存在且 force=False 就直接返回，不重复拉取。
    git 仓在 force 时会先探测远端 HEAD；若与本地 resolved_commit 相同且 tar 已在，
    同样跳过——「同步」按钮点第二次不该白跑一次 clone。

    整段工作在**该仓库的排他锁**内执行（见 _acquire_repo_lock）：并发同步会互相
    清掉对方的临时 clone 目录，是之前「点了没反应」的直接原因。
    wait_lock=False 用于用户手动的「同步」按钮：锁被占用时立即 409，
    而不是陪着排队等上十分钟。实例创建/准备阶段用默认的 wait_lock=True，
    让它们排在正在进行的同步后面，不该因为别人正在同步就创建失败。

    repo 是 code_repos 的一行（dict）。调用方不必关心来源类型。
    """
    fd = _acquire_repo_lock(repo, blocking=wait_lock)
    try:
        return await _ensure_repo_ready_locked(repo, force=force)
    finally:
        _release_repo_lock(fd)


async def _ensure_repo_ready_locked(repo: dict, *, force: bool) -> dict:
    """ensure_repo_ready 的实际逻辑。调用方必须已持有该仓库的锁。"""
    tar_path = repo_tar_path(repo)

    if not is_git(repo):
        src_dir, _ = repo_dirs(repo)
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
        tar_path = await _pack_obs(repo)
        return {
            "tar_path": tar_path,
            "src_path": content_path(repo),
            "commit": "",
            "already_exists": False,
            "updated": True,
        }

    local = (repo.get("resolved_commit") or "").strip()
    work = _work_root(repo)

    # 非强制且本地已有工作副本与产物：直接用，不碰网络。实例准备阶段走这条，
    # 否则每次创建实例都要等一次 fetch。
    if local and not force and os.path.isfile(tar_path) and os.path.isdir(work):
        return {
            "tar_path": tar_path,
            "src_path": work,
            "commit": local,
            "already_exists": True,
            "updated": False,
        }

    # 原地更新到远端分支最新。不再先 ls-remote 比对：fetch 本身就能拿到最新 sha，
    # 多一次往返只会在慢链路上放大等待。
    actual, subject = await _git_pull(repo)

    if actual == local and os.path.isfile(tar_path):
        # 即使没有新 commit 也回写一次：描述首行的摘要可能是旧格式（只有 sha）
        # 或压根没有（历史记录），synced_at 也该反映这次同步。
        # 否则「点同步却什么都没变」会让人以为操作没生效。
        _record_sync(repo["id"], actual, subject)
        return {
            "tar_path": tar_path,
            "src_path": work,
            "commit": actual,
            "already_exists": True,
            "updated": False,
        }

    tar_path = await _pack_git(repo)
    _record_sync(repo["id"], actual, subject)
    return {
        "tar_path": tar_path,
        "src_path": work,
        "commit": actual,
        "already_exists": False,
        "updated": actual != local,
    }


def repo_status(repo: dict) -> dict:
    """只读磁盘，判断该仓是否已下载 / 已打包。

    git 仓的「已下载」指工作副本存在（src_dir/.repo 里有 .git），
    因为原地更新的产物就是那个工作副本，不再有按 commit 分的内容目录。
    """
    commit = (repo.get("resolved_commit") or "").strip()
    tar_path = repo_tar_path(repo)
    tar_exists = os.path.isfile(tar_path)

    if is_git(repo):
        work = _work_root(repo)
        src_exists = os.path.isdir(os.path.join(work, ".git"))
        src_path = work
    else:
        src_path = content_path(repo)
        src_exists = os.path.isdir(src_path)

    return {
        "downloaded": src_exists,
        "packaged": tar_exists,
        "tar_path": tar_path if tar_exists else None,
        "src_path": src_path if src_exists else None,
        "commit": commit,
        # 只有 git 仓会长时间同步；OBS 的下载/打包是创建实例时顺手做的，
        # 不该在代码仓列表上闪「同步中」。
        "syncing": is_git(repo) and is_syncing(repo),
    }
