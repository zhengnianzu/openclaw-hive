from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.code_repo import (
    CodeRepoCreate,
    CodeRepoInfo,
    CodeRepoTagCreate,
    CodeRepoTagInfo,
    CodeRepoUpdate,
)
from ..services.code_repo_sync import (
    ensure_repo_ready,
    is_git,
    repo_status,
    resolve_remote_head,
)

router = APIRouter(prefix="/api/code-repos", tags=["code-repos"])


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
    # 默认代码仓置顶：is_default DESC 让唯一那条默认记录排最前，其余仍按时间倒序。
    # 排序放在 SQL 而不是前端，因为列表是后端分页的（前端只切当前页），
    # 前端排序只能作用于当前页，置顶会失效。
    sql += " ORDER BY is_default DESC, created_at DESC"
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
            "INSERT INTO code_repos (name, obs_path, version, description, main_python_file, tag, is_default, "
            "source_type, repo_url, branch, subdir, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (req.name.strip(), req.obs_path.strip(), req.version.strip(), req.description.strip(),
             req.main_python_file.strip(), _check_tag(conn, req.tag),
             req.source_type, req.repo_url.strip(), req.branch.strip(), req.subdir.strip().strip("/"),
             user["username"]),
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
    """允许改来源地址 / main_python_file / description / tag，但不允许改 name / version / source_type。

    name+version 决定了 code/src/{name}/{version} 与 code/tar/{name}/{version} 两个目录，
    实例配置里的 main_code_tar 也是这么拼的（见 instances.py 的代码仓分支）。改了会让
    已经下载/打包好的产物对不上，所以要换名称或版本应当新建一条记录。source_type 同理：
    两种来源的目录规则不同（git 多一层 commit），切换会让存量产物对不上。

    只拼请求里实际给了的字段，避免「只改描述」把 obs_path 一起覆盖掉。
    """
    sets, params = [], []
    if req.repo_url is not None:
        sets.append("repo_url = ?")
        params.append(req.repo_url.strip())
    if req.branch is not None:
        sets.append("branch = ?")
        params.append(req.branch.strip())
    if req.subdir is not None:
        sets.append("subdir = ?")
        params.append(req.subdir.strip().strip("/"))
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
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="代码仓不存在")
        # 「地址不能清空」的判据要看来源：obs 仓离了 obs_path 没法下载，
        # 但 git 仓的 obs_path 本就该是空的，不能照搬原来的非空校验。
        if req.obs_path is not None:
            obs_path = req.obs_path.strip()
            if not obs_path and not is_git(dict(row)):
                raise HTTPException(status_code=400, detail="OBS路径不能为空")
            sets.append("obs_path = ?")
            params.append(obs_path)
        if req.repo_url is not None and not req.repo_url.strip() and is_git(dict(row)):
            raise HTTPException(status_code=400, detail="仓库地址不能为空")
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


def _fetch_repo(repo_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM code_repos WHERE id = ?", (repo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="代码仓不存在")
    return dict(row)


@router.post("/{repo_id}/download")
async def download_code_repo(repo_id: int, user: dict = Depends(require_operator)):
    """惰性下载并打包：产物已在就直接返回，不重复拉取。

    两种来源的实际拉取都在 services/code_repo_sync.ensure_repo_ready 里，
    与实例准备阶段（instances.py）共用同一份实现。
    """
    result = await ensure_repo_ready(_fetch_repo(repo_id))
    return {
        "message": "代码仓已下载并打包" if result["already_exists"] else "下载并打包成功",
        **result,
    }


@router.post("/{repo_id}/sync")
async def sync_code_repo(repo_id: int, user: dict = Depends(require_operator)):
    """同步到来源的最新代码。

    git 仓：先 ls-remote 比对远端 HEAD 与本地 resolved_commit，一致且产物还在就
    直接返回 updated=false（重复点按钮不该白跑一次 clone）；不一致则拉到一个新的
    commit 目录、回写 resolved_commit/synced_at，旧目录保留。
    obs 仓：没有「版本」概念可比，退化为强制重下重打包。
    """
    repo = _fetch_repo(repo_id)
    # wait_lock=False：已有一个同步在跑时立即返回 409「正在同步中」，
    # 而不是让按钮点下去之后长时间无声等待（并发同步会互相破坏，见服务层注释）。
    result = await ensure_repo_ready(repo, force=True, wait_lock=False)
    if result["updated"]:
        msg = f"已同步到 {result['commit'][:12]}" if result["commit"] else "已重新下载并打包"
    else:
        msg = "已是最新，无需同步"
    return {"message": msg, **result}


@router.get("/{repo_id}/remote-head")
async def code_repo_remote_head(repo_id: int, user: dict = Depends(get_current_user)):
    """只读探测远端最新 commit，用于页面提示「远端有新 commit」。

    obs 仓没有远端版本可查，返回 supported=false 让前端不显示该提示。
    """
    repo = _fetch_repo(repo_id)
    if not is_git(repo):
        return {"supported": False, "remote_commit": "", "local_commit": "", "behind": False}
    remote = await resolve_remote_head(repo)
    local = (repo.get("resolved_commit") or "").strip()
    return {
        "supported": True,
        "remote_commit": remote,
        "local_commit": local,
        "behind": remote != local,
    }


@router.get("/{repo_id}/status")
def check_code_repo_status(repo_id: int, user: dict = Depends(get_current_user)):
    return repo_status(_fetch_repo(repo_id))
