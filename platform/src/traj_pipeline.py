# -*- coding: utf-8 -*-
"""从源仓 /root/traj_output/traj_pipeline/download_workspace_and_run.py 迁来的最小函数集。

只包含 output_pipeline 实际依赖的「workspace 快路径」：枚举 task 子目录 → 并发下载
logs/traj_stats_result.json → 展平成 per_task → stats_from_per_task 聚合成统一漏斗(L0-L3)。

与原函数逐字迁移（仅去掉源文件对 traj_stats 的跨仓 sys.path 依赖，改为导入同包 traj_stats_light）。
不包含：慢路径(逐 task 全量下载原始轨迹)、detail 展示、Hermes include/exclude 下载。
"""
from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from .traj_stats_light import has_task_done_marker

# 桶名/协议丢弃，且丢弃桶内第一个 in-bucket 段（属主/项目前缀，如 zhangchen），保留 batch 段
# （obs://bucket/<owner>/<batch>/<sub...>/<leaf> → <batch>/<sub...>/<leaf>）。
# 与 offline_analysis._cache_subdir_for 同规则；避免同目录名 task 撞缓存目录。
def cache_subdir_for(task_obs):
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    rel = task_obs.split("://", 1)[-1].strip("/")
    parts = rel.split("/")
    parts = parts[1:] if len(parts) > 1 else parts    # 丢桶名
    parts = parts[1:] if len(parts) > 1 else parts    # 丢属主段
    return "/".join(parts)


# ============ 枚举 ============

def list_task_dirs(obsutil, workspace_obs, obs_cred_args=None):
    """用 obsutil ls -d 枚举 workspace 下的直接子目录(task), 处理 Next marker 翻页。

    obs_cred_args: 可选 ["-i", ak, "-k", sk, "-e", endpoint], 用于覆盖 obsutil 全局默认凭证
    (访问另一个账号/桶时用, 不传则走全局默认)。
    返回 task 的 obs URL 列表(每个以 / 结尾)。
    """
    workspace_obs = workspace_obs if workspace_obs.endswith("/") else workspace_obs + "/"
    tasks = []
    marker = None
    while True:
        cmd = [obsutil, "ls", workspace_obs, "-d", "-limit", "1000"] + (obs_cred_args or [])
        if marker:
            cmd += ["-marker", marker]
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"obsutil ls 失败(退出码 {res.returncode}): {res.stdout}\n{res.stderr}")

        next_marker = None
        for raw in (res.stdout or "").splitlines():
            line = raw.strip()
            # 子目录行: 以 workspace_obs 开头、比它多一段并以 / 结尾, 排除它自身
            if line.startswith(workspace_obs) and line.endswith("/") and line != workspace_obs:
                if line not in tasks:
                    tasks.append(line)
            elif line.startswith("Next marker:"):
                next_marker = line.split(":", 1)[1].strip()

        if not next_marker:
            break
        marker = next_marker
    return tasks


# ============ 下载 ============

def _fetch_task_done_marker(obsutil, task_obs, origin, leaf, log_file,
                            obs_cred_args=None):
    """快速路径补齐 TASK_DONE: traj_stats_result.json 不含 task_done 字段,
    该标记只在主 .log 正文里, 故这里额外下载主 log 并扫「【Task_Done】」标记。

    log_file 来自 traj_stats_result.json 的 log_file 字段(相对 task 目录, 如 logs/xxx.log);
    缺失时回退按 check_task_done_in_logs_dir 的候选名(<leaf>.log / harness_automation.log)逐个尝试。
    命中任一即 True; 全部下载失败/无标记返回 False。
    """
    candidates = []
    if log_file:
        candidates.append(log_file.replace("\\", "/").lstrip("/"))
    for name in (leaf + ".log", "harness_automation.log"):
        rel = "logs/" + name
        if rel not in candidates:
            candidates.append(rel)
    sub = cache_subdir_for(task_obs)
    for rel in candidates:
        dest = os.path.join(origin, sub, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        obj_url = task_obs + rel
        cmd = [obsutil, "cp", obj_url, dest, "-f"] + (obs_cred_args or [])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=300)
        except Exception:
            continue
        if res.returncode != 0 or not os.path.isfile(dest):
            continue                  # 该候选名不存在, 试下一个
        # 成功下到主 log 即为定论: 一个 task 只有一份主 log, 有没有标记看这份即可,
        # 不再试其它候选名(避免绝大多数「未完成」任务白白多下 1~2 个 log 文件)。
        return has_task_done_marker(dest)
    return False


