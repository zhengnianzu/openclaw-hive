# -*- coding: utf-8 -*-
"""看板聚合纯函数。

输入统一是「已 enrich 的实例 dict」——即 instances.py 里
`_with_resources(_with_harness_type(...))` 之后的形态，字段含
status / created_by / concurrent_num / cpu_request / memory_request /
sandbox_type / harness_type / sandbox_namespace / started_at / stopped_at。

这里不做 I/O、不碰 DB，便于单测；路由层负责取数。
"""
import json
from datetime import datetime, timedelta

# 全量状态顺序（与 Dashboard.vue 的 statusText 一致，缺状态补 0 保证图例稳定）
STATUS_ORDER = ["running", "preparing", "queued", "completed", "finished", "stopped", "failed", "created"]
STATUS_LABEL = {
    "running": "运行中",
    "preparing": "准备中",
    "queued": "排队中",
    "completed": "已完成",
    "finished": "已结束",
    "stopped": "已停止",
    "failed": "失败",
    "created": "待启动",
}

# 占用资源的状态：preparing 已向集群申请额度，和 running 一样计入「已用」
OCCUPYING = ("running", "preparing")
UNKNOWN_USER = "未知"

# 终态但 stopped_at 缺失时，重建区间的最大长度（防止一条无限长平线污染整张图）
ORPHAN_MAX_HOURS = 24


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def effective_pods(row: dict) -> float:
    """实例此刻真实占用的沙箱数（= 活着的 worker 数）。

    hive.py 是 concurrent_num 个 worker 各领一个任务、每个任务一个沙箱，
    所以 running 实例的活沙箱数 = min(并发, 剩余任务数)。尾部时这个值远小于
    并发上限——tail_skip 放行的新任务正是利用这部分空出来的额度，按并发上限
    统计会把两个实例的占用都算满，明显高估。
    口径与 instances.py 单实例概览的 running_pods 一致。

    total_tasks 未知（0）时无法判断尾部，回退到配置并发，保持历史行为。
    preparing 还未建沙箱，但队列槽位已占用，维持全额计入（见 OCCUPYING 注释）。
    """
    n = _num(row.get("concurrent_num"))
    if row.get("status") != "running":
        return n
    total = _num(row.get("total_tasks"))
    if total <= 0:
        return n
    remaining = total - (_num(row.get("completed_tasks")) + _num(row.get("failed_tasks")))
    return max(0.0, min(n, remaining))


def instance_cpu(row: dict) -> float:
    return effective_pods(row) * _num(row.get("cpu_request"))


def instance_mem(row: dict) -> float:
    return effective_pods(row) * _num(row.get("memory_request"))


def instance_cpu_limit(row: dict) -> float:
    return effective_pods(row) * _num(row.get("cpu_limit"))


def instance_mem_limit(row: dict) -> float:
    return effective_pods(row) * _num(row.get("memory_limit"))


def aggregate_summary(rows: list) -> dict:
    """活跃集合（running/preparing/queued）→ 顶部 KPI。"""
    used_cpu = used_cpu_limit = used_mem = used_mem_limit = 0.0
    queued_cpu = queued_mem = 0.0
    pods = 0
    running = preparing = queued = 0
    for r in rows:
        status = r.get("status")
        if status in OCCUPYING:
            # 已用 / 上限 / 沙箱数三者同口径，都按此刻存活沙箱数折算
            used_cpu += instance_cpu(r)
            used_cpu_limit += instance_cpu_limit(r)
            used_mem += instance_mem(r)
            used_mem_limit += instance_mem_limit(r)
            pods += int(effective_pods(r))
            if status == "running":
                running += 1
            else:
                preparing += 1
        elif status == "queued":
            queued_cpu += instance_cpu(r)
            queued_mem += instance_mem(r)
            queued += 1
    return {
        "active": running + preparing,
        "running": running,
        "preparing": preparing,
        "queued": queued,
        "used_cpu": round(used_cpu, 2),
        "used_cpu_limit": round(used_cpu_limit, 2),
        "used_memory_gb": round(used_mem, 2),
        "used_memory_gb_limit": round(used_mem_limit, 2),
        "queued_cpu": round(queued_cpu, 2),
        "queued_memory_gb": round(queued_mem, 2),
        "pods": pods,
    }


