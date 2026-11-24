# -*- coding: utf-8 -*-
"""数据集同步 worker：把本地改过的文件异步回传到 OBS。

背景（与 api/services/dataset_files.py 配套）：
    数据集注册后整份下载到本地，平台的浏览/编辑全部读写本地副本。
    保存只写本地（毫秒级、原子），云端一致性交给这个进程。

判定依据是**哈希**，不是时间戳：
    task_registration_files.sha256          本地当前内容
    task_registration_files.synced_sha256   上次成功上传时的内容
    status = 'modified'  ⇔  两者不等

每轮：
    1. 取 status='modified' 的行
    2. 传之前重算一次本地哈希 —— 期间又被编辑过就跳过（下一轮再传），
       避免把「上传瞬间的旧内容」标记成已同步
    3. 传成功后重算哈希，仍等于刚才上传的那份才 mark_synced
       （上传耗时时文件可能又被改了，那就保持 modified，下轮再传）
    4. 更新 task_registrations.last_synced_at

用法:
    python offline/sync_worker.py --once            # 扫一轮就退出（调试/CI）
    python offline/sync_worker.py --interval 30     # 常驻，每 30s 一轮（默认）
    python offline/sync_worker.py --once --reg 10   # 只同步指定数据集
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.config import settings          # noqa: E402
from api.core.database import get_connection  # noqa: E402
from api.services import dataset_files as dfs  # noqa: E402
from offline.heartbeat import write_heartbeat  # noqa: E402

DEFAULT_INTERVAL = 30
WORKER_NAME = "sync-worker"


def _log(msg: str) -> None:
    print(f"[sync {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _upload(local_path: str, cloud_url: str) -> tuple[bool, str]:
    """上传单个文件。-f 覆盖同名对象。

    不加 -u：增量模式靠大小/mtime 判断，内容等长时会被判为「没变」而漏传，
    而这里恰恰经常是「等长内容替换」。
    """
    try:
        proc = subprocess.run(
            [settings.OBSUTIL_PATH, "cp", local_path, cloud_url, "-f"],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "obsutil 超时"
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return False, detail[:400] or f"exit={proc.returncode}"
    return True, ""


def sync_once(reg_id: int | None = None) -> dict:
    """扫一轮。返回统计，便于 --once 模式下观察。"""
    stats = {"regs": 0, "files": 0, "ok": 0, "failed": 0, "skipped": 0}

    with get_connection() as conn:
        sql = """
            SELECT f.id, f.registration_id, f.rel_path, f.sha256, f.status,
                   r.task_path_obs
              FROM task_registration_files f
              JOIN task_registrations r ON r.id = f.registration_id
             WHERE f.status = 'modified'
        """
        params: list = []
        if reg_id is not None:
            sql += " AND f.registration_id = ?"
            params.append(reg_id)
        sql += " ORDER BY f.registration_id, f.rel_path"
        rows = conn.execute(sql, params).fetchall()

    touched = set()
    for row in rows:
        stats["files"] += 1
        rel_root = dfs.dataset_root(row)
        if not rel_root:
            stats["skipped"] += 1
            continue

        local = dfs.find_local(rel_root, row["rel_path"])
        if not local:
            # 文件被本地删掉了。当前策略是「不自动删除云端对象」：
            # 删云端不可逆，误判代价太大。保持 modified 让人工处理。
            _log(f"跳过 {row['rel_path']}：本地文件不存在（云端不自动删除）")
            stats["skipped"] += 1
            continue

        # 传之前先确认本地内容还是数据库里记的那份
        try:
            digest = dfs.sha256_file(local)
        except OSError as e:
            _log(f"读取失败 {row['rel_path']}: {e}")
            stats["failed"] += 1
            continue
        if digest != (row["sha256"] or ""):
            # 期间又被编辑过 —— 让下一轮用新内容传，避免上传中间态
            with get_connection() as conn:
                conn.execute(
                    "UPDATE task_registration_files SET sha256 = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (digest, row["id"]))
            stats["skipped"] += 1
            continue

        cloud_url = dfs.object_url(rel_root, row["rel_path"])
        ok, err = _upload(local, cloud_url)
        if not ok:
            _log(f"上传失败 {rel_root}/{row['rel_path']}: {err}")
            stats["failed"] += 1
            continue

        # 上传期间可能又被改了：重算，只有仍是这份内容才标记已同步
        try:
            after = dfs.sha256_file(local)
        except OSError:
            after = digest
        with get_connection() as conn:
            if after == digest:
                dfs.mark_synced(conn, row["id"])
            else:
                conn.execute(
                    "UPDATE task_registration_files SET sha256 = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?", (after, row["id"]))
            dfs.refresh_modified_count(conn, row["registration_id"])
            conn.execute(
                "UPDATE task_registrations SET last_synced_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["registration_id"],),
            )
        touched.add(row["registration_id"])
        stats["ok"] += 1

    stats["regs"] = len(touched)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="数据集本地改动 → OBS 异步同步")
    ap.add_argument("--once", action="store_true", help="扫一轮就退出")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="常驻轮询秒数")
    ap.add_argument("--reg", type=int, default=None, help="只同步指定数据集 id")
    args = ap.parse_args()

    if args.once:
        stats = sync_once(args.reg)
        print(f"[sync] 本轮: {stats}", flush=True)
        return

    _log(f"启动，轮询间隔 {args.interval}s")
    while True:
        t0 = time.time()
        try:
            stats = sync_once(args.reg)
            if stats["ok"] or stats["failed"]:
                _log(f"{stats}")
            # 心跳：只写同步动作数，不写 regs（那是「这一轮碰过几个数据集」，
            # 静默轮恒为 0，作为进度没有意义）。失败数单独记。
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(),
                            stats["ok"], stats["failed"],
                            f"跳过={stats['skipped']} 文件={stats['files']}")
        except Exception as e:
            _log(f"异常: {e}")
            write_heartbeat(WORKER_NAME, os.getpid(), t0, time.time(), 0, 1,
                            f"本轮异常: {str(e)[:150]}")
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    main()
