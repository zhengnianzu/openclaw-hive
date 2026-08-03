import asyncio
import json
import os
import shutil
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.harness_config import HarnessConfigCreate, HarnessConfigUpdate, HarnessConfigInfo

router = APIRouter(prefix="/api/harness-configs", tags=["harness-configs"])

HARNESS_FILES = {
    "openclaw": "openclaw.json",
    "hermes": "hermes_config.yaml",
    "claude-code": "cc_settings.json",
    "openjiuwen": "openjiuwen.json",
    "common": None,
}

EXPECTED_FILES = {
    "openclaw": ["openclaw.json", "config.yaml", "user_proxy_model.json"],
    "hermes": ["hermes_config.yaml", "config.yaml", "user_proxy_model.json"],
    "claude-code": ["cc_settings.json", "config.yaml", "user_proxy_model.json"],
    "openjiuwen": ["openjiuwen.json", "config.yaml", "user_proxy_model.json"],
    "common": ["config.yaml", "user_proxy_model.json"],
}

DEFAULT_VERSION = "默认"


def _config_dir(harness_type: str, version: str) -> str:
    if version == DEFAULT_VERSION:
        return settings.SETTINGS_DIR
    return os.path.join(settings.SETTINGS_DIR, harness_type, version)


def _config_dir_by_id(config_id: int) -> str | None:
    with get_connection() as conn:
        row = conn.execute("SELECT harness_type, version FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        return None
    return _config_dir(row["harness_type"], row["version"])


def _scan_files_dir(d: str, harness_type: str) -> list[str]:
    if not os.path.isdir(d):
        return []
    expected = set(EXPECTED_FILES.get(harness_type, EXPECTED_FILES["common"]))
    return sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and f in expected)


def _build_info(row) -> HarnessConfigInfo:
    info = dict(row)
    info.setdefault("config_files_json", "[]")
    info.setdefault("is_default", 0)
    info.setdefault("obs_source_path", "")
    info.setdefault("obs_harness_path", "")
    info.setdefault("obs_task_path", "")
    info.setdefault("obs_proxy_path", "")
    info.setdefault("created_by", "")
    info.setdefault("updated_at", "")
    return HarnessConfigInfo(**info)


def _file_type_for(filename: str, harness_type: str) -> str:
    harness_file = HARNESS_FILES.get(harness_type)
    if harness_file and filename == harness_file:
        return "harness"
    if filename == "config.yaml":
        return "task"
    if filename == "user_proxy_model.json":
        return "proxy"
    return "unknown"


def _obs_path_for_type(row: dict, file_type: str) -> str:
    if file_type == "harness":
        return row.get("obs_harness_path") or ""
    elif file_type == "task":
        return row.get("obs_task_path") or ""
    elif file_type == "proxy":
        return row.get("obs_proxy_path") or ""
    return ""


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


def _update_files_json(conn, config_id: int, harness_type: str, version: str):
    d = _config_dir(harness_type, version)
    file_list = _scan_files_dir(d, harness_type)
    conn.execute(
        "UPDATE harness_configs SET config_files_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(file_list), datetime.now().isoformat(), config_id),
    )


# ============================================================================
# CRUD
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
    return [_build_info(r) for r in rows]


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
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    return _build_info(row)