def status_distribution(counts: dict) -> list:
    """SQL GROUP BY 的 {status: count} → 固定顺序的列表，缺失补 0。"""
    out = []
    for s in STATUS_ORDER:
        out.append({"status": s, "label": STATUS_LABEL[s], "count": int(counts.get(s, 0))})
    # 未知/新增状态不丢
    for s, c in counts.items():
        if s not in STATUS_LABEL and c:
            out.append({"status": s, "label": s, "count": int(c)})
    return out


def _group(rows: list, key: str) -> dict:
    g = {}
    for r in rows:
        if r.get("status") not in OCCUPYING:
            continue
        k = r.get(key) or ("openclaw" if key == "harness_type" else "linux")
        item = g.setdefault(k, {"count": 0, "cpu": 0.0, "memory_gb": 0.0, "pods": 0,
                                "inferred_count": 0})
        item["count"] += 1
        item["cpu"] += instance_cpu(r)
        item["memory_gb"] += instance_mem(r)
        item["pods"] += int(effective_pods(r))
        if r.get("sandbox_type_inferred"):
            item["inferred_count"] += 1
    return g


def harness_distribution(rows: list) -> list:
    g = _group(rows, "harness_type")
    out = [
        {"harness_type": k, "count": v["count"], "cpu": round(v["cpu"], 2),
         "memory_gb": round(v["memory_gb"], 2), "pods": v["pods"]}
        for k, v in g.items()
    ]
    return sorted(out, key=lambda x: (-x["cpu"], x["harness_type"]))


def sandbox_type_distribution(rows: list) -> list:
    g = _group(rows, "sandbox_type")
    out = [
        {"sandbox_type": k, "count": v["count"], "cpu": round(v["cpu"], 2),
         "memory_gb": round(v["memory_gb"], 2), "pods": v["pods"],
         "inferred_count": v["inferred_count"]}
        for k, v in g.items()
    ]
    return sorted(out, key=lambda x: (-x["cpu"], x["sandbox_type"]))


def sandbox_spec_distribution(rows: list) -> list:
    """按单沙箱规格（cpu_request × memory_request）聚合，按沙箱数降序。

    与 `_group()` 同口径：只统计 running/preparing，`pods` 用 effective_pods（尾部折算），
    cpu / memory_gb 用 instance_cpu / instance_mem。`queued` 不占资源，不计入。

    规格组合数远多于 sandbox_type（2 种），所以是复合键而不是 `_group` 的单字段键；
    `label` 在后端拼好（"2 核 / 6 GiB"），前端直接用，避免两边各写一套格式化。
    """
    g = {}
    for r in rows:
        if r.get("status") not in OCCUPYING:
            continue
        cpu = round(_num(r.get("cpu_request")), 2)
        mem = round(_num(r.get("memory_request")), 2)
        item = g.setdefault((cpu, mem), {"count": 0, "cpu": 0.0,
                                         "memory_gb": 0.0, "pods": 0})
        item["count"] += 1
        item["cpu"] += instance_cpu(r)
        item["memory_gb"] += instance_mem(r)
        item["pods"] += int(effective_pods(r))
    out = [
        {"cpu_request": cpu, "memory_request": mem,
         "label": f"{_fmt_num(cpu)} 核 / {_fmt_num(mem)} GiB",
         "count": v["count"], "pods": v["pods"],
         "cpu": round(v["cpu"], 2), "memory_gb": round(v["memory_gb"], 2)}
        for (cpu, mem), v in g.items()
    ]
    return sorted(out, key=lambda x: (-x["pods"], -x["cpu"], -x["memory_request"]))


def _fmt_num(v: float) -> str:
    """2.0 → "2"，1.5 → "1.5"：整数不带小数尾。"""
    return str(int(v)) if float(v).is_integer() else str(round(v, 2))


def os_resource_comparison(rows: list) -> dict:
    """Linux / Windows 资源消耗对比（固定返回两键，缺的一侧补 0）。"""
    g = _group(rows, "sandbox_type")
    out = {}
    for k in ("linux", "windows"):
        v = g.get(k) or {"count": 0, "cpu": 0.0, "memory_gb": 0.0, "pods": 0}
        out[k] = {
            "instances": v["count"],
            "pods": v["pods"],
            "cpu": round(v["cpu"], 2),
            "memory_gb": round(v["memory_gb"], 2),
        }
    return out


