"""文档中心存储层 —— 全部在 `platform/wiki/` 一个目录里，方便整体同步/备份。

    platform/wiki/
      ├── md/
      │     ├── README.md            正文 + YAML frontmatter（唯一内容真相）
      │     └── openclaw-v3-release.md
      └── index.db                   索引 SQLite（独立文件，不含用户/密码数据）

- **md 是内容真相**：可用 IDE 直接编辑、可 git 管理、可整体拷贝。
- **index.db 是索引**：只存文档元数据与分类，删掉后由 md 自动重建。
- 把整个 `platform/wiki/` 目录拷走再拷回来即可完成同步，不涉及主库 `platform.db`。

主库里的 `wiki_documents` / `wiki_categories` 不再使用（旧的空表保留无害）。
通知与已读状态仍在主库：那是 per-user 运行时数据，不属于文档目录。
"""
import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from typing import Optional

import yaml

from ..core.config import settings

SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

FRONTMATTER_KEYS = (
    "title", "category", "tags", "summary",
    "linked_entity_type", "linked_entity_key",
    "is_pinned", "sort_order", "author", "updated_by", "created_at", "updated_at",
)


# ---------------------------------------------------------------- 路径与连接


def wiki_root() -> str:
    os.makedirs(settings.WIKI_DIR, exist_ok=True)
    return settings.WIKI_DIR


def md_dir() -> str:
    path = os.path.join(wiki_root(), "md")
    os.makedirs(path, exist_ok=True)
    return path


def doc_path(slug: str) -> str:
    if not SLUG_RE.match(slug or "") or ".." in slug:
        raise ValueError(f"非法文件名: {slug}")
    return os.path.join(md_dir(), f"{slug}.md")