def _fetch_one_task_stats(obsutil, task_obs, origin, obs_cred_args=None,
                          with_task_done=True):
    """下载单个 task 的 logs/traj_stats_result.json 到 origin/<batch>/<leaf>/logs/traj_stats_result.json,
    解析并经 harness_tsr_to_entries 展平。文件不存在/失败返回 []。

    with_task_done=True 时额外下载主 log 扫「【Task_Done】」标记, 回填每条 entry 的 task_done
    (traj_stats_result.json 本身不含该字段)。"""
    task_obs = task_obs if task_obs.endswith("/") else task_obs + "/"
    leaf = task_obs.rstrip("/").split("/")[-1]
    dest = os.path.join(origin, cache_subdir_for(task_obs), "logs", "traj_stats_result.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    obj_url = task_obs + "logs/traj_stats_result.json"
    cmd = [obsutil, "cp", obj_url, dest, "-f"] + (obs_cred_args or [])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
    except Exception:
        return []
    if res.returncode != 0 or not os.path.isfile(dest):
        return []                     # 该 task 无此文件(老 workspace), 正常, 不刷错误
    try:
        with open(dest, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    entries = harness_tsr_to_entries(data, task_obs=task_obs)
    if entries and with_task_done:
        done = _fetch_task_done_marker(obsutil, task_obs, origin, leaf,
                                       data.get("log_file"), obs_cred_args)
        for e in entries:
            e["task_done"] = done
    return entries


def fetch_per_task_stats_files(obsutil, workspace_obs, origin, obs_cred_args=None,
                               concurrency=8, with_task_done=True):
    """快速路径: 枚举 workspace 下各 task, 并发下载每个 <task>/logs/traj_stats_result.json
    (每份约 1KB), 展平成 per_task 列表返回。

    with_task_done=True 时每个 task 额外下载主 log 扫「【Task_Done】」标记(带宽略增, 但仍远小于
    整包轨迹); False 则跳过, TASK_DONE 计数恒为 0(与旧 fast 行为一致)。

    一个都没拿到(老 workspace 或无此文件)返回 None, 调用方回退逐 task 全量下载老逻辑。
    """
    tasks = list_task_dirs(obsutil, workspace_obs, obs_cred_args=obs_cred_args)
    if not tasks:
        return None
    total = len(tasks)
    step = "下 stats + 扫主 log(TASK_DONE)" if with_task_done else "下 stats"
    print(f"      [fast] 共 {total} 个 task, 并发 {concurrency} {step}, 逐个上报进度 ...",
          flush=True)
    per_task = []
    got = 0
    done_n = 0
    import time
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_fetch_one_task_stats, obsutil, t, origin, obs_cred_args,
                          with_task_done): t
                for t in tasks}
        for fut in as_completed(futs):
            done_n += 1
            entries = fut.result()
            if entries:
                got += 1
                per_task.extend(entries)
            # 逐 task 上报: 每完成一个都打一行, 让前端/终端能看到进度, 不再长时间静默。
            # 附实时速率(task/s, 按已用时均值) + 并发数 + 估算剩余时间(ETA)。
            if done_n % 5 == 0 or done_n == total:
                elapsed = time.time() - t_start
                rate = done_n / elapsed if elapsed > 0 else 0.0
                eta = (total - done_n) / rate if rate > 0 else 0.0
                print(f"      [fast] 进度 {done_n}/{total} (命中 {got}) | "
                      f"{rate:.1f} task/s @ 并发 {concurrency} | ETA {eta/60:.1f} min",
                      flush=True)
    if got == 0:
        return None
    print(f"      [fast] {got}/{total} 个 task 命中 logs/traj_stats_result.json", flush=True)
    return per_task


# ============ 展平 ============