def user_leaderboard(rows: list) -> list:
    """按 created_by 聚合活跃占用，按 CPU 降序。不 join users 表。

    同时给出 cpu / memory_gb / pods 三个口径，前端用页签切换排序与展示。
    """
    g = {}
    for r in rows:
        if r.get("status") not in OCCUPYING:
            continue
        user = (r.get("created_by") or "").strip() or UNKNOWN_USER
        item = g.setdefault(user, {"user": user, "instances": 0, "running": 0,
                                   "preparing": 0, "queued": 0, "pods": 0,
                                   "cpu": 0.0, "memory_gb": 0.0,
                                   "linux_cpu": 0.0, "windows_cpu": 0.0,
                                   "linux_memory_gb": 0.0, "windows_memory_gb": 0.0})
        item["instances"] += 1
        if r.get("status") == "running":
            item["running"] += 1
        else:
            item["preparing"] += 1
        item["pods"] += int(effective_pods(r))
        item["cpu"] += instance_cpu(r)
        item["memory_gb"] += instance_mem(r)
        os_key = "windows" if r.get("sandbox_type") == "windows" else "linux"
        item[os_key + "_cpu"] += instance_cpu(r)
        item[os_key + "_memory_gb"] += instance_mem(r)

    # 排队中不占资源，但排行榜里要能看到「他在排队等着」
    for r in rows:
        if r.get("status") != "queued":
            continue
        user = (r.get("created_by") or "").strip() or UNKNOWN_USER
        item = g.setdefault(user, {"user": user, "instances": 0, "running": 0,
                                   "preparing": 0, "queued": 0, "pods": 0,
                                   "cpu": 0.0, "memory_gb": 0.0,
                                   "linux_cpu": 0.0, "windows_cpu": 0.0,
                                   "linux_memory_gb": 0.0, "windows_memory_gb": 0.0})
        item["queued"] += 1

    out = []
    for v in g.values():
        out.append({
            **v,
            "cpu": round(v["cpu"], 2),
            "memory_gb": round(v["memory_gb"], 2),
            "linux_cpu": round(v["linux_cpu"], 2),
            "windows_cpu": round(v["windows_cpu"], 2),
            "linux_memory_gb": round(v["linux_memory_gb"], 2),
            "windows_memory_gb": round(v["windows_memory_gb"], 2),
        })
    return sorted(out, key=lambda x: (-x["cpu"], -x["queued"], x["user"]))


def pool_usage(rows: list, pools: dict) -> list:
    """每个资源池的已用/排队占用。

    pools = {ns: 池容量 dict}（见 resource_pools.load_pools），除 total_cpu /
    total_memory_gb 外还带 max_sandboxes（沙箱并发上限，老格式池为 0）。
    `pods` 是池内此刻存活的沙箱数，与 aggregate_summary 同口径。
    """
    out = {}
    for ns, spec in (pools or {}).items():
        out[ns] = {
            "namespace": ns,
            "total_cpu": _num(spec.get("total_cpu")),
            "total_memory_gb": _num(spec.get("total_memory_gb")),
            "max_sandboxes": _num(spec.get("max_sandboxes")),
            "used_cpu": 0.0, "used_memory_gb": 0.0,
            "queued_cpu": 0.0, "queued_memory_gb": 0.0,
            "pods": 0, "instances": 0, "configured": True,
        }
    for r in rows:
        status = r.get("status")
        if status not in OCCUPYING and status != "queued":
            continue
        ns = r.get("sandbox_namespace") or ""
        if ns not in out:
            # 未在 resource_pools.yaml 登记的池：分母为 0，前端显示「未配置总量」
            out[ns] = {
                "namespace": ns or "未归属",
                "total_cpu": 0.0, "total_memory_gb": 0.0, "max_sandboxes": 0.0,
                "used_cpu": 0.0, "used_memory_gb": 0.0,
                "queued_cpu": 0.0, "queued_memory_gb": 0.0,
                "pods": 0, "instances": 0, "configured": False,
            }
        item = out[ns]
        if status == "queued":
            item["queued_cpu"] += instance_cpu(r)
            item["queued_memory_gb"] += instance_mem(r)
        else:
            item["used_cpu"] += instance_cpu(r)
            item["used_memory_gb"] += instance_mem(r)
            item["pods"] += int(effective_pods(r))
            item["instances"] += 1
    for item in out.values():
        for k in ("used_cpu", "used_memory_gb", "queued_cpu", "queued_memory_gb"):
            item[k] = round(item[k], 2)
    return sorted(out.values(), key=lambda x: (not x["configured"], x["namespace"]))


