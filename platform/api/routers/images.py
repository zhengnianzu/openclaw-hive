from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.image import ImageCreate, ImageInfo, ImageUpdate

router = APIRouter(prefix="/api/images", tags=["images"])

VALID_OS = ("linux", "windows")


def _check_os(value: str) -> str:
    os_value = (value or "linux").strip().lower()
    if os_value not in VALID_OS:
        raise HTTPException(status_code=400, detail=f"操作系统只能是 {' / '.join(VALID_OS)}")
    return os_value


@router.get("", response_model=List[ImageInfo])
def list_images(
    harness_type: Optional[str] = Query(None),
    os: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    sql = "SELECT * FROM images"
    where, params = [], []
    if harness_type:
        where.append("(harness_type = ? OR harness_type = 'common')")
        params.append(harness_type)
    if os:
        where.append("os = ?")
        params.append(_check_os(os))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


@router.post("", response_model=ImageInfo)
def create_image(req: ImageCreate, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO images (name, address, harness_type, os, created_by) VALUES (?, ?, ?, ?, ?)",
            (req.name.strip(), req.address.strip(), req.harness_type,
             _check_os(req.os), user["username"]),
        )
        row = conn.execute("SELECT * FROM images WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@router.patch("/{image_id}", response_model=ImageInfo)
def update_image(image_id: int, req: ImageUpdate, user: dict = Depends(require_operator)):
    """只允许改 os —— 名称/地址是实例配置里引用的标识，改了会对不上历史任务。"""
    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone():
            raise HTTPException(status_code=404, detail="镜像不存在")
        conn.execute("UPDATE images SET os = ? WHERE id = ?", (_check_os(req.os), image_id))
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return dict(row)


@router.delete("/{image_id}")
def delete_image(image_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="镜像不存在")
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
    return {"detail": "已删除"}
