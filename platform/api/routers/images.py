from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection
from ..core.security import get_current_user, require_operator
from ..models.image import ImageCreate, ImageInfo

router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("", response_model=List[ImageInfo])
def list_images(
    harness_type: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    with get_connection() as conn:
        if harness_type:
            rows = conn.execute(
                "SELECT * FROM images WHERE harness_type = ? ORDER BY created_at DESC",
                (harness_type,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM images ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@router.post("", response_model=ImageInfo)
def create_image(req: ImageCreate, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO images (name, address, harness_type, created_by) VALUES (?, ?, ?, ?)",
            (req.name, req.address, req.harness_type, user["username"]),
        )
        row = conn.execute("SELECT * FROM images WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


@router.delete("/{image_id}")
def delete_image(image_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="镜像不存在")
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
    return {"detail": "已删除"}
