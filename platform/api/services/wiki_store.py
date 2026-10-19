"""文档存储层 —— **Markdown 文件是内容真相，SQLite 只是可重建的索引**。

- 正文与元数据都在 `settings.WIKI_DIR/*.md`（默认 `platform/doc/wiki/`），由 Git 管理：
  有 commit 历史、diff、blame、回滚，也能在 IDE 里直接编辑。
- `wiki_documents` 表只存派生索引（标题/标签/分类/关联实体/时间戳/内容 hash），
  随时可以从目录完整重建；换机器、删库都不丢内容。
- 通知记录与已读状态仍在 DB —— 那是 per-user 运行时数据，天然不属于 Git。

文件名即 slug，同时也是路由参数（`/wiki/:slug`），跨机器稳定，不像自增 id 会因重建顺序漂移。

frontmatter 用 YAML：

    ---
    title: openclaw_v3 发布说明
    category: 发布说明          # 存分类名而非 id，diff 里可读
    tags: [发布, harness]
    summary: v3 变更点
    linked_entity_type: harness_config
    linked_entity_key: openclaw_v3
    is_pinned: false
    author: admin
    created_at: 2026-10-19T11:00:00
    updated_at: 2026-10-19T11:00:00
    ---

    ## 变更
    ...
"""
import hashlib
import os
import re
import tempfile
from datetime import datetime
from typing import Optional

import yaml

from ..core.config import settings
from ..core.database import get_connection

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

FRONTMATTER_KEYS = (
    "title", "category", "tags", "summary",
    "linked_entity_type", "linked_entity_key",
    "is_pinned", "author", "created_at", "updated_at",
)


def wiki_dir() -> str:
    os.makedirs(settings.WIKI_DIR, exist_ok=True)
    return settings.WIKI_DIR


def doc_path(slug: str) -> str:
    if not SLUG_RE.match(slug or "") or ".." in slug:
        raise ValueError(f"非法文件名: {slug}")
    return os.path.join(wiki_dir(), f"{slug}.md")


