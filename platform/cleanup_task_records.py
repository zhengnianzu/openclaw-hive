#!/usr/bin/env python3
"""一次性清理 + 收缩 task_records（低峰执行）。

背景见 plan：task_records 因「全量重写 + phantom 行」膨胀到 461 万行 / 1.26GB。
本脚本：备份 -> 删孤儿行 -> 删存量 phantom 行 -> checkpoint + VACUUM 收缩。

安全须知：
  * VACUUM 需要独占锁。强烈建议【先停掉后端服务】再跑本脚本，
    否则会与后台 8s 写任务争锁、相互拖慢，VACUUM 也可能反复重试。
  * 会先把 platform.db 复制一份 .bak.<时间戳>，出问题可回滚。
  * VACUUM 期间会临时占用「与当前库同等大小」的额外磁盘（构建新库文件），
    执行前确认磁盘可用空间 > 当前 platform.db 大小。

用法：
    cd /root/openclaw-hive/platform
    # 建议先停服务，再执行：
    python3 cleanup_task_records.py
"""
import os
import shutil
import sqlite3
import sys
import time

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.db")


def main():
    if not os.path.exists(DB):
        print(f"找不到数据库: {DB}", file=sys.stderr)
        sys.exit(1)

    size_before = os.path.getsize(DB)
    print(f"platform.db 当前大小: {size_before/1024/1024:.1f} MB")

    # 1) 备份
    ts = time.strftime("%Y%m%d%H%M%S")
    bak = f"{DB}.bak.{ts}"
    print(f"备份到 {bak} ...")
    shutil.copy2(DB, bak)

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=30000")

    def count():
        return conn.execute("SELECT COUNT(*) FROM task_records").fetchone()[0]

    print(f"清理前 task_records 行数: {count()}")

    # 2) 删孤儿行（instance 已不在 task_instances）
    cur = conn.execute(
        "DELETE FROM task_records "
        "WHERE instance_id NOT IN (SELECT id FROM task_instances)"
    )
    print(f"  删除孤儿行: {cur.rowcount}")

    # 3) 删存量 phantom 行（config= 未解析时用文件名 task-N.log 兜底建的重复行）
    cur = conn.execute(
        "DELETE FROM task_records WHERE config_name LIKE 'task-%.log'"
    )
    print(f"  删除 phantom 行: {cur.rowcount}")

    conn.commit()
    print(f"清理后 task_records 行数: {count()}")

    # 4) 收缩：先把 WAL 落盘并截断，再 VACUUM 重建紧凑库文件
    print("wal_checkpoint(TRUNCATE) ...")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print("VACUUM ...（可能耗时数分钟，勿中断）")
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size_after = os.path.getsize(DB)
    print(f"platform.db 收缩后大小: {size_after/1024/1024:.1f} MB "
          f"(节省 {(size_before-size_after)/1024/1024:.1f} MB)")
    print("完成。确认服务正常后可删除备份文件。")


if __name__ == "__main__":
    main()
