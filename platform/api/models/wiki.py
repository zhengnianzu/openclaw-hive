from typing import Optional

from pydantic import BaseModel

# linked_entity_type 允许的取值。空串表示不关联任何实体。
LINKED_ENTITY_TYPES = (
    "image",
    "code_repo",
    "harness_config",
    "sandbox_config",
    "resource_pool",
    "config_template",
)


class WikiCategoryCreate(BaseModel):
    name: str
    sort_order: int = 0


class WikiCategoryUpdate(BaseModel):
    name: Optional[str] = None
    sort_order: Optional[int] = None


class WikiCategoryInfo(BaseModel):
    id: int
    name: str
    sort_order: int = 0
    doc_count: int = 0


class WikiDocumentCreate(BaseModel):
    title: str
    slug: Optional[str] = None       # 为空时由标题推导；非 ASCII 标题退化为时间戳
    category_id: Optional[int] = None
    tags: str = ""
    summary: str = ""
    content_md: str = ""
    linked_entity_type: str = ""
    linked_entity_key: str = ""
    is_pinned: int = 0
    sort_order: int = 100


class WikiDocumentUpdate(BaseModel):
    title: Optional[str] = None
    category_id: Optional[int] = None
    tags: Optional[str] = None
    summary: Optional[str] = None
    content_md: Optional[str] = None
    linked_entity_type: Optional[str] = None
    linked_entity_key: Optional[str] = None
    is_pinned: Optional[int] = None
    sort_order: Optional[int] = None


class WikiDocumentListItem(BaseModel):
    """列表项 —— 刻意不含 content_md，避免列表响应体积随文档长度增长。"""
    slug: str
    title: str
    category_id: Optional[int] = None
    category: str = ""               # 分类名（来自 frontmatter），前端直接展示
    tags: str = ""
    summary: str = ""
    linked_entity_type: str = ""
    linked_entity_key: str = ""
    author: str = ""
    updated_by: str = ""
    is_pinned: int = 0
    sort_order: int = 100
    last_notification_id: Optional[int] = None
    created_at: str = ""
    updated_at: str = ""


class WikiDocumentDetail(WikiDocumentListItem):
    content_md: str = ""


class WikiDocumentList(BaseModel):
    items: list[WikiDocumentListItem]
    total: int
    page: int
    page_size: int


class WikiPublishRequest(BaseModel):
    push: bool = True
    title: Optional[str] = None       # 为空时用文档标题
    content_md: Optional[str] = None  # 为空时用文档正文
    level: str = "info"


class WikiPublishResponse(BaseModel):
    notification_id: Optional[int] = None
