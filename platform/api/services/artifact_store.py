# -*- coding: utf-8 -*-
"""沙箱产物：本地磁盘导航 + 预览。

架构
----
产物由 `offline/output_worker.py` 的 `_consume_artifacts` **全量预下载**到
`OUTPUT_CACHE/<batch>/<leaf>/`（与 deep 详情同一个 task 目录，见
`src/offline_analysis._cache_subdir_for`）。本模块只读本地磁盘，**读路径零 obsutil 调用**。

与上一版的区别：不再有「按层 ls + TTL + 单飞 + 失败退避」这套缓存。实测下来：
  - 目录视图根层只有 4 个目录、0 个文件，用户以为坏了；
  - 每次展开都打一次 obsutil，递归整 task 2~65 秒（OBS 长尾波动大）；
  - 缓存层复杂度远大于收益。
全量下载后本地遍历是毫秒级，且同一份数据同时满足「工作区 tab」和「会话详情」。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from ..core.config import settings

# 单文件读取上限：超过只读尾部，防超大 jsonl 拖垮进程
TEXT_MAX_BYTES = 20 * 1024 * 1024
# LibreOffice 转换超时
SOFFICE_TIMEOUT = 180

# 交付件格式分类。前端 utils/artifacts.js 的 GROUP_RULES / previewKindOf 必须与此保持一致，
# 否则会出现「后端按文本返回、前端当二进制处理」之类的错配。
#
# 需要 LibreOffice 转 PDF 才能预览的（缺少 soffice 时降级为仅下载）：
_OFFICE_EXTS = {
    ".pptx", ".ppt", ".ppsx", ".pps",        # PowerPoint
    ".docx", ".doc", ".rtf", ".odt",         # Word
    ".xlsx", ".xls", ".xlsm", ".ods",         # Excel
    # 注意：csv/tsv 不在这里。它们是纯文本，前端用表格渲染器直接读，
    # 拖进 LibreOffice 只会多花几十秒且丢精度。
}
# 浏览器原生可渲染的：
_HTML_EXTS = {".html", ".htm", ".xhtml"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
               ".bmp", ".ico", ".tiff", ".avif"}
# 纯文本直读：
_TEXT_EXTS = {".txt", ".log", ".md", ".markdown", ".json", ".jsonl", ".ndjson",
              ".yaml", ".yml", ".xml", ".toml", ".ini", ".conf", ".cfg",
              ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash",
              ".sql", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".css", ".scss",
              ".tsv", ".env", ".gitignore", ".dockerfile"}
# PDF 单独一类（浏览器内置阅读器，无需转换）
_PDF_EXTS = {".pdf"}

# 不是沙箱交付件的目录，遍历时必须跳过：
#   _pdf/ = LibreOffice 转换结果缓存（平台生成的中间产物）
#   .git/ = 沙箱 agent 的版本库元数据（objects/refs 下大量空目录，纯粹拖慢遍历）
_INTERNAL_DIRS = {"_pdf", ".git"}


# ── 路径与安全 ──────────────────────────────────────────────────────────────

def cache_root() -> str:
    """产物缓存根 = OUTPUT_CACHE（与 worker 的 origin 必须解析成同一个绝对路径）。

    解析基准必须与 worker 一致：offline_analysis._load_env_cache_dir 对相对路径是
    **基于 platform/ 解析**的。这里若用 abspath（跟进程 CWD），uvicorn 不从 platform/
    启动时两边就指向不同目录 —— 症状是文件明明在盘上，/tree 却恒返回
    「该 task 尚未下载到本地」，且没有任何报错可查。
    """
    root = settings.OUTPUT_CACHE
    if not os.path.isabs(root):
        # platform/ 目录 = api/services -> api -> platform
        platform_dir = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        root = os.path.join(platform_dir, root)
    root = os.path.normpath(root)
    os.makedirs(root, exist_ok=True)
    return root


def clean_rel(key: str) -> str:
    """把 task 内相对路径规整成安全的相对路径；非法抛 ValueError。

    拒绝 `..`、反斜杠（windows 沙箱产出的 key 可能用反斜杠）、NUL、超长段。
    """
    if not isinstance(key, str) or not key:
        raise ValueError("空路径")
    if key.startswith("/") or "\\" in key or "\x00" in key:
        raise ValueError("非法路径")
    parts = [p for p in key.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("非法路径")
    if any(len(p.encode("utf-8")) > 255 for p in parts):
        raise ValueError("路径段过长")
    return "/".join(parts)


def safe_join(root: str, rel: str) -> str:
    """root 下拼 rel 并做 realpath 包含性校验（防 ../ 穿越与软链出逃）。"""
    target = os.path.realpath(os.path.join(root, *rel.split("/")))
    real_root = os.path.realpath(root)
    if not (target == real_root or target.startswith(real_root + os.sep)):
        raise ValueError("路径越界")
    return target


def task_local_dir(cache_subdir: str) -> str:
    """某个 task 的本地目录（绝对路径）。

    cache_subdir 由调用方用 `_cache_subdir_for(task_obs)` 算好（如
    `frontend_1023_1143/task_175`），这里只做拼接与包含性校验。
    """
    rel = clean_rel(cache_subdir)
    return safe_join(cache_root(), rel)


def is_downloaded(cache_subdir: str) -> bool:
    try:
        return os.path.isdir(task_local_dir(cache_subdir))
    except ValueError:
        return False


# ── 本地目录树 ──────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> int:
    return int(n)


def walk_tree(root: str, rel_prefix: str = "", max_entries: int = 20000) -> list[dict]:
    """递归遍历本地目录，返回扁平文件列表（不含目录）。

    每项 {rel, name, size, mtime}，rel 是相对 root 的路径。目录结构由前端从 rel 推导。

    `_pdf/` 是 LibreOffice 转换的中间产物目录，`.git/` 是沙箱 agent 的版本库元数据，
    都不是沙箱产出的交付件，必须跳过。
    """
    out, _truncated = walk_tree_limited(root, max_entries)
    return out


def walk_tree_limited(root: str, max_entries: int = 20000,
                      time_budget: float = 20.0) -> tuple[list[dict], bool]:
    """同 walk_tree，额外返回「是否被截断」，并带墙钟上限。

    目录在 NFS 上，单个 task 实测可达 3.3 万文件；遍历期间必须能主动放弃，
    否则调用方（接口）会长时间占住。截断必须显式告诉前端，不能静默少列文件。
    """
    out: list[dict] = []
    root = os.path.realpath(root)
    started = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地过滤：os.walk 会尊重这个修改
        dirnames[:] = sorted(d for d in dirnames if d not in _INTERNAL_DIRS)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append({
                "rel": rel,
                "name": fn,
                "size": _fmt_size(st.st_size),
                "mtime": st.st_mtime,
            })
            if len(out) >= max_entries:
                return out, True
        if time.monotonic() - started > time_budget:
            return out, True
    return out, False


def list_subdirs(root: str) -> list[str]:
    """root 下的直接子目录名（用于列出实例的 task 目录）。"""
    try:
        with os.scandir(root) as it:
            return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


# ── 文本读取 ────────────────────────────────────────────────────────────────

def read_text_slice(path: str, offset: int = 0, limit: int = 200_000) -> dict:
    """按【字符】分页读取文本（与 logs.py obs-view 的语义一致）。

    按字符而非按行：trajectory.jsonl / models.json 这类文件行数很少但单行十几万字符，
    按行分页拆不开，前端渲染整坨超长单行会卡死。
    """
    size = os.path.getsize(path)
    truncated_file = size > TEXT_MAX_BYTES
    with open(path, "rb") as f:
        if truncated_file:
            f.seek(size - TEXT_MAX_BYTES)
        data = f.read()
    text = data.decode("utf-8", "replace")

    total = len(text)
    offset = max(0, min(offset, total))
    chunk = text[offset:offset + limit]
    return {
        "text": chunk,
        "offset": offset,
        "next_offset": offset + len(chunk),
        "has_more": offset + len(chunk) < total,
        "total_chars": total,
        "truncated_file": truncated_file,
    }


# ── 类型判定 ────────────────────────────────────────────────────────────────

def ext_of(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def media_type(name: str) -> tuple[str, bool]:
    """返回 (MIME, 是否允许内联)。html/svg 内联时由路由加 CSP sandbox。

    判定顺序很重要：csv/tsv 既是「表格」也是「纯文本」，这里按**纯文本**返回，
    前端用表格渲染器展示（不需要 LibreOffice）。
    """
    ext = ext_of(name)
    if ext in _HTML_EXTS:
        return "text/html; charset=utf-8", True
    if ext == ".svg":
        return "image/svg+xml", True
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg", True
    if ext in _PDF_EXTS:
        return "application/pdf", True
    # 纯文本优先于 office：csv/tsv 不走 LibreOffice
    if ext in _TEXT_EXTS or ext in (".csv", ".tsv"):
        return "text/plain; charset=utf-8", True
    if ext in _IMAGE_EXTS:
        mime = {".tiff": "image/tiff", ".avif": "image/avif"}.get(
            ext, f"image/{ext.lstrip('.')}")
        return mime, True
    return "application/octet-stream", False


def is_office(name: str) -> bool:
    return ext_of(name) in _OFFICE_EXTS


def soffice_available() -> bool:
    return bool(shutil.which(settings.SOFFICE_PATH))


_PDF_NAME = re.compile(r"[^\w.\-]+")


def pdf_cache_path(task_dir: str, rel: str) -> str:
    """office 文件对应的 PDF 缓存路径（放在 task 目录下的 _pdf/ 子目录）。"""
    flat = _PDF_NAME.sub("_", rel.replace("/", "__")) + ".pdf"
    return os.path.join(task_dir, "_pdf", flat)


def _pdf_is_fresh(pdf: str, src: str) -> bool:
    """PDF 比源文件新、且非空即视为有效。

    校验大小是必须的：NFSv3 上不能 rename，缓存只能原地覆盖写，
    覆盖过程中崩溃会留下一个 mtime 很新的**半截文件**，只看 mtime 会把它
    当成有效缓存一直返回。
    """
    try:
        if os.path.getsize(pdf) < 1024:      # 正常 PDF 不会小于 1KB
            return False
        return os.path.getmtime(pdf) >= os.path.getmtime(src)
    except OSError:
        return False


def ensure_pdf(task_dir: str, rel: str) -> str:
    """把 office 文件转成 PDF 并缓存，返回 PDF 绝对路径（同步实现，调用方放线程池）。

    跨进程隔离：每次调用带独立 `-env:UserInstallation` profile，否则多个 uvicorn
    worker 各自开的 soffice 会共享 ~/.config/libreoffice 的 profile 锁而互相挂死。
    杀进程用进程组（真正干活的子进程是 soffice.bin）。
    """
    if not soffice_available():
        raise RuntimeError("未安装 LibreOffice，无法转换预览，请下载后查看")

    src = safe_join(task_dir, rel)
    if not os.path.isfile(src):
        raise FileNotFoundError(rel)

    pdf = pdf_cache_path(task_dir, rel)
    if os.path.isfile(pdf) and _pdf_is_fresh(pdf, src):
        return pdf

    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="lo-")
    profile = tempfile.mkdtemp(prefix="loprof-")
    try:
        proc = subprocess.run(
            [settings.SOFFICE_PATH, "--headless", "--norestore",
             f"-env:UserInstallation=file://{profile}",
             "--convert-to", "pdf", "--outdir", workdir, src],
            capture_output=True, text=True, cwd=workdir,
            timeout=SOFFICE_TIMEOUT, start_new_session=True,
        )
        produced = None
        for fn in os.listdir(workdir):
            if fn.lower().endswith(".pdf"):
                produced = os.path.join(workdir, fn)
                break
        if not produced:
            raise RuntimeError(
                f"LibreOffice 未产出 PDF（exit={proc.returncode}）: "
                f"{(proc.stderr or proc.stdout or '')[-300:]}")
        # 注意：这里**不能**用 os.replace/os.rename —— OUTPUT_CACHE 在 sfs_turbo 上，
        # 是 NFSv3（无 rename 语义），实测 os.replace 直接 EPERM（Operation not permitted）。
        # 原地覆盖写：先写 .part，再 copyfile 到目标名（copyfile 是 open+write，不含 rename），
        # 最后删 .part。代价是目标文件在被覆盖的瞬间不是原子的，所以 _pdf_is_fresh
        # 还要校验大小，避免把半截文件当成有效缓存返回。
        tmp = pdf + ".part"
        shutil.copyfile(produced, tmp)
        shutil.copyfile(tmp, pdf)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return pdf
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)


# ── 下载状态查询（读 task_traj_records 的产物列） ───────────────────────────

def artifact_rows(instance_id: str) -> dict[str, dict]:
    """该实例的产物下载状态，key = traj_name。"""
    from ..core.database import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT traj_name, artifacts_status, error, artifact_files, "
            "artifact_bytes, artifact_downloaded_at, artifact_complete, updated_at "
            "FROM task_traj_records WHERE instance_id=?",
            (instance_id,),
        ).fetchall()
    return {r["traj_name"]: dict(r) for r in rows}
