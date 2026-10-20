"""沙箱配置 —— 文件权威，DB 只存索引。

架构（用户定稿）：
- 权威内容在 platform/settings/sandbox/*.yaml（从 configs/*.yaml 抽出的沙箱相关段：
  global + sandbox + sandbox_namespace + s3）。
- 菜单里的每一条「沙箱配置」= 一个派生文件 settings/sandbox/<type>_<id>.yaml。
  由对应模板（linux.yaml / windows.yaml）复制而来，并按界面上的字段改写。
- 创建任务时，把该派生文件的这些段整段替换进实例的 config.yaml。

DB 的 sandbox_configs 表只是索引：id / name / type / sandbox_key / file / is_default / enabled。
不存资源、环境变量等业务字段——那些以文件为准，避免两处真相。
"""
import json
import os
import re
import shutil
from collections.abc import MutableMapping
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from omegaconf import OmegaConf

from ..core.config import settings
from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.sandbox_config import (
    SandboxConfigCreate, SandboxConfigUpdate, SandboxConfigInfo, SandboxFileContent,
)
from . import resource_pools

router = APIRouter(prefix="/api/sandbox-configs", tags=["sandbox-configs"])

# sandbox_type → 模板文件名 与 config.yaml 顶层 sandbox.<key>
_TEMPLATES = {"linux": ("linux.yaml", "x86_cpu"), "windows": ("windows.yaml", "windows")}
_ALLOWED_TYPES = set(_TEMPLATES)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")
# 资源池 = sandbox_namespace，遵循 K8s namespace 命名规则（RFC 1123 label）
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# 派生文件/模板所在目录
SANDBOX_DIR = os.path.join(settings.SETTINGS_DIR, "sandbox")


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="配置名称不能为空")
    if not _NAME_RE.match(name) or ".." in name:
        raise HTTPException(
            status_code=400,
            detail="配置名称只能包含字母、数字、点、下划线、连字符，且不能以点开头",
        )
    return name


def _validate_namespace(ns: str) -> str:
    """资源池 = sandbox_namespace，写进配置后会用于建沙箱，按 K8s 规则校验。

    除了语法，还要求该 namespace 已在 settings/sandbox/resource_pools.yaml 登记：
    未登记的池不会被首页看板计入总量，允许保存会让「已用」静默超过「总量」。
    """
    ns = (ns or "").strip()
    if not ns:
        raise HTTPException(status_code=400, detail="请选择资源池（sandbox_namespace 不能为空）")
    if len(ns) > 63 or not _NAMESPACE_RE.match(ns):
        raise HTTPException(
            status_code=400,
            detail="资源池（sandbox_namespace）只能包含小写字母、数字和连字符，"
                   "且必须以字母或数字开头结尾、不超过 63 个字符",
        )
    registered = resource_pools.load_pools()
    if ns not in registered:
        available = "、".join(sorted(registered)) or "（当前没有任何资源池，请先配置 resource_pools.yaml）"
        raise HTTPException(
            status_code=400,
            detail=f"资源池 {ns} 未在 settings/sandbox/resource_pools.yaml 登记。可用资源池：{available}",
        )
    return ns


def _template_path(sandbox_type: str) -> str:
    if sandbox_type not in _TEMPLATES:
        raise HTTPException(status_code=400, detail=f"沙箱类型必须是 {sorted(_ALLOWED_TYPES)} 之一")
    return os.path.join(SANDBOX_DIR, _TEMPLATES[sandbox_type][0])


def _derived_path(sandbox_type: str, config_id: int) -> str:
    """派生文件名：<type>_<id>.yaml，如 windows_3.yaml。"""
    return os.path.join(SANDBOX_DIR, f"{sandbox_type}_{config_id}.yaml")


def _file_for(row: dict) -> str:
    """该行对应的派生文件绝对路径，优先用 DB 记的 file，缺失时按命名规则推导。"""
    fn = (row.get("file") or "").strip()
    if fn:
        return os.path.join(SANDBOX_DIR, os.path.basename(fn))
    return _derived_path(row["sandbox_type"], row["id"])


