import asyncio
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.database import get_connection
from ..core.security import get_current_user, require_admin
from ..models.registration import (
    RegistrationCreate,
    RegistrationUpdate,
    RegistrationInfo,
    RegistrationVersionInfo,
)
from ..services import dataset_files as dfs
from ..services.obs_path import strip_bucket
from ..services.sample_count import count_sample, dataset_cache_dir
router = APIRouter(prefix="/api/registrations", tags=["registrations"])

# 进入版本快照的字段：可编辑、非机写、非密钥。
#
# 刻意排除三类：
#   - api_key / eval_config_api_key —— 密钥。历史表会长期留存，不该成为第二份明文密钥库。
#   - data_total / count_status / count_error / counted_at —— sample_count 后台任务
#     直接 UPDATE 回写，若纳入快照，每次重新统计都会凭空多一个「版本」，那不是用户改的。
#   - status / linked_instance_id / export_path_obs / traj_path —— 运行时态，不是配置。
SNAPSHOT_FIELDS = (
    "task_name", "requester", "task_path_obs", "skill_dir_obs", "agent_dir_obs",
    "user_folder_obs", "model_name", "eval_model_name", "user_proxy_model_name",
    "harness_type", "base_url", "eval_config_model", "eval_config_base_url",
    "eval_config_api", "default_skills", "traj_save_path", "remark",
)

# 字段中文名。与前端 TaskRegister.vue / 详情弹窗用的是同一套叫法，
# 后端用它拼版本说明的默认文案（「修改 用户Config目录」）。
FIELD_LABELS = {
    "task_name": "任务名称",
    "requester": "申请者",
    "task_path_obs": "用户Config目录",
    "skill_dir_obs": "技能目录",
    "agent_dir_obs": "Agent 目录",
    "user_folder_obs": "用户Profile目录",
    "model_name": "模型",
    "eval_model_name": "评估模型",
    "user_proxy_model_name": "用户模拟模型",
    "harness_type": "Harness 类型",
    "base_url": "Base URL",
    "eval_config_model": "评估配置模型",
    "eval_config_base_url": "评估配置 Base URL",
    "eval_config_api": "评估配置 API",
    "default_skills": "默认技能",
    "traj_save_path": "轨迹保存路径",
    "remark": "备注",
}


def _snapshot(row) -> dict:
    """按白名单抽字段。row 可以是 sqlite3.Row 或 dict。"""
    keys = row.keys() if hasattr(row, "keys") else row
    return {f: (row[f] if f in keys else "") for f in SNAPSHOT_FIELDS}


def _diff_snapshots(old: dict, new: dict) -> list[dict]:
    """只比较白名单字段，返回 [{field, old, new}]。空列表 = 没有实质变更。"""
    changes = []
    for f in SNAPSHOT_FIELDS:
        a, b = old.get(f, ""), new.get(f, "")
        # 统一成字符串比较：None 与 '' 在语义上是同一件事（字段留空）
        a = "" if a is None else a
        b = "" if b is None else b
        if a != b:
            changes.append({"field": f, "old": a, "new": b})
    return changes


