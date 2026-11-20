import asyncio

from fastapi import APIRouter, Depends, HTTPException

from ..core.database import get_connection
from ..core.security import get_current_user, require_admin
from ..models.registration import RegistrationCreate, RegistrationUpdate, RegistrationInfo
from ..services.sample_count import count_sample

router = APIRouter(prefix="/api/registrations", tags=["registrations"])


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
    # config_template_id 已废弃，不再透出
    info.pop("config_template_id", None)
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
    """手动触发样本数统计（异步执行，立即返回）。"""
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


@router.put("/{reg_id}", response_model=RegistrationInfo)
async def update_registration(reg_id: int, req: RegistrationUpdate, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        old_path = row["task_path_obs"]
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE task_registrations SET {set_clause} WHERE id = ?",
                (*updates.values(), reg_id),
            )
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
    return {"message": "已删除"}