def harness_tsr_to_entries(d, task_obs=None):
    """把单个 task 的采集侧 logs/traj_stats_result.json 映射成 traj_stats 内部 per_task entry。

    采集 harness 在每个 task 目录下产出 <task>/logs/traj_stats_result.json, 结构是单 task 对象:
      {task, config_file, log_file, harness_home, task_level, best_completion,
       agents:[{agent, trajectory, tool_calls, assistant_rounds, plain_rounds,
                has_ge3_toolcalls, has_plain_round, has_eval, evaluator_completion,
                verdict_source, level}]}
    注意该文件**不含 char_len / token** 字段(那两个要读轨迹正文才算), 故这里也不产出这些键,
    使 stats_from_per_task 里 char_len_stats/token_stats 归 None(快速路径两列先留空, 详情按需回填)。

    task_obs: 所属 task 的 obs URL; 提供时 entry.trajectory 映射为 origin 相对路径
    <batch>/<leaf>/<容器相对路径>（与慢路径缓存目录一致）; 缺省回退旧式 <task>/<相对路径>。
    对 agents 里每个 agent 产出一条 entry; 无法解析(缺 agents)时返回 []。
    """
    task = d.get("task")
    agents = d.get("agents")
    if not task or not isinstance(agents, list):
        return []
    harness_home = d.get("harness_home") or ""
    harness = "openclaw" if ".openclaw" in harness_home else "hermes"
    sub = cache_subdir_for(task_obs) if task_obs else None
    entries = []
    for a in agents:
        if not isinstance(a, dict):
            continue
        tc = a.get("tool_calls") or 0
        pr = a.get("plain_rounds") or 0
        has_ge3 = bool(a.get("has_ge3_toolcalls"))
        has_plain = bool(a.get("has_plain_round"))
        # L1 门槛: openclaw = ≥3工具调用 且 有纯轮; hermes = 有产出(纯轮>0)。与 process_root 口径一致。
        passed = (has_ge3 and has_plain) if harness == "openclaw" else has_plain
        # trajectory 绝对容器路径(如 <harness_home>/agents/main/sessions/<uuid>.jsonl)
        # → 映射成 origin 相对路径 <batch>/<leaf>/agents/main/sessions/<uuid>.jsonl,
        #   供详情页按需下载定位（与慢路径 _load_per_task_entries 同目录布局）。
        traj_abs = a.get("trajectory") or ""
        try:
            rel = os.path.relpath(traj_abs, harness_home) if (traj_abs and harness_home) else traj_abs
        except ValueError:
            rel = traj_abs
        traj_rel = os.path.join(sub, rel) if (rel and sub) else (rel or None)
        entries.append({
            "task": task,
            "trajectory": traj_rel,
            "tool_calls": tc,
            "assistant_rounds": a.get("assistant_rounds") or 0,
            "plain_rounds": pr,
            "has_ge3_toolcalls": has_ge3,
            "has_plain_round": has_plain,
            "passed_gate": passed,
            "has_eval": bool(a.get("has_eval")),
            "evaluator_completion": a.get("evaluator_completion"),
            "verdict_source": a.get("verdict_source"),
            "harness": harness,
            # 不写 char_len / total_tokens: 快速路径两列留空, 详情按需回填
        })
    return entries


# ============ 聚合 ============

def _avg_tokens(entries, tier_key, tier_entries):
    """计算某一档次的平均 token 总长度，同时返回原始 sum + count 以便跨任务聚合。

    entries: 完整的 per_task 原始行列表
    tier_entries: 该档次在 entries 中的下标列表
    返回 {avg_total_tokens, sum_total, count}，
    若该档次无 token 数据则返回 None。
    """
    sum_total = 0
    count = 0
    for idx in tier_entries:
        row = entries[idx]
        total = row.get("total_tokens")
        if total is None:
            continue
        sum_total += total
        count += 1
    if count == 0:
        return None
    return {
        "avg_total_tokens": round(sum_total / count),
        "sum_total": sum_total,
        "count": count,
    }


def _avg_char_len(entries, tier_entries):
    """计算某一档次的平均轨迹字符数，同 _avg_tokens 结构，供跨任务聚合。"""
    sum_total = 0
    count = 0
    for idx in tier_entries:
        row = entries[idx]
        total = row.get("char_len")
        if total is None:
            continue
        sum_total += total
        count += 1
    if count == 0:
        return None
    return {
        "avg_char_len": round(sum_total / count),
        "sum_total": sum_total,
        "count": count,
    }