@router.post("", response_model=HarnessConfigInfo)
async def create_harness_config(req: HarnessConfigCreate, user: dict = Depends(require_operator)):
    name = f"{req.harness_type}_{req.version}"

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND version = ?",
            (req.harness_type, req.version),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"版本 {req.version} 已存在")

        cursor = conn.execute(
            """INSERT INTO harness_configs
               (name, harness_type, version, description, obs_harness_path, obs_task_path, obs_proxy_path, created_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, req.harness_type, req.version, req.description,
             req.obs_harness_path, req.obs_task_path, req.obs_proxy_path,
             user["username"], datetime.now().isoformat()),
        )
        config_id = cursor.lastrowid

        d = _config_dir(req.harness_type, req.version)
        os.makedirs(d, exist_ok=True)

        if req.obs_harness_path:
            try:
                await _pull_obs_file(req.obs_harness_path, d)
            except Exception:
                pass
        if req.obs_task_path:
            try:
                await _pull_obs_file(req.obs_task_path, d)
            except Exception:
                pass
        if req.obs_proxy_path:
            try:
                await _pull_obs_file(req.obs_proxy_path, d)
            except Exception:
                pass

        _update_files_json(conn, config_id, req.harness_type, req.version)

        existing_default = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND is_default = 1",
            (req.harness_type,),
        ).fetchone()
        if not existing_default:
            conn.execute("UPDATE harness_configs SET is_default = 1 WHERE id = ?", (config_id,))

        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        return _build_info(row)


@router.put("/{config_id}", response_model=HarnessConfigInfo)
def update_harness_config(config_id: int, req: HarnessConfigUpdate, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="配置不存在")
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if updates:
            updates["updated_at"] = datetime.now().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE harness_configs SET {set_clause} WHERE id = ?",
                (*updates.values(), config_id),
            )
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        return _build_info(row)


@router.delete("/{config_id}")
def delete_harness_config(config_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="配置不存在")
        if row["version"] == DEFAULT_VERSION:
            raise HTTPException(status_code=400, detail="默认配置不允许删除")
        conn.execute("DELETE FROM harness_configs WHERE id = ?", (config_id,))

    d = _config_dir(row["harness_type"], row["version"])
    if os.path.isdir(d) and d != settings.SETTINGS_DIR:
        shutil.rmtree(d)
    return {"detail": "已删除"}


@router.put("/{config_id}/set-default")
def set_default(config_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="配置不存在")
        conn.execute(
            "UPDATE harness_configs SET is_default = 0 WHERE harness_type = ?",
            (row["harness_type"],),
        )
        conn.execute("UPDATE harness_configs SET is_default = 1 WHERE id = ?", (config_id,))
    return {"detail": "已设为默认"}


# ============================================================================
# File management
# ============================================================================

@router.get("/{config_id}/files")
def list_files(config_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")

    harness_type = row["harness_type"]
    d = _config_dir(harness_type, row["version"])
    expected = EXPECTED_FILES.get(harness_type, EXPECTED_FILES["common"])

    files = []
    for fname in expected:
        fpath = os.path.join(d, fname)
        exists = os.path.isfile(fpath)
        file_type = "yaml" if fname.endswith((".yaml", ".yml")) else "json"
        ft = _file_type_for(fname, harness_type)
        obs_path = _obs_path_for_type(dict(row), ft)
        files.append({
            "name": fname,
            "type": file_type,
            "category": ft,
            "exists": exists,
            "size": os.path.getsize(fpath) if exists else 0,
            "obs_path": obs_path,
        })
    return {"config_id": config_id, "harness_type": harness_type, "files": files}


@router.get("/{config_id}/files/{filename}")
def get_file(config_id: int, filename: str, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")

    fpath = os.path.join(_config_dir(row["harness_type"], row["version"]), filename)
    if not os.path.isfile(fpath):
        raise HTTPException(status_code=404, detail="文件不存在")

    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()

    file_type = "yaml" if filename.endswith((".yaml", ".yml")) else "json"
    return {"filename": filename, "type": file_type, "content": content}


@router.put("/{config_id}/files/{filename}")
def save_file(config_id: int, filename: str, content: str = Body(..., media_type="text/plain"), user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")

    d = _config_dir(row["harness_type"], row["version"])
    os.makedirs(d, exist_ok=True)
    fpath = os.path.join(d, filename)

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)

    _update_files_json(conn, config_id, row["harness_type"], row["version"])
    return {"detail": "已保存", "filename": filename}


class InitFileRequest(BaseModel):
    filename: str


@router.post("/{config_id}/init-file")
def init_file(config_id: int, req: InitFileRequest, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")

    src = os.path.join(settings.SETTINGS_DIR, req.filename)
    if not os.path.isfile(src):
        raise HTTPException(status_code=404, detail=f"模板文件不存在: {req.filename}")

    d = _config_dir(row["harness_type"], row["version"])
    os.makedirs(d, exist_ok=True)
    shutil.copy2(src, os.path.join(d, req.filename))

    _update_files_json(conn, config_id, row["harness_type"], row["version"])
    return {"detail": f"已从模板初始化 {req.filename}"}


class PullObsRequest(BaseModel):
    file_type: str


@router.post("/{config_id}/pull-obs")
async def pull_obs(config_id: int, req: PullObsRequest, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")

    obs_path = _obs_path_for_type(dict(row), req.file_type)
    if not obs_path:
        raise HTTPException(status_code=400, detail="未配置该类型的 OBS 路径")

    d = _config_dir(row["harness_type"], row["version"])
    os.makedirs(d, exist_ok=True)

    await _pull_obs_file(obs_path, d)

    _update_files_json(conn, config_id, row["harness_type"], row["version"])
    return {"detail": "已从 OBS 拉取更新"}


class CopyRequest(BaseModel):
    version: str


@router.post("/{config_id}/copy", response_model=HarnessConfigInfo)
def copy_harness_config(config_id: int, body: CopyRequest, user: dict = Depends(require_operator)):
    new_version = body.version.strip()
    if not new_version:
        raise HTTPException(status_code=400, detail="版本号不能为空")
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (config_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="配置不存在")
        src = dict(row)

        dup = conn.execute(
            "SELECT id FROM harness_configs WHERE harness_type = ? AND version = ?",
            (src["harness_type"], new_version),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=400, detail=f"版本 \"{new_version}\" 已存在")

        new_name = f"{src['harness_type']}_{new_version}"
        cursor = conn.execute(
            """INSERT INTO harness_configs
               (name, harness_type, version, description, obs_harness_path, obs_task_path, obs_proxy_path, created_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_name, src["harness_type"], new_version,
             src.get("description", ""), src.get("obs_harness_path", ""),
             src.get("obs_task_path", ""), src.get("obs_proxy_path", ""),
             user["username"], datetime.now().isoformat()),
        )
        new_id = cursor.lastrowid

        src_dir = _config_dir(src["harness_type"], src["version"])
        dst_dir = _config_dir(src["harness_type"], new_version)
        os.makedirs(dst_dir, exist_ok=True)
        for fname in EXPECTED_FILES.get(src["harness_type"], []):
            src_file = os.path.join(src_dir, fname)
            if os.path.isfile(src_file):
                shutil.copy2(src_file, os.path.join(dst_dir, fname))

        _update_files_json(conn, new_id, src["harness_type"], new_version)

        new_row = conn.execute("SELECT * FROM harness_configs WHERE id = ?", (new_id,)).fetchone()
        return _build_info(new_row)