def _append_version(conn, reg_id: int, old_snap: dict | None, new_snap: dict,
                    summary: str, user: dict):
    """追加一个版本行并把 current_version 推到主表。

    diff 为空时**不写**：用户点开编辑框又原样保存不该刷出一个版本。
    old_snap 为 None 表示首次创建（v1），change_json 记为空数组。
    """
    changes = [] if old_snap is None else _diff_snapshots(old_snap, new_snap)
    if old_snap is not None and not changes:
        return None

    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS m FROM task_registration_versions "
        "WHERE registration_id = ?",
        (reg_id,),
    ).fetchone()
    version = (row["m"] or 0) + 1

    conn.execute(
        """INSERT INTO task_registration_versions
           (registration_id, version, snapshot_json, change_json, summary, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (reg_id, version, json.dumps(new_snap, ensure_ascii=False),
         json.dumps(changes, ensure_ascii=False), summary, user["username"]),
    )
    conn.execute(
        "UPDATE task_registrations SET current_version = ? WHERE id = ?",
        (version, reg_id),
    )
    return version


def _version_rollup(conn, reg_id: int) -> dict:
    """列表页聚合：版本总数 + 最新版本的变更说明。"""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM task_registration_versions WHERE registration_id = ?",
        (reg_id,),
    ).fetchone()
    count = row["c"] if row else 0
    if not count:
        return {"version_count": 0, "latest_change": ""}
    latest = conn.execute(
        "SELECT summary FROM task_registration_versions WHERE registration_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (reg_id,),
    ).fetchone()
    return {"version_count": count, "latest_change": (latest["summary"] if latest else "") or ""}


def _build_version(row) -> RegistrationVersionInfo:
    return RegistrationVersionInfo(
        id=row["id"],
        registration_id=row["registration_id"],
        version=row["version"],
        snapshot_json=row["snapshot_json"] or "{}",
        change_json=row["change_json"] or "[]",
        summary=row["summary"] or "",
        created_by=row["created_by"] or "",
        created_at=str(row["created_at"] or ""),
    )


def _get_version_row(conn, reg_id: int, version_id: int):
    vrow = conn.execute(
        "SELECT * FROM task_registration_versions WHERE id = ? AND registration_id = ?",
        (version_id, reg_id),
    ).fetchone()
    if not vrow:
        raise HTTPException(status_code=404, detail="版本不存在")
    return vrow


def _mask_api_key(info: dict, user: dict) -> dict:
    """Only admin and the creator can see the full api_key."""
    if user.get("role") != "admin" and user.get("username") != info.get("created_by"):
        for field in ("api_key", "eval_config_api_key"):
            key = info.get(field)
            if key:
                if len(key) > 8:
                    info[field] = key[:4] + "****" + key[-4:]
                else:
                    info[field] = "****"
    return info


def _build_info(row, conn, user) -> RegistrationInfo:
    info = dict(row)
    info.setdefault("completed_tasks", 0)
    info.setdefault("failed_tasks", 0)
    info.setdefault("base_url", "")
    info.setdefault("api_key", "")
    info.setdefault("eval_config_model", "")
    info.setdefault("eval_config_base_url", "")
    info.setdefault("eval_config_api_key", "")
    info.setdefault("eval_config_api", "")
    info.setdefault("default_skills", "")
    info.setdefault("remark", "")
    info.setdefault("seq", 0)
    info.setdefault("count_status", "")
    info.setdefault("count_error", "")
    info.setdefault("counted_at", None)
    info.setdefault("updated_at", None)
    info.setdefault("current_version", 0)
    info.setdefault("download_status", "")
    info.setdefault("download_error", "")
    info.setdefault("downloaded_at", None)
    info.setdefault("modified_count", 0)
    info.setdefault("last_synced_at", None)
    # config_template_id 已废弃，不再透出
    info.pop("config_template_id", None)
    info.update(_version_rollup(conn, row["id"]))
    if info.get("linked_instance_id"):
        inst = conn.execute(
            "SELECT completed_tasks, failed_tasks FROM task_instances WHERE id = ?",
            (info["linked_instance_id"],),
        ).fetchone()
        if inst:
            info["completed_tasks"] = inst["completed_tasks"]
            info["failed_tasks"] = inst["failed_tasks"]
    info = _mask_api_key(info, user)
    return RegistrationInfo(**info)


def _next_seq(conn) -> int:
    """取当前最大 seq + 1（同一连接/事务内调用，不会并发撞号）。"""
    row = conn.execute("SELECT MAX(seq) AS m FROM task_registrations").fetchone()
    return (row["m"] or 0) + 1


@router.get("", response_model=list[RegistrationInfo])
def list_registrations(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_registrations ORDER BY created_at DESC").fetchall()
        return [_build_info(r, conn, user) for r in rows]


@router.post("", response_model=RegistrationInfo)
async def create_registration(req: RegistrationCreate, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        seq = _next_seq(conn)
        cursor = conn.execute(
            """INSERT INTO task_registrations
               (created_by, task_name, requester, task_path_obs, data_total,
                skill_dir_obs, agent_dir_obs, user_folder_obs,
                model_name, eval_model_name, user_proxy_model_name, harness_type,
                base_url, api_key,
                eval_config_model, eval_config_base_url, eval_config_api_key, eval_config_api,
                default_skills, traj_save_path, remark, seq)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["username"], req.task_name, req.requester, req.task_path_obs,
             req.data_total, req.skill_dir_obs, req.agent_dir_obs, req.user_folder_obs,
             req.model_name, req.eval_model_name, req.user_proxy_model_name, req.harness_type,
             req.base_url, req.api_key,
             req.eval_config_model, req.eval_config_base_url, req.eval_config_api_key, req.eval_config_api,
             req.default_skills, req.traj_save_path, req.remark, seq),
        )
        reg_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        # 首次创建 = v1。old_snap 为 None → 不比较，change_json 留空数组。
        conn.execute(
            "UPDATE task_registrations SET updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), reg_id),
        )
        _append_version(conn, reg_id, None, _snapshot(row), "初始创建", user)
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        result = _build_info(row, conn, user)
    # 自动统计样本数（若填了 task_path_obs）
    if req.task_path_obs:
        asyncio.create_task(count_sample(reg_id))
    return result


