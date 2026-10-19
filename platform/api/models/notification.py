from typing import Optional

from pydantic import BaseModel


class NotificationInfo(BaseModel):
    id: int
    title: str
    content_md: str = ""
    level: str = "info"
    doc_id: Optional[int] = None
    doc_slug: Optional[str] = None
    created_by: str = ""
    created_at: str = ""
    is_read: int = 0


class NotificationList(BaseModel):
    items: list[NotificationInfo]
    total: int
    unread: int
    page: int
    page_size: int


class NotificationUnreadCount(BaseModel):
    unread: int


class NotificationSentItem(NotificationInfo):
    """管理员视角的「我发出的」：多两个统计字段。"""
    read_count: int = 0
    total_users: int = 0