def stats_from_per_task(per_task, source_type="workspace"):
    """把 traj_stats 的 per_task(process_root 的返回, 或 traj_stats_result.json 的 details)
    按 workspace 门槛口径聚合成 filter_stats.json 结构。

    抽出此函数是为了让「快速路径」(直接吃 workspace 根目录已有的 traj_stats_result.json.details)
    与「老路径」(本地 process_root)共用同一套 tier 聚合逻辑, 保证两条路口径一致。
    source_type 仅用于 note/source_type 标注, 不影响聚合。
    """
    per_session = []
    kept = with_eval = ge05 = eq1 = 0
    dropped = 0
    task_done_count = 0
    # 记录每个 tier 在 per_task 中的下标（用于 token 统计）
    l0_idx, l1_idx, l15_idx, l2_idx, l3_idx, td_idx = [], [], [], [], [], []
    for i, row in enumerate(per_task):
        comp = row.get("evaluator_completion")
        has_score = isinstance(comp, (int, float))         # L1.5: 有首轮数值分(不含 null)
        # L1 门槛: 直接读 traj_stats 算好的 passed_gate(openclaw/Hermes 同式: ≥3工具调用+纯轮),
        # 不在此重算, 保证两套 harness 口径统一。
        passed = bool(row.get("passed_gate"))
        task_done = bool(row.get("task_done"))
        if task_done:
            task_done_count += 1
            td_idx.append(i)
        if passed:
            kept += 1
            l1_idx.append(i)
            if has_score:                                  # L1.5: 门槛内且有数值分
                with_eval += 1
                l15_idx.append(i)
            if has_score and comp >= 0.5:                  # L2
                ge05 += 1
                l2_idx.append(i)
            if has_score and comp == 1:                    # L3
                eq1 += 1
                l3_idx.append(i)
        else:
            dropped += 1
        l0_idx.append(i)                                   # L0: 所有 task
        per_session.append({
            "session": row["task"],           # 用 task 目录名; 详情暂不支持
            "passed_gate": passed,            # 是否通过 L1 门槛(≥3工具调用+纯轮)
            "has_eval": has_score,            # L1.5 口径: 有 turn=1 数值 completion
            "eval_qc": "",
            "completion": comp,
            "tool_calls": row.get("tool_calls"),
            "plain_rounds": row.get("plain_rounds"),
            "trajectory": row.get("trajectory"),
            "harness": row.get("harness", "openclaw"),
            "task_done": task_done,           # log 是否含「【Task_Done】」标记
        })
        # 透传 Hermes token 数据到 per_session（供 api 按任务查询）
        if "total_tokens" in row:
            per_session[-1]["input_tokens"] = row["input_tokens"]
            per_session[-1]["output_tokens"] = row["output_tokens"]
            per_session[-1]["reasoning_tokens"] = row["reasoning_tokens"]
            per_session[-1]["total_tokens"] = row["total_tokens"]
        if "char_len" in row:
            per_session[-1]["char_len"] = row["char_len"]

    token_stats = {}
    char_len_stats = {}
    for tier, label in [("L0", l0_idx), ("L1", l1_idx), ("T_DONE", td_idx),
                        ("L1.5", l15_idx), ("L2", l2_idx), ("L3", l3_idx)]:
        avg = _avg_tokens(per_task, tier, label)
        if avg is not None:
            token_stats[tier] = avg
        char_avg = _avg_char_len(per_task, label)
        if char_avg is not None:
            char_len_stats[tier] = char_avg

    return {
        "filtered_count":    kept,            # L1
        "with_eval_count":   with_eval,       # L1.5
        "completion_ge_0.5": ge05,            # L2
        "completion_eq_1":   eq1,             # L3
        "dropped_count":     dropped,         # 未过 L1 门槛的轨迹(L0 - L1)
        "task_done_count":   task_done_count, # 主 log 含「【Task_Done】」标记的 task 数(Hermes 未下载主 log 时恒为 0)
        "note": ("来源=workspace(原始轨迹按需下载); L0=总轨迹数, "
                 "L1=ge3_and_plain_round(≥3工具调用+有纯轮), "
                 "L1.5=L1内有 turn=1 数值 completion(不含 null), "
                 "L2/L3=L1.5内 turn=1 completion>=0.5 / ==1"),
        "source_type": source_type,
        "per_session": per_session,
        "token_stats": token_stats if token_stats else None,
        "char_len_stats": char_len_stats if char_len_stats else None,
    }