# ---------------------------------------------------------------- 解析 / 序列化


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (meta, body)。没有 frontmatter 时 meta 为空、body 为全文。"""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                meta = yaml.safe_load(raw) or {}
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            return meta, body.lstrip("\n")
    return {}, text


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v)]


def parse_file(slug: str) -> Optional[dict]:
    path = doc_path(slug)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    meta, body = _split_frontmatter(text)
    st = os.stat(path)
    return {
        "slug": slug,
        "title": str(meta.get("title") or slug),
        "category": str(meta.get("category") or ""),
        "tags": ",".join(_as_list(meta.get("tags"))),
        "summary": str(meta.get("summary") or ""),
        "linked_entity_type": str(meta.get("linked_entity_type") or ""),
        "linked_entity_key": str(meta.get("linked_entity_key") or ""),
        "is_pinned": 1 if meta.get("is_pinned") else 0,
        "author": str(meta.get("author") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "content_md": body,
        "content_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "file_mtime": st.st_mtime,
    }


def serialize(meta: dict, body: str) -> str:
    fm = {k: meta.get(k) for k in FRONTMATTER_KEYS if meta.get(k) not in (None, "")}
    fm["title"] = meta.get("title") or ""
    fm["tags"] = _as_list(meta.get("tags"))
    fm["is_pinned"] = bool(meta.get("is_pinned"))
    dumped = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{dumped}---\n\n{body.lstrip(chr(10))}"


def write_file(slug: str, meta: dict, body: str) -> dict:
    """原子写入（临时文件 + os.replace），避免并发读到半截内容。"""
    path = doc_path(slug)
    text = serialize(meta, body)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return parse_file(slug)


# ---------------------------------------------------------------- 索引重建


def _category_id(conn, name: str) -> Optional[int]:
    if not name:
        return None
    row = conn.execute("SELECT id FROM wiki_categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO wiki_categories (name, sort_order) VALUES (?, 100)", (name,))
    return cur.lastrowid


def sync_index(force: bool = False) -> int:
    """扫描 WIKI_DIR，把变化的文件同步进索引表。返回重新解析的文件数。

    以文件为真相：目录里没有的 slug 会从索引删除（文件被删/被 git 移除）。
    每个请求前调用一次都足够便宜 —— 只有 mtime 变化的文件才会重新读盘解析。
    """
    directory = wiki_dir()
    slugs_on_disk = {
        name[:-3] for name in os.listdir(directory)
        if name.endswith(".md") and not name.startswith(".")
    }

    changed = 0
    with get_connection() as conn:
        existing = {
            r["slug"]: r
            for r in conn.execute("SELECT slug, content_hash, file_mtime FROM wiki_documents").fetchall()
        }

        # 文件已不在目录里 → 从索引剔除
        for slug in set(existing) - slugs_on_disk:
            conn.execute("DELETE FROM wiki_documents WHERE slug = ?", (slug,))

        for slug in slugs_on_disk:
            try:
                st = os.stat(doc_path(slug))
            except OSError:
                continue
            prev = existing.get(slug)
            if not force and prev and abs(prev["file_mtime"] - st.st_mtime) < 1e-6:
                continue
            try:
                rec = parse_file(slug)
            except Exception:
                continue
            if not rec:
                continue
            if prev and prev["content_hash"] == rec["content_hash"]:
                conn.execute(
                    "UPDATE wiki_documents SET file_mtime = ? WHERE slug = ?",
                    (rec["file_mtime"], slug),
                )
                continue

            category_id = _category_id(conn, rec["category"])
            now = datetime.now().isoformat()
            conn.execute(
                """INSERT INTO wiki_documents
                   (slug, title, category_id, tags, summary, linked_entity_type, linked_entity_key,
                    author, updated_by, is_pinned, content_hash, file_mtime, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                     title=excluded.title, category_id=excluded.category_id, tags=excluded.tags,
                     summary=excluded.summary, linked_entity_type=excluded.linked_entity_type,
                     linked_entity_key=excluded.linked_entity_key, author=excluded.author,
                     is_pinned=excluded.is_pinned, content_hash=excluded.content_hash,
                     file_mtime=excluded.file_mtime, updated_at=excluded.updated_at""",
                (slug, rec["title"], category_id, rec["tags"], rec["summary"],
                 rec["linked_entity_type"], rec["linked_entity_key"],
                 rec["author"], rec["author"], rec["is_pinned"],
                 rec["content_hash"], rec["file_mtime"],
                 rec["created_at"] or now, rec["updated_at"] or now),
            )
            changed += 1
    return changed


def index_record(slug: str) -> Optional[dict]:
    """从索引取一行（不含正文）。调用前应先 sync_index()。"""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM wiki_documents WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def load_doc(slug: str) -> Optional[dict]:
    """索引 + 正文，供详情页使用。正文永远从文件读，保证与 Git 里的一致。"""
    sync_index()
    rec = index_record(slug)
    if not rec:
        return None
    parsed = parse_file(slug)
    if not parsed:
        return None
    rec["content_md"] = parsed["content_md"]
    rec["category"] = parsed["category"]
    # 索引里 category 存的是 id，前端需要名字
    if rec.get("category_id"):
        with get_connection() as conn:
            crow = conn.execute(
                "SELECT name FROM wiki_categories WHERE id = ?", (rec["category_id"],)
            ).fetchone()
        rec["category"] = crow["name"] if crow else parsed["category"]
    return rec


def slugify(title: str) -> str:
    """把标题转成可用的文件名；中文等非 ASCII 标题退回时间戳。"""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (title or "").strip().lower()).strip("-")
    if base and re.search(r"[A-Za-z0-9]", base):
        return base[:80]
    return "doc-" + datetime.now().strftime("%Y%m%d-%H%M%S")