def _load_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"沙箱文件不存在: {path}")
    try:
        return OmegaConf.to_container(OmegaConf.load(path), resolve=True) or {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"沙箱文件解析失败: {e}")


def _dump_yaml(cfg: dict, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(OmegaConf.to_yaml(OmegaConf.create(cfg), resolve=True))


def _set_inner_image(inner: dict, image: str) -> None:
    """写 sandbox.<key>.sandbox.image，并同步 boot_source.ref。

    Windows 配置把同一个镜像地址存了两份（image + boot_source.ref），控制面若读
    boot_source，只改 image 就会静默启动旧镜像。仅在 boot_source.type == image 时同步，
    snapshot/其他类型保持不变。

    注意：apply_image() 传进来的 inner 是 OmegaConf 的 DictConfig，不是 dict，
    所以这里用 MutableMapping 判断（isinstance(..., dict) 对 DictConfig 为 False）。
    """
    if not isinstance(inner, MutableMapping):
        return
    inner["image"] = image
    bs = inner.get("boot_source")
    if isinstance(bs, MutableMapping) and bs.get("type") == "image":
        bs["ref"] = image


# ============================================================================
# 读取：从文件里抽出界面需要的字段（列表 + 表单回填共用）
# ============================================================================

def _extract_summary(cfg: dict, sandbox_key: str) -> dict:
    """把派生/模板文件解析成界面用的扁平字段。"""
    sb = (cfg.get("sandbox") or {}).get(sandbox_key) or {}
    inner = sb.get("sandbox") or {}
    res = inner.get("resources") or {}
    if "requests" in res or "limits" in res:
        cpu_req = (res.get("requests") or {}).get("cpu", "2")
        cpu_lim = (res.get("limits") or {}).get("cpu", "3")
        mem_req = (res.get("requests") or {}).get("memory", "6Gi")
        mem_lim = (res.get("limits") or {}).get("memory", "8Gi")
    else:
        cpu_req, cpu_lim = res.get("cpu_request", "2"), res.get("cpu_limit", "3")
        mem_req, mem_lim = res.get("memory_request", "6Gi"), res.get("memory_limit", "8Gi")

    gl = cfg.get("global") or {}
    s3 = cfg.get("s3") or {}
    return {
        "controlplane_base_url": gl.get("controlplane_base_url", ""),
        "controlplane_api_key": gl.get("api_key", ""),
        "sandbox_namespace": cfg.get("sandbox_namespace", "omni-env-default-worker"),
        "cpu_request": str(cpu_req),
        "cpu_limit": str(cpu_lim),
        "memory_request": str(mem_req),
        "memory_limit": str(mem_lim),
        "env_json": json.dumps(inner.get("env") or [], ensure_ascii=False),
        "ttl_seconds": int(sb.get("max_ttl_second", 8000)),
        "lease_ttl_seconds": int(sb.get("lease_ttl_seconds", 3600)),
        "wait_timeout_seconds": int(sb.get("wait_timeout_seconds", 3600)),
        "image": inner.get("image", ""),
        "s3_uploader_script": s3.get("s3_uploader_script", ""),
        "s3_download_script": s3.get("s3_download_script", s3.get("s3_uploader_script", "")),
    }


def _apply_summary(cfg: dict, sandbox_key: str, data: dict) -> dict:
    """把界面字段写回 cfg（保留文件里其余结构与键序）。"""
    sb = cfg.setdefault("sandbox", {}).setdefault(sandbox_key, {})
    inner = sb.setdefault("sandbox", {})
    res = inner.setdefault("resources", {})

    if "lease_ttl_seconds" in data:
        sb["lease_ttl_seconds"] = data["lease_ttl_seconds"]
    if "ttl_seconds" in data:
        sb["max_ttl_second"] = data["ttl_seconds"]
    if "wait_timeout_seconds" in data:
        sb["wait_timeout_seconds"] = data["wait_timeout_seconds"]

    if "image" in data and data["image"]:
        _set_inner_image(inner, data["image"])
    if "env_json" in data and data["env_json"] is not None:
        inner["env"] = json.loads(data["env_json"] or "[]")

    if "requests" in res or "limits" in res:
        for src, dst in (("cpu_request", "cpu"), ("memory_request", "memory")):
            if src in data:
                res.setdefault("requests", {})[dst] = str(data[src])
        for src, dst in (("cpu_limit", "cpu"), ("memory_limit", "memory")):
            if src in data:
                res.setdefault("limits", {})[dst] = str(data[src])
    else:
        for k in ("cpu_request", "cpu_limit", "memory_request", "memory_limit"):
            if k in data:
                res[k] = str(data[k])

    if "controlplane_base_url" in data or "controlplane_api_key" in data:
        gl = cfg.setdefault("global", {})
        if "controlplane_base_url" in data:
            gl["controlplane_base_url"] = data["controlplane_base_url"]
        if "controlplane_api_key" in data:
            gl["api_key"] = data["controlplane_api_key"]
    if "sandbox_namespace" in data:
        cfg["sandbox_namespace"] = data["sandbox_namespace"]
    if "s3_uploader_script" in data and data["s3_uploader_script"]:
        s3 = cfg.setdefault("s3", {})
        s3["s3_uploader_script"] = data["s3_uploader_script"]
        s3["s3_download_script"] = data.get("s3_download_script") or data["s3_uploader_script"]
    return cfg


# ============================================================================
# 任务侧：把派生文件的沙箱段整段替换进实例 config.yaml
# ============================================================================

# 从派生文件整段搬进实例 config.yaml 的顶层键，**顺序即写入 config.yaml 的顺序**：
#   前 5 个 = 沙箱配置；run_config = 运行任务配置，始终排在最后。
CONFIG_SECTION_ORDER = ("global", "sandbox", "sandbox_id_prefix", "sandbox_namespace", "s3", "run_config")
SANDBOX_SECTIONS = CONFIG_SECTION_ORDER[:5]          # 沙箱配置部分

# 排在 s3 之后、run_config 之前（平台写入的元字段）
_AFTER_SANDBOX = ("sandbox_active_key",)


def reorder_sections(base, sections: dict = None):
    """按 CONFIG_SECTION_ORDER 重建 base 的顶层顺序，并用 sections 覆盖对应顶层键。

    base 里原有的键全部保留（含 run_config），sections 只做覆盖、不做删除。
    未列在 CONFIG_SECTION_ORDER 里的其它顶层键原样保留。
    """
    merged = {k: base[k] for k in base.keys()}     # 以 base 为底
    if sections:
        merged.update(sections)                    # 用派生文件的段覆盖
    # 未列出的键：平台元字段排在 s3 后，其余夹在 run_config 之前
    extras = {k: v for k, v in merged.items()
              if k not in CONFIG_SECTION_ORDER and k not in _AFTER_SANDBOX}
    ordered = {}
    for k in CONFIG_SECTION_ORDER:
        if k == "run_config":
            ordered.update(extras)                 # 夹带的其它键落在 run_config 之前
        if k in merged:
            ordered[k] = merged[k]
        if k == "s3":
            for a in _AFTER_SANDBOX:               # sandbox_active_key 紧跟 s3
                if a in merged:
                    ordered[a] = merged[a]
    return ordered


def rewrite_config_sections(base, sections: dict = None):
    """就地重建 base 的顶层键与顺序（先算好再删，避免读到中间态）。"""
    ordered = reorder_sections(base, sections)
    for k in list(base.keys()):
        del base[k]
    for k, v in ordered.items():
        base[k] = v
    return base


def apply_sandbox_to_config(base, row: dict):
    """把沙箱配置对应的派生文件里的沙箱段，整段替换进 base（实例 config.yaml）。

    base 是 OmegaConf 的 DictConfig（已从 harness 模板加载）。
    返回实际使用的 sandbox_key。

    写入规则：先按 CONFIG_SECTION_ORDER 重建顶层顺序（global/sandbox/sandbox_id_prefix/
    sandbox_namespace/s3 在前，run_config 在最后），再整体替换 base 的顶层键。

    注意：镜像**不在这里处理**。镜像独立于沙箱配置，由 apply_image() 统一写当前实例。
    """
    path = _file_for(row)
    cfg = _load_yaml(path)
    sandbox_key = row["sandbox_key"]

    if sandbox_key not in (cfg.get("sandbox") or {}):
        raise HTTPException(
            status_code=400,
            detail=f"沙箱文件 {os.path.basename(path)} 里没有 sandbox.{sandbox_key} 块",
        )

    sections = {k: cfg[k] for k in SANDBOX_SECTIONS if k in cfg}
    # sandbox_id_prefix 由任务表单决定（用户定稿），派生文件里的值不参与
    sections.pop("sandbox_id_prefix", None)

    base.sandbox_active_key = sandbox_key      # 先就位，重排时才会被放到 s3 之后
    rewrite_config_sections(base, sections)
    return sandbox_key


def active_sandbox_key(base) -> Optional[str]:
    """当前 base 里生效的 sandbox key：优先 sandbox_active_key，其次 sandbox 下的第一个键。"""
    key = base.get("sandbox_active_key")
    if key:
        return key
    sb = base.get("sandbox")
    return next(iter(sb.keys()), None) if sb else None


def apply_image(base, image: str) -> Optional[str]:
    """把任务页选的镜像写进 base 里生效的 sandbox.<key>.sandbox.image。

    镜像独立于沙箱配置：无论有没有选沙箱配置，都只改写**当前实例**的 config.yaml，
    不动任何模板/派生文件。返回实际写入的 sandbox key（无处可写时返回 None）。
    """
    image = (image or "").strip()
    if not image:
        return None
    key = active_sandbox_key(base)
    if not key:
        return None
    sb = base.get("sandbox")
    if not sb or sb.get(key) is None:
        return None
    sb[key].setdefault("sandbox", {})
    _set_inner_image(sb[key]["sandbox"], image)
    return key


def _build_info(row) -> SandboxConfigInfo:
    """索引行 + 从派生文件读出的字段，合成前端要的完整对象。"""
    info = dict(row)
    path = _file_for(info)
    try:
        cfg = _load_yaml(path)
        info.update(_extract_summary(cfg, info["sandbox_key"]))
        info["file_exists"] = True
    except HTTPException:
        info["file_exists"] = False
    info.setdefault("env_json", "[]")
    info.setdefault("is_default", 0)
    info.setdefault("enabled", 1)
    info.setdefault("description", "")
    info.setdefault("created_by", "")
    info.setdefault("created_at", "")
    info.setdefault("updated_at", "")
    return SandboxConfigInfo(**info)


def _get_row_or_404(config_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sandbox_configs WHERE id = ?", (config_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="沙箱配置不存在")
    return dict(row)


def _is_referenced(config_id: int) -> bool:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, create_params FROM task_instances WHERE create_params IS NOT NULL"
        ).fetchall()
    for r in rows:
        try:
            params = json.loads(r["create_params"] or "{}")
        except Exception:
            continue
        if params.get("sandbox_config_id") == config_id:
            return True
    return False


# ============================================================================
# CRUD
# ============================================================================

@router.get("", response_model=list[SandboxConfigInfo])
def list_sandbox_configs(
    sandbox_type: Optional[str] = Query(default=None),
    enabled_only: bool = Query(default=False),
    user: dict = Depends(get_current_user),
):
    sql = "SELECT * FROM sandbox_configs WHERE 1=1"
    params: list = []
    if sandbox_type:
        sql += " AND sandbox_type = ?"
        params.append(sandbox_type)
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY is_default DESC, sandbox_type ASC, id ASC"
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_build_info(r) for r in rows]


