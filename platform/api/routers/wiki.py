"""文档中心（Wiki）—— 正文在 `platform/wiki/md/*.md`，索引在 `platform/wiki/index.db`。

- 读：所有登录用户。每次请求先 `sync_index()`（按 content_hash 判断变化），再查索引；
  正文永远从 `.md` 文件读，所以整体拷贝 / IDE 手改都能立刻反映到界面。
- 写：仅 admin。编辑器保存时写回 `.md`，同时刷新索引。
- 整个 `platform/wiki/` 目录可单独同步或备份，不涉及主库 `platform.db`。
- 发布通知：把文档正文快照写进 `notifications`（DB），文档后续修改不影响已发公告。

设计理由见 api/services/wiki_store.py 的模块注释。
"""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.database import get_connection
from ..services.wiki_store import get_wiki_connection, _init_wiki_db
from ..core.security import get_current_user, require_admin
from ..models.wiki import (
    LINKED_ENTITY_TYPES,
    WikiCategoryCreate,
    WikiCategoryInfo,
    WikiCategoryUpdate,
    WikiDocumentCreate,
    WikiDocumentDetail,
    WikiDocumentList,
    WikiDocumentListItem,
    WikiDocumentUpdate,
    WikiPublishRequest,
    WikiPublishResponse,
)
from ..services import wiki_store

router = APIRouter(prefix="/api/wiki", tags=["wiki"])

# 写权限的唯一开关。将来要放开给 operator，只改这一行 + 前端按钮的 v-if。
WIKI_WRITE = require_admin

MAX_CONTENT_LEN = 200_000


def _validate_entity(linked_entity_type: str, linked_entity_key: str):
    if linked_entity_type and linked_entity_type not in LINKED_ENTITY_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的关联实体类型: {linked_entity_type}")
    if linked_entity_type and not linked_entity_key:
        raise HTTPException(status_code=400, detail="关联实体类型与关联键必须同时填写")


def _check_slug(slug: str) -> str:
    """把 wiki_store 的 ValueError 转成 400。

    路径参数会直接拼进文件路径，必须在校验失败时返回 400 而不是 500；
    否则 `DELETE /documents/..` 这类请求会抛 ValueError 变成 500。
    """
    try:
        wiki_store.doc_path(slug)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法文件名")
    return slug


