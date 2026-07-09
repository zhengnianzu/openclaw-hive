from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from ..core.database import get_connection
from ..core.security import get_current_user
from ..models.config_template import ConfigTemplateCreate, ConfigTemplateInfo, ConfigTemplateUpdate

router = APIRouter(prefix="/api/config-templates", tags=["config-templates"])


@router.get("", response_model=list[ConfigTemplateInfo])
def list_templates(default: bool = False, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        if default:
            rows = conn.execute(
                "SELECT * FROM config_templates WHERE owner = ? AND is_default = 1 LIMIT 1",
                (user["username"],),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM config_templates WHERE owner = ? ORDER BY is_default DESC, updated_at DESC",
                (user["username"],),
            ).fetchall()
    return [ConfigTemplateInfo(**dict(r)) for r in rows]


@router.get("/{template_id}", response_model=ConfigTemplateInfo)
def get_template(template_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM config_templates WHERE id = ? AND owner = ?",
            (template_id, user["username"]),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="模板不存在")
    return ConfigTemplateInfo(**dict(row))


@router.post("", response_model=ConfigTemplateInfo)
def create_template(req: ConfigTemplateCreate, user: dict = Depends(get_current_user)):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) as cnt FROM config_templates WHERE owner = ?",
            (user["username"],),
        ).fetchone()
        is_default = 1 if existing["cnt"] == 0 else 0

        cursor = conn.execute(
            """INSERT INTO config_templates
               (name, owner, is_default, harness_type, model_base_url, invite_code,
                model_api_type, model_id, agents_json, image_name, code_repo_id,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.name, user["username"], is_default, req.harness_type,
             req.model_base_url, req.invite_code, req.model_api_type, req.model_id,
             req.agents_json, req.image_name, req.code_repo_id, now, now),
        )
        row = conn.execute("SELECT * FROM config_templates WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return ConfigTemplateInfo(**dict(row))


@router.put("/{template_id}", response_model=ConfigTemplateInfo)
def update_template(template_id: int, req: ConfigTemplateUpdate, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM config_templates WHERE id = ? AND owner = ?",
            (template_id, user["username"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="模板不存在")

        updates = {k: v for k, v in req.model_dump().items() if v is not None}
        if not updates:
            return ConfigTemplateInfo(**dict(existing))

        updates["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [template_id, user["username"]]
        conn.execute(
            f"UPDATE config_templates SET {set_clause} WHERE id = ? AND owner = ?",
            values,
        )
        row = conn.execute("SELECT * FROM config_templates WHERE id = ?", (template_id,)).fetchone()
    return ConfigTemplateInfo(**dict(row))


@router.delete("/{template_id}")
def delete_template(template_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM config_templates WHERE id = ? AND owner = ?",
            (template_id, user["username"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="模板不存在")
        conn.execute("DELETE FROM config_templates WHERE id = ?", (template_id,))
    return {"message": "模板已删除"}


@router.put("/{template_id}/set-default")
def set_default(template_id: int, user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM config_templates WHERE id = ? AND owner = ?",
            (template_id, user["username"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="模板不存在")
        conn.execute(
            "UPDATE config_templates SET is_default = 0 WHERE owner = ?",
            (user["username"],),
        )
        conn.execute(
            "UPDATE config_templates SET is_default = 1 WHERE id = ?",
            (template_id,),
        )
    return {"message": "已设为默认模板"}