# ============================================================================
# 代理负载：模型请求打到哪台自部署网关（按角色拆分）
# ============================================================================

# 角色顺序固定，前端三栏布局依赖它（缺角色补空，避免面板列数跳动）
PROXY_ROLES = ("主Agent", "用户模拟", "Evaluator")
_PROXY_ROLE_UNCONFIGURED = "未配置"


def _create_params(row: dict) -> dict:
    """create_params 是 JSON 文本；损坏或缺失时当成空配置。"""
    raw = row.get("create_params")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _row_agent_urls(params: dict) -> list:
    """从 create_params 取出 [(role, base_url), ...]。

    主Agent = 顶层 model_base_url；agents[] 里 name == 'evaluator' 归 Evaluator，
    其余（user_simulator 等）归用户模拟 —— 与 batch_output._build_proxy_rows 一致。
    """
    out = [("主Agent", params.get("model_base_url") or "")]
    for a in (params.get("agents") or []):
        if not isinstance(a, dict):
            continue
        name = a.get("name") or ""
        role = "Evaluator" if name == "evaluator" else "用户模拟"
        out.append((role, a.get("base_url") or ""))
    return out


def proxy_load(rows: list, proxy_map: dict) -> list:
    """按 (角色, 代理) 聚合运行中实例的并发负载。

    口径：只统计 running；负载 = 当前存活沙箱数累加（尾部实例只剩少量沙箱在打
    这台代理，按配置并发累加会高估）。排队/准备中的实例还没发请求，不计入。
    """
    from . import proxy as proxy_service

    agg = {}
    for r in rows:
        if r.get("status") != "running":
            continue
        pods = int(effective_pods(r))
        cpu, mem = instance_cpu(r), instance_mem(r)
        for role, url in _row_agent_urls(_create_params(r)):
            ident = proxy_service.proxy_identity(url, proxy_map)
            key = (role, ident["host"])
            item = agg.setdefault(key, {
                "role": role,
                "host": ident["host"],
                "proxy_name": ident["proxy_name"],
                "baseurl": ident["baseurl"],
                "instances": 0,
                "pods": 0,
                "cpu": 0.0,
                "memory_gb": 0.0,
            })
            item["instances"] += 1
            item["pods"] += pods
            item["cpu"] += cpu
            item["memory_gb"] += mem

    for item in agg.values():
        item["cpu"] = round(item["cpu"], 2)
        item["memory_gb"] = round(item["memory_gb"], 2)
        if not item["host"]:
            # 没有配 base_url 的实例也要露面，否则面板上的总量对不上实例数
            item["host"] = _PROXY_ROLE_UNCONFIGURED

    role_rank = {r: i for i, r in enumerate(PROXY_ROLES)}
    return sorted(
        agg.values(),
        key=lambda x: (role_rank.get(x["role"], len(PROXY_ROLES)), -x["pods"], x["host"]),
    )


# ============================================================================
# 趋势：从 started_at / stopped_at 重建（无采样表）
# ============================================================================

