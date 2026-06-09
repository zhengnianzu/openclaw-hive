import asyncio
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import settings
from ..core.security import get_current_user
from ..models.instance import ObsItem, ObsListResponse

router = APIRouter(prefix="/api/obs", tags=["obs"])


async def _run_obsutil(args: list[str], timeout: int = 30) -> str:
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
    解析 obsutil ls -d 的输出格式：
      Folder list:
      obs://rl-agentdata/path/dir1/
      obs://rl-agentdata/path/dir2/
      Folder number: 2

    以及 obsutil ls（不带 -d）列出文件的格式：
      obs://rl-agentdata/path/file.json   1.2KB   2026-06-01
    """
    items = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line.startswith("obs://"):
            continue

        full_path = line.split()[0] if line.split() else line
        name = full_path.replace(base_path, "").strip("/")
        if not name:
            continue

        is_dir = full_path.endswith("/")
        if is_dir:
            name = name.rstrip("/")

        size = None
        size_match = re.search(r'(\d+(?:\.\d+)?(?:B|KB|MB|GB|TB))', line)
        if size_match:
            size = size_match.group(1)

        items.append(ObsItem(
            name=name, path=full_path, is_dir=is_dir,
            size=size, last_modified=None,
        ))
    return items


@router.get("/list", response_model=ObsListResponse)
async def list_obs_dir(
    path: str = Query(default="obs://rl-agentdata/", description="OBS路径"),
    show_files: bool = Query(default=False, description="是否同时显示文件"),
    user: dict = Depends(get_current_user),
):
    if not path.endswith("/"):
        path += "/"

    args = ["ls", path, "-limit=200"]
    if not show_files:
        args.append("-d")

    output = await _run_obsutil(args)
    items = _parse_obsutil_ls(output, path)
    return ObsListResponse(path=path, items=items)


@router.get("/files", response_model=ObsListResponse)
async def list_obs_files(
    path: str = Query(description="OBS路径"),
    user: dict = Depends(get_current_user),
):
    """列出目录下的文件（不带 -d）"""
    if not path.endswith("/"):
        path += "/"
    output = await _run_obsutil(["ls", path, "-limit=500"])
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
    output = await _run_obsutil(["ls", path, "-d", "-limit=500"], timeout=60)
    items = _parse_obsutil_ls(output, path)
    filtered = [item for item in items if keyword.lower() in item.name.lower()]
    return ObsListResponse(path=path, items=filtered)
