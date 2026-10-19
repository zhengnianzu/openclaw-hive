"""全局站内通知 —— 广播 + 已读表。

数据模型与设计理由见 core/database.py 的 notifications / notification_reads 建表注释：
通知只写一行，「谁读过了」按需插一行。未读数用 LEFT JOIN 即时算。

⚠️ 路由顺序：/unread-count、/read-all、/sent 这类静态段必须定义在 /{notification_id}
之前。FastAPI 按注册顺序匹配，否则 "unread-count" 会被当作 int 路径参数报 422。

⚠️ 这里没有任何后台循环 / startup 钩子。uvicorn 用 --workers 4 时，只有进程级后台任务
会被每个 worker 各跑一遍；普通请求天然只由一个 worker 处理。
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection
from ..core.security import get_current_user, require_admin
from ..models.notification import (
    NotificationInfo,
    NotificationList,
    NotificationSentItem,
    NotificationUnreadCount,
)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_UNREAD_SQL = """SELECT COUNT(*) AS cnt FROM notifications n
    LEFT JOIN notification_reads r
           ON r.notification_id = n.id AND r.username = ?
    WHERE n.revoked = 0 AND r.notification_id IS NULL"""


def _unread_count(conn, username: str) -> int:
    return conn.execute(_UNREAD_SQL, (username,)).fetchone()["cnt"]


@router.get("", response_model=NotificationList)
def list_notifications(
    unread_only: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    username = user["username"]
    where = "n.revoked = 0"
    if unread_only:
        where += " AND r.notification_id IS NULL"
    with get_connection() as conn:
        total = conn.execute(
            f"""SELECT COUNT(*) AS cnt FROM notifications n
                LEFT JOIN notification_reads r
                       ON r.notification_id = n.id AND r.username = ?
                WHERE {where}""",
            (username,),
        ).fetchone()["cnt"]
        rows = conn.execute(
            f"""SELECT n.id, n.title, n.content_md, n.level, n.doc_id, n.doc_slug, n.created_by, n.created_at,
                       CASE WHEN r.notification_id IS NULL THEN 0 ELSE 1 END AS is_read
                FROM notifications n
                LEFT JOIN notification_reads r
                       ON r.notification_id = n.id AND r.username = ?
                WHERE {where}
                ORDER BY n.created_at DESC, n.id DESC
                LIMIT ? OFFSET ?""",
            (username, page_size, (page - 1) * page_size),
        ).fetchall()
        unread = _unread_count(conn, username)
    return NotificationList(
        items=[NotificationInfo(**dict(r)) for r in rows],
        total=total,
        unread=unread,
        page=page,
        page_size=page_size,
    )


@router.get("/unread-count", response_model=NotificationUnreadCount)
def get_unread_count(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        return NotificationUnreadCount(unread=_unread_count(conn, user["username"]))


@router.post("/read-all")
def read_all(user: dict = Depends(get_current_user)):
    username = user["username"]
    with get_connection() as conn:
        before = _unread_count(conn, username)
        # 一条语句把当前所有未撤销通知标记为已读；已读过的被 PRIMARY KEY 挡掉
        conn.execute(
            """INSERT OR IGNORE INTO notification_reads (notification_id, username)
               SELECT id, ? FROM notifications WHERE revoked = 0""",
            (username,),
        )
        after = _unread_count(conn, username)
    return {"marked": before - after, "unread": after}


@router.get("/sent", response_model=list[NotificationSentItem])
def list_sent(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_admin),
):
    """管理员「我发出的」：附带已读人数 / 当前用户总数，便于判断公告是否触达。"""
    with get_connection() as conn:
        total_users = conn.execute(
            "SELECT COUNT(*) AS cnt FROM users WHERE is_active = 1"
        ).fetchone()["cnt"]
        rows = conn.execute(
            """SELECT n.id, n.title, n.content_md, n.level, n.doc_id, n.doc_slug, n.created_by, n.created_at,
                      (SELECT COUNT(*) FROM notification_reads r WHERE r.notification_id = n.id) AS read_count
               FROM notifications n
               WHERE n.revoked = 0 AND n.created_by = ?
               ORDER BY n.created_at DESC, n.id DESC
               LIMIT ? OFFSET ?""",
            (user["username"], page_size, (page - 1) * page_size),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["is_read"] = 1
        d["total_users"] = total_users
        items.append(NotificationSentItem(**d))
    return items


@router.post("/{notification_id}/read", response_model=NotificationUnreadCount)
def mark_read(notification_id: int, user: dict = Depends(get_current_user)):
    username = user["username"]
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT id FROM notifications WHERE id = ? AND revoked = 0", (notification_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="通知不存在")
        conn.execute(
            "INSERT OR IGNORE INTO notification_reads (notification_id, username) VALUES (?, ?)",
            (notification_id, username),
        )
        return NotificationUnreadCount(unread=_unread_count(conn, username))


@router.delete("/{notification_id}")
def revoke_notification(notification_id: int, user: dict = Depends(require_admin)):
    """撤回 = 软删，保留已读记录（便于审计「谁看过这条公告」）。"""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM notifications WHERE id = ?", (notification_id,)
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="通知不存在")
        conn.execute("UPDATE notifications SET revoked = 1 WHERE id = ?", (notification_id,))
    return {"message": "通知已撤回"}