def _category_names(conn) -> dict[int, str]:
    return {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM categories")}


def _to_list_item(rec: dict, cat_names: dict) -> WikiDocumentListItem:
    return WikiDocumentListItem(
        slug=rec["slug"],
        title=rec["title"],
        category_id=rec.get("category_id"),
        category=cat_names.get(rec.get("category_id"), ""),
        tags=rec.get("tags") or "",
        summary=rec.get("summary") or "",
        linked_entity_type=rec.get("linked_entity_type") or "",
        linked_entity_key=rec.get("linked_entity_key") or "",
        author=rec.get("author") or "",
        updated_by=rec.get("updated_by") or "",
        is_pinned=rec.get("is_pinned") or 0,
        last_notification_id=rec.get("last_notification_id"),
        created_at=str(rec.get("created_at") or ""),
        updated_at=str(rec.get("updated_at") or ""),
    )


def _meta_for_file(rec: dict, cat_name: str) -> dict:
    """DB 行 → 写回 frontmatter 的元数据（分类存名字，diff 可读）。"""
    return {
        "title": rec.get("title") or "",
        "category": cat_name,
        "tags": rec.get("tags") or "",
        "summary": rec.get("summary") or "",
        "linked_entity_type": rec.get("linked_entity_type") or "",
        "linked_entity_key": rec.get("linked_entity_key") or "",
        "is_pinned": rec.get("is_pinned") or 0,
        "author": rec.get("author") or "",
        "created_at": str(rec.get("created_at") or ""),
        "updated_at": str(rec.get("updated_at") or ""),
    }


# ---------------------------------------------------------------- 分类


@router.get("/categories", response_model=list[WikiCategoryInfo])
def list_categories(user: dict = Depends(get_current_user)):
    wiki_store.sync_index()
    conn = get_wiki_connection()
    try:
        rows = conn.execute(
            """SELECT c.id, c.name, c.sort_order, COUNT(d.slug) AS doc_count
               FROM categories c
               LEFT JOIN documents d ON d.category_id = c.id
               GROUP BY c.id, c.name, c.sort_order
               ORDER BY c.sort_order ASC, c.id ASC"""
        ).fetchall()
    finally:
        conn.close()
    return [WikiCategoryInfo(**dict(r)) for r in rows]


@router.post("/categories", response_model=WikiCategoryInfo)
def create_category(req: WikiCategoryCreate, user: dict = Depends(WIKI_WRITE)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="分类名称不能为空")
    conn = get_wiki_connection()
    try:
        try:
            cursor = conn.execute(
                "INSERT INTO categories (name, sort_order) VALUES (?, ?)",
                (name, req.sort_order),
            )
            conn.commit()
        except Exception:
            raise HTTPException(status_code=400, detail="分类名称已存在")
        row = conn.execute(
            "SELECT id, name, sort_order FROM categories WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    finally:
        conn.close()
    return WikiCategoryInfo(**dict(row), doc_count=0)


@router.put("/categories/{category_id}", response_model=WikiCategoryInfo)
def update_category(category_id: int, req: WikiCategoryUpdate, user: dict = Depends(WIKI_WRITE)):
    updates = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip()
        if not updates["name"]:
            raise HTTPException(status_code=400, detail="分类名称不能为空")

    conn = get_wiki_connection()
    try:
        _init_wiki_db(conn)
        row0 = conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not row0:
            raise HTTPException(status_code=404, detail="分类不存在")
        old_name = row0["name"]
        new_name = updates.get("name")
        slugs = [
            r["slug"] for r in conn.execute(
                "SELECT slug FROM documents WHERE category_id = ?", (category_id,)
            ).fetchall()
        ]
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            try:
                conn.execute(
                    f"UPDATE categories SET {set_clause} WHERE id = ?",
                    list(updates.values()) + [category_id],
                )
                conn.commit()
            except Exception:
                raise HTTPException(status_code=400, detail="分类名称已存在")
        row = conn.execute(
            """SELECT c.id, c.name, c.sort_order, COUNT(d.slug) AS doc_count
               FROM categories c
               LEFT JOIN documents d ON d.category_id = c.id
               WHERE c.id = ? GROUP BY c.id""",
            (category_id,),
        ).fetchone()
    finally:
        conn.close()

    # 分类名写在每个 md 的 frontmatter 里，改名要同步改写所有引用它的文件
    if new_name and new_name != old_name:
        for slug in slugs:
            _rewrite_category_in_file(slug, new_name)
        wiki_store.sync_index(force=True)
    return WikiCategoryInfo(**dict(row))


def _rewrite_category_in_file(slug: str, new: str):
    parsed = wiki_store.parse_file(slug)
    if not parsed:
        return
    meta = _meta_for_file(parsed, new or "")
    wiki_store.write_file(slug, meta, parsed["content_md"])


@router.delete("/categories/{category_id}")
def delete_category(category_id: int, user: dict = Depends(WIKI_WRITE)):
    conn = get_wiki_connection()
    try:
        _init_wiki_db(conn)
        existing = conn.execute(
            "SELECT id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="分类不存在")
        slugs = [
            r["slug"] for r in conn.execute(
                "SELECT slug FROM documents WHERE category_id = ?", (category_id,)
            ).fetchall()
        ]
        conn.execute("UPDATE documents SET category_id = NULL WHERE category_id = ?", (category_id,))
        conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        conn.commit()
    finally:
        conn.close()
    # 文档不删，只把 frontmatter 里的 category 去掉
    for slug in slugs:
        _rewrite_category_in_file(slug, "")
    if slugs:
        wiki_store.sync_index(force=True)
    return {"message": "分类已删除"}


# ---------------------------------------------------------------- 文档


@router.get("/documents", response_model=WikiDocumentList)
def list_documents(
    q: Optional[str] = None,
    category_id: Optional[int] = None,
    tag: Optional[str] = None,
    linked_entity_type: Optional[str] = None,
    linked_entity_key: Optional[str] = None,
    pinned: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    wiki_store.sync_index()

    where = ["1=1"]
    params: list = []
    if q:
        where.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
        params += [f"%{q}%"] * 3
    if category_id:
        where.append("category_id = ?")
        params.append(category_id)
    if tag:
        # tags 是逗号分隔串，两端补逗号后按整词匹配，避免 "harness" 命中 "harness2"
        where.append("(',' || tags || ',') LIKE ?")
        params.append(f"%,{tag},%")
    if linked_entity_type:
        where.append("linked_entity_type = ?")
        params.append(linked_entity_type)
    if linked_entity_key:
        where.append("linked_entity_key = ?")
        params.append(linked_entity_key)
    if pinned is not None:
        where.append("is_pinned = ?")
        params.append(1 if pinned else 0)

    where_sql = " AND ".join(where)
    conn = get_wiki_connection()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS cnt FROM documents WHERE {where_sql}", params
        ).fetchone()["cnt"]
        rows = conn.execute(
            f"""SELECT * FROM documents
                WHERE {where_sql}
                ORDER BY is_pinned DESC, updated_at DESC
                LIMIT ? OFFSET ?""",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
        cat_names = _category_names(conn)
    finally:
        conn.close()
    return WikiDocumentList(
        items=[_to_list_item(dict(r), cat_names) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/documents/{slug}", response_model=WikiDocumentDetail)
def get_document(slug: str, user: dict = Depends(get_current_user)):
    _check_slug(slug)
    rec = wiki_store.load_doc(slug)
    if not rec:
        raise HTTPException(status_code=404, detail="文档不存在")
    return WikiDocumentDetail(
        **_to_list_item(rec, {rec.get("category_id"): rec.get("category", "")}).model_dump(),
        content_md=rec["content_md"],
    )


def _resolve_category(conn, name: str) -> Optional[int]:
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO categories (name, sort_order) VALUES (?, 100)", (name,))
    return cur.lastrowid


@router.post("/documents", response_model=WikiDocumentDetail)
def create_document(req: WikiDocumentCreate, user: dict = Depends(WIKI_WRITE)):
    title = req.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    if len(req.content_md) > MAX_CONTENT_LEN:
        raise HTTPException(status_code=400, detail="正文过长（上限 200KB）")
    _validate_entity(req.linked_entity_type, req.linked_entity_key)

    slug = (req.slug or "").strip() or wiki_store.slugify(title)
    if not wiki_store.SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail="文件名只能包含字母、数字、点、下划线和短横线")
    if wiki_store.parse_file(slug):
        raise HTTPException(status_code=400, detail=f"文件 wiki/md/{slug}.md 已存在")

    now = datetime.now().isoformat()
    cat_name = ""
    if req.category_id:
        conn = get_wiki_connection()
        try:
            row = conn.execute(
                "SELECT name FROM categories WHERE id = ?", (req.category_id,)
            ).fetchone()
            cat_name = row["name"] if row else ""
        finally:
            conn.close()
    meta = {
        "title": title, "category": cat_name, "tags": req.tags, "summary": req.summary,
        "linked_entity_type": req.linked_entity_type, "linked_entity_key": req.linked_entity_key,
        "is_pinned": req.is_pinned, "author": user["username"],
        "created_at": now, "updated_at": now,
    }
    wiki_store.write_file(slug, meta, req.content_md)
    wiki_store.sync_index(force=True)
    return get_document(slug, user)


@router.put("/documents/{slug}", response_model=WikiDocumentDetail)
def update_document(slug: str, req: WikiDocumentUpdate, user: dict = Depends(WIKI_WRITE)):
    _check_slug(slug)
    parsed = wiki_store.parse_file(slug)
    if not parsed:
        raise HTTPException(status_code=404, detail="文档不存在")

    updates = {k: v for k, v in req.model_dump(exclude_unset=True).items() if v is not None}
    if "title" in updates:
        updates["title"] = str(updates["title"]).strip()
        if not updates["title"]:
            raise HTTPException(status_code=400, detail="标题不能为空")
    if "content_md" in updates and len(updates["content_md"]) > MAX_CONTENT_LEN:
        raise HTTPException(status_code=400, detail="正文过长（上限 200KB）")

    # 更新前的完整元数据（分类以文件里的名字为准）
    rec = dict(parsed)
    rec.setdefault("category", parsed.get("category") or "")

    entity_type = updates.get("linked_entity_type", rec.get("linked_entity_type", ""))
    entity_key = updates.get("linked_entity_key", rec.get("linked_entity_key", ""))
    _validate_entity(entity_type or "", entity_key or "")

    cat_name = rec.get("category") or ""
    if "category_id" in req.model_dump(exclude_unset=True):
        cid = req.category_id
        if cid:
            conn = get_wiki_connection()
            try:
                row = conn.execute("SELECT name FROM categories WHERE id = ?", (cid,)).fetchone()
            finally:
                conn.close()
            if not row:
                raise HTTPException(status_code=400, detail="分类不存在")
            cat_name = row["name"]
        else:
            cat_name = ""

    meta = {
        "title": updates.get("title", rec.get("title")),
        "category": cat_name,
        "tags": updates.get("tags", rec.get("tags")),
        "summary": updates.get("summary", rec.get("summary")),
        "linked_entity_type": entity_type,
        "linked_entity_key": entity_key,
        "is_pinned": updates.get("is_pinned", rec.get("is_pinned")),
        "author": rec.get("author") or user["username"],
        "created_at": rec.get("created_at") or datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    body = updates.get("content_md", parsed["content_md"])
    wiki_store.write_file(slug, meta, body)
    wiki_store.sync_index(force=True)
    return get_document(slug, user)


@router.delete("/documents/{slug}")
def delete_document(slug: str, user: dict = Depends(WIKI_WRITE)):
    path = wiki_store.doc_path(_check_slug(slug))
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="文档不存在")
    # 文件是真相：删文件。已发通知保留（正文是发布时快照），前端遇到悬空 doc_slug 时降级为纯文本。
    os.remove(path)
    conn = get_wiki_connection()
    try:
        _init_wiki_db(conn)
        conn.execute("DELETE FROM documents WHERE slug = ?", (slug,))
        conn.commit()
    finally:
        conn.close()
    return {"message": "文档已删除"}


@router.post("/documents/{slug}/publish", response_model=WikiPublishResponse)
def publish_document(slug: str, req: WikiPublishRequest, user: dict = Depends(WIKI_WRITE)):
    """把文档发布为全局站内通知。

    普通同步请求，由某一个 uvicorn worker 处理，写一行即返回 —— 不会像 startup 里的
    asyncio 循环那样被 4 个 worker 各执行一遍。
    """
    _check_slug(slug)
    self_doc = wiki_store.load_doc(slug)
    if not self_doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    if not req.push:
        return WikiPublishResponse(notification_id=None)

    level = req.level if req.level in ("info", "success", "warning", "danger") else "info"
    title = (req.title or self_doc["title"]).strip() or self_doc["title"]
    content_md = req.content_md if req.content_md is not None else self_doc["content_md"]

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO notifications (title, content_md, level, doc_id, doc_slug, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, content_md, level, None, slug, user["username"]),
        )
        notification_id = cursor.lastrowid
    # 通知记录在主库（运行时数据），last_notification_id 只是索引侧的展示缓存
    wconn = get_wiki_connection()
    try:
        _init_wiki_db(wconn)
        wconn.execute(
            "UPDATE documents SET last_notification_id = ? WHERE slug = ?",
            (notification_id, slug),
        )
        wconn.commit()
    finally:
        wconn.close()
    return WikiPublishResponse(notification_id=notification_id)
