import os
import subprocess
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.code_repo import CodeRepoCreate, CodeRepoInfo

router = APIRouter(prefix="/api/code-repos", tags=["code-repos"])


def _get_code_dir() -> str:
    return os.path.join(settings.HIVE_ROOT, "platform", "code")


def _obs_dir_name(obs_path: str) -> str:
    return obs_path.rstrip("/").split("/")[-1]


@router.get("", response_model=List[CodeRepoInfo])
def list_code_repos(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM code_repos ORDER BY created_at DESC").fetchall()
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
            "INSERT INTO code_repos (name, obs_path, version, description, main_python_file, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (req.name.strip(), req.obs_path.strip(), req.version.strip(), req.description.strip(), req.main_python_file.strip(), user["username"]),
        )
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (cursor.lastrowid,)).fetchone()
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
def download_code_repo(repo_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="代码仓不存在")

    repo = dict(row)
    src_dir = os.path.join(_get_code_dir(), "src", repo["name"], repo["version"])
    tar_dir = os.path.join(_get_code_dir(), "tar")
    tar_path = os.path.join(tar_dir, f"{repo['name']}-{repo['version']}.tar")

    if os.path.isfile(tar_path):
        return {"message": "代码仓已下载并打包", "tar_path": tar_path, "already_exists": True}

    # Download from OBS
    if not (os.path.isdir(src_dir) and os.listdir(src_dir)):
        os.makedirs(src_dir, exist_ok=True)
        obs_path = repo["obs_path"]
        if not obs_path.endswith("/"):
            obs_path += "/"
        ret = subprocess.run(
            [settings.OBSUTIL_PATH, "cp", obs_path, src_dir, "-r", "-f"],
            capture_output=True, text=True, timeout=600,
        )
        if ret.returncode != 0:
            raise HTTPException(status_code=500, detail=f"OBS下载失败: {ret.stderr[:500]}")

    dir_name = _obs_dir_name(repo["obs_path"])
    actual_dir = os.path.join(src_dir, dir_name)

    # Package as tar
    os.makedirs(tar_dir, exist_ok=True)
    ret = subprocess.run(
        ["tar", "cf", tar_path, dir_name],
        cwd=src_dir,
        capture_output=True, text=True, timeout=120,
    )
    if ret.returncode != 0:
        raise HTTPException(status_code=500, detail=f"打包失败: {ret.stderr[:500]}")

    return {"message": "下载并打包成功", "tar_path": tar_path, "src_path": actual_dir, "already_exists": False}


@router.get("/{repo_id}/status")
def check_code_repo_status(repo_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="代码仓不存在")

    repo = dict(row)
    tar_path = os.path.join(_get_code_dir(), "tar", f"{repo['name']}.tar")
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