@router.get("/{config_id}", response_model=SandboxConfigInfo)
def get_sandbox_config(config_id: int, user: dict = Depends(get_current_user)):
    return _build_info(_get_row_or_404(config_id))


@router.post("", response_model=SandboxConfigInfo)
def create_sandbox_config(req: SandboxConfigCreate, user: dict = Depends(require_operator)):
    name = _validate_name(req.name)
    req.sandbox_namespace = _validate_namespace(req.sandbox_namespace)
    tpl_path = _template_path(req.sandbox_type)
    sandbox_key = req.sandbox_key or _TEMPLATES[req.sandbox_type][1]
    now = datetime.now().isoformat()

    with get_connection() as conn:
        dup = conn.execute("SELECT id FROM sandbox_configs WHERE name = ?", (name,)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"配置名称 \"{name}\" 已存在")
        has_default = conn.execute(
            "SELECT id FROM sandbox_configs WHERE sandbox_type = ? AND is_default = 1",
            (req.sandbox_type,),
        ).fetchone()
        cursor = conn.execute(
            """INSERT INTO sandbox_configs
               (name, sandbox_type, sandbox_key, file, is_default, enabled,
                description, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (name, req.sandbox_type, sandbox_key, "", 0 if has_default else 1, 1,
             req.description, user["username"], now, now),
        )
        new_id = cursor.lastrowid

    # 派生文件：复制模板，再按表单字段改写
    dst = _derived_path(req.sandbox_type, new_id)
    if not os.path.isfile(tpl_path):
        with get_connection() as conn:
            conn.execute("DELETE FROM sandbox_configs WHERE id = ?", (new_id,))
        # 不把服务端绝对路径暴露给前端
        raise HTTPException(
            status_code=400,
            detail=f"{req.sandbox_type} 沙箱模板缺失，请联系管理员补齐 settings/sandbox/ 下的模板文件",
        )
    shutil.copy2(tpl_path, dst)

    data = req.model_dump(exclude={"name", "sandbox_type", "sandbox_key", "description"})
    cfg = _apply_summary(_load_yaml(dst), sandbox_key, data)
    _dump_yaml(cfg, dst)

    with get_connection() as conn:
        conn.execute(
            "UPDATE sandbox_configs SET file = ?, updated_at = ? WHERE id = ?",
            (os.path.basename(dst), now, new_id),
        )
    return _build_info(_get_row_or_404(new_id))


@router.put("/{config_id}", response_model=SandboxConfigInfo)
def update_sandbox_config(
    config_id: int,
    req: SandboxConfigUpdate,
    user: dict = Depends(require_operator),
):
    row = _get_row_or_404(config_id)
    data = req.model_dump(exclude_none=True)
    if not data:
        return _build_info(row)

    if "name" in data:
        data["name"] = _validate_name(data["name"])
        with get_connection() as conn:
            dup = conn.execute(
                "SELECT id FROM sandbox_configs WHERE name = ? AND id != ?",
                (data["name"], config_id),
            ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"配置名称 \"{data['name']}\" 已存在")

    if "sandbox_namespace" in data:
        data["sandbox_namespace"] = _validate_namespace(data["sandbox_namespace"])

    # 请求 ≤ 限制（前端已拦，后端兜底）
    for req_f, lim_f, label in [("cpu_request", "cpu_limit", "CPU"),
                                ("memory_request", "memory_limit", "内存")]:
        r = data.get(req_f, row.get(req_f))
        l = data.get(lim_f, row.get(lim_f))
        if r is not None and l is not None and _to_quantity(r) > _to_quantity(l):
            raise HTTPException(status_code=400, detail=f"{label} 请求不得大于限制")

    # 业务字段写进派生文件
    file_data = {k: v for k, v in data.items() if k not in ("name", "description")}
    path = _file_for(row)
    cfg = _apply_summary(_load_yaml(path), row["sandbox_key"], file_data)
    _dump_yaml(cfg, path)

    # 索引字段写 DB
    sets, vals = [], []
    if "name" in data:
        sets.append("name = ?"); vals.append(data["name"])
    if "description" in data:
        sets.append("description = ?"); vals.append(data["description"])
    sets.append("updated_at = ?"); vals.append(datetime.now().isoformat())
    with get_connection() as conn:
        conn.execute(f"UPDATE sandbox_configs SET {', '.join(sets)} WHERE id = ?", (*vals, config_id))
    return _build_info(_get_row_or_404(config_id))


def _to_quantity(v) -> float:
    s = str(v or "").strip()
    m = re.match(r"^([\d.]+)\s*([A-Za-z]*)$", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2).lower()
    return num * {"gi": 1024, "mi": 1, "g": 1000, "m": 1, "": 1}.get(unit, 1)


@router.delete("/{config_id}")
def delete_sandbox_config(config_id: int, user: dict = Depends(require_operator)):
    row = _get_row_or_404(config_id)
    if row["is_default"]:
        raise HTTPException(status_code=400, detail="默认配置不可删除，请先设为其他配置为默认")
    if _is_referenced(config_id):
        raise HTTPException(status_code=400, detail="该配置已被任务实例引用，无法删除")
    path = _file_for(row)
    if os.path.isfile(path):
        os.remove(path)
    with get_connection() as conn:
        conn.execute("DELETE FROM sandbox_configs WHERE id = ?", (config_id,))
    return {"detail": "已删除"}


@router.post("/{config_id}/copy", response_model=SandboxConfigInfo)
def copy_sandbox_config(
    config_id: int,
    body: dict = Body(default={}),
    user: dict = Depends(require_operator),
):
    src = _get_row_or_404(config_id)
    new_name = _validate_name(body.get("name") or f"{src['name']}-copy")
    now = datetime.now().isoformat()

    with get_connection() as conn:
        dup = conn.execute("SELECT id FROM sandbox_configs WHERE name = ?", (new_name,)).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail=f"配置名称 \"{new_name}\" 已存在")
        cursor = conn.execute(
            """INSERT INTO sandbox_configs
               (name, sandbox_type, sandbox_key, file, is_default, enabled,
                description, created_by, created_at, updated_at)
               VALUES (?,?,?,?,0,?,?,?,?,?)""",
            (new_name, src["sandbox_type"], src["sandbox_key"], "", src["enabled"],
             src["description"], user["username"], now, now),
        )
        new_id = cursor.lastrowid

    dst = _derived_path(src["sandbox_type"], new_id)
    src_path = _file_for(src)
    if os.path.isfile(src_path):
        shutil.copy2(src_path, dst)          # 连内容一起复制
    else:
        shutil.copy2(_template_path(src["sandbox_type"]), dst)
    with get_connection() as conn:
        conn.execute("UPDATE sandbox_configs SET file = ? WHERE id = ?", (os.path.basename(dst), new_id))
    return _build_info(_get_row_or_404(new_id))


@router.put("/{config_id}/set-default")
def set_default(config_id: int, user: dict = Depends(require_operator)):
    row = _get_row_or_404(config_id)
    with get_connection() as conn:
        conn.execute("UPDATE sandbox_configs SET is_default = 0 WHERE sandbox_type = ?",
                     (row["sandbox_type"],))
        conn.execute("UPDATE sandbox_configs SET is_default = 1, updated_at = ? WHERE id = ?",
                     (datetime.now().isoformat(), config_id))
    return {"detail": "已设为默认"}


@router.put("/{config_id}/enabled")
def set_enabled(config_id: int, body: dict = Body(...), user: dict = Depends(require_operator)):
    row = _get_row_or_404(config_id)
    enabled = 1 if body.get("enabled", True) else 0
    if not enabled and row["is_default"]:
        raise HTTPException(status_code=400, detail="默认配置不可停用，请先切换默认配置")
    with get_connection() as conn:
        conn.execute("UPDATE sandbox_configs SET enabled = ?, updated_at = ? WHERE id = ?",
                     (enabled, datetime.now().isoformat(), config_id))
    return {"detail": "已启用" if enabled else "已停用"}


# ============================================================================
# 文件内容：直接读写派生文件（文件权威）
# ============================================================================

@router.get("/{config_id}/file", response_model=SandboxFileContent)
def get_sandbox_file(config_id: int, user: dict = Depends(get_current_user)):
    row = _get_row_or_404(config_id)
    path = _file_for(row)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="派生文件不存在")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return SandboxFileContent(
        filename=os.path.basename(path), path=path, content=content,
        template=os.path.basename(_template_path(row["sandbox_type"])),
    )


@router.put("/{config_id}/file")
def save_sandbox_file(
    config_id: int,
    content: str = Body(..., media_type="text/plain"),
    user: dict = Depends(require_operator),
):
    row = _get_row_or_404(config_id)
    # 先校验能解析，避免存进一个坏文件（会污染后续建任务）
    try:
        import yaml as _yaml
        parsed = _yaml.safe_load(content)
        if not isinstance(parsed, dict) or "sandbox" not in parsed:
            raise ValueError("必须是含顶层 sandbox: 的 YAML 映射")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败: {e}")
    path = _file_for(row)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    with get_connection() as conn:
        conn.execute("UPDATE sandbox_configs SET updated_at = ? WHERE id = ?",
                     (datetime.now().isoformat(), config_id))
    return {"detail": "已保存"}


@router.post("/{config_id}/reset")
def reset_sandbox_file(config_id: int, user: dict = Depends(require_operator)):
    """从模板重新派生（丢弃文件上的手改）。

    资源池（sandbox_namespace）是用户在表单里显式选的归属，不属于「手改」，
    重置后保留原值，否则配置会静默跳回模板里的默认池。
    """
    row = _get_row_or_404(config_id)
    tpl = _template_path(row["sandbox_type"])
    dst = _file_for(row)
    if not os.path.isfile(tpl):
        raise HTTPException(
            status_code=400,
            detail=f"{row['sandbox_type']} 沙箱模板缺失，请联系管理员",
        )
    old_ns = ""
    try:
        old_ns = (_load_yaml(dst) or {}).get("sandbox_namespace") or ""
    except HTTPException:
        pass  # 原文件已损坏时没有可保留的池，按模板默认走
    shutil.copy2(tpl, dst)
    if old_ns:
        cfg = _load_yaml(dst)
        cfg["sandbox_namespace"] = old_ns
        _dump_yaml(cfg, dst)
    with get_connection() as conn:
        conn.execute("UPDATE sandbox_configs SET updated_at = ? WHERE id = ?",
                     (datetime.now().isoformat(), config_id))
    return {"detail": f"已从模板 {os.path.basename(tpl)} 重置"}


# ============================================================================
# 种子：首次启动时按模板派生两条默认配置
# ============================================================================

def ensure_defaults():
    """首次启动（表为空）时，从 settings/sandbox/{linux,windows}.yaml 派生默认配置。"""
    with get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM sandbox_configs").fetchone()
        if n and n["n"]:
            return

    for sandbox_type, (tpl_name, key) in _TEMPLATES.items():
        tpl = os.path.join(SANDBOX_DIR, tpl_name)
        if not os.path.isfile(tpl):
            continue
        name = f"default-{sandbox_type}"
        now = datetime.now().isoformat()
        with get_connection() as conn:
            dup = conn.execute("SELECT id FROM sandbox_configs WHERE name = ?", (name,)).fetchone()
            if dup:
                continue
            cur = conn.execute(
                """INSERT INTO sandbox_configs
                   (name, sandbox_type, sandbox_key, file, is_default, enabled,
                    description, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,1,1,?,?,?,?)""",
                (name, sandbox_type, key, "", f"从 settings/sandbox/{tpl_name} 派生",
                 "system", now, now),
            )
            new_id = cur.lastrowid
        dst = _derived_path(sandbox_type, new_id)
        shutil.copy2(tpl, dst)
        with get_connection() as conn:
            conn.execute("UPDATE sandbox_configs SET file = ? WHERE id = ?",
                         (os.path.basename(dst), new_id))
