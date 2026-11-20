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


def _check_harness_types(types: List[str]) -> List[str]:
    """去重保序; common 是合法值(兜底类镜像), 空列表拒绝 —— 至少映射一个。"""
    seen, out = set(), []
    for t in (types or []):
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise HTTPException(status_code=400, detail="至少选择一个 Harness 类型")
    return out


def _load_maps(conn, image_ids: List[int]) -> dict:
    """image_id -> {harness_types: [...], defaults: [...]}, 一次查询批量装载。"""
    out: dict = {}
    if not image_ids:
        return out
    marks = ",".join("?" * len(image_ids))
    rows = conn.execute(
        f"SELECT image_id, harness_type, is_default FROM image_harness_map "
        f"WHERE image_id IN ({marks})",
        tuple(image_ids),
    ).fetchall()
    for r in rows:
        d = out.setdefault(r["image_id"], {"harness_types": [], "defaults": []})
        d["harness_types"].append(r["harness_type"])
        if r["is_default"]:
            d["defaults"].append(r["harness_type"])
    return out


def _to_info(row: dict, mapping: dict) -> dict:
    d = dict(row)
    m = mapping.get(d["id"], {"harness_types": [], "defaults": []})
    d["harness_types"] = m["harness_types"]
    d["defaults"] = m["defaults"]
    d["enabled"] = bool(d.get("enabled", 1))
    d.pop("harness_type", None)  # 已废弃的单值列, 不再下发
    return d


def _apply_default(conn, image_id: int, harness_types: List[str], is_default: bool) -> None:
    """设置/取消该镜像在其全部映射 harness 上的默认标记。

    默认唯一性 per-harness: 设为默认时, 先清掉这些 harness 上其他镜像的默认。
    """
    marks = ",".join("?" * len(harness_types))
    if is_default:
        conn.execute(
            f"UPDATE image_harness_map SET is_default = 0 WHERE is_default = 1 "
            f"AND harness_type IN ({marks}) AND image_id != ?",
            (*harness_types, image_id),
        )
        conn.execute(
            f"UPDATE image_harness_map SET is_default = 1 "
            f"WHERE image_id = ? AND harness_type IN ({marks})",
            (image_id, *harness_types),
        )
    else:
        conn.execute(
            f"UPDATE image_harness_map SET is_default = 0 "
            f"WHERE image_id = ? AND harness_type IN ({marks})",
            (image_id, *harness_types),
        )


def _replace_maps(conn, image_id: int, harness_types: List[str]) -> None:
    """整体替换映射(编辑用): 先删后插, 保留仍在映射内 harness 的默认标记。"""
    old_defaults = {
        r["harness_type"]
        for r in conn.execute(
            "SELECT harness_type FROM image_harness_map WHERE image_id = ? AND is_default = 1",
            (image_id,),
        ).fetchall()
    }
    conn.execute("DELETE FROM image_harness_map WHERE image_id = ?", (image_id,))
    for ht in harness_types:
        conn.execute(
            "INSERT INTO image_harness_map (image_id, harness_type, is_default) VALUES (?, ?, ?)",
            (image_id, ht, 1 if ht in old_defaults else 0),
        )


@router.get("", response_model=List[ImageInfo])
def list_images(
    harness_type: Optional[str] = Query(None),
    os: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """harness_type 过滤走映射表: 该 harness 的专属映射 + common 兜底。"""
    sql = (
        "SELECT DISTINCT i.* FROM images i "
        "LEFT JOIN image_harness_map m ON m.image_id = i.id"
    )
    where, params = [], []
    if harness_type:
        where.append("(m.harness_type = ? OR m.harness_type = 'common')")
        params.append(harness_type)
    if os:
        where.append("i.os = ?")
        params.append(_check_os(os))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        maps = _load_maps(conn, [r["id"] for r in rows])
    return [_to_info(dict(r), maps) for r in rows]


@router.post("", response_model=ImageInfo)
def create_image(req: ImageCreate, user: dict = Depends(require_operator)):
    harness_types = _check_harness_types(req.harness_types)
    with get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO images (name, address, os, remark, enabled, created_by) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (req.name.strip(), req.address.strip(), _check_os(req.os),
             req.remark.strip(), user["username"]),
        )
        image_id = cursor.lastrowid
        for ht in harness_types:
            conn.execute(
                "INSERT INTO image_harness_map (image_id, harness_type, is_default) "
                "VALUES (?, ?, 0)",
                (image_id, ht),
            )
        if req.is_default:
            _apply_default(conn, image_id, harness_types, True)
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        maps = _load_maps(conn, [image_id])
    return _to_info(dict(row), maps)


@router.patch("/{image_id}", response_model=ImageInfo)
def update_image(image_id: int, req: ImageUpdate, user: dict = Depends(require_operator)):
    """允许改 name / os / remark / harness 映射 / 默认标记 / 启用, 不允许改 address。

    实例配置与模板里引用的是镜像**地址**（见 CreateInstance 的 :value="img.address"），
    名称只是给人看的标签，所以改名不会让历史任务对不上；地址则必须钉死。
    只拼请求里实际给了的字段，避免「只改备注」把 os 一起覆盖掉。
    """
    sets, params = [], []
    if req.name is not None:
        name = req.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="镜像名称不能为空")
        sets.append("name = ?")
        params.append(name)
    if req.os is not None:
        sets.append("os = ?")
        params.append(_check_os(req.os))
    if req.remark is not None:
        sets.append("remark = ?")
        params.append(req.remark.strip())
    if req.enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if req.enabled else 0)

    with get_connection() as conn:
        if not conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone():
            raise HTTPException(status_code=404, detail="镜像不存在")
        if sets:
            conn.execute(
                f"UPDATE images SET {', '.join(sets)} WHERE id = ?",
                (*params, image_id),
            )
        if req.harness_types is not None:
            harness_types = _check_harness_types(req.harness_types)
            _replace_maps(conn, image_id, harness_types)
            # 与映射一起给的 is_default(含 False)要在替换后生效
            if req.is_default is not None:
                _apply_default(conn, image_id, harness_types, req.is_default)
        elif req.is_default is not None:
            current = [
                r["harness_type"]
                for r in conn.execute(
                    "SELECT harness_type FROM image_harness_map WHERE image_id = ?",
                    (image_id,),
                ).fetchall()
            ]
            if current:
                _apply_default(conn, image_id, current, req.is_default)
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        maps = _load_maps(conn, [image_id])
    return _to_info(dict(row), maps)


@router.delete("/{image_id}")
def delete_image(image_id: int, user: dict = Depends(require_operator)):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="镜像不存在")
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.execute("DELETE FROM image_harness_map WHERE image_id = ?", (image_id,))
    return {"detail": "已删除"}
