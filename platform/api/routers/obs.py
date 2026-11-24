import asyncio
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import settings
from ..core.security import get_current_user
from ..models.instance import ObsItem, ObsListResponse

router = APIRouter(prefix="/api/obs", tags=["obs"])


def _bucket_root() -> str:
    """归一化后的桶根路径，始终以 / 结尾。"""
    return settings.OBS_BUCKET.rstrip("/") + "/"


@router.get("/config")
async def get_obs_config(user: dict = Depends(get_current_user)):
    """暴露 OBS 桶配置给前端，避免前端硬编码桶名。"""
    return {"bucket": _bucket_root()}


async def run_obsutil(args: list[str], timeout: int = 30) -> str:
    cmd = [settings.OBSUTIL_PATH] + args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="obsutil 命令超时")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"obsutil 错误: {stderr.decode()}")
    return stdout.decode()


def _parse_obsutil_ls(output: str, base_path: str) -> list[ObsItem]:
    """
    解析 obsutil ls 的输出。两种形态都要吃：

    1) 目录（-d）：一行一个
         Folder list:
         obs://b/path/dir1/
         obs://b/path/dir2/

    2) 对象：**两行一条**，key 单独一行，size/时间在下一行
         Object list:
         key                        LastModified        Size    ...
         obs://b/path/file.json
                                    2026-11-16T09:56:51Z   781B   standard  "etag"

    之前只按「一行一条」解析，导致：
      - 对象的 size 全为空（size 在下一行，被当成噪声丢掉了）
      - 不带 -d 时同一批目录会出现两次（调用方分别拉 -d 和不带 -d，两边都含目录）
    """
    items = []
    seen = set()
    lines = output.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line.startswith("obs://"):
            continue

        full_path = line.split()[0] if line.split() else line
        name = full_path.replace(base_path, "").strip("/")
        if not name:
            continue          # 被列目录本身（base_path 那一行）

        is_dir = raw.rstrip().endswith("/")
        if is_dir:
            name = name.rstrip("/")

        # 去重：目录可能同时出现在 Folder list 和上一级的前缀里
        if full_path in seen:
            continue
        seen.add(full_path)

        size = None
        if not is_dir:
            # 同行带 size 的旧版本 / 下一行为 size 的新版本，都试一遍
            size_match = re.search(r"\s(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB))(?!\w)", line)
            if not size_match and i + 1 < len(lines):
                nxt = lines[i + 1]
                if not nxt.strip().startswith("obs://"):
                    size_match = re.search(r"\s(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB))(?!\w)", nxt)
            if size_match:
                size = size_match.group(1).replace(" ", "")

        items.append(ObsItem(
            name=name, path=full_path, is_dir=is_dir,
            size=size, last_modified=None,
        ))
    return items


@router.get("/list", response_model=ObsListResponse)
async def list_obs_dir(
    path: Optional[str] = Query(default=None, description="OBS路径，缺省为桶根"),
    show_files: bool = Query(default=False, description="是否同时显示文件"),
    user: dict = Depends(get_current_user),
):
    if not path:
        path = _bucket_root()
    if not path.endswith("/"):
        path += "/"

    # 不带 -d 的 ls 已经同时返回目录和文件，所以 show_files 时**只发一次请求**。
    # 早先是「-d 拉目录 + 不带 -d 拉文件」两次并发，目录会在两边各出现一次，
    # 前端拼出来的列表里每个子目录都重复。
    args = ["ls", path, "-limit=200"]
    if not show_files:
        args.append("-d")
    output = await run_obsutil(args)
    return ObsListResponse(path=path, items=_parse_obsutil_ls(output, path))


@router.get("/files", response_model=ObsListResponse)
async def list_obs_files(
    path: str = Query(description="OBS路径"),
    user: dict = Depends(get_current_user),
):
    """列出目录下的文件（不带 -d）"""
    if not path.endswith("/"):
        path += "/"
    output = await run_obsutil(["ls", path, "-limit=500"])
    items = _parse_obsutil_ls(output, path)
    return ObsListResponse(path=path, items=items)


@router.get("/search")
async def search_obs(
    path: str = Query(description="OBS搜索路径"),
    keyword: str = Query(description="搜索关键词"),
    user: dict = Depends(get_current_user),
):
    if not path.endswith("/"):
        path += "/"
    output = await run_obsutil(["ls", path, "-d", "-limit=500"], timeout=60)
    items = _parse_obsutil_ls(output, path)
    filtered = [item for item in items if keyword.lower() in item.name.lower()]
    return ObsListResponse(path=path, items=filtered)


# 在线预览单个对象的硬上限。obsutil cat 是「整个对象读进内存再打印」，
# 没有分页参数，所以必须在调用前挡住大文件：一个几百 MB 的 jsonl 就能把
# API 进程打爆（4 个 worker 共享同一台机器）。
CAT_MAX_BYTES = 2 * 1024 * 1024


def _parse_size(text: str) -> int:
    """把 obsutil 的 '1.2KB' / '894B' / '3.5MB' 归一成字节数，解析失败返回 -1。"""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)\s*$", text or "", re.I)
    if not m:
        return -1
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2,
            "GB": 1024 ** 3, "TB": 1024 ** 4}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


async def _object_size(cloud_url: str) -> int:
    """取对象字节数；拿不到时返回 -1（调用方据此决定是否放行）。"""
    try:
        out = await run_obsutil(["stat", cloud_url, "-bf=human-readable"], timeout=30)
    except HTTPException:
        return -1
    # stat 输出形如 "Size: 1.2KB"（大小写与空格随版本略有差异）
    m = re.search(r"Size\s*[:：]\s*([0-9.]+\s*(?:B|KB|MB|GB|TB))", out, re.I)
    return _parse_size(m.group(1)) if m else -1


@router.get("/cat")
async def cat_obs_object(
    path: str = Query(description="OBS 对象完整路径"),
    user: dict = Depends(get_current_user),
):
    """读取单个文本对象的原文，供「数据集目录浏览 → 点开 config.json」使用。

    只读、不落盘。超过 CAT_MAX_BYTES 的拒绝，避免把 API 进程内存打满。
    """
    cloud_url = (path or "").strip()
    if not cloud_url.startswith("obs://"):
        raise HTTPException(status_code=400, detail="需要完整的 obs:// 对象路径")
    if cloud_url.endswith("/"):
        raise HTTPException(status_code=400, detail="这是一个目录，不是文件")

    size = await _object_size(cloud_url)
    if size > CAT_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"文件过大（{size / 1024:.0f} KB > {CAT_MAX_BYTES // 1024} KB），"
                    f"不支持在线查看，请下载后用编辑器打开"),
        )

    try:
        content = await run_obsutil(["cat", cloud_url], timeout=60)
    except HTTPException as e:
        # obsutil 把「对象不存在」也报成非零退出，这里透出更可读的提示
        detail = str(e.detail).replace("obsutil 错误:", "").strip()[:300]
        raise HTTPException(status_code=404, detail=f"读取失败：{detail}") from e

    return {
        "path": cloud_url,
        "size": len(content.encode("utf-8")),
        "content": content,
        "truncated": False,
    }
