import asyncio
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.code_repo import (
    CodeRepoCreate,
    CodeRepoInfo,
    CodeRepoTagCreate,
    CodeRepoTagInfo,
    CodeRepoUpdate,
)

router = APIRouter(prefix="/api/code-repos", tags=["code-repos"])


def _get_code_dir() -> str:
    return os.path.join(settings.HIVE_ROOT, "platform", "code")


def _obs_dir_name(obs_path: str) -> str:
    return obs_path.rstrip("/").split("/")[-1]


@router.get("", response_model=List[CodeRepoInfo])
def list_code_repos(
    tag: Optional[str] = Query(None, description="按标签精确筛选"),
    version: Optional[str] = Query(None, description="按版本模糊搜索"),
    user: dict = Depends(get_current_user),
):
    sql = "SELECT * FROM code_repos"
    where, params = [], []
    if tag:
        where.append("tag = ?")
        params.append(tag.strip())
    if version:
        where.append("version LIKE ?")
        params.append(f"%{version.strip()}%")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


@router.post("", response_model=CodeRepoInfo)
def create_code_repo(req: CodeRepoCreate, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM code_repos WHERE name = ? AND version = ?",
            (req.name.strip(), req.version.strip()),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail=f"代码仓 {req.name.strip()} 版本 {req.version.strip()} 已存在")
        cursor = conn.execute(
            "INSERT INTO code_repos (name, obs_path, version, description, main_python_file, tag, is_default, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (req.name.strip(), req.obs_path.strip(), req.version.strip(), req.description.strip(),
             req.main_python_file.strip(), _check_tag(conn, req.tag), user["username"]),
        )
        if req.is_default:
            _set_default_repo(conn, cursor.lastrowid)
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def _set_default_repo(conn, repo_id: int) -> None:
    """全局默认代码仓唯一: 设新默认前先清掉旧默认。"""
    conn.execute("UPDATE code_repos SET is_default = 0 WHERE is_default = 1 AND id != ?", (repo_id,))
    conn.execute("UPDATE code_repos SET is_default = 1 WHERE id = ?", (repo_id,))


def _check_tag(conn, value: str) -> str:
    """标签必须是字典表里已有的值（空串表示未归类）。

    不做「不存在就自动建」—— 否则一个拼写错误会悄悄长出一个新标签，
    字典表就失去了统一口径的意义。新标签走显式的 POST /tags。
    """
    tag = (value or "").strip()
    if not tag:
        return ""
    if not conn.execute("SELECT 1 FROM code_repo_tags WHERE name = ?", (tag,)).fetchone():
        raise HTTPException(status_code=400, detail=f"标签 {tag} 不存在，请先创建")
    return tag