def parse_ts(value):
    """实例时间戳 → epoch 秒；解析不了返回 None。

    started_at/stopped_at 由后端用 datetime.now().isoformat() 写入（本地时间，无时区），
    历史数据里也可能出现 "YYYY-MM-DD HH:MM:SS" 或带 Z 的写法。
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "").replace("T", " ")
    if "+" in text:
        text = text.split("+", 1)[0]
    text = text.split(".")[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return None


def bucket_seconds(hours: int) -> int:
    """区间 → 分桶粒度，保证点数 ≤ 360（30 天也只用 180 个点）。"""
    if hours <= 6:
        return 60        # 360
    if hours <= 24:
        return 300       # 288
    if hours <= 72:
        return 900       # 288
    if hours <= 168:
        return 3600      # 168
    return 14400         # 720h → 180


def reconstruct_trend(rows: list, start_ts: float, end_ts: float, step: int) -> list:
    """扫描线重建并发资源曲线（每个分桶取**区间内峰值**）。

    每个实例 → 占用区间 [started_at | prepare_started_at, stopped_at | now]。
    没有起点（created/queued）的实例不占资源，跳过。
    终态但缺 stopped_at 的脏数据：区间截断到起点 + ORPHAN_MAX_HOURS。

    取峰值而不是在分桶边界上取瞬时值：分桶远大于实例存活时间时（例如 1 小时桶里
    跑了一个 10 秒的任务），边界采样会把它完全抹掉，看板看起来像坏了。
    """
    if step <= 0 or end_ts <= start_ts:
        return []

    # 桶边界对齐到 step 的整数倍。不对齐时最后一个桶的起点可能正好等于 end_ts，
    # 于是「运行中实例的结束事件（= now）」在桶起点就被消化，末点会凭空掉到 0。
    start_ts = (int(start_ts) // step) * step
    # 末桶起点取「不晚于 now」的对齐点：再 +step 会造出一个未来时间点的空桶
    end_ts = (int(end_ts) // step) * step

    events = []  # (ts, cpu, mem, pods, delta, os_key)
    now = datetime.now().timestamp()
    terminal = ("completed", "finished", "stopped", "failed")
    for r in rows:
        s = parse_ts(r.get("started_at")) or parse_ts(r.get("prepare_started_at"))
        if not s:
            continue
        e = parse_ts(r.get("stopped_at"))
        if not e:
            if r.get("status") in terminal:
                # 终态但缺 stopped_at 的脏数据：只按 +24h 计，避免无限长平线
                e = s + ORPHAN_MAX_HOURS * 3600
            else:
                e = None    # 仍在运行：只有起点，没有结束事件
        if e is not None and e <= s:
            continue
        if (e is not None and e <= start_ts) or s >= end_ts:
            continue
        s = max(s, start_ts)
        if e is not None:
            e = min(e, end_ts)
        # 趋势是历史区间重建：不知道「当时」还剩多少任务，只能按配置并发估算。
        # 与实时口径（effective_pods，只看此刻）刻意不同，勿合并——否则整条
        # 历史曲线会被压成尾部值，反而失真。
        pods = int(_num(r.get("concurrent_num")))
        cpu = pods * _num(r.get("cpu_request"))
        mem = pods * _num(r.get("memory_request"))
        os_key = "windows" if r.get("sandbox_type") == "windows" else "linux"
        events.append((s, cpu, mem, pods, 1, os_key))
        # 运行中的实例不产生结束事件：否则末桶会把它算成「已经结束」，曲线尾部掉 0。
        if e is not None:
            events.append((e, cpu, mem, pods, -1, os_key))

    events.sort(key=lambda x: x[0])

    # 每桶取峰值：总量 + 按 OS 的 CPU / 内存 / 沙箱数拆分
    keys = ("cpu", "mem", "pods", "running",
            "linux_cpu", "windows_cpu", "linux_mem", "windows_mem",
            "linux_pods", "windows_pods")
    cur = dict.fromkeys(keys, 0.0)
    points = []
    idx = 0
    ts = start_ts
    while ts <= end_ts:
        bucket_end = ts + step

        def _apply(ev):
            _, ecpu, emem, epods, delta, os_key = ev
            cur["cpu"] += ecpu * delta
            cur["mem"] += emem * delta
            cur["pods"] += epods * delta
            cur["running"] += delta
            cur[os_key + "_cpu"] += ecpu * delta
            cur[os_key + "_mem"] += emem * delta
            cur[os_key + "_pods"] += epods * delta

        # 先把桶起点之前的事件全部消化
        while idx < len(events) and events[idx][0] <= ts:
            _apply(events[idx]); idx += 1
        peak = {k: cur[k] for k in keys}
        # 再把桶内事件全部消化，取整段峰值。
        # 只消化「严格早于桶结束」的事件：恰好落在 now 的运行中实例结束事件
        # 不会被算进当前桶，末点才是「此刻仍在占用」的真实值。
        while idx < len(events) and events[idx][0] < bucket_end:
            _apply(events[idx]); idx += 1
            for k in keys:
                peak[k] = max(peak[k], cur[k])

        def _pos(k):
            return round(max(peak[k], 0), 2)

        points.append({
            "ts": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
            "used_cpu": _pos("cpu"),
            "used_memory_gb": _pos("mem"),
            "running": int(_pos("running")),
            "pods": int(_pos("pods")),
            "linux_cpu": _pos("linux_cpu"),
            "windows_cpu": _pos("windows_cpu"),
            "linux_memory_gb": _pos("linux_mem"),
            "windows_memory_gb": _pos("windows_mem"),
            "linux_pods": int(_pos("linux_pods")),
            "windows_pods": int(_pos("windows_pods")),
        })
        ts = bucket_end
    return points
