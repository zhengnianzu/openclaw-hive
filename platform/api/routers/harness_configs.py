"""
Harness 配置：方案（scheme）→ 版本（version snapshot）两层模型。

- `harness_configs` = 方案：业务用途，如「研究版本」「编码版本」，归属某个 harness_type。
  每个方案在磁盘上有一份**可编辑草稿**：`settings/<type>_<slug>/`
- `harness_config_versions` = 版本：方案的不可变快照，存在 `settings/<type>_<slug>/.versions/<version>/`。
  只有点「创建版本」时才把草稿冻结成快照；实例创建只能引用已发布（published）的版本。

旧模型（一行 = type+version 目录）的数据由 `ensure_defaults()` 幂等迁移：
每条旧记录 → 一个方案 + 一个 published 版本，旧目录 settings/<type>/<version>/ 原样保留。
"""

import asyncio
import difflib
import json
import os
import re
import shutil
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from omegaconf import OmegaConf
from pydantic import BaseModel

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.harness_config import (
    HarnessConfigCreate,
    HarnessConfigUpdate,
    HarnessConfigInfo,
    HarnessConfigVersionCreate,
    HarnessConfigVersionInfo,
    HarnessConfigVersionRollback,
    HarnessConfigVersionStatus,
)
from . import harness_meta

router = APIRouter(prefix="/api/harness-configs", tags=["harness-configs"])

# 由 platform/settings/harness_config.json 派生; 新增 harness 只需改 JSON
HARNESS_FILES = {k: v.get("agent_config") for k, v in harness_meta._entries().items()}
EXPECTED_FILES = harness_meta.EXPECTED_FILES()

DEFAULT_VERSION = "默认"
# 草稿子目录名；版本目录与它同级：settings/harness/<type>/harness-<id>/{draft,<version>}/
VALID_STATUS = ("draft", "testing", "published")
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


# ============================================================================
# 路径解析 —— 全项目唯一的目录推导处
# ============================================================================

def expected_files_for(harness_type: str) -> list[str]:
    return EXPECTED_FILES.get(harness_type, EXPECTED_FILES["common"])


def scheme_slug(row) -> str:
    """旧布局目录名（settings/<type>_<slug>/）。仅迁移时读取，不再用于新路径。"""
    slug = (row["slug"] if "slug" in row.keys() else "") or ""
    if slug:
        return slug
    return (row["version"] if "version" in row.keys() else "") or ""


DRAFT_DIRNAME = "draft"


def scheme_rel_dir(row) -> str:
    """方案目录相对 HARNESS_DIR 的路径。

    优先用库里存的值；没有则按 harness-<id> 推导（历史默认布局）。
    """
    rel = (row["harness_dir"] if "harness_dir" in row.keys() else "") or ""
    if rel:
        return rel.strip("/")
    return f"harness/{row['harness_type']}/harness-{row['id']}"


def scheme_dir(row) -> str:
    """方案目录绝对路径 = HARNESS_DIR + harness_dir。

    路径不再是拼出来的固定结构：每个方案在库里记着自己的相对路径，
    老方案可以指向任意既有目录而不动磁盘。
    """
    return os.path.join(settings.HARNESS_DIR, scheme_rel_dir(row))


def draft_dir(row) -> str:
    """草稿目录：<方案目录>/draft/"""
    return os.path.join(scheme_dir(row), DRAFT_DIRNAME)


def version_dir(row, version: str) -> str:
    """版本快照目录：<方案目录>/<version>/（固定规则，不入库）"""
    return os.path.join(scheme_dir(row), version)


def legacy_dir(harness_type: str, version: str) -> str:
    """旧模型目录 settings/<type>/<version>/，仅供迁移时读取源文件。"""
    if version == DEFAULT_VERSION:
        return settings.SETTINGS_DIR
    return os.path.join(settings.SETTINGS_DIR, harness_type, version)


def _former_flat_dir(row) -> str:
    """上一版布局的目录 settings/<type>_<slug>/，仅迁移时读取。"""
    slug = scheme_slug(row)
    if not slug:
        return ""
    return os.path.join(settings.SETTINGS_DIR, f"{row['harness_type']}_{slug}")


def _scan_files_dir(d: str, harness_type: str) -> list[str]:
    if not os.path.isdir(d):
        return []
    expected = set(expected_files_for(harness_type))
    return sorted(f for f in os.listdir(d)
                  if os.path.isfile(os.path.join(d, f)) and f in expected)


MD_DIRNAME = "md"


def md_dir(root_dir: str) -> str:
    """md 子目录：<草稿或版本目录>/md/"""
    return os.path.join(root_dir, MD_DIRNAME)


def _scan_md_dir(root_dir: str) -> list[str]:
    """已初始化的 md 文件（不递归，只取 md/ 顶层）。"""
    d = md_dir(root_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if f.lower().endswith(".md") and os.path.isfile(os.path.join(d, f)))


def _copy_md(src_root: str, dst_root: str) -> list[str]:
    """把 src_root/md/*.md 复制到 dst_root/md/，返回文件名列表。

    不递归：md/ 下只应有一层文件，避免历史那种无限嵌套事故。
    """
    names = _scan_md_dir(src_root)
    if not names:
        return []
    dst_md = md_dir(dst_root)
    os.makedirs(dst_md, exist_ok=True)
    for name in names:
        shutil.copy2(os.path.join(md_dir(src_root), name), os.path.join(dst_md, name))
    return names