@router.get("/tags", response_model=List[CodeRepoTagInfo])
def list_code_repo_tags(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM code_repo_tags ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@router.post("/tags", response_model=CodeRepoTagInfo)
def create_code_repo_tag(req: CodeRepoTagCreate, user: dict = Depends(require_operator)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="标签名不能为空")
    with get_connection() as conn:
        if conn.execute("SELECT 1 FROM code_repo_tags WHERE name = ?", (name,)).fetchone():
            raise HTTPException(status_code=400, detail=f"标签 {name} 已存在")
        cursor = conn.execute(
            "INSERT INTO code_repo_tags (name, created_by) VALUES (?, ?)",
            (name, user["username"]),
        )
        row = conn.execute("SELECT * FROM code_repo_tags WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@router.delete("/tags/{tag_id}")
def delete_code_repo_tag(tag_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repo_tags WHERE id = ?", (tag_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="标签不存在")
        used = conn.execute(
            "SELECT COUNT(*) AS cnt FROM code_repos WHERE tag = ?", (row["name"],)
        ).fetchone()["cnt"]
        if used:
            raise HTTPException(status_code=400, detail=f"标签 {row['name']} 仍被 {used} 个代码仓使用，无法删除")
        conn.execute("DELETE FROM code_repo_tags WHERE id = ?", (tag_id,))
    return {"detail": "已删除"}


@router.patch("/{repo_id}", response_model=CodeRepoInfo)
def update_code_repo(repo_id: int, req: CodeRepoUpdate, user: dict = Depends(require_operator)):
    """允许改 obs_path / main_python_file / description / tag，但不允许改 name / version。

    name+version 决定了 code/src/{name}/{version} 与 code/tar/{name}/{version} 两个目录，
    实例配置里的 main_code_tar 也是这么拼的（见 instances.py 的代码仓分支）。改了会让
    已经下载/打包好的产物对不上，所以要换名称或版本应当新建一条记录。

    只拼请求里实际给了的字段，避免「只改描述」把 obs_path 一起覆盖掉。
    """
    sets, params = [], []
    if req.obs_path is not None:
        obs_path = req.obs_path.strip()
        if not obs_path:
            raise HTTPException(status_code=400, detail="OBS路径不能为空")
        sets.append("obs_path = ?")
        params.append(obs_path)
    if req.main_python_file is not None:
        main_py = req.main_python_file.strip()
        if not main_py:
            raise HTTPException(status_code=400, detail="入口文件不能为空")
        sets.append("main_python_file = ?")
        params.append(main_py)
    if req.description is not None:
        sets.append("description = ?")
        params.append(req.description.strip())

    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM code_repos WHERE id = ?", (repo_id,)).fetchone():
            raise HTTPException(status_code=404, detail="代码仓不存在")
        if req.tag is not None:
            sets.append("tag = ?")
            params.append(_check_tag(conn, req.tag))
        if req.enabled is not None:
            sets.append("enabled = ?")
            params.append(1 if req.enabled else 0)
        if sets:
            conn.execute(
                f"UPDATE code_repos SET {', '.join(sets)} WHERE id = ?",
                (*params, repo_id),
            )
        if req.is_default is not None:
            if req.is_default:
                _set_default_repo(conn, repo_id)
            else:
                conn.execute("UPDATE code_repos SET is_default = 0 WHERE id = ?", (repo_id,))
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    return dict(row)


@router.delete("/{repo_id}")
def delete_code_repo(repo_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="代码仓不存在")
        conn.execute("DELETE FROM code_repos WHERE id = ?", (repo_id,))
    return {"detail": "已删除"}


@router.post("/{repo_id}/download")
async def download_code_repo(repo_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="代码仓不存在")

    repo = dict(row)
    src_dir = os.path.join(_get_code_dir(), "src", repo["name"], repo["version"])
    tar_dir = os.path.join(_get_code_dir(), "tar", repo["name"], repo["version"])
    tar_path = os.path.join(tar_dir, "openclaw-task.tar")

    if os.path.isfile(tar_path):
        return {"message": "代码仓已下载并打包", "tar_path": tar_path, "already_exists": True}

    # Download from OBS
    if not (os.path.isdir(src_dir) and os.listdir(src_dir)):
        os.makedirs(src_dir, exist_ok=True)
        obs_path = repo["obs_path"]
        if not obs_path.endswith("/"):
            obs_path += "/"
        proc = await asyncio.create_subprocess_exec(
            settings.OBSUTIL_PATH, "cp", obs_path, src_dir, "-r", "-f",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise HTTPException(status_code=500, detail="OBS下载超时(600s)")
        if proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"OBS下载失败: {stderr.decode(errors='replace')[:500]}")

    dir_name = _obs_dir_name(repo["obs_path"])
    actual_dir = os.path.join(src_dir, dir_name)

    # Package as tar
    os.makedirs(tar_dir, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "tar", "cf", tar_path, dir_name,
        cwd=src_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=500, detail="打包超时(120s)")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"打包失败: {stderr.decode(errors='replace')[:500]}")

    return {"message": "下载并打包成功", "tar_path": tar_path, "src_path": actual_dir, "already_exists": False}


@router.get("/{repo_id}/status")
def check_code_repo_status(repo_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="代码仓不存在")

    repo = dict(row)
    tar_path = os.path.join(_get_code_dir(), "tar", repo["name"], repo["version"], "openclaw-task.tar")
    src_dir = os.path.join(_get_code_dir(), "src", repo["name"], repo["version"])
    dir_name = _obs_dir_name(repo["obs_path"])
    actual_dir = os.path.join(src_dir, dir_name)

    tar_exists = os.path.isfile(tar_path)
    src_exists = os.path.isdir(actual_dir)

    return {
        "downloaded": src_exists,
        "packaged": tar_exists,
        "tar_path": tar_path if tar_exists else None,
        "src_path": actual_dir if src_exists else None,
    }
