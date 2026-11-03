# -*- coding: utf-8 -*-
"""分库迁移：把两张巨表从 platform.db 搬到 database/task_data.db。

## 为什么

task_records(1030 万行) + task_traj_records(915 万行) 连同索引占了 platform.db
的 85%（9.1GB / 10.6GB）。SQLite 的 WAL checkpoint 要把 WAL 回写进主库，库越大
越久，而 checkpoint **独占写锁** —— 10GB 的库在 NFS 盘上轻易超过 busy_timeout
(15s)，于是 queue_scheduler / 各 worker 到处报 `database is locked`。
搬走之后主库约 1.5GB，checkpoint 回到秒级。

## 做法

ATTACH 新库 → `CREATE TABLE ... AS` 不行（丢约束/索引），所以：
  1. 在新库按**原始 DDL** 建表（从 sqlite_master 取，保证列/约束完全一致）
  2. `INSERT INTO taskdata.t SELECT * FROM main.t` 整表搬
  3. 校验行数 + 抽样校验内容
  4. 在新库建回所有索引（原始 DDL）
  5. 只有前面全部成功，才 DROP 主库的表
  6. 主库 VACUUM 回收空间（10GB → 约 1.5GB）

**安全约束**：
  - 校验不过 → 不删原表、新库原样保留供排查，主库完全没变
  - 迁移期间必须停掉所有写进程（worker + uvicorn），否则搬完的表会丢新写入
  - 不用 `--dry-run` 跑完就以为没事：真正的风险在「搬完后主库被 DROP」那一步，
    脚本会在 DROP 前再打印一次行数对比并要求 --yes

用法：
    python migrate_split_db.py --dry-run          # 只看现状与计划，不动数据
    python migrate_split_db.py --copy             # 建新库 + 搬数据 + 校验（不删原表）
    python migrate_split_db.py --drop-old --yes    # 校验通过后删主库旧表
    python migrate_split_db.py --vacuum            # 回收主库空间（耗时，单独一步）
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

PLATFORM_DIR = os.path.dirname(os.path.abspath(__file__))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.core.database import (  # noqa: E402
    TASK_DATA_TABLES, get_db_path, get_task_data_path)

BUSY_TIMEOUT_MS = 60000          # 迁移是独占操作，等久一点无所谓


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _objects_for(conn: sqlite3.Connection, table: str, schema: str = "main") -> dict:
    """取某表的原始 DDL + 它的索引 DDL（跳过 sqlite_ 自动索引）。

    用原始 DDL 而不是自己拼：列顺序、DEFAULT、UNIQUE 约束、部分索引的 WHERE
    必须和原表**逐字一致**，否则迁移后行为会悄悄变（比如 UNIQUE 没了就允许重复行）。
    """
    tbl = conn.execute(
        f"SELECT sql FROM {schema}.sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    idx = conn.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,)).fetchall()
    return {"table_sql": tbl["sql"] if tbl else None,
            "indexes": [(r["name"], r["sql"]) for r in idx]}


def _count(conn: sqlite3.Connection, table: str, schema: str = "main") -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()[0]
    except sqlite3.Error:
        return -1


def _file_size(path: str) -> str:
    try:
        return f"{os.path.getsize(path) / 1e9:.2f}GB"
    except OSError:
        return "—"


def show_status() -> dict:
    """打印主库/新库现状，返回 {table: (main_rows, new_rows)}。"""
    main_path = get_db_path()
    new_path = get_task_data_path()
    print(f"主库 : {main_path}  ({_file_size(main_path)})")
    print(f"新库 : {new_path}  ({_file_size(new_path) if os.path.exists(new_path) else '不存在'})")
    print()

    conn = _connect(main_path)
    has_new = os.path.exists(new_path)
    if has_new:
        conn.execute("ATTACH DATABASE ? AS taskdata", (new_path,))

    out = {}
    print(f"{'表':24s} {'主库行数':>12s} {'新库行数':>12s}")
    for t in TASK_DATA_TABLES:
        m = _count(conn, t, "main")
        n = _count(conn, t, "taskdata") if has_new else -1
        out[t] = (m, n)
        fmt = lambda v: f"{v:,}" if v >= 0 else "无表"
        print(f"  {t:22s} {fmt(m):>12s} {fmt(n):>12s}")
    conn.close()
    return out


def do_copy(sample: int = 200) -> bool:
    """建新库 + 搬数据 + 校验。返回是否全部成功（不删原表）。"""
    main_path = get_db_path()
    new_path = get_task_data_path()
    os.makedirs(os.path.dirname(new_path), exist_ok=True)

    conn = _connect(main_path)
    conn.execute("ATTACH DATABASE ? AS taskdata", (new_path,))
    conn.execute("PRAGMA taskdata.journal_mode=WAL")
    conn.execute("PRAGMA taskdata.synchronous=NORMAL")

    ok = True
    for t in TASK_DATA_TABLES:
        src_rows = _count(conn, t, "main")
        if src_rows < 0:
            print(f"[skip] {t}: 主库里没有这张表（可能已迁移过）")
            continue

        objs = _objects_for(conn, t, "main")
        if not objs["table_sql"]:
            print(f"[skip] {t}: 取不到建表 DDL")
            continue

        dst_rows = _count(conn, t, "taskdata")
        if dst_rows == src_rows and src_rows > 0:
            print(f"[skip] {t}: 新库已有 {dst_rows:,} 行且与主库一致，不重复搬")
            continue
        if dst_rows > 0:
            # 上次搬到一半被打断：清掉重搬，避免 UNIQUE 冲突或半份数据
            print(f"[warn] {t}: 新库已有 {dst_rows:,} 行但与主库 {src_rows:,} 不一致 → 清空重搬")
            conn.execute(f"DELETE FROM taskdata.{t}")
            conn.commit()

        # 1) 建表（把 DDL 里的表名指到 taskdata）
        ddl = objs["table_sql"].replace(
            f"CREATE TABLE {t}", f"CREATE TABLE taskdata.{t}", 1).replace(
            f'CREATE TABLE "{t}"', f'CREATE TABLE taskdata."{t}"', 1)
        if "taskdata." not in ddl:
            print(f"[fail] {t}: 无法把建表 DDL 指向 taskdata，原始 DDL=\n{objs['table_sql'][:200]}")
            ok = False
            continue
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e):
                print(f"[fail] {t}: 建表失败 {e}")
                ok = False
                continue

        # 2) 搬数据。一条 INSERT...SELECT 是单事务，1000 万行约几分钟；
        #    分批反而更慢（每批都要重扫源表定位 offset），这里就整表搬。
        print(f"[copy] {t}: 搬 {src_rows:,} 行 ...", flush=True)
        t0 = time.time()
        conn.execute(f"INSERT INTO taskdata.{t} SELECT * FROM main.{t}")
        conn.commit()
        elapsed = time.time() - t0

        # 3) 校验行数
        dst_rows = _count(conn, t, "taskdata")
        if dst_rows != src_rows:
            print(f"[FAIL] {t}: 行数不一致 主库={src_rows:,} 新库={dst_rows:,}")
            ok = False
            continue
        print(f"[ok]   {t}: {dst_rows:,} 行，{elapsed:.1f}s "
              f"({src_rows / max(elapsed, 0.01):,.0f} 行/s)")

        # 4) 抽样校验内容（行数对了也可能列错位，比如 DDL 列顺序不同）
        if not _verify_sample(conn, t, sample):
            ok = False
            continue

        # 5) 建索引
        for name, idx_sql in objs["indexes"]:
            stmt = idx_sql.replace(f" {name} ", f" taskdata.{name} ", 1)
            if "taskdata." not in stmt:
                print(f"[warn] {t}: 索引 {name} DDL 无法改写，跳过")
                continue
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "already exists" not in str(e):
                    print(f"[warn] {t}: 建索引 {name} 失败 {e}")
        conn.commit()
        n_idx = len(_objects_for(conn, t, "taskdata")["indexes"])
        print(f"[ok]   {t}: 索引 {n_idx}/{len(objs['indexes'])} 个")

    conn.close()
    return ok


def _verify_sample(conn: sqlite3.Connection, table: str, sample: int) -> bool:
    """抽样比对主库与新库的行内容（按主键 id 取样）。"""
    try:
        ids = [r[0] for r in conn.execute(
            f"SELECT id FROM main.{table} ORDER BY RANDOM() LIMIT ?", (sample,))]
    except sqlite3.Error as e:
        print(f"[warn] {table}: 抽样取 id 失败（可能无 id 列）{e}，跳过内容校验")
        return True
    if not ids:
        return True
    marks = ",".join("?" * len(ids))
    a = conn.execute(
        f"SELECT * FROM main.{table} WHERE id IN ({marks}) ORDER BY id", ids).fetchall()
    b = conn.execute(
        f"SELECT * FROM taskdata.{table} WHERE id IN ({marks}) ORDER BY id", ids).fetchall()
    if len(a) != len(b):
        print(f"[FAIL] {table}: 抽样行数不一致 {len(a)} vs {len(b)}")
        return False
    for ra, rb in zip(a, b):
        if tuple(ra) != tuple(rb):
            print(f"[FAIL] {table}: 抽样内容不一致 id={ra['id']}")
            print(f"       主库={tuple(ra)}")
            print(f"       新库={tuple(rb)}")
            return False
    print(f"[ok]   {table}: 抽样 {len(a)} 行内容一致")
    return True


def do_drop_old(assume_yes: bool) -> bool:
    """校验通过后删除主库里的旧表。**这是不可逆的一步**。"""
    main_path = get_db_path()
    new_path = get_task_data_path()
    if not os.path.exists(new_path):
        print("[abort] 新库不存在，先跑 --copy")
        return False

    conn = _connect(main_path)
    conn.execute("ATTACH DATABASE ? AS taskdata", (new_path,))

    # 删之前最后一次核对：行数必须完全一致
    plan = []
    for t in TASK_DATA_TABLES:
        m, n = _count(conn, t, "main"), _count(conn, t, "taskdata")
        if m < 0:
            print(f"[skip] {t}: 主库已无此表")
            continue
        if n != m:
            print(f"[abort] {t}: 行数不一致 主库={m:,} 新库={n:,} —— 不删")
            conn.close()
            return False
        plan.append((t, m))
        print(f"[check] {t}: 主库={m:,} 新库={n:,} 一致 ✓")

    if not plan:
        print("没有需要删除的表")
        conn.close()
        return True
    if not assume_yes:
        print("\n加 --yes 才会真的删除主库旧表（不可逆）")
        conn.close()
        return False

    for t, n in plan:
        conn.execute(f"DROP TABLE main.{t}")
        print(f"[drop] main.{t}（{n:,} 行已在新库）")
    conn.commit()
    conn.close()
    print("\n旧表已删除。主库文件大小要等 --vacuum 才会真正缩小。")
    return True


def do_vacuum() -> None:
    """回收主库空间。DROP TABLE 只把页标记为空闲，文件不缩小，必须 VACUUM。

    VACUUM 需要约等于当前库大小的临时空间，且全程独占锁。
    """
    main_path = get_db_path()
    print(f"VACUUM 前: {_file_size(main_path)}")
    print("（需要约同等大小的临时空间，全程独占锁，请确保已停掉所有写进程）", flush=True)
    t0 = time.time()
    conn = _connect(main_path)
    conn.execute("VACUUM")
    conn.close()
    print(f"VACUUM 后: {_file_size(main_path)}  耗时 {time.time()-t0:.1f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description="把两张巨表迁到 database/task_data.db")
    ap.add_argument("--dry-run", action="store_true", help="只看现状，不动数据")
    ap.add_argument("--copy", action="store_true", help="建新库+搬数据+校验（不删原表）")
    ap.add_argument("--drop-old", action="store_true", help="删主库旧表（需 --yes）")
    ap.add_argument("--vacuum", action="store_true", help="回收主库空间")
    ap.add_argument("--yes", action="store_true", help="确认不可逆操作")
    ap.add_argument("--sample", type=int, default=200, help="抽样校验行数（默认 200）")
    a = ap.parse_args()

    if not any((a.dry_run, a.copy, a.drop_old, a.vacuum)):
        ap.print_help()
        print("\n推荐顺序：--dry-run → --copy → --drop-old --yes → --vacuum")
        return

    print("=" * 70)
    show_status()
    print("=" * 70)

    if a.dry_run:
        print("\n[dry-run] 不做任何修改。下一步：--copy")
        return
    if a.copy:
        print("\n>>> 搬数据\n")
        ok = do_copy(a.sample)
        print("\n" + "=" * 70)
        show_status()
        print("=" * 70)
        print("\n✅ 搬运与校验全部通过，下一步：--drop-old --yes" if ok
              else "\n❌ 有失败项，主库未改动，请看上面的 [FAIL]")
        return
    if a.drop_old:
        print("\n>>> 删除主库旧表\n")
        do_drop_old(a.yes)
        return
    if a.vacuum:
        print("\n>>> 回收主库空间\n")
        do_vacuum()


if __name__ == "__main__":
    main()