def get_wiki_connection() -> sqlite3.Connection:
    """打开索引库。表结构在连接时就确保存在，调用方不必关心初始化。"""
    conn = sqlite3.connect(settings.WIKI_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_wiki_db(conn)
    return conn


def _init_wiki_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 100
        );

        CREATE TABLE IF NOT EXISTS documents (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category_id INTEGER,
            tags TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            linked_entity_type TEXT NOT NULL DEFAULT '',
            linked_entity_key TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            updated_by TEXT NOT NULL DEFAULT '',
            is_pinned INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 100,
            content_hash TEXT NOT NULL DEFAULT '',
            file_mtime REAL NOT NULL DEFAULT 0,
            last_notification_id INTEGER,           -- 当前展示用，纯缓存，md 里不存
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category_id);
        CREATE INDEX IF NOT EXISTS idx_documents_entity
            ON documents(linked_entity_type, linked_entity_key);
        CREATE INDEX IF NOT EXISTS idx_documents_updated ON documents(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_documents_sort ON documents(is_pinned DESC, sort_order ASC);
    """)
    # 已有 index.db 补列（用 try/except 保持幂等，和主库迁移风格一致）
    try:
        conn.execute("ALTER TABLE documents ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 100")
    except sqlite3.OperationalError:
        pass


# ---------------------------------------------------------------- md 解析 / 写入


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("".join(lines[1:i])) or {}
                if not isinstance(meta, dict):
                    meta = {}
            except Exception:
                meta = {}
            return meta, "".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def _as_int(v, default: int = 100) -> int:
    """frontmatter 里的 sort_order 可能是字符串/None，统一成 int。"""
    try:
        if v is None or v == "":
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


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
        "sort_order": _as_int(meta.get("sort_order"), 100),
        "author": str(meta.get("author") or ""),
        "updated_by": str(meta.get("updated_by") or meta.get("author") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
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


def write_file(slug: str, meta: dict, body: str) -> Optional[dict]:
    """原子写 md（临时文件 + os.replace），避免并发读到半截内容。"""
    path = doc_path(slug)
    fd, tmp = tempfile.mkstemp(dir=md_dir(), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialize(meta, body))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return parse_file(slug)


# ---------------------------------------------------------------- 索引同步


def _category_id(conn, name: str) -> Optional[int]:
    if not name:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO categories (name, sort_order) VALUES (?, 100)", (name,))
    return cur.lastrowid


def sync_index(force: bool = False) -> int:
    """让 index.db 与 md/ 一致。返回重新解析的文件数。

    用 content_hash 判断变化（而不是 mtime）：git checkout / 整体拷贝都会改变 mtime，
    但内容未变时不该重新解析。
    """
    directory = md_dir()
    slugs_on_disk = {
        name[:-3] for name in os.listdir(directory)
        if name.endswith(".md") and not name.startswith((".", "_"))
    }

    changed = 0
    conn = get_wiki_connection()
    try:
        _init_wiki_db(conn)
        existing = {
            r["slug"]: r
            for r in conn.execute("SELECT slug, content_hash FROM documents").fetchall()
        }

        for slug in sorted(slugs_on_disk):
            try:
                rec = parse_file(slug)
            except Exception:
                continue
            if not rec:
                continue
            prev = existing.get(slug)
            if not force and prev and prev["content_hash"] == rec["content_hash"]:
                conn.execute(
                    "UPDATE documents SET file_mtime = ? WHERE slug = ?",
                    (rec["file_mtime"], slug))
                continue

            cat_id = _category_id(conn, rec["category"].strip())
            conn.execute(
                """INSERT INTO documents
                   (slug, title, category_id, tags, summary,
                    linked_entity_type, linked_entity_key,
                    author, updated_by, is_pinned, sort_order, content_hash, file_mtime,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                     title=excluded.title, category_id=excluded.category_id, tags=excluded.tags,
                     summary=excluded.summary, linked_entity_type=excluded.linked_entity_type,
                     linked_entity_key=excluded.linked_entity_key, author=excluded.author,
                     updated_by=excluded.updated_by, is_pinned=excluded.is_pinned,
                     sort_order=excluded.sort_order,
                     content_hash=excluded.content_hash, file_mtime=excluded.file_mtime,
                     created_at=excluded.created_at, updated_at=excluded.updated_at""",
                (slug, rec["title"], cat_id, rec["tags"], rec["summary"],
                 rec["linked_entity_type"], rec["linked_entity_key"],
                 rec["author"], rec["updated_by"], rec["is_pinned"], rec["sort_order"],
                 rec["content_hash"], rec["file_mtime"],
                 rec["created_at"] or datetime.now().isoformat(),
                 rec["updated_at"] or datetime.now().isoformat()),
            )
            changed += 1

        for slug in set(existing) - slugs_on_disk:
            conn.execute("DELETE FROM documents WHERE slug = ?", (slug,))
            changed += 1

        conn.commit()
    finally:
        conn.close()
    return changed


# ---------------------------------------------------------------- 读


def index_record(slug: str) -> Optional[dict]:
    """从索引取一行（不含正文）。调用前应先 sync_index()。"""
    conn = get_wiki_connection()
    try:
        _init_wiki_db(conn)
        row = conn.execute("SELECT * FROM documents WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def load_doc(slug: str) -> Optional[dict]:
    """索引 + 正文。正文永远从文件读，保证与 md 目录里的一致。"""
    sync_index()
    rec = index_record(slug)
    if not rec:
        return None
    parsed = parse_file(slug)
    if not parsed:
        return None
    rec["content_md"] = parsed["content_md"]
    rec["category"] = parsed["category"]
    if rec.get("category_id"):
        conn = get_wiki_connection()
        try:
            crow = conn.execute(
                "SELECT name FROM categories WHERE id = ?", (rec["category_id"],)).fetchone()
            if crow:
                rec["category"] = crow["name"]
        finally:
            conn.close()
    return rec


def slugify(title: str) -> str:
    """标题 → 文件名；中文等非 ASCII 标题退回时间戳。"""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (title or "").strip().lower()).strip("-")
    if base and re.search(r"[A-Za-z0-9]", base):
        return base[:80]
    return "doc-" + datetime.now().strftime("%Y%m%d-%H%M%S")