def _snapshot_dir(src: str, dst: str, harness_type: str) -> list[str]:
    """把 src 中属于该 harness 的配置文件复制到 dst，返回复制成功的文件名。

    「创建版本」「回滚」「迁移」三处共用；不递归，避免历史上那种
    把 settings/ 下的子目录一起卷进去、产生无限嵌套目录的事故。
    md/ 下的文件是唯一被允许的子目录，且只拷一层。
    """
    os.makedirs(dst, exist_ok=True)
    copied = []
    for fname in expected_files_for(harness_type):
        src_file = os.path.join(src, fname)
        if os.path.isfile(src_file):
            shutil.copy2(src_file, os.path.join(dst, fname))
            copied.append(fname)
    _copy_md(src, dst)
    return copied


def _sync_harness_md(root_dir: str):
    """把 root_dir/config.yaml 的 run_config.harness_md 重写为当前 md/ 文件的绝对路径列表。

    失败只记日志不抛错——md 保存不该因为 config.yaml 格式问题而失败。
    """
    cfg_path = os.path.join(root_dir, "config.yaml")
    if not os.path.isfile(cfg_path):
        return
    names = _scan_md_dir(root_dir)
    paths = [os.path.join(md_dir(root_dir), n) for n in names]
    try:
        cfg = OmegaConf.load(cfg_path)
        if not OmegaConf.is_dict(cfg.get("run_config")):
            cfg.run_config = {}
        cfg.run_config.harness_md = paths
        OmegaConf.save(cfg, cfg_path)
    except Exception as e:
        print(f"[harness_configs] 同步 harness_md 失败 {cfg_path}: {e}")



def _md_file_list(root_dir: str, harness_type: str) -> list[dict]:
    """该方案/版本的 md 文件视图：模板里有的都列出来，未初始化的 exists=False。"""
    present = set(_scan_md_dir(root_dir))
    out = []
    for name in harness_meta.md_templates(harness_type):
        exists = name in present
        fpath = os.path.join(md_dir(root_dir), name)
        out.append({
            "name": name,
            "exists": exists,
            "size": os.path.getsize(fpath) if exists else 0,
        })
    return out

# ============================================================================
# 行 → 响应模型
# ============================================================================

def _version_rollup(conn, config_id: int) -> dict:
    rows = conn.execute(
        "SELECT version, status, created_at FROM harness_config_versions "
        "WHERE config_id = ? ORDER BY created_at DESC, id DESC",
        (config_id,),
    ).fetchall()
    if not rows:
        return {"version_count": 0, "latest_version": "", "latest_status": "", "published_count": 0}
    return {
        "version_count": len(rows),
        "latest_version": rows[0]["version"],
        "latest_status": rows[0]["status"],
        "published_count": sum(1 for r in rows if r["status"] == "published"),
    }


def _build_info(row, conn=None) -> HarnessConfigInfo:
    info = dict(row)
    info.setdefault("config_files_json", "[]")
    info.setdefault("is_default", 0)
    info.setdefault("obs_source_path", "")
    info.setdefault("obs_harness_path", "")
    info.setdefault("obs_task_path", "")
    info.setdefault("obs_proxy_path", "")
    info.setdefault("slug", "")
    info.setdefault("created_by", "")
    info.setdefault("created_at", "")
    info.setdefault("updated_at", "")
    info.pop("version_legacy", None)
    if conn is not None:
        info.update(_version_rollup(conn, row["id"]))
    return HarnessConfigInfo(**info)


def _build_version(row) -> HarnessConfigVersionInfo:
    d = dict(row)
    d.setdefault("files_json", "[]")
    d.setdefault("description", "")
    d.setdefault("obs_harness_path", "")
    d.setdefault("obs_task_path", "")
    d.setdefault("obs_proxy_path", "")
    d.setdefault("created_by", "")
    d.setdefault("created_at", "")
    d.setdefault("published_by", "")
    return HarnessConfigVersionInfo(**d)


def _file_type_for(filename: str, harness_type: str) -> str:
    harness_file = HARNESS_FILES.get(harness_type)
    if harness_file and filename == harness_file:
        return "harness"
    if filename == "config.yaml":
        return "task"
    if filename == "user_proxy_model.json":
        return "proxy"
    return "unknown"


def _obs_path_for_type(row, file_type: str) -> str:
    if file_type == "harness":
        return row["obs_harness_path"] or ""
    elif file_type == "task":
        return row["obs_task_path"] or ""
    elif file_type == "proxy":
        return row["obs_proxy_path"] or ""
    return ""


def _update_files_json(conn, config_id: int, harness_type: str, row=None):
    """刷新方案草稿的文件清单（方案列保留，兼容旧前端）。"""
    if row is None:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        return
    file_list = _scan_files_dir(draft_dir(row), harness_type)
    conn.execute(
        "UPDATE harness_configs SET config_files_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(file_list), datetime.now().isoformat(), config_id),
    )


async def _pull_obs_file(obs_path: str, dest_dir: str):
    if not obs_path:
        return
    proc = await asyncio.create_subprocess_exec(
        settings.OBSUTIL_PATH, "cp", obs_path, dest_dir, "-r", "-f",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(status_code=504, detail="OBS 拉取超时")
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"OBS 拉取失败: {stderr.decode()[:500]}")


def _get_scheme_row(conn, config_id: int):
    row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置方案不存在")
    return row


