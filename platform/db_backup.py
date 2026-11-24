#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台数据库备份：把「重要且不可再生」的东西复制到共享盘，并按保留策略清理。

为什么需要这个脚本
------------------
全仓在此之前**没有任何自动化备份**。仅有的备份动作散落在几个一次性修复脚本里
（fix_missing_task_rows.py / cleanup_task_records.py 的 shutil.copy2），只保护
它们自己那一次写操作。日常运行中一旦有人手抖 rm、跑错迁移、或 VACUUM 写坏库，
没有任何可回滚的副本。

备份什么、不备份什么
--------------------
纳入（不可再生，丢了要重配/重扫）：
  * platform.db       业务主库（users / task_instances / harness_configs /
                      batch_experiments / code_repos / wiki 索引…）+ 队列状态
  * task_data.db      两张千万级表（task_records / task_traj_records）+ 代理用量。
                      理论可从 OBS 重扫重建，但代价极高，故按周级低频备份。
  * 配置与密钥        platform/.env、configs/hive_*.yaml、platform/settings/**、
                      platform/wiki/assets/。git 里只有 *.example，真实值只在盘上。

不纳入（可再生或已废弃，理由见 README_backup.md）：
  * platform/wiki/index.db   由 wiki/md/*.md 自动重建
  * platform/hive_output.db  已废弃（见 api/routers/batch_output.py 顶部注释）
  * platform/code/           从代码仓/OBS 重取
  * platform/downloads/      从 OBS 重取
  * OUTPUT_CACHE 本身        沙箱产物，源自 OBS

关键实现约束
------------
1. **必须用 SQLite 在线备份 API（src.backup(dst)），不能用 shutil.copy2。**
   库跑在 WAL 模式，-wal 里可能有 GB 级未 checkpoint 的数据；直接拷主库文件会得到
   「老数据 + 缺尾巴」的库，看起来能打开但内容不对。backup API 拿到一致快照，
   且只持读锁，不阻塞在线的 worker 写入 —— 备份时不需要停服务。

2. **sfs_turbo（NFSv3）不支持 rename。** 实测 os.rename 对文件和目录都返回
   EPERM，所以「写临时名 → 原子改名」不可用。改用完成标记：先把内容写进正式
   名字的目录，**最后**写 manifest.json；list / 保留清理只认「有 manifest.json
   且 complete=true」的目录，中途失败留下的半成品会被 list 标成不完整并跳过。

用法
----
    cd /root/openclaw-hive/platform
    python3 -I db_backup.py run                  # 每日套件（cron）
    python3 -I db_backup.py run --full           # 含 task_data.db（周级）
    python3 -I db_backup.py list                 # 只读：列出现有备份
    python3 -I db_backup.py audit                # 只读：盘点 platform/ 下的存量 .bak

也可以走 ./run.sh db-backup / db-backup-full / db-backup-list / db-backup-audit。
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))          # platform/
HIVE_ROOT = os.path.dirname(HERE)                           # openclaw-hive/

MAIN_DB = os.path.join(HERE, "platform.db")
TASK_DATA_DB = os.path.join(HERE, "database", "task_data.db")

# 默认落点：与 OUTPUT_CACHE 同级，**不在其内部**。
# 放内部的风险：OUTPUT_CACHE 根下的子目录名会被 batch_output._resolve_task_dir
# 的兜底分支当候选拼成 <cache_root>/<traj_name>，没必要让备份目录进入产物解析的
# 搜索空间。可用 --dest 或 BACKUP_DIR 覆盖。
DEFAULT_BACKUP_DIR = ("/mnt/sfs_turbo/s3-asset-b-hd-cce-aifm-nlp-exp/"
                      "zhengnianzu/platform_backups")

# 配置与密钥类文件：全部打进一个 tar.gz。
# 只列**实际配置**，不含 *.example（那些在 git 里）。
CONFIG_ITEMS = [
    (os.path.join(HERE, ".env"), ".env"),
    (os.path.join(HIVE_ROOT, "config.yaml"), "config.yaml"),
    (os.path.join(HIVE_ROOT, "config_openclaw.yaml"), "config_openclaw.yaml"),
    (os.path.join(HERE, "settings"), "settings"),
    (os.path.join(HERE, "wiki", "assets"), "wiki/assets"),
]
# configs/ 下只收 hive_*.yaml（hive_*.yaml.example 在 git 里，没必要重复备份）
CONFIGS_GLOB_DIR = os.path.join(HIVE_ROOT, "configs")
CONFIGS_GLOB_PATTERN = "hive_*.yaml"

KEEP_DAYS = 7        # 备份最多保留最近 7 天（每日套件）
KEEP_FULL_DAYS = 7   # 周级套件同样最多留 7 天（每天最多一份，实际只会留 1~2 份）
KEEP_KIND_LABEL = "7 天（每日 + 周级均按天计算）"

# manifest 里的表行数抽样：恢复后拿它跟现场对齐，能一眼看出备份对不对。
ROWCOUNT_TABLES = ("users", "task_instances", "harness_configs",
                   "batch_experiments", "task_queues")
# task_data.db 里要记行数的表（与主库完全不同，不能共用上面那份清单）
TASK_DATA_ROWCOUNT_TABLES = ("task_records", "task_traj_records")

DIR_RE = r"^\d{8}-\d{6}(\.full)?$"


# ── 小工具 ──────────────────────────────────────────────────────────────────

class Log:
    """同时打到 stdout 和 <dest>/db_backup.log（dest 可能还不存在，惰性打开）。"""

    def __init__(self, dest: str | None):
        self.path = os.path.join(dest, "db_backup.log") if dest else None

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass   # 日志写不进去不该让备份失败


log = Log(None)


def human(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def sha256_of(path: str, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", HIVE_ROOT, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def load_env_value(key: str) -> str | None:
    """读 platform/.env 单键值（与 src/offline_analysis._load_env_value 同款）。"""
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def warn_if_dest_drifts(dest: str) -> None:
    """OUTPUT_CACHE 换盘时，默认备份落点会悄悄留在旧盘上 —— 这里提醒一句。"""
    oc = load_env_value("OUTPUT_CACHE")
    if not oc or not os.path.isabs(oc):
        return
    oc_parent = os.path.dirname(os.path.normpath(oc))
    if os.path.normpath(os.path.dirname(os.path.normpath(dest))) != oc_parent:
        log(f"提示: 备份落点 {dest} 与 OUTPUT_CACHE 的父目录 {oc_parent} 不同。"
            f"若已换共享盘，请同步改 DEFAULT_BACKUP_DIR 或设 BACKUP_DIR。")


# ── SQLite 在线备份 ─────────────────────────────────────────────────────────

def backup_sqlite(src_path: str, local_path: str, tables=None) -> dict:
    """把 src 备份到**本地盘**的 local_path，返回元数据（不含 sha256）。

    tables：要记行数的表。默认 ROWCOUNT_TABLES（主库的业务表）；task_data.db
    要传它自己的两张巨表，否则抽样列表为空、恢复后无从核对。

    分两步而不是直接写到共享盘，是因为 sfs_turbo 的随机读极慢：
    在 NFS 上跑 PRAGMA quick_check 实测会卡进 D 状态、十几分钟不返回
    （顺序读同一文件只要 2 秒）。所以：备份与校验都留本地，最后**顺序**写出去，
    写完不再回读。

    用 backup API：WAL 下拿一致快照、只持读锁、不阻塞写入，因此无需停服务。
    源库以 mode=ro 打开，避免本脚本意外写生产库。
    """
    started = time.monotonic()
    uri = f"file:{src_path}?mode=ro"
    src = sqlite3.connect(uri, uri=True, timeout=60)
    try:
        src.execute("PRAGMA busy_timeout=60000")
        dst = sqlite3.connect(local_path, timeout=60)
        try:
            src.backup(dst)          # 默认整库复制，自动把 -wal 内容折进目标

            # 备份出来的库继承源库的 journal_mode=WAL，于是副本也会带 -wal/-shm。
            # 备份本身是自包含的单文件，留着这两个空壳只会让恢复时困惑
            # （「要不要一起拷？」），这里落成 DELETE 模式收尾，得到一个干净的文件。
            try:
                dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                dst.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass                  # 回退失败不影响正确性，后面清理空壳即可
        finally:
            dst.close()
    finally:
        src.close()

    for suffix in ("-wal", "-shm"):
        try:
            os.remove(local_path + suffix)
        except OSError:
            pass

    # 校验在本地做（毫秒级），不要放到共享盘上做。
    size = os.path.getsize(local_path)
    probe = sqlite3.connect(f"file:{local_path}?mode=ro", uri=True, timeout=60)
    try:
        rows = probe.execute("PRAGMA quick_check").fetchall()
        integrity = "ok" if [r[0] for r in rows] == ["ok"] else "; ".join(
            str(r[0]) for r in rows)
        counts = {}
        for t in (tables or ROWCOUNT_TABLES):
            try:
                counts[t] = probe.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                pass          # 表可能在这个库里不存在（如拿主库的表名去查 task_data）
    finally:
        probe.close()

    return {
        "bytes": size,
        "integrity": integrity,
        "rowcounts": counts,
        "elapsed_sec": round(time.monotonic() - started, 1),
        "check": "quick_check",
    }


def pack_configs(dst_tar: str) -> dict:
    """把配置与密钥打成一个 tar.gz（保留相对路径，避免恢复时放错位置）。"""
    members: list[str] = []
    for path, arc in CONFIG_ITEMS:
        if os.path.exists(path):
            members.append((path, arc))
    if os.path.isdir(CONFIGS_GLOB_DIR):
        for fn in sorted(os.listdir(CONFIGS_GLOB_DIR)):
            if fn.startswith("hive_") and fn.endswith(".yaml"):
                members.append((os.path.join(CONFIGS_GLOB_DIR, fn), f"configs/{fn}"))

    if not members:
        return {"bytes": 0, "files": 0}

    with tarfile.open(dst_tar, "w:gz") as tar:
        for path, arc in members:
            tar.add(path, arcname=arc)   # 目录自动递归；符号链接按原样收录
    return {"bytes": os.path.getsize(dst_tar), "files": len(members)}


# ── 保留策略 ────────────────────────────────────────────────────────────────

def list_backups(dest: str) -> list[dict]:
    """现有备份目录。只认名字符合 YYYYMMDD-HHMMSS[.full] 的，其余一概忽略。"""
    import re
    pat = re.compile(DIR_RE)
    out = []
    if not os.path.isdir(dest):
        return out
    for name in os.listdir(dest):
        if not pat.match(name):
            continue
        full = os.path.join(dest, name)
        if not os.path.isdir(full):
            continue
        mf = os.path.join(full, "manifest.json")
        info = {"name": name, "path": full, "complete": False, "size": 0}
        if os.path.exists(mf):
            try:
                with open(mf, encoding="utf-8") as f:
                    info["manifest"] = json.load(f)
                info["complete"] = bool(info["manifest"].get("complete"))
            except (OSError, ValueError):
                pass
        info["size"] = _dir_size(full)
        out.append(info)
    out.sort(key=lambda x: x["name"], reverse=True)
    return out


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _backup_age_days(name: str) -> float | None:
    """从目录名 YYYYMMDD-HHMMSS[.full] 解析出距今天数；解析不了返回 None。"""
    try:
        ts = datetime.strptime(name.replace(".full", ""), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return (datetime.now() - ts).total_seconds() / 86400.0


def prune(dest: str) -> list[str]:
    """删掉超过保留期的备份 + 所有不完整的半成品。

    **按天算，不按份数**：需求是「最多维持一周的备份」，按份数保留会在
    「一天跑多次」时留下跨越很久的旧备份，按天算才是字面意思。
    只删本脚本自己写的目录（名字正则匹配的那些），别人的目录一概不碰。
    """
    removed = []
    for b in list_backups(dest):
        # 不完整的半成品一律删掉：它要么是上次崩在中途，要么是被打断的
        if not b["complete"]:
            removed.append(b["name"] + "（不完整）")
            shutil.rmtree(b["path"], ignore_errors=True)
            continue
        age = _backup_age_days(b["name"])
        limit = KEEP_FULL_DAYS if b["name"].endswith(".full") else KEEP_DAYS
        if age is None:
            continue                  # 名字不合规的不删（list_backups 已过滤，这里兜底）
        if age > limit:
            removed.append(f"{b['name']}（{age:.0f} 天前，超出 {limit} 天）")
            shutil.rmtree(b["path"], ignore_errors=True)
    return removed


# ── 主流程 ──────────────────────────────────────────────────────────────────

def _lock(dest: str):
    """flock 互斥：防 cron 与手动同时跑。返回持锁的 fd（调用方负责关闭）。"""
    os.makedirs(dest, exist_ok=True)
    fd = os.open(os.path.join(dest, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES):
            raise SystemExit("另一个备份进程正在运行（.lock 被占用），本次跳过。")
        raise
    return fd


def _free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _copy_sequential(src: str, dst: str, logf) -> None:
    """把本地暂存文件**顺序**写到共享盘，写完不校验、不回读。

    为什么不用 shutil.copy2：sfs_turbo 是 NFSv3，随机读极慢且会把进程卡进
    D 状态（不可中断内核等待）。本函数只做 read→write 的线性搬运。
    """
    total = os.path.getsize(src)
    done = 0
    chunk = 8 << 20
    t0 = time.monotonic()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf)
            done += len(buf)
    try:
        shutil.copystat(src, dst)     # 保留 mtime；不计入数据正确性
    except OSError:
        pass
    logf(f"    {os.path.basename(dst)}: {human(total)} "
         f"{time.monotonic() - t0:.1f}s")


def cmd_run(args) -> int:
    dest = os.path.abspath(args.dest)
    global log
    log = Log(dest)

    if not os.path.exists(MAIN_DB):
        log(f"找不到主库 {MAIN_DB}，中止。")
        return 1

    warn_if_dest_drifts(dest)
    os.makedirs(dest, exist_ok=True)
    try:
        os.chmod(dest, 0o700)     # 含密钥，仅 root 可读
    except OSError:
        pass
    lock_fd = _lock(dest)

    local_stage = None
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = ts + (".full" if args.full else "")
        out_dir = os.path.join(dest, name)

        # 预估大小：主库 + 配置 + （full 时）task_data
        need = os.path.getsize(MAIN_DB) + (20 << 20)
        if args.full and os.path.exists(TASK_DATA_DB):
            need += os.path.getsize(TASK_DATA_DB)
        free = _free_bytes(dest)
        if free < need * 1.2:
            log(f"空间不足：需要约 {human(need)}，{dest} 仅剩 {human(free)}，本次跳过。")
            return 2

        # ── 第一阶段：本地暂存 + 校验 ──
        # 在本地盘生成并校验，避免在共享盘上做随机读（实测 sqlite3 的
        # quick_check 在 sfs 上会卡进 D 状态十几分钟）。sfs 上只做顺序写。
        local_stage = tempfile.mkdtemp(prefix="dbbackup-")
        log(f"本地暂存 {local_stage}（{'周级 full' if args.full else '每日'}）")

        manifest = {
            "schema": 1,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "kind": "full" if args.full else "daily",
            "host": os.uname().nodename,
            "git_commit": git_commit(),
            "complete": False,           # 全部成功后才置 True
            "items": {},
        }

        # 1) 主库
        local_db = os.path.join(local_stage, "platform.db")
        log("  platform.db ...")
        r = backup_sqlite(MAIN_DB, local_db)
        r["sha256"] = sha256_of(local_db)
        manifest["items"]["platform.db"] = r
        log(f"    完成 {human(r['bytes'])}  integrity={r['integrity']}  "
            f"{r['elapsed_sec']}s  task_instances={r['rowcounts'].get('task_instances')}")

        # 2) 配置与密钥
        log("  configs.tar.gz ...")
        local_tar = os.path.join(local_stage, "configs.tar.gz")
        cfg = pack_configs(local_tar)
        if cfg["files"]:
            cfg["sha256"] = sha256_of(local_tar)
        manifest["items"]["configs.tar.gz"] = cfg
        log(f"    完成 {human(cfg['bytes'])}  {cfg['files']} 项")

        # 3) 周级：task_data.db
        if args.full:
            if os.path.exists(TASK_DATA_DB):
                log("  task_data.db ...（大库，本地快照可能数十秒）")
                local_td = os.path.join(local_stage, "task_data.db")
                r = backup_sqlite(TASK_DATA_DB, local_td, TASK_DATA_ROWCOUNT_TABLES)
                r["sha256"] = sha256_of(local_td)
                manifest["items"]["task_data.db"] = r
                log(f"    完成 {human(r['bytes'])}  integrity={r['integrity']}  "
                    f"{r['elapsed_sec']}s")
            else:
                log(f"    跳过：{TASK_DATA_DB} 不存在（可能尚未做分库迁移）")

        bad = [k for k, v in manifest["items"].items()
               if v.get("integrity") not in (None, "ok")]
        if bad:
            log(f"校验失败: {bad} —— 丢弃本次结果，不写共享盘。")
            return 3

        # ── 第二阶段：顺序写到共享盘，写完不回读 ──
        log(f"写出 -> {out_dir}")
        os.makedirs(out_dir, exist_ok=True)
        for fn in sorted(os.listdir(local_stage)):
            src_p = os.path.join(local_stage, fn)
            if not os.path.isfile(src_p):
                continue
            _copy_sequential(src_p, os.path.join(out_dir, fn), log)
        log("  写出完成")

        # ── 第三阶段：完成标记 + 保留清理 ──
        # manifest 最后写：它是「这份备份完整可用」的唯一凭据，
        # list / prune 只认有 complete=true manifest 的目录。
        manifest["complete"] = True
        with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        for rm in prune(dest):
            log(f"  清理旧备份: {rm}")

        total = _dir_size(out_dir)
        log(f"完成：{name}（{human(total)}），保留最近 {KEEP_KIND_LABEL}")
        return 0
    finally:
        if local_stage:
            shutil.rmtree(local_stage, ignore_errors=True)
        os.close(lock_fd)


def cmd_list(args) -> int:
    dest = os.path.abspath(args.dest)
    rows = list_backups(dest)
    if not rows:
        print(f"{dest} 下暂无备份。")
        return 0
    print(f"备份目录: {dest}\n")
    print(f"{'名称':<22}{'类型':<8}{'大小':>10}  {'状态':<8}git")
    for b in rows:
        mf = b.get("manifest") or {}
        print(f"{b['name']:<22}{mf.get('kind', '?'):<8}"
              f"{human(b['size']):>10}  "
              f"{'完整' if b['complete'] else '不完整':<8}"
              f"{(mf.get('git_commit') or '')[:8]}")
        rc = (mf.get("items", {}).get("platform.db", {}) or {}).get("rowcounts") or {}
        if rc:
            print(f"    platform.db 行数: "
                  + "  ".join(f"{k}={v}" for k, v in rc.items()))
    print(f"\n共 {len(rows)} 份，合计 {human(sum(b['size'] for b in rows))}")
    return 0


def cmd_audit(args) -> int:
    """只读盘点 platform/ 下的存量 .bak。**不删除任何东西。**

    这些是历史上一堆一次性脚本各自 shutil.copy2 出来的，占了 83GB 且最老到 8 月。
    清理与否由人看过清单后决定，脚本不提供自动删除。
    """
    import glob
    patterns = [
        os.path.join(HERE, "*.db.bak.*"),
        os.path.join(HERE, "backup_*.db"),
        os.path.join(HERE, "database", "*.bak.*"),
    ]
    seen, files = set(), []
    for pat in patterns:
        for p in glob.glob(pat):
            if p.endswith(("-wal", "-shm")) or p in seen:
                continue
            seen.add(p)
            files.append(p)
    if not files:
        print("未发现存量 .bak。")
        return 0

    rows = []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        counts = ""
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
            try:
                n = con.execute("SELECT COUNT(*) FROM task_instances").fetchone()[0]
                counts = f"task_instances={n}"
            finally:
                con.close()
        except sqlite3.Error as e:
            counts = f"({str(e)[:28]})"
        rows.append((st.st_size, st.st_mtime, p, counts))
    rows.sort(reverse=True)

    print(f"{'大小':>10}  {'mtime':<17}{'行数抽样':<28}路径")
    for size, mt, p, counts in rows:
        print(f"{human(size):>10}  "
              f"{datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M'):<17}"
              f"{counts:<28}{os.path.relpath(p, HIVE_ROOT)}")
    print(f"\n共 {len(rows)} 份，合计 {human(sum(r[0] for r in rows))}")
    print("以上仅盘点，未删除任何文件。确认可弃后再手动清理。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="平台数据库备份（默认落点 " + DEFAULT_BACKUP_DIR + "）")
    ap.add_argument("--dest", default=os.environ.get("BACKUP_DIR", DEFAULT_BACKUP_DIR),
                    help="备份根目录；也可用环境变量 BACKUP_DIR")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行一次备份")
    p_run.add_argument("--full", action="store_true",
                       help="周级：额外备份 task_data.db（13G）")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("list", help="只读：列出现有备份").set_defaults(func=cmd_list)
    sub.add_parser("audit", help="只读：盘点存量 .bak（不删除）").set_defaults(func=cmd_audit)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
