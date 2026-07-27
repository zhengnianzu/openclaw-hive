"""
把 instances/ 目录下已存在的实例回填到 task_instances 表。
- id            = 目录名
- name          = 目录名中间段(task_name)
- config_path   = <dir>/config.yaml (绝对路径)
- status        = 终态: 无 failed -> completed, 有 failed -> finished
- completed/failed = 读 outputs/config/{complete,failed}.jsonl 行数
- total_tasks   = completed + failed
- concurrent_num= 从 config.yaml run_config.concurrent_num 读取
- created_by    = admin (目录无此信息, 兜底)
幂等: 已存在的 id 跳过。
"""
import os
import sqlite3
from pathlib import Path
from datetime import datetime

try:
    from omegaconf import OmegaConf
    HAS_OC = True
except Exception:
    HAS_OC = False

HERE = Path(__file__).resolve().parent
INSTANCES_DIR = HERE / "instances"
DB_PATH = HERE / "platform.db"


def count_lines(p: Path) -> int:
    if not p.exists():
        return 0
    n = 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            n += chunk.count(b"\n")
    return n


def read_concurrent(cfg_path: Path) -> int:
    if not (HAS_OC and cfg_path.exists()):
        return 100
    try:
        cfg = OmegaConf.load(str(cfg_path))
        return int(OmegaConf.select(cfg, "run_config.concurrent_num") or 100)
    except Exception:
        return 100


def parse_name(instance_id: str) -> str:
    parts = instance_id.split("-")
    return parts[1] if len(parts) >= 3 else instance_id


def dir_mtime_iso(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat()


def main():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    existing = {r[0] for r in conn.execute("SELECT id FROM task_instances")}

    inserted, skipped = [], []
    for d in sorted(INSTANCES_DIR.iterdir()):
        if not d.is_dir():
            continue
        instance_id = d.name
        cfg = d / "config.yaml"
        if not cfg.exists():
            skipped.append((instance_id, "no config.yaml"))
            continue
        if instance_id in existing:
            skipped.append((instance_id, "already in db"))
            continue

        out_dir = d / "outputs" / "config"
        completed = count_lines(out_dir / "complete.jsonl")
        failed = count_lines(out_dir / "failed.jsonl")
        total = completed + failed
        status = "completed" if failed == 0 else "finished"
        concurrent = read_concurrent(cfg)
        created = dir_mtime_iso(d)

        conn.execute(
            """INSERT INTO task_instances
               (id, name, config_path, status, created_by,
                total_tasks, completed_tasks, failed_tasks, concurrent_num,
                created_at, stopped_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_id, parse_name(instance_id), str(cfg), status, "admin",
             total, completed, failed, concurrent, created, created),
        )
        inserted.append((instance_id, status, f"{completed}/{failed}/{total}"))

    conn.commit()
    conn.close()

    print(f"插入 {len(inserted)} 条:")
    for i in inserted:
        print(f"  + {i[0]:28s} status={i[1]:9s} c/f/total={i[2]}")
    if skipped:
        print(f"跳过 {len(skipped)} 条:")
        for s in skipped:
            print(f"  - {s[0]:28s} ({s[1]})")


if __name__ == "__main__":
    main()