@router.get("/{reg_id}", response_model=RegistrationInfo)
def get_registration(reg_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        return _build_info(row, conn, user)


@router.post("/{reg_id}/count-sample")
async def trigger_count_sample(reg_id: int, user: dict = Depends(get_current_user)):
    """重新统计样本数（本地已有副本时不会重新下载）。"""
    with get_connection() as conn:
        row = conn.execute("SELECT id, task_path_obs FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        if not row["task_path_obs"]:
            raise HTTPException(status_code=400, detail="该数据集未填写任务路径（task_path_obs）")
        conn.execute(
            "UPDATE task_registrations SET count_status='counting', count_error='' WHERE id=?",
            (reg_id,),
        )
    asyncio.create_task(count_sample(reg_id))
    return {"status": "counting"}


@router.post("/{reg_id}/redownload")
async def redownload_dataset(reg_id: int, user: dict = Depends(require_admin)):
    """从 OBS 重新整份下载，覆盖本地副本并重建哈希基线。

    会丢弃本地未同步的改动 —— 调用方（前端）必须先确认。
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, task_path_obs, modified_count FROM task_registrations WHERE id = ?",
            (reg_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        if not row["task_path_obs"]:
            raise HTTPException(status_code=400, detail="该数据集未填写任务路径（task_path_obs）")
        if (row["modified_count"] or 0) > 0:
            raise HTTPException(
                status_code=409,
                detail=f"有 {row['modified_count']} 个本地修改尚未同步到云端，"
                       f"重新下载会丢弃它们。请先等待同步完成，或确认放弃后再试",
            )
        conn.execute(
            "UPDATE task_registrations SET count_status='counting', download_status='downloading', "
            "count_error='', download_error='' WHERE id=?",
            (reg_id,),
        )
    asyncio.create_task(count_sample(reg_id, force_download=True))
    return {"status": "downloading"}


@router.put("/{reg_id}", response_model=RegistrationInfo)
async def update_registration(reg_id: int, req: RegistrationUpdate, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        old_path = row["task_path_obs"]
        old_snap = _snapshot(row)
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        # change_summary 只写给版本表，没有对应的主表列 —— 混进 UPDATE 会 SQL 报错
        summary = (updates.pop("change_summary", None) or "").strip()
        if updates:
            updates["updated_at"] = datetime.now().isoformat()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE task_registrations SET {set_clause} WHERE id = ?",
                (*updates.values(), reg_id),
            )
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        new_snap = _snapshot(row)
        if not summary:
            # 没填说明时用 diff 自动拼一句，列表页「最新变更」列才不会是空白
            changes = _diff_snapshots(old_snap, new_snap)
            summary = "修改 " + "、".join(
                FIELD_LABELS.get(c["field"], c["field"]) for c in changes[:3]
            ) if changes else ""
        # diff 为空时 _append_version 内部会跳过，不会因为空保存刷版本
        _append_version(conn, reg_id, old_snap, new_snap, summary, user)
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        result = _build_info(row, conn, user)
    # task_path_obs 变化时重新统计
    new_path = req.task_path_obs
    if new_path is not None and new_path != old_path and new_path:
        asyncio.create_task(count_sample(reg_id))
    return result


@router.delete("/{reg_id}")
def delete_registration(reg_id: int, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        # 引用保护：被批量实验引用时拒绝删除
        refs = conn.execute(
            "SELECT id, name FROM batch_experiments WHERE registration_id = ? LIMIT 5",
            (reg_id,),
        ).fetchall()
        if refs:
            names = "、".join(r["name"] for r in refs)
            raise HTTPException(
                status_code=409,
                detail=f"该数据集被以下批量实验引用，请先处理后再删除：{names}",
            )
        conn.execute("DELETE FROM task_registrations WHERE id = ?", (reg_id,))
        conn.execute("DELETE FROM task_registration_versions WHERE registration_id = ?", (reg_id,))
    return {"message": "已删除"}


# ============================================================================
# 版本：列表 / 详情 / 差异 / 恢复
#
# 与 harness_configs 的差异：这里没有「创建版本」和「发布」。数据集每次编辑保存
# 都会自动产生一个版本（见 update_registration），所以只有读 + 恢复两类操作。
# ============================================================================

@router.get("/{reg_id}/versions", response_model=list[RegistrationVersionInfo])
def list_versions(reg_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        if not conn.execute("SELECT id FROM task_registrations WHERE id = ?", (reg_id,)).fetchone():
            raise HTTPException(status_code=404, detail="登记不存在")
        rows = conn.execute(
            "SELECT * FROM task_registration_versions WHERE registration_id = ? "
            "ORDER BY version DESC",
            (reg_id,),
        ).fetchall()
        return [_build_version(r) for r in rows]


@router.get("/{reg_id}/versions/{version_id}", response_model=RegistrationVersionInfo)
def get_version(reg_id: int, version_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        return _build_version(_get_version_row(conn, reg_id, version_id))


@router.get("/{reg_id}/versions/{version_id}/diff")
def diff_version(reg_id: int, version_id: int, user: dict = Depends(get_current_user)):
    """返回两个方向的差异：

    - `previous`：这一版相对上一版改了什么（历史里「这次改动的原始内容」）
    - `current`：当前主行相对这一版改了什么（判断「这版之后又被谁改过」）

    v1 没有上一版，`previous` 是空列表。
    """
    with get_connection() as conn:
        vrow = _get_version_row(conn, reg_id, version_id)
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        target = json.loads(vrow["snapshot_json"] or "{}")

        prev = conn.execute(
            "SELECT snapshot_json FROM task_registration_versions "
            "WHERE registration_id = ? AND version < ? ORDER BY version DESC LIMIT 1",
            (reg_id, vrow["version"]),
        ).fetchone()
        previous = _diff_snapshots(
            json.loads(prev["snapshot_json"] or "{}") if prev else {}, target
        )
        current = _diff_snapshots(target, _snapshot(row))

    return {
        "version": vrow["version"],
        "previous": previous,
        "current": current,
        "labels": FIELD_LABELS,
    }


@router.post("/{reg_id}/versions/{version_id}/restore", response_model=RegistrationInfo)
async def restore_version(reg_id: int, version_id: int, user: dict = Depends(require_admin)):
    """把某个历史版本的字段写回主行，并**追加**一个新版本。

    与 harness_configs 的 rollback 同一原则：历史只追加，不删除、不改写。
    恢复后如果 diff 为空（当前内容本来就等于该版本），不产生新版本。
    """
    with get_connection() as conn:
        vrow = _get_version_row(conn, reg_id, version_id)
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        old_snap = _snapshot(row)
        old_path = row["task_path_obs"]

        target = json.loads(vrow["snapshot_json"] or "{}")
        # 只回写白名单内的字段；列可能因版本演化而缺失，取交集
        keys = [f for f in SNAPSHOT_FIELDS if f in target and f in row.keys()]
        if keys:
            set_clause = ", ".join(f"{k} = ?" for k in keys)
            conn.execute(
                f"UPDATE task_registrations SET {set_clause}, updated_at = ? WHERE id = ?",
                (*(target[k] for k in keys), datetime.now().isoformat(), reg_id),
            )
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        new_snap = _snapshot(row)
        created = _append_version(
            conn, reg_id, old_snap, new_snap, f"恢复自 v{vrow['version']}", user
        )
        # 必须重新读：current_version 刚被 _append_version 推到主表，上面那行 row 是旧值
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        result = _build_info(row, conn, user)

    new_path = new_snap.get("task_path_obs")
    if new_path and new_path != old_path:
        asyncio.create_task(count_sample(reg_id))
    return result


# ============================================================================
# 数据集文件：浏览 / 读取 / 编辑保存
#
# 数据集的权威副本在**本地**：注册后整份下载到 OUTPUT_CACHE/dataset/<rel>，
# 编辑只改本地。云端靠 offline/sync_worker.py 异步回传，不在保存路径上。
# 这样保存是毫秒级的，也不会有「传一半失败」把数据集弄坏的问题。
#
# 路径/哈希逻辑全在 api/services/dataset_files.py，这里只做 HTTP 包装。
# ============================================================================

CAT_MAX_BYTES = 2 * 1024 * 1024


def _dataset_root(row) -> str:
    return dfs.dataset_root(row)


def _find_cached(row, rel_in_ds: str) -> str:
    return dfs.find_local(_dataset_root(row), rel_in_ds)


def _object_url(row, rel_in_ds: str) -> str:
    return dfs.object_url(_dataset_root(row), rel_in_ds)


def _resolve_object(row, path: str) -> str:
    """校验对象属于这个数据集，返回它在数据集内的相对路径。

    只做前缀比对不够（/a/bc 会匹配 /a/b），必须按目录边界比。
    """
    root = _dataset_root(row)
    if not root:
        raise HTTPException(status_code=400, detail="该数据集未填写用户Config目录")
    got = strip_bucket(path)
    if not got:
        raise HTTPException(status_code=400, detail="需要完整的 obs:// 对象路径")
    if got != root and not got.startswith(root + "/"):
        raise HTTPException(status_code=403, detail="只能访问该数据集目录内的文件")
    rel = got[len(root):].lstrip("/")
    if not rel:
        raise HTTPException(status_code=400, detail="这是一个目录，不是文件")
    if ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")
    return rel


@router.get("/{reg_id}/tree")
def list_local_tree(reg_id: int, rel: str = "",
                    user: dict = Depends(get_current_user)):
    """列**本地副本**的目录内容。返回本地绝对路径，前端直接显示。

    不再走 /obs/list：数据集已经整份下载到本地，浏览应当只看本地——
    这样「看到什么」和「能编辑什么、同步什么」是同一份内容，不会出现
    界面列的是云端、编辑的是本地这种错位。
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")

    root = dfs.dataset_root(row)
    if not root:
        raise HTTPException(status_code=400, detail="该数据集未填写用户Config目录")

    rel = (rel or "").strip().strip("/")
    if ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="非法路径")

    base_dir = dfs.content_root(root) if not rel else dfs.local_path(root, rel)
    if not os.path.isdir(base_dir):
        raise HTTPException(
            status_code=409,
            detail="本地没有这份数据集的副本（或该目录不存在）—— 请先重新下载",
        )

    items = []
    try:
        with os.scandir(base_dir) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                child_rel = (rel + "/" + e.name).lstrip("/") if rel else e.name
                is_dir = e.is_dir()
                items.append({
                    "name": e.name,
                    "rel": child_rel,
                    "is_dir": is_dir,
                    "size": (None if is_dir else e.stat().st_size),
                })
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取本地目录失败：{e}") from e

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {
        "rel": rel,
        "local_path": base_dir,                      # 让前端显示真正的本地路径
        "dataset_root": root,                        # OBS 上的相对前缀（只作参考）
        "items": items,
    }


def _resolve_file_ref(row, path: str = "", rel: str = "") -> str:
    """读/写接口既接受 obs:// 全路径，也接受数据集内相对路径。

    前端浏览本地副本时手里只有 rel（相对路径），没必要再拼 obs:// 全路径
    绕一圈——拼错了反而会指向别的对象。
    """
    rel = (rel or "").strip().strip("/")
    if rel:
        if ".." in rel.split("/"):
            raise HTTPException(status_code=400, detail="非法路径")
        return rel
    if not path:
        raise HTTPException(status_code=400, detail="需要 path（obs:// 全路径）或 rel（数据集内相对路径）")
    return _resolve_object(row, path)


class DatasetFileSave(BaseModel):
    path: str = ""
    rel: str = ""      # 数据集内相对路径；与 path 二选一
    content: str
    # 保存前是否要求内容仍是合法 JSON。config.json 存坏了会让整个数据集跑不了，
    # 所以默认校验；确实要存半成品时由前端显式置 false。
    validate_json: bool = True


@router.get("/{reg_id}/file")
async def read_dataset_file(reg_id: int, path: str = "", rel: str = "",
                            user: dict = Depends(get_current_user)):
    """读一个数据集内文件的原文。

    只读本地。数据集注册后已经整份下载，缺文件说明本地副本不完整 ——
    这种情况应该重新下载，而不是悄悄回落到 OBS 读一份可能和本地不一致的内容
    （那会让「本地已改」的哈希基线失去意义）。
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        rel = _resolve_file_ref(row, path, rel)
        frow = conn.execute(
            "SELECT * FROM task_registration_files WHERE registration_id = ? AND rel_path = ?",
            (reg_id, rel),
        ).fetchone()

    local = _find_cached(row, rel)
    if not local:
        raise HTTPException(
            status_code=409,
            detail="本地没有这个文件的副本 —— 数据集可能尚未下载完成，请先「重新下载」",
        )
    size = os.path.getsize(local)
    if size > CAT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（{size / 1024:.0f} KB），不支持在线查看")
    with open(local, encoding="utf-8", errors="replace") as f:
        content = f.read()

    return {
        "path": path, "rel": rel, "content": content, "size": size,
        "source": "local", "editable": True,
        "status": (frow["status"] if frow else "clean"),
        "sha256": (frow["sha256"] if frow else ""),
    }


@router.put("/{reg_id}/file")
async def save_dataset_file(reg_id: int, req: DatasetFileSave,
                            user: dict = Depends(require_admin)):
    """保存到**本地**。云端由 offline/sync_worker.py 异步回传。

    以前是保存时同步上传 OBS：一次网络往返、失败要回滚、还要处理「本地写成功
    云端写失败」的半截状态。改成只写本地后，保存是原子的、毫秒级；
    云端一致性交给 sync_worker（它按哈希判断，不会重复传）。
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        rel = _resolve_file_ref(row, req.path, req.rel)

    body = req.content or ""
    if req.validate_json and rel.lower().endswith(".json"):
        try:
            json.loads(body)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"JSON 格式错误（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}",
            ) from e

    encoded = body.encode("utf-8")
    if len(encoded) > CAT_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"内容过大（>{CAT_MAX_BYTES // 1024} KB）")

    local = _find_cached(row, rel)
    if not local:
        raise HTTPException(
            status_code=409,
            detail="本地没有这个文件的副本，无法编辑 —— 请先重新下载数据集",
        )
    with open(local, "wb") as f:
        f.write(encoded)

    digest = dfs.sha256_file(local)
    with get_connection() as conn:
        r = conn.execute(
            "SELECT id, synced_sha256 FROM task_registration_files "
            "WHERE registration_id = ? AND rel_path = ?",
            (reg_id, rel),
        ).fetchone()
        if r:
            status = "clean" if digest == (r["synced_sha256"] or "") else "modified"
            conn.execute(
                """UPDATE task_registration_files
                   SET sha256 = ?, size = ?, status = ?,
                       modified_at = CASE WHEN ? = 'modified' THEN CURRENT_TIMESTAMP
                                          ELSE modified_at END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (digest, len(encoded), status, status, r["id"]),
            )
        else:
            # 没有基线行 = 云端那一版未知（老数据集 / 本地新加的文件）。
            # 不能假定「和云端一致」——那会让这个文件永远不同步。
            # 标 modified 让 sync_worker 传一次；内容相同的话上传也是无害的。
            conn.execute(
                """INSERT INTO task_registration_files
                   (registration_id, rel_path, sha256, synced_sha256, size, status, modified_at)
                   VALUES (?, ?, ?, '', ?, 'modified', CURRENT_TIMESTAMP)""",
                (reg_id, rel, digest, len(encoded)),
            )
            status = "modified"
        modified_count = dfs.refresh_modified_count(conn, reg_id)

    return {
        "path": req.path, "rel": rel, "size": len(encoded),
        "sha256": digest, "status": status,
        "modified_count": modified_count,
        "detail": ("已保存到本地，等待同步到云端" if status == "modified"
                   else "已保存到本地"),
    }


@router.get("/{reg_id}/files")
def list_dataset_files(reg_id: int, only_modified: bool = False,
                       user: dict = Depends(get_current_user)):
    """文件清单 + 每个文件的哈希状态（列表页/抽屉用来标「已修改」）。"""
    with get_connection() as conn:
        if not conn.execute("SELECT id FROM task_registrations WHERE id = ?", (reg_id,)).fetchone():
            raise HTTPException(status_code=404, detail="登记不存在")
        sql = "SELECT * FROM task_registration_files WHERE registration_id = ?"
        params = [reg_id]
        if only_modified:
            sql += " AND status = 'modified'"
        sql += " ORDER BY rel_path"
        rows = conn.execute(sql, params).fetchall()
    return [{
        "id": r["id"], "rel_path": r["rel_path"], "size": r["size"],
        "sha256": r["sha256"], "synced_sha256": r["synced_sha256"],
        "status": r["status"],
        "modified_at": r["modified_at"], "synced_at": r["synced_at"],
    } for r in rows]


# ============================================================================
# 启动期：给存量数据集补一条初始版本（幂等）
# ============================================================================

def ensure_versions():
    """current_version = 0 的存量数据集 → 用当前行生成 v1。

    不带 diff（没有「上一版」可比），summary 标明是回填，避免和真实编辑混淆。
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM task_registrations WHERE COALESCE(current_version, 0) = 0"
        ).fetchall()
        for row in rows:
            has = conn.execute(
                "SELECT id FROM task_registration_versions WHERE registration_id = ? LIMIT 1",
                (row["id"],),
            ).fetchone()
            if has:
                # 表里有版本但冗余列没同步（迁移中断），把它对齐回最大版本号
                latest = conn.execute(
                    "SELECT MAX(version) AS m FROM task_registration_versions "
                    "WHERE registration_id = ?", (row["id"],),
                ).fetchone()
                conn.execute(
                    "UPDATE task_registrations SET current_version = ? WHERE id = ?",
                    (latest["m"] or 0, row["id"]),
                )
                continue
            conn.execute(
                """INSERT INTO task_registration_versions
                   (registration_id, version, snapshot_json, change_json, summary, created_by)
                   VALUES (?, 1, ?, '[]', '历史数据回填', 'system')""",
                (row["id"], json.dumps(_snapshot(row), ensure_ascii=False)),
            )
            conn.execute(
                "UPDATE task_registrations SET current_version = 1, "
                "updated_at = COALESCE(updated_at, created_at) WHERE id = ?",
                (row["id"],),
            )
