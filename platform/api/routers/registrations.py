from fastapi import APIRouter, Depends, HTTPException

from ..core.database import get_connection
from ..core.security import get_current_user, require_admin
from ..models.registration import RegistrationCreate, RegistrationUpdate, RegistrationInfo

router = APIRouter(prefix="/api/registrations", tags=["registrations"])


def _mask_api_key(info: dict, user: dict) -> dict:
    """Only admin and the creator can see the full api_key."""
    if info.get("api_key") and user.get("role") != "admin" and user.get("username") != info.get("created_by"):
        key = info["api_key"]
        if len(key) > 8:
            info["api_key"] = key[:4] + "****" + key[-4:]
        else:
            info["api_key"] = "****"
    return info


def _build_info(row, conn, user) -> RegistrationInfo:
    info = dict(row)
    info.setdefault("completed_tasks", 0)
    info.setdefault("failed_tasks", 0)
    info.setdefault("base_url", "")
    info.setdefault("api_key", "")
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


@router.get("", response_model=list[RegistrationInfo])
def list_registrations(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM task_registrations ORDER BY created_at DESC").fetchall()
        return [_build_info(r, conn, user) for r in rows]


@router.post("", response_model=RegistrationInfo)
def create_registration(req: RegistrationCreate, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO task_registrations
               (created_by, task_name, requester, task_path_obs, data_total,
                skill_dir_obs, agent_dir_obs, user_folder_obs,
                model_name, eval_model_name, user_proxy_model_name, harness_type,
                base_url, api_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user["username"], req.task_name, req.requester, req.task_path_obs,
             req.data_total, req.skill_dir_obs, req.agent_dir_obs, req.user_folder_obs,
             req.model_name, req.eval_model_name, req.user_proxy_model_name, req.harness_type,
             req.base_url, req.api_key),
        )
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _build_info(row, conn, user)


@router.get("/{reg_id}", response_model=RegistrationInfo)
def get_registration(reg_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        return _build_info(row, conn, user)


@router.put("/{reg_id}", response_model=RegistrationInfo)
def update_registration(reg_id: int, req: RegistrationUpdate, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE task_registrations SET {set_clause} WHERE id = ?",
                (*updates.values(), reg_id),
            )
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        return _build_info(row, conn, user)


@router.put("/{reg_id}/link")
def link_instance(reg_id: int, instance_id: str, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        conn.execute(
            "UPDATE task_registrations SET linked_instance_id = ?, status = 'executing' WHERE id = ?",
            (instance_id, reg_id),
        )
    return {"message": "已关联实例"}


@router.delete("/{reg_id}")
def delete_registration(reg_id: int, user: dict = Depends(require_admin)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM task_registrations WHERE id = ?", (reg_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="登记不存在")
        conn.execute("DELETE FROM task_registrations WHERE id = ?", (reg_id,))
    return {"message": "已删除"}