# ============================================================================
# Auto-register defaults
# ============================================================================

def ensure_defaults():
    """Auto-register default harness configs from settings/ for each type."""
    with get_connection() as conn:
        for htype in ["openclaw", "hermes", "claude-code", "openjiuwen"]:
            existing = conn.execute(
                "SELECT id FROM harness_configs WHERE harness_type = ? AND version = ?",
                (htype, DEFAULT_VERSION),
            ).fetchone()
            if existing:
                # Refresh file list
                config_id = existing["id"]
                file_list = _scan_files_dir(settings.SETTINGS_DIR, htype)
                conn.execute(
                    "UPDATE harness_configs SET config_files_json = ? WHERE id = ?",
                    (json.dumps(file_list), config_id),
                )
                continue

            name = f"{htype}_{DEFAULT_VERSION}"
            cursor = conn.execute(
                """INSERT INTO harness_configs
                   (name, harness_type, version, description, is_default, created_by, updated_at)
                   VALUES (?, ?, ?, '系统默认配置 (settings/)', 1, 'system', ?)""",
                (name, htype, DEFAULT_VERSION, datetime.now().isoformat()),
            )
            config_id = cursor.lastrowid

            file_list = _scan_files_dir(settings.SETTINGS_DIR, htype)
            conn.execute(
                "UPDATE harness_configs SET config_files_json = ? WHERE id = ?",
                (json.dumps(file_list), config_id),
            )