def _get_version_row(conn, config_id: int, version_id: int):
    row = conn.execute(
        "SELECT * FROM harness_config_versions WHERE id = ? AND config_id = ?",
        (version_id, config_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")
    return row


def _ensure_scheme_dirs(row) -> str:
    d = draft_dir(row)
    os.makedirs(d, exist_ok=True)
    return d


def resolve_settings_dir(conn, harness_type: str,
                         version_id: int | None,
                         config_id: int | None) -> tuple[str, dict | None, str]:
    """给实例创建用的目录解析。返回 (目录, 版本行dict | None, 说明)。

    优先级：具体版本 → 方案的最新已发布版本 → 扁平 settings/ 兜底。
    版本目录缺失时直接报错，不静默回退——否则会拿错配置去建实例。
    """
    if version_id:
        v = conn.execute(
            "SELECT * FROM harness_config_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if not v:
            raise HTTPException(status_code=404, detail="配置版本不存在")
        scheme = conn.execute(
            "SELECT * FROM harness_configs WHERE id = ?", (v["config_id"],)
        ).fetchone()
        if not scheme:
            raise HTTPException(status_code=404, detail="配置方案不存在")
        if scheme["harness_type"] != harness_type:
            raise HTTPException(
                status_code=400,
                detail=f"配置方案属于 {scheme['harness_type']}，与请求的 harness_type（{harness_type}）不一致",
            )
        if v["status"] != "published":
            raise HTTPException(
                status_code=400,
                detail=f"版本 {v['version']} 尚未发布，不能用于创建实例",
            )
        d = version_dir(scheme, v["version"])
        if not os.path.isdir(d):
            raise HTTPException(status_code=409, detail=f"版本 {v['version']} 的快照目录缺失，请重新创建版本")
        return d, dict(v), f"{scheme['name']} / {v['version']}"

    if config_id:
        scheme = conn.execute(
            "SELECT * FROM harness_configs WHERE id = ?", (config_id,)
        ).fetchone()
        if scheme and scheme["harness_type"] == harness_type:
            v = conn.execute(
                "SELECT * FROM harness_config_versions WHERE config_id = ? AND status = 'published' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (config_id,),
            ).fetchone()
            if v:
                d = version_dir(scheme, v["version"])
                if os.path.isdir(d):
                    return d, dict(v), f"{scheme['name']} / {v['version']}"
        # 旧客户端 / 老数据：沿用扁平 settings/ 兜底
        return settings.SETTINGS_DIR, None, "settings/ (默认)"

    return settings.SETTINGS_DIR, None, "settings/ (默认)"


# ============================================================================
# 方案 CRUD
# ============================================================================

@router.get("", response_model=list[HarnessConfigInfo])
def list_harness_configs(
    harness_type: str = Query(default=None),
    user: dict = Depends(get_current_user),
):
    with get_connection() as conn:
        if harness_type:
            rows = conn.execute(
                "SELECT * FROM harness_configs WHERE harness_type = ? ORDER BY is_default DESC, updated_at DESC",
                (harness_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM harness_configs ORDER BY harness_type, is_default DESC, updated_at DESC"
            ).fetchall()
        return [_build_info(r, conn) for r in rows]


@router.get("/field-mappings")
def get_field_mappings(user: dict = Depends(get_current_user)):
    fpath = os.path.join(settings.SETTINGS_DIR, "field_mappings.json")
    if not os.path.exists(fpath):
        return {}
    with open(fpath, "r", encoding="utf-8") as f:
        return json.load(f)


@router.get("/{config_id}", response_model=HarnessConfigInfo)
def get_harness_config(config_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        return _build_info(row, conn)


@router.post("", response_model=HarnessConfigInfo)
async def create_harness_config(req: HarnessConfigCreate, user: dict = Depends(require_operator)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请输入方案名称")

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND name = ?",
            (req.harness_type, name),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"该类型下已存在名为「{name}」的方案")

        cursor = conn.execute(
            """INSERT INTO harness_configs
               (name, harness_type, slug, version, description, obs_harness_path,
                obs_task_path, obs_proxy_path, created_by, updated_at)
               VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?)""",
            (name, req.harness_type, req.description,
             req.obs_harness_path, req.obs_task_path, req.obs_proxy_path,
             user["username"], datetime.now().isoformat()),
        )
        config_id = cursor.lastrowid
        # 新方案目录固定为 harness/<type>/harness-<id>
        conn.execute(
            "UPDATE harness_configs SET harness_dir = ? WHERE id = ?",
            (f"harness/{req.harness_type}/harness-{config_id}", config_id),
        )

        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        d = _ensure_scheme_dirs(row)

        for path in (req.obs_harness_path, req.obs_task_path, req.obs_proxy_path):
            if path:
                try:
                    await _pull_obs_file(path, d)
                except Exception:
                    pass

        _update_files_json(conn, config_id, req.harness_type)

        has_default = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND is_default = 1",
            (req.harness_type,),
        ).fetchone()
        if not has_default:
            conn.execute("UPDATE harness_configs SET is_default = 1 WHERE id = ?", (config_id,))

        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        return _build_info(row, conn)


@router.put("/{config_id}", response_model=HarnessConfigInfo)
def update_harness_config(config_id: int, req: HarnessConfigUpdate, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if "name" in updates:
            name = (updates["name"] or "").strip()
            if not name:
                raise HTTPException(status_code=400, detail="方案名称不能为空")
            dup = conn.execute(
                "SELECT id FROM harness_configs WHERE harness_type = ? AND name = ? AND id != ?",
                (row["harness_type"], name, config_id),
            ).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail=f"该类型下已存在名为「{name}」的方案")
            updates["name"] = name

        if updates:
            updates["updated_at"] = datetime.now().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE harness_configs SET {set_clause} WHERE id = ?",
                (*updates.values(), config_id),
            )
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        return _build_info(row, conn)


@router.delete("/{config_id}")
def delete_harness_config(config_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        # 表间没有开启外键约束，版本行必须手工删
        conn.execute("DELETE FROM harness_config_versions WHERE config_id = ?", (config_id,))
        conn.execute("DELETE FROM harness_configs WHERE id = ?", (config_id,))

    d = scheme_dir(row)
    # 扁平 settings/ 绝不能删；其余只删本方案目录 settings/harness/<type>/<slug>/
    if d != settings.SETTINGS_DIR and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return {"detail": "已删除"}


@router.put("/{config_id}/set-default")
def set_default(config_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        conn.execute(
            "UPDATE harness_configs SET is_default = 0 WHERE harness_type = ?",
            (row["harness_type"],),
        )
        conn.execute("UPDATE harness_configs SET is_default = 1 WHERE id = ?", (config_id,))
    return {"detail": "已设为默认"}


# ============================================================================
# 草稿文件管理
# ============================================================================

@router.get("/{config_id}/files")
def list_files(config_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

    harness_type = row["harness_type"]
    d = draft_dir(row)

    files = []
    for fname in expected_files_for(harness_type):
        fpath = os.path.join(d, fname)
        exists = os.path.isfile(fpath)
        file_type = "yaml" if fname.endswith((".yaml", ".yml")) else "json"
        ft = _file_type_for(fname, harness_type)
        files.append({
            "name": fname,
            "type": file_type,
            "category": ft,
            "exists": exists,
            "size": os.path.getsize(fpath) if exists else 0,
            "obs_path": _obs_path_for_type(row, ft),
        })
    return {"config_id": config_id, "harness_type": harness_type,
            "files": files, "md_files": _md_file_list(d, harness_type)}


@router.get("/{config_id}/files/{filename}")
def get_file(config_id: int, filename: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

    fpath = os.path.join(draft_dir(row), filename)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="文件不存在")

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    file_type = "yaml" if filename.endswith((".yaml", ".yml")) else "json"
    return {"filename": filename, "type": file_type, "content": content}


# ============================================================================
# harness md：草稿可写、版本只读、未初始化可从模板导入
# ============================================================================

def _md_name_list(harness_type: str) -> list[str]:
    return harness_meta.md_templates(harness_type)


def _assert_md_name(harness_type: str, name: str):
    """文件名必须在 harness_mds/<type>/ 白名单内，否则视为路径穿越。"""
    if name not in _md_name_list(harness_type):
        raise HTTPException(status_code=404, detail=f"md 模板不存在: {name}")


@router.get("/{config_id}/md/{name}")
def get_draft_md(config_id: int, name: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
    _assert_md_name(row["harness_type"], name)

    fpath = os.path.join(md_dir(draft_dir(row)), name)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="文件未初始化")
    with open(fpath, "r", encoding="utf-8") as f:
        return {"filename": name, "content": f.read()}


@router.put("/{config_id}/md/{name}")
def save_draft_md(config_id: int, name: str,
                  content: str = Body(..., media_type="text/plain"),
                  user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
    _assert_md_name(row["harness_type"], name)

    root = draft_dir(row)
    d = md_dir(root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(content)
    _sync_harness_md(root)
    return {"detail": "已保存", "filename": name}


@router.post("/{config_id}/md/{name}/init")
def init_draft_md(config_id: int, name: str, user: dict = Depends(require_operator)):
    """从 settings/harness_mds/<type>/ 复制模板到草稿的 md/。"""
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
    _assert_md_name(row["harness_type"], name)

    src = harness_meta.md_template_path(row["harness_type"], name)
    if not src or not os.path.isfile(src):
        raise HTTPException(status_code=404, detail=f"模板不存在: {name}")

    root = draft_dir(row)
    d = md_dir(root)
    os.makedirs(d, exist_ok=True)
    shutil.copy2(src, os.path.join(d, name))
    _sync_harness_md(root)
    return {"detail": f"已从模板初始化 {name}", "filename": name}


@router.get("/{config_id}/versions/{version_id}/md/{name}")
def get_version_md(config_id: int, version_id: int, name: str,
                   user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        vrow = _get_version_row(conn, config_id, version_id)
    _assert_md_name(row["harness_type"], name)

    fpath = os.path.join(md_dir(version_dir(row, vrow["version"])), name)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="该版本没有这个 md 文件")
    with open(fpath, "r", encoding="utf-8") as f:
        return {"filename": name, "content": f.read()}


@router.put("/{config_id}/files/{filename}")
def save_file(config_id: int, filename: str, content: str = Body(..., media_type="text/plain"),
              user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

    d = _ensure_scheme_dirs(row)
    fpath = os.path.join(d, filename)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)
    with get_connection() as conn:
        _update_files_json(conn, config_id, row["harness_type"])
    return {"detail": "已保存", "filename": filename}


class InitFileRequest(BaseModel):
    filename: str


@router.post("/{config_id}/init-file")
def init_file(config_id: int, req: InitFileRequest, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

    # 模板源始终是扁平的 settings/<filename>
    src = os.path.join(settings.SETTINGS_DIR, req.filename)
    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail=f"模板文件不存在: {req.filename}")

    d = _ensure_scheme_dirs(row)
    shutil.copy2(src, os.path.join(d, req.filename))
    with get_connection() as conn:
        _update_files_json(conn, config_id, row["harness_type"])
    return {"detail": f"已从模板初始化 {req.filename}"}


class PullObsRequest(BaseModel):
    file_type: str


@router.post("/{config_id}/pull-obs")
async def pull_obs(config_id: int, req: PullObsRequest, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)

    obs_path = _obs_path_for_type(row, req.file_type)
    if not obs_path:
        raise HTTPException(status_code=400, detail="未配置该类型的 OBS 路径")

    d = _ensure_scheme_dirs(row)
    await _pull_obs_file(obs_path, d)
    with get_connection() as conn:
        _update_files_json(conn, config_id, row["harness_type"])
    return {"detail": "已从 OBS 拉取更新"}


class CopyRequest(BaseModel):
    name: str = ""


@router.post("/{config_id}/copy", response_model=HarnessConfigInfo)
def copy_harness_config(config_id: int, body: CopyRequest, user: dict = Depends(require_operator)):
    """把当前方案的草稿文件复制成一个新方案（不复制版本历史）。"""
    with get_connection() as conn:
        src = _get_scheme_row(conn, config_id)

        new_name = (body.name or "").strip() or f"{src['name']} (副本)"
        dup = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND name = ?",
            (src["harness_type"], new_name),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"该类型下已存在名为「{new_name}」的方案")

        cursor = conn.execute(
            """INSERT INTO harness_configs
               (name, harness_type, slug, version, description, obs_harness_path,
                obs_task_path, obs_proxy_path, created_by, updated_at)
               VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?)""",
            (new_name, src["harness_type"], src["description"],
             src["obs_harness_path"], src["obs_task_path"], src["obs_proxy_path"],
             user["username"], datetime.now().isoformat()),
        )
        new_id = cursor.lastrowid
        conn.execute(
            "UPDATE harness_configs SET harness_dir = ? WHERE id = ?",
            (f"harness/{src['harness_type']}/harness-{new_id}", new_id),
        )
        new_row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (new_id,)).fetchone()

        _snapshot_dir(draft_dir(src), draft_dir(new_row), src["harness_type"])
        _update_files_json(conn, new_id, src["harness_type"])

        new_row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (new_id,)).fetchone()
        return _build_info(new_row, conn)


# ============================================================================
# 版本：创建 / 列表 / 状态 / 发布 / 回滚 / 只读文件 / diff
# ============================================================================

@router.get("/{config_id}/versions", response_model=list[HarnessConfigVersionInfo])
def list_versions(config_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        _get_scheme_row(conn, config_id)
        rows = conn.execute(
            "SELECT * FROM harness_config_versions WHERE config_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (config_id,),
        ).fetchall()
        return [_build_version(r) for r in rows]


@router.post("/{config_id}/versions", response_model=HarnessConfigVersionInfo)
def create_version(config_id: int, req: HarnessConfigVersionCreate,
                   user: dict = Depends(require_operator)):
    """冻结当前草稿为不可变快照。"""
    version = (req.version or "").strip()
    if not SLUG_RE.match(version) or version in (".", ".."):
        raise HTTPException(status_code=400, detail="版本号仅支持字母、数字、点、下划线、短横线，最长 40 位")

    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        dup = conn.execute(
            "SELECT id FROM harness_config_versions WHERE config_id = ? AND version = ?",
            (config_id, version),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"版本 {version} 已存在")

        files = _scan_files_dir(draft_dir(row), row["harness_type"])
        if not files:
            raise HTTPException(status_code=400, detail="草稿目录没有任何配置文件，无法创建版本")

        dst = version_dir(row, version)
        copied = _snapshot_dir(draft_dir(row), dst, row["harness_type"])
        if not copied:
            raise HTTPException(status_code=400, detail="草稿目录没有可快照的配置文件")
        # 快照里的 config.yaml 也要指向快照自己的 md/，而不是草稿的
        _sync_harness_md(dst)

        cursor = conn.execute(
            """INSERT INTO harness_config_versions
               (config_id, version, status, description, files_json,
                obs_harness_path, obs_task_path, obs_proxy_path, created_by)
               VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)""",
            (config_id, version, req.description, json.dumps(copied),
             row["obs_harness_path"], row["obs_task_path"], row["obs_proxy_path"],
             user["username"]),
        )
        vid = cursor.lastrowid
        vrow = conn.execute("SELECT * FROM harness_config_versions WHERE id = ?", (vid,)).fetchone()
        return _build_version(vrow)


@router.put("/{config_id}/versions/{version_id}/status", response_model=HarnessConfigVersionInfo)
def set_version_status(config_id: int, version_id: int, req: HarnessConfigVersionStatus,
                       user: dict = Depends(require_operator)):
    status = (req.status or "").strip()
    if status not in VALID_STATUS:
        raise HTTPException(status_code=400, detail=f"状态必须是 {'/'.join(VALID_STATUS)} 之一")
    with get_connection() as conn:
        _get_scheme_row(conn, config_id)
        _get_version_row(conn, config_id, version_id)
        if status == "published":
            conn.execute(
                "UPDATE harness_config_versions SET status = 'published', published_at = ?, published_by = ? "
                "WHERE id = ?",
                (datetime.now().isoformat(), user["username"], version_id),
            )
        else:
            conn.execute(
                "UPDATE harness_config_versions SET status = ? WHERE id = ?",
                (status, version_id),
            )
        vrow = conn.execute("SELECT * FROM harness_config_versions WHERE id = ?", (version_id,)).fetchone()
        return _build_version(vrow)


@router.put("/{config_id}/versions/{version_id}/publish", response_model=HarnessConfigVersionInfo)
def publish_version(config_id: int, version_id: int, user: dict = Depends(require_operator)):
    return set_version_status(config_id, version_id, HarnessConfigVersionStatus(status="published"), user)


@router.post("/{config_id}/versions/{version_id}/apply-to-draft")
def apply_version_to_draft(config_id: int, version_id: int,
                           user: dict = Depends(require_operator)):
    """把某个版本的内容覆盖到工作台（草稿）。

    只是覆盖工作副本，不改动任何版本历史——覆盖后工作台相对该版本无差异，
    可以继续改、再创建新版本。这是「基于旧版本继续改」的正常姿势。
    """
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        src = _get_version_row(conn, config_id, version_id)

    src_dir = version_dir(row, src["version"])
    if not _scan_files_dir(src_dir, row["harness_type"]):
        raise HTTPException(status_code=400, detail=f"版本 {src['version']} 的快照目录为空")

    dst = draft_dir(row)
    os.makedirs(dst, exist_ok=True)
    # 先清掉工作台现有的配置文件与 md，避免残留旧文件（只删已知白名单，不递归）
    for name in expected_files_for(row["harness_type"]):
        p = os.path.join(dst, name)
        if os.path.isfile(p):
            os.remove(p)
    old_md = md_dir(dst)
    if os.path.isdir(old_md):
        for name in _scan_md_dir(dst):
            try:
                os.remove(os.path.join(old_md, name))
            except OSError:
                pass

    copied = _snapshot_dir(src_dir, dst, row["harness_type"])
    _sync_harness_md(dst)

    with get_connection() as conn:
        _update_files_json(conn, config_id, row["harness_type"])
    return {
        "detail": f"已把版本 {src['version']} 的内容覆盖到工作台",
        "files": copied,
        "md_files": _scan_md_dir(dst),
    }


@router.post("/{config_id}/versions/{version_id}/rollback", response_model=HarnessConfigVersionInfo)
def rollback_version(config_id: int, version_id: int, req: HarnessConfigVersionRollback,
                     user: dict = Depends(require_operator)):
    """回滚 = 以旧版本内容**新建**一个版本，历史保持只追加。"""
    version = (req.version or "").strip()
    if not SLUG_RE.match(version) or version in (".", ".."):
        raise HTTPException(status_code=400, detail="版本号仅支持字母、数字、点、下划线、短横线，最长 40 位")

    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        src = _get_version_row(conn, config_id, version_id)
        dup = conn.execute(
            "SELECT id FROM harness_config_versions WHERE config_id = ? AND version = ?",
            (config_id, version),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"版本 {version} 已存在")

        src_dir = version_dir(row, src["version"])
        dst_dir = version_dir(row, version)
        copied = _snapshot_dir(src_dir, dst_dir, row["harness_type"])
        if not copied:
            raise HTTPException(status_code=400, detail=f"版本 {src['version']} 的快照目录为空，无法回滚")
        _sync_harness_md(dst_dir)

        desc = req.description or f"回滚自 {src['version']}"
        cursor = conn.execute(
            """INSERT INTO harness_config_versions
               (config_id, version, status, description, files_json,
                obs_harness_path, obs_task_path, obs_proxy_path, created_by)
               VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)""",
            (config_id, version, desc, json.dumps(copied),
             src["obs_harness_path"], src["obs_task_path"], src["obs_proxy_path"],
             user["username"]),
        )
        vid = cursor.lastrowid
        vrow = conn.execute("SELECT * FROM harness_config_versions WHERE id = ?", (vid,)).fetchone()
        return _build_version(vrow)


@router.get("/{config_id}/versions/{version_id}/files")
def list_version_files(config_id: int, version_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        vrow = _get_version_row(conn, config_id, version_id)

    d = version_dir(row, vrow["version"])
    files = []
    for fname in expected_files_for(row["harness_type"]):
        fpath = os.path.join(d, fname)
        exists = os.path.isfile(fpath)
        files.append({
            "name": fname,
            "type": "yaml" if fname.endswith((".yaml", ".yml")) else "json",
            "category": _file_type_for(fname, row["harness_type"]),
            "exists": exists,
            "size": os.path.getsize(fpath) if exists else 0,
        })
    return {"config_id": config_id, "version_id": version_id,
            "files": files, "md_files": _md_file_list(d, row["harness_type"])}


@router.get("/{config_id}/versions/{version_id}/files/{filename}")
def get_version_file(config_id: int, version_id: int, filename: str,
                     user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        vrow = _get_version_row(conn, config_id, version_id)

    fpath = os.path.join(version_dir(row, vrow["version"]), filename)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="文件不存在")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return {"filename": filename, "content": content}


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _diff_files(a_path: str, b_path: str, filename: str, a_label: str, b_label: str) -> dict:
    a = _read_text(a_path)
    b = _read_text(b_path)
    if a == b:
        return {"file": filename, "base": a_label, "target": b_label, "identical": True, "lines": []}

    lines = []
    old_no = new_no = 0
    for raw in difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile=a_label, tofile=b_label, lineterm="", n=3,
    ):
        if raw.startswith("---") or raw.startswith("+++"):
            lines.append({"type": "header", "text": raw})
        elif raw.startswith("@@"):
            lines.append({"type": "hunk", "text": raw})
        elif raw.startswith("+"):
            new_no += 1
            lines.append({"type": "add", "text": raw[1:], "old_no": None, "new_no": new_no})
        elif raw.startswith("-"):
            old_no += 1
            lines.append({"type": "del", "text": raw[1:], "old_no": old_no, "new_no": None})
        else:
            old_no += 1
            new_no += 1
            lines.append({"type": "ctx", "text": raw[1:] if raw.startswith(" ") else raw,
                          "old_no": old_no, "new_no": new_no})
    return {"file": filename, "base": a_label, "target": b_label, "identical": False, "lines": lines}


@router.get("/{config_id}/versions/{version_id}/diff")
def diff_version(config_id: int, version_id: int, base: str = Query(default="draft"),
                 user: dict = Depends(get_current_user)):
    """版本 vs 草稿（base=draft）或 vs 另一版本（base=<version 字符串>）。"""
    with get_connection() as conn:
        row = _get_scheme_row(conn, config_id)
        vrow = _get_version_row(conn, config_id, version_id)
        if base != "draft":
            base_row = conn.execute(
                "SELECT * FROM harness_config_versions WHERE config_id = ? AND version = ?",
                (config_id, base),
            ).fetchone()
            if not base_row:
                raise HTTPException(status_code=404, detail=f"基准版本 {base} 不存在")

    target_dir = version_dir(row, vrow["version"])
    if base == "draft":
        base_dir, base_label = draft_dir(row), "草稿"
    else:
        base_dir, base_label = version_dir(row, base), base

    files = []
    for fname in expected_files_for(row["harness_type"]):
        a = os.path.join(base_dir, fname)
        b = os.path.join(target_dir, fname)
        if not os.path.isfile(a) and not os.path.isfile(b):
            continue
        files.append(_diff_files(a, b, fname, base_label, vrow["version"]))
    return {
        "config_id": config_id,
        "version": vrow["version"],
        "base": base_label,
        "identical": all(f["identical"] for f in files),
        "files": files,
    }


# ============================================================================
# 启动时：迁移旧模型 + 注册默认方案（幂等）
# ============================================================================

def _slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (text or "").strip()).strip("-")
    return s[:40]


def ensure_defaults():
    """把旧模型数据迁移成「方案 + 版本」，并补齐每个 harness 类型的默认方案。

    幂等：重复执行不会产生重复行，也不会覆盖已有快照。
    """
    with get_connection() as conn:
        # 1) 旧行回填 slug：用旧 version 当目录名，保持磁盘位置不变
        conn.execute(
            "UPDATE harness_configs SET slug = version WHERE (slug IS NULL OR slug = '') AND version != ''"
        )

        # 1b) 回填 harness_dir：老方案优先用「当前实际在用的 harness-<id>」，
        #     那份是上一轮迁移的产物、也是工作台实际读写的位置；
        #     它不存在时才回退到旧布局 harness/<type>/<slug>。
        for r in conn.execute("SELECT * FROM harness_configs").fetchall():
            if (r["harness_dir"] or "").strip():
                continue
            htype = r["harness_type"]
            candidates = [
                f"harness/{htype}/harness-{r['id']}",
                f"harness/{htype}/{scheme_slug(r)}" if scheme_slug(r) else "",
            ]
            for rel in candidates:
                if rel and os.path.isdir(os.path.join(settings.HARNESS_DIR, rel)):
                    conn.execute(
                        "UPDATE harness_configs SET harness_dir = ? WHERE id = ?", (rel, r["id"]))
                    break
            else:
                # 都不存在：按新规则占位，后续首次写入时会建目录
                conn.execute(
                    "UPDATE harness_configs SET harness_dir = ? WHERE id = ?",
                    (f"harness/{htype}/harness-{r['id']}", r["id"]))

        # 2) 每个 harness 类型补一个默认方案
        for htype in harness_meta.listable_types():
            existing = conn.execute(
                "SELECT id FROM harness_configs WHERE harness_type = ? AND slug = ?",
                (htype, DEFAULT_VERSION),
            ).fetchone()
            if not existing:
                cur = conn.execute(
                    """INSERT INTO harness_configs
                       (name, harness_type, slug, version, description, is_default, created_by, updated_at)
                       VALUES (?, ?, ?, '', '系统默认配置 (settings/)', 1, 'system', ?)""",
                    (f"{htype}_{DEFAULT_VERSION}", htype, DEFAULT_VERSION, datetime.now().isoformat()),
                )
                # 新机器首次启动：默认方案也要显式写入目录，不能留空靠推导
                conn.execute(
                    "UPDATE harness_configs SET harness_dir = ? WHERE id = ?",
                    (f"harness/{htype}/harness-{cur.lastrowid}", cur.lastrowid),
                )

        # 3) 迁移：目录 + 首个 published 版本
        rows = conn.execute("SELECT * FROM harness_configs ORDER BY id").fetchall()
        for row in rows:
            htype = row["harness_type"]
            slug = scheme_slug(row)

            # 3a) 草稿目录：settings/harness/<type>/harness-<id>/draft/
            #     不存在则依次从旧布局目录、扁平 settings/ 复制
            d = draft_dir(row)
            if _scan_files_dir(d, htype):
                pass  # 已有草稿
            else:
                src_candidates = [
                    _former_flat_dir(row),          # settings/<type>_<slug>/draft（上一版布局）
                    os.path.join(_former_flat_dir(row), DRAFT_DIRNAME) if _former_flat_dir(row) else "",
                    legacy_dir(htype, row["version"] or slug),  # settings/<type>/<version>/
                    settings.SETTINGS_DIR,          # 兜底模板
                ]
                for src in src_candidates:
                    if src and src != d and os.path.isdir(src) and _scan_files_dir(src, htype):
                        _snapshot_dir(src, d, htype)
                        break
                else:
                    # 全都为空时仍建目录，后续可从模板初始化
                    os.makedirs(d, exist_ok=True)

            # 3a-2) 版本快照：旧布局的 <scheme>/<version>/ 复制到 harness-<id>/<version>/
            for v in conn.execute(
                "SELECT version FROM harness_config_versions WHERE config_id = ?", (row["id"],)
            ).fetchall():
                vd = version_dir(row, v["version"])
                if _scan_files_dir(vd, htype):
                    continue
                for src in (
                    os.path.join(_former_flat_dir(row), v["version"]) if _former_flat_dir(row) else "",
                    os.path.join(_former_flat_dir(row), ".versions", v["version"]) if _former_flat_dir(row) else "",
                ):
                    if src and os.path.isdir(src) and _scan_files_dir(src, htype):
                        _snapshot_dir(src, vd, htype)
                        break

            # 3b) 至少一个版本：以草稿内容建一个 published 快照
            has_ver = conn.execute(
                "SELECT id FROM harness_config_versions WHERE config_id = ? LIMIT 1", (row["id"],)
            ).fetchone()
            if not has_ver:
                ver = (row["version"] or slug or "v1").strip() or "v1"
                dst = version_dir(row, ver)
                copied = _snapshot_dir(d, dst, htype)
                if copied:
                    conn.execute(
                        """INSERT INTO harness_config_versions
                           (config_id, version, status, description, files_json,
                            obs_harness_path, obs_task_path, obs_proxy_path,
                            created_by, published_at, published_by)
                           VALUES (?, ?, 'published', '从旧配置迁移', ?, ?, ?, ?, 'system', ?, 'system')""",
                        (row["id"], ver, json.dumps(copied),
                         row["obs_harness_path"], row["obs_task_path"], row["obs_proxy_path"],
                         datetime.now().isoformat()),
                    )

            # 3c) 刷新文件清单
            file_list = _scan_files_dir(d, htype)
            conn.execute(
                "UPDATE harness_configs SET config_files_json = ? WHERE id = ?",
                (json.dumps(file_list), row["id"]),
            )

        # 3d) 自愈：版本行存在但快照目录缺失/为空时，从草稿重建。
        for v in conn.execute("SELECT * FROM harness_config_versions").fetchall():
            scheme = conn.execute(
                "SELECT * FROM harness_configs WHERE id = ?", (v["config_id"],)
            ).fetchone()
            if not scheme:
                continue
            vd = version_dir(scheme, v["version"])
            if _scan_files_dir(vd, scheme["harness_type"]):
                continue
            copied = _snapshot_dir(draft_dir(scheme), vd, scheme["harness_type"])
            if copied:
                conn.execute(
                    "UPDATE harness_config_versions SET files_json = ? WHERE id = ?",
                    (json.dumps(copied), v["id"]),
                )

        # 旧布局目录（settings/<type>_<slug>/、settings/<type>/<version>/）不在这里删：
        # 启动时自动 rmtree 不可逆且每次启动都跑，风险太高。清理由一次性脚本
        # scripts/migrate_harness_layout.py 完成，它会先备份再删。

        # 3f) 给已有 config.yaml 补 run_config.harness_md 键（不创建 md/、不复制模板）。
        #     只补缺失的键，已有值不动，保证幂等。
        for row in conn.execute("SELECT * FROM harness_configs").fetchall():
            roots = [draft_dir(row)]
            for v in conn.execute(
                "SELECT version FROM harness_config_versions WHERE config_id = ?", (row["id"],)
            ).fetchall():
                roots.append(version_dir(row, v["version"]))
            for root in roots:
                cfg_path = os.path.join(root, "config.yaml")
                if not os.path.isfile(cfg_path):
                    continue
                try:
                    cfg = OmegaConf.load(cfg_path)
                    if OmegaConf.select(cfg, "run_config.harness_md") is not None:
                        continue
                    _sync_harness_md(root)
                except Exception:
                    continue

        # 4) 修复历史脏数据：同一类型下多个 is_default，只留第一条
        for htype in harness_meta.listable_types():
            defs = conn.execute(
                "SELECT id FROM harness_configs WHERE harness_type = ? AND is_default = 1 ORDER BY id",
                (htype,),
            ).fetchall()
            if len(defs) > 1:
                keep = defs[0]["id"]
                # 优先保留真正的「默认」方案
                for r in defs:
                    if scheme_slug(dict(conn.execute(
                            "SELECT * FROM harness_configs WHERE id = ?", (r["id"],)).fetchone())) == DEFAULT_VERSION:
                        keep = r["id"]
                        break
                conn.execute(
                    "UPDATE harness_configs SET is_default = 0 WHERE harness_type = ? AND id != ?",
                    (htype, keep),
                )
