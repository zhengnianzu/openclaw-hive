#!/usr/bin/env python3
"""拆分旧格式重跑实例的 complete.jsonl（一次性修复）。

## 背景

「重跑失败」会新建实例并把源实例的 complete.jsonl 整个拷过来当跳过名单。
旧实现直接写进 complete.jsonl，而平台按 **行数** 算 completed_tasks，
于是新实例从创建起就顶着源实例的成功数（生产出现过 total=32 / completed=68）。

修复后的实现把继承记录写进独立的 complete.inherited.jsonl，complete.jsonl
只留本轮真正跑出来的。但**修复前已存在的 292 个重跑实例仍是混合格式**，
需要本脚本回溯拆分。

## 拆分依据

旧实现下：complete(X) = complete(S) ∪ 本轮成功(X)
其中 S = X.copied_from。所以：

    继承集合(X) = complete(S) 的「累计成功集合」
    本轮成功(X) = complete(X) − 继承集合(X)

链式重跑（A→B→C）要递归：B 若已被本脚本修过，它的 complete.jsonl 已只含本轮，
此时继承(C) = complete(B) ∪ inherited(B)。下面用 collect_chain_success() 处理。

## 安全须知

  * 默认 **dry-run**，只打印不写。确认数字无误后加 --apply 才真正落盘。
  * --apply 会先备份 platform.db（sqlite3 backup API）和每个被改动的 jsonl 文件。
  * 实例目录在 /mnt/sfs_turbo（NFSv3，**不支持 rename**），所以只能原地
    truncate 重写，不能用「临时文件 + os.replace」。这也是要备份的原因：
    写到一半失败无法靠 rename 回滚。
  * 不修改任何实例的 status；只改 complete.jsonl / complete.inherited.jsonl /
    failed.jsonl 三个文件，以及 task_instances 的 completed_tasks / failed_tasks /
    total_tasks 三列。

用法：
    cd /root/openclaw-hive/platform
    python3 fix_inherited_counts.py                 # dry-run，看影响面
    python3 fix_inherited_counts.py --apply         # 真正写入
    python3 fix_inherited_counts.py --apply --only <instance_id>   # 只修一个
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.db")
INHERITED = "complete.inherited.jsonl"
COMPLETE = "complete.jsonl"
FAILED = "failed.jsonl"


def read_lines(path):
    """读 jsonl，返回去重保序的列表。文件不存在返回 []。"""
    if not os.path.exists(path):
        return []
    out, seen = [], set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
    except OSError as e:
        print(f"    [warn] 读取失败 {path}: {e}")
    return out


def write_lines(path, lines):
    """原地 truncate 写（NFSv3 不支持 rename）。"""
    with open(path, "w", encoding="utf-8") as f:
        if lines:
            f.write("\n".join(lines) + "\n")


def output_dir(config_path):
    stem = Path(config_path).stem
    return os.path.join(str(Path(config_path).parent), "outputs", stem)


def count_lines(path):
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            n += chunk.count(b"\n")
    return n


def collect_chain_success(inst_id, by_id, _seen=None):
    """实例 inst_id 的「累计成功集合」= 本轮成功 ∪ 更早各代的成功。

    兼容两种格式：
      - 新格式（有 complete.inherited.jsonl）：complete ∪ inherited
      - 旧格式（只有 complete.jsonl）：complete 本身就是累计的
    """
    _seen = _seen or set()
    if inst_id in _seen or inst_id not in by_id:
        return set()
    _seen.add(inst_id)
    r = by_id[inst_id]
    od = output_dir(r["config_path"])
    cur = set(read_lines(os.path.join(od, COMPLETE)))
    inh_file = os.path.join(od, INHERITED)
    if os.path.exists(inh_file):
        return cur | set(read_lines(inh_file))
    # 旧格式：complete 已是累计集合，直接用
    return cur


def success_from_logs(config_path):
    """源实例已删除时的兜底：从本实例本地日志还原「本次真正跑成功」的集合。

    task-<idx>.log 是 hive 每个任务收尾写的，含 `config=<name>.json` 与
    `任务执行状态=任务成功` 两行。它是本实例自己跑出来的结果，
    天然不含继承 —— 正是我们要的「本轮成功」。

    返回 (success_set, failed_set)。日志目录不存在返回 (None, None)。
    """
    import re
    from concurrent.futures import ThreadPoolExecutor

    od = output_dir(config_path)
    logs_dir = os.path.join(od, "logs")
    if not os.path.isdir(logs_dir):
        return None, None
    try:
        names = [f for f in os.listdir(logs_dir)
                 if f.startswith("task-") and f.endswith(".log")]
    except OSError:
        return None, None
    if not names:
        return None, None

    cfg_re = re.compile(r"config=(\S+?\.json)")
    st_re = re.compile(r"任务执行状态=(任务成功|任务失败|任务异常)")

    def scan(fname):
        try:
            txt = open(os.path.join(logs_dir, fname),
                       encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        m = cfg_re.findall(txt)
        s = st_re.findall(txt)
        if not m:
            return None
        return (os.path.basename(m[-1]), s[-1] if s else None)

    ok, bad = set(), set()
    with ThreadPoolExecutor(max_workers=32) as ex:
        for res in ex.map(scan, names):
            if not res:
                continue
            name, status = res
            if status == "任务成功":
                ok.add(name)
            elif status in ("任务失败", "任务异常"):
                bad.add(name)
    return ok, bad


def chain_depth(inst_id, by_id, _seen=None):
    """链深度：0 = 链头（不是重跑出来的）。用于自顶向下处理。"""
    _seen = _seen or set()
    if inst_id in _seen or inst_id not in by_id:
        return 0
    _seen.add(inst_id)
    src = by_id[inst_id].get("copied_from")
    if not src or src not in by_id:
        return 0
    return 1 + chain_depth(src, by_id, _seen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    ap.add_argument("--only", default=None, help="只处理指定实例 id")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print(f"找不到数据库: {DB}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, status, copied_from, config_path, created_at, "
        "       total_tasks, completed_tasks, failed_tasks "
        "FROM task_instances"
    )]
    conn.close()
    by_id = {r["id"]: r for r in rows}

    targets = [r for r in rows if r.get("copied_from")]
    if args.only:
        targets = [r for r in targets if r["id"] == args.only]
    if not targets:
        print("没有需要修复的重跑实例。")
        return

    # 自顶向下：先修链头附近的，链尾才能拿到正确的源集合
    targets.sort(key=lambda r: (chain_depth(r["id"], by_id), r["created_at"] if "created_at" in r else ""))

    print(f"{'实例':34} {'状态':10} {'旧ok':>7} {'继承':>7} {'本轮ok':>7} {'新ok':>7} {'total':>7}")
    print("-" * 92)

    plans = []
    skipped_running = []
    skipped_no_logs = []
    for r in targets:
        od = output_dir(r["config_path"])
        if not os.path.isdir(od):
            print(f"  [skip] {r['id'][:32]:34} 输出目录不存在")
            continue
        comp_path = os.path.join(od, COMPLETE)
        if not os.path.exists(comp_path):
            continue

        # 正在运行的实例绝对不能动：hive 正以 append 模式写 complete.jsonl，
        # 此刻 truncate 会让它的写入偏移错乱，后续 append 直接写进空洞。
        # 等它跑完、或用新口径重跑即可。
        if r["status"] in ("running", "preparing"):
            skipped_running.append(r["id"])
            continue

        src = r["copied_from"]
        cur = read_lines(comp_path)
        if src in by_id:
            inherited = collect_chain_success(src, by_id)
            inherited_in_file = [x for x in cur if x in inherited]
            this_round = [x for x in cur if x not in inherited]
        else:
            # 源实例已被删除：用本地日志还原本轮成功集合。
            # 日志是本实例自己跑出来的，天然不含继承。
            ok_set, bad_set = success_from_logs(r["config_path"])
            if ok_set is None:
                skipped_no_logs.append(r["id"])
                continue
            this_round = [x for x in cur if x in ok_set]
            inherited_in_file = [x for x in cur if x not in ok_set]
            if not inherited_in_file:
                continue

        # 没有实际继承（源文件已被清理/为空）→ 跳过，避免误伤
        if not inherited_in_file:
            continue

        old_ok = count_lines(comp_path)
        new_ok = len(this_round)
        total = r["total_tasks"] or 0
        new_total = max(0, total - len(inherited_in_file))
        plans.append({
            "inst": r, "od": od, "inherited": inherited_in_file,
            "this_round": this_round, "old_ok": old_ok, "new_ok": new_ok,
            "new_total": new_total, "status": r["status"],
        })
        print(f"  {r['id'][:32]:34} {r['status']:10} {old_ok:>7} "
              f"{len(inherited_in_file):>7} {new_ok:>7} {new_ok:>7} {new_total:>7}")

    if skipped_running:
        print(f"\n[跳过] {len(skipped_running)} 个正在运行的实例（等跑完再处理）:")
        for i in skipped_running:
            print(f"    {i}")
    if skipped_no_logs:
        print(f"\n[跳过] {len(skipped_no_logs)} 个源已删除且无本地日志、无法判定:")
        for i in skipped_no_logs:
            print(f"    {i}")

    if not plans:
        print("\n没有发现需要拆分的实例。")
        return

    tot_inh = sum(len(p["inherited"]) for p in plans)
    print(f"\n合计：{len(plans)} 个实例将被拆分，共移出 {tot_inh:,} 条继承记录。")

    if not args.apply:
        print("\n[dry-run] 未写入任何文件。确认无误后加 --apply 执行。")
        return

    # ---- 备份 ----
    ts = time.strftime("%Y%m%d%H%M%S")
    bak_db = f"{DB}.bak.{ts}"
    print(f"\n备份数据库 -> {bak_db}")
    src_conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(bak_db)
    src_conn.backup(dst_conn)
    dst_conn.close()
    src_conn.close()

    bak_dir = os.path.join(os.path.dirname(DB), f"jsonl_bak_{ts}")
    os.makedirs(bak_dir, exist_ok=True)
    print(f"备份 jsonl -> {bak_dir}/")

    # ---- 写入 ----
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=30000")
    done = 0
    for p in plans:
        od = p["od"]
        iid = p["inst"]["id"]
        safe = iid.replace("/", "_")
        try:
            for fname in (COMPLETE, FAILED, INHERITED):
                fp = os.path.join(od, fname)
                if os.path.exists(fp):
                    shutil.copy2(fp, os.path.join(bak_dir, f"{safe}.{fname}"))

            # 1) 继承记录单独成文件
            inh_path = os.path.join(od, INHERITED)
            existing_inh = read_lines(inh_path)
            write_lines(inh_path, list(dict.fromkeys(existing_inh + p["inherited"])))

            # 2) complete.jsonl 只留本轮
            write_lines(os.path.join(od, COMPLETE), p["this_round"])

            # 3) failed.jsonl 去重（旧实现重复 append，生产实测有 4257 行仅 3162 唯一）
            failed_path = os.path.join(od, FAILED)
            if os.path.exists(failed_path):
                failed_lines = read_lines(failed_path)
                # 本轮已成功的从失败清单里剔除
                keep = [x for x in failed_lines if x not in set(p["this_round"] + p["inherited"])]
                write_lines(failed_path, keep)
                new_failed = len(keep)
            else:
                new_failed = 0

            conn.execute(
                "UPDATE task_instances SET completed_tasks=?, failed_tasks=?, total_tasks=? "
                "WHERE id=?",
                (p["new_ok"], new_failed, p["new_total"], iid),
            )
            done += 1
        except Exception as e:
            print(f"  [fail] {iid}: {e}")

    conn.commit()
    conn.close()
    print(f"\n完成：{done}/{len(plans)} 个实例已拆分。")
    print(f"数据库备份: {bak_db}")
    print(f"jsonl 备份: {bak_dir}/")
    print("确认服务正常后可删除备份。")


if __name__ == "__main__":
    main()
