"""数据集本地副本与文件哈希。

数据集注册后会整份下载到本地缓存。这里负责三件事：

1. **路径映射**：数据集 OBS 根目录 ↔ 本地缓存目录 ↔ 单个文件。
2. **哈希基线**：首次下载时给每个文件算 sha256，作为「云端那一版」的指纹。
3. **脏检测**：本地内容变了（sha256 与 synced_sha256 不一致）就标 modified，
   交给 offline/sync_worker.py 自动回传 OBS。

目录结构约定（由 obsutil cp <obs_prefix>/ <cache_dir> -r 的语义决定）：
    cache_dir = OUTPUT_CACHE/dataset/<root 把 / 换成 __>
    cache_dir/<root 最后一段>/...    ← 真正的文件树，与 OBS 前缀下结构一致
所以「相对内容根的路径」=「相对 OBS 前缀的路径」，上传时直接拼回去即可。
"""

from __future__ import annotations

import hashlib
import os

from ..core.config import settings
from .obs_path import join_bucket, strip_bucket


def dataset_root(row) -> str:
    """数据集在 OBS 上的目录（相对路径，无首尾斜杠）。"""
    return strip_bucket(row["task_path_obs"])


def cache_dir(rel_root: str) -> str:
    """数据集的本地缓存根。

    rel_path 是剥离桶名后的相对路径；把 / 换成 __ 作为单层目录名，
    放在 OUTPUT_CACHE/dataset/ 下（与产物缓存 OUTPUT_CACHE/<batch>/<leaf> 同根）。
    与 instances._prepare_instance、sample_count 共用同一份缓存。
    """
    cache_key_path = (rel_root or "").strip("/").replace("/", "__")
    return os.path.join(settings.OUTPUT_CACHE, "dataset", cache_key_path)


def content_root(rel_root: str) -> str:
    """真正的文件树根：cache_dir/<数据集目录名>。

    obsutil cp 把 <obs_prefix>/ 拷到 cache_dir 时，会在里面建一层以该前缀最后
    一段命名的目录。老缓存可能没有这层（直接平铺），所以两种都接受。
    """
    base = cache_dir(rel_root)
    leaf = (rel_root or "").rstrip("/").rsplit("/", 1)[-1]
    if leaf:
        nested = os.path.join(base, leaf)
        if os.path.isdir(nested):
            return nested
    return base


def local_path(rel_root: str, rel: str) -> str:
    """数据集内相对路径 → 本地文件绝对路径。"""
    return os.path.join(content_root(rel_root), rel.strip("/"))


def find_local(rel_root: str, rel: str) -> str:
    """存在则返回本地路径，否则空串。"""
    p = local_path(rel_root, rel)
    return p if os.path.isfile(p) else ""


def object_url(rel_root: str, rel: str) -> str:
    """数据集内相对路径 → 完整 obs:// 对象地址。"""
    return join_bucket(settings.OBS_BUCKET, rel_root + "/" + rel.strip("/")).rstrip("/")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_local_files(rel_root: str):
    """遍历本地文件树，产出 (rel_path, abs_path, size)。

    rel_path 用 / 分隔，相对内容根（= 相对 OBS 前缀）。
    跳过隐藏文件与 obsutil 的临时/断点文件。
    """
    root = content_root(rel_root)
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.endswith(".temp") or name.endswith(".obsutil"):
                continue
            abs_path = os.path.join(dirpath, name)
            if not os.path.isfile(abs_path):
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                continue
            yield rel, abs_path, size


def scan_and_store(conn, reg_id: int, rel_root: str) -> dict:
    """扫描本地文件树，把每个文件的 sha256 写进 task_registration_files。

    这是**哈希基线**的建立点：首次扫描时 synced_sha256 = sha256（视为与云端一致）。
    已存在的行重新算哈希：
      - sha256 与 synced_sha256 一致 → clean
      - 不一致                       → modified（本地被改过，等 sync_worker 回传）
    本地已删除的文件把对应行删掉（云端由 sync_worker 决定要不要删，这里只清索引）。

    返回 {total, modified, new, removed}。
    """
    seen = set()
    new_rows = 0
    modified = 0
    total = 0

    for rel, abs_path, size in iter_local_files(rel_root):
        seen.add(rel)
        total += 1
        try:
            digest = sha256_file(abs_path)
        except OSError:
            continue

        row = conn.execute(
            "SELECT id, sha256, synced_sha256 FROM task_registration_files "
            "WHERE registration_id = ? AND rel_path = ?",
            (reg_id, rel),
        ).fetchone()

        if not row:
            conn.execute(
                """INSERT INTO task_registration_files
                   (registration_id, rel_path, sha256, synced_sha256, size, status)
                   VALUES (?, ?, ?, ?, ?, 'clean')""",
                (reg_id, rel, digest, digest, size),
            )
            new_rows += 1
            continue

        synced = row["synced_sha256"] or ""
        same = (digest == (row["sha256"] or ""))
        if not same:
            status = "clean" if digest == synced else "modified"
            if status == "modified":
                modified += 1
            conn.execute(
                """UPDATE task_registration_files
                   SET sha256 = ?, size = ?, status = ?,
                       modified_at = CASE WHEN ? = 'modified' THEN CURRENT_TIMESTAMP
                                          ELSE modified_at END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (digest, size, status, status, row["id"]),
            )
        elif row["synced_sha256"] == "":
            # 历史行补基线（迁移窗口期）
            conn.execute(
                "UPDATE task_registration_files SET synced_sha256 = ? WHERE id = ?",
                (digest, row["id"]),
            )

    # 本地已不存在的文件：清索引
    existing = conn.execute(
        "SELECT id, rel_path FROM task_registration_files WHERE registration_id = ?",
        (reg_id,),
    ).fetchall()
    removed = 0
    for r in existing:
        if r["rel_path"] not in seen:
            conn.execute("DELETE FROM task_registration_files WHERE id = ?", (r["id"],))
            removed += 1

    return {"total": total, "modified": modified, "new": new_rows, "removed": removed}


def refresh_modified_count(conn, reg_id: int) -> int:
    """把数据集下 modified 文件数冗余回 task_registrations，供列表页直接读。"""
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM task_registration_files "
        "WHERE registration_id = ? AND status = 'modified'",
        (reg_id,),
    ).fetchone()["c"] or 0
    conn.execute(
        "UPDATE task_registrations SET modified_count = ? WHERE id = ?", (n, reg_id)
    )
    return n


def mark_synced(conn, file_id: int) -> None:
    """同步成功后：把云端指纹跟上本地，状态回到 clean。"""
    conn.execute(
        """UPDATE task_registration_files
           SET synced_sha256 = sha256, status = 'clean',
               synced_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (file_id,),
    )
