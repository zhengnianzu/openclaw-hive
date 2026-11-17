#!/usr/bin/env python3
"""按 k8s 实测的节点容量，统计/回写 resource_pools.yaml 里的资源池总量。

背景
----
resource_pools.yaml 的 total_cpu / total_memory_gb 是首页看板「当前资源使用
情况」的分母。原来靠手工填，节点池一扩缩容就失准（实测配置 35712 核 vs 集群
35328 核，差一台 384 核节点）。

本脚本从 run_clear.py 用的那套 kubeconfig 里读真实容量，按 server 去重集群，
汇总后可选回写 yaml。

口径
----
- 默认用 node.status.capacity（整机容量），不是 allocatable。allocatable 要扣
  掉系统预留，得不出 yaml 里那种 384 的整数倍。
- CPU 输出单位「核」，内存输出单位 GiB，与 yaml 注释一致。
- 这是**总量**，不是实时可用量。可用量需另算（describe node 的 Allocated
  resources，或按 sandbox pod 的 requests 求和）。

用量
----
# 只看不写（默认）
python3 sync_resource_pools.py

# 回写 yaml
python3 sync_resource_pools.py --write

# 多资源池时，显式指定每个 pool 对应哪个集群（按 server 末段或 kubeconfig 路径片段匹配）
python3 sync_resource_pools.py --map omni-env-default-worker=192.168.33.116 \
                               --map omni-env-other=192.168.2.158 --write

# 换一套 kubeconfig 目录
python3 sync_resource_pools.py --kubeconfig-dir /path/to/kubeconfigs
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

# 与 run_clear.py 保持一致：三个 worker 集群的 kubeconfig 目录。
# main/rl-agentic-cce.yaml 与 workers/internal-green-prod-worker/rl-agentic-cce.yaml
# 指向同一个 server，按 server 去重后实际是三个集群。
DEFAULT_KUBECONFIG_DIR = (
    "/data/ai-infra/omni-env/team-mm-rl/region-cn-southwest-2/"
    "cluster-rl-agentic-cce/plane-default/version-v1.5.0/kubeconfigs"
)
DEFAULT_POOLS_YAML = (
    "/root/zengxiang/openclaw-hive/platform/settings/sandbox/resource_pools.yaml"
)

# 大集群 API server 扫 etcd 就要 60s+，给足余量（同 run_clear.py 的取值）。
KUBECTL_TIMEOUT = 180

_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5}


# --------------------------------------------------------------------------
# 数量解析
# --------------------------------------------------------------------------
def parse_cpu_millicores(value: str) -> int:
    """把 k8s 的 CPU 数量串转成毫核。'384' -> 384000, '382970m' -> 382970。"""
    v = str(value).strip()
    if not v:
        return 0
    if v.endswith("m"):
        return int(round(float(v[:-1])))
    if v.endswith("n"):
        return int(round(float(v[:-1]) / 1_000_000))
    return int(round(float(v) * 1000))


def parse_bytes(value: str) -> int:
    """把 k8s 的内存数量串转成字节。'1543387380Ki' / '1471Gi' / '1024' 均可。"""
    v = str(value).strip()
    if not v:
        return 0
    for suffix, mult in _UNITS.items():
        if v.endswith(suffix):
            return int(float(v[: -len(suffix)]) * mult)
    # 带小写 k/M/G 或纯数字：按字节处理
    return int(float(v))


# --------------------------------------------------------------------------
# kubeconfig 发现（复用 run_clear.py 的思路：按 server 去重，不按文件数）
# --------------------------------------------------------------------------
def kubeconfig_server(path: str) -> str:
    """读取 kubeconfig 里第一个 cluster 的 server，用于跨文件去重。失败返回空串。"""
    try:
        r = subprocess.run(
            ["kubectl", "--kubeconfig", path, "config", "view",
             "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def discover_kubeconfigs(root: str) -> list[tuple[str, str]]:
    """递归找 kubeconfig，按 server 去重。返回 [(server, path), ...]。

    不去重的话 main/ 和 workers/internal-green-prod-worker/ 会让同一个集群
    被统计两遍，总量直接翻倍。
    """
    paths = sorted(glob.glob(os.path.join(root, "**", "*.yaml"), recursive=True))
    paths += sorted(glob.glob(os.path.join(root, "**", "*.yml"), recursive=True))
    if not paths:
        raise FileNotFoundError(f"no kubeconfig found under {root}")

    seen: dict[str, str] = {}
    for p in paths:
        server = kubeconfig_server(p)
        if not server:
            print(f"  [warn] 无法读取 server，跳过: {p}", file=sys.stderr)
            continue
        if server in seen:
            print(f"  [dedupe] {os.path.basename(p)} 与 {os.path.basename(seen[server])} "
                  f"同一集群 ({server})，跳过")
            continue
        seen[server] = p
    return [(s, p) for s, p in seen.items()]


# --------------------------------------------------------------------------
# 容量采集
# --------------------------------------------------------------------------
@dataclass
class ClusterCapacity:
    server: str
    kubeconfig: str
    nodes: int = 0
    cpu_millicores: int = 0
    mem_bytes: int = 0
    error: str = ""

    @property
    def cpu_cores(self) -> float:
        return self.cpu_millicores / 1000

    @property
    def mem_gib(self) -> float:
        return self.mem_bytes / 1024**3

    @property
    def ok(self) -> bool:
        return not self.error and self.nodes > 0


def collect_cluster_capacity(server: str, kubeconfig: str, metric: str) -> ClusterCapacity:
    """查一个集群所有节点的 capacity/allocatable 并求和。"""
    cap = ClusterCapacity(server=server, kubeconfig=kubeconfig)
    cmd = [
        "kubectl", "--kubeconfig", kubeconfig,
        "get", "nodes", "-o", "json", f"--request-timeout={KUBECTL_TIMEOUT - 60}s",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT)
    except subprocess.TimeoutExpired:
        cap.error = f"kubectl 超时 ({KUBECTL_TIMEOUT}s)"
        return cap
    except OSError as e:
        cap.error = f"kubectl 无法执行: {e}"
        return cap

    if r.returncode != 0:
        cap.error = (r.stderr or "").strip().splitlines()[0][:200] if r.stderr else f"rc={r.returncode}"
        return cap

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        cap.error = f"JSON 解析失败: {e}"
        return cap

    for item in data.get("items") or []:
        status = item.get("status") or {}
        res = status.get(metric) or {}
        if not res:
            continue
        cap.nodes += 1
        cap.cpu_millicores += parse_cpu_millicores(res.get("cpu", "0"))
        cap.mem_bytes += parse_bytes(res.get("memory", "0"))
    if cap.nodes == 0:
        cap.error = "没有可用节点"
    return cap


# --------------------------------------------------------------------------
# 资源池 <-> 集群 归属
# --------------------------------------------------------------------------
def match_pool_to_clusters(pool: str, mapping: dict[str, str],
                           clusters: list[ClusterCapacity]) -> list[ClusterCapacity]:
    """按 --map 的规则给资源池挑集群。mapping[pool] 是匹配串（server 或路径片段）。"""
    needle = mapping.get(pool)
    if not needle:
        return []
    return [c for c in clusters if needle in c.server or needle in c.kubeconfig]


# --------------------------------------------------------------------------
# yaml 读写（ruamel round-trip，保留注释与排版）
# --------------------------------------------------------------------------
def load_pools(path: str):
    from ruamel.yaml import YAML
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    with open(path, encoding="utf-8") as f:
        return yaml, yaml.load(f)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="按 k8s 实测节点容量统计/回写 resource_pools.yaml",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--pools", default=DEFAULT_POOLS_YAML, help="resource_pools.yaml 路径")
    ap.add_argument("--kubeconfig-dir", default=DEFAULT_KUBECONFIG_DIR,
                    help=f"kubeconfig 目录 (默认 {DEFAULT_KUBECONFIG_DIR})")
    ap.add_argument("--metric", choices=("capacity", "allocatable"), default="capacity",
                    help="节点资源口径 (默认 capacity，与现有一致)")
    ap.add_argument("--map", action="append", default=[], metavar="POOL=NEEDLE",
                    help="指定资源池对应的集群(server 或 kubeconfig 路径片段)，可重复")
    ap.add_argument("--write", action="store_true", help="回写 yaml（默认只打印）")
    ap.add_argument("--allow-partial", action="store_true",
                    help="有集群查询失败时仍然回写（默认拒绝，避免写入偏小的值）")
    args = ap.parse_args()

    if not os.path.isfile(args.pools):
        print(f"错误: 找不到 {args.pools}", file=sys.stderr)
        return 2

    mapping: dict[str, str] = {}
    for item in args.map:
        if "=" not in item:
            print(f"错误: --map 需要 POOL=NEEDLE 形式，收到 {item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        mapping[k.strip()] = v.strip()

    # 1) 发现集群
    print(f"kubeconfig 目录: {args.kubeconfig_dir}")
    try:
        discovered = discover_kubeconfigs(args.kubeconfig_dir)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    print(f"去重后集群数: {len(discovered)}\n")

    # 2) 采集容量
    clusters: list[ClusterCapacity] = []
    for server, kc in discovered:
        print(f"查询 {server} ...", end="", flush=True)
        cap = collect_cluster_capacity(server, kc, args.metric)
        clusters.append(cap)
        if cap.ok:
            print(f" {cap.nodes} 节点, {cap.cpu_cores:.0f} 核, {cap.mem_gib:.0f} GiB")
        else:
            print(f" 失败: {cap.error}")

    failed = [c for c in clusters if not c.ok]
    if failed and not args.allow_partial:
        print(f"\n错误: {len(failed)} 个集群查询失败，"
              f"写入会得到偏小的总量。确认无误可加 --allow-partial。", file=sys.stderr)
        return 1

    # 3) 读现有 yaml
    yaml, doc = load_pools(args.pools)
    pools = (doc or {}).get("pools") or {}
    if not pools:
        print(f"错误: {args.pools} 里没有 pools 段", file=sys.stderr)
        return 2

    print(f"\n{'资源池':<32}{'实测CPU':>10}{'实测内存GiB':>14}{'配置CPU':>10}{'配置内存GiB':>14}   状态")
    print("-" * 92)

    changes: list[tuple[str, int, int]] = []
    errors: list[str] = []

    pool_names = list(pools.keys())
    for pool in pool_names:
        entry = pools[pool] or {}

        if mapping:
            matched = match_pool_to_clusters(pool, mapping, clusters)
            if not matched:
                errors.append(f"{pool}: --map 没匹配到任何集群")
                print(f"{pool:<32}{'-':>10}{'-':>14}   未匹配到集群")
                continue
        elif len(pool_names) == 1:
            # 只有一个池时，按全部集群汇总（当前就是这种情况）
            matched = clusters
        else:
            errors.append(f"{pool}: 存在多个资源池，请用 --map 指定所属集群")
            print(f"{pool:<32}{'-':>10}{'-':>14}   需 --map 指定集群")
            continue

        cpu = sum(c.cpu_millicores for c in matched) / 1000
        mem = sum(c.mem_bytes for c in matched) / 1024**3
        new_cpu = int(round(cpu))
        new_mem = int(round(mem))

        cur_cpu = entry.get("total_cpu")
        cur_mem = entry.get("total_memory_gb")
        if cur_cpu == new_cpu and cur_mem == new_mem:
            status = "一致"
        else:
            d_cpu = new_cpu - (cur_cpu if isinstance(cur_cpu, int) else 0)
            d_mem = new_mem - (cur_mem if isinstance(cur_mem, int) else 0)
            status = f"待更新 ({d_cpu:+d} 核, {d_mem:+d} GiB)"
            changes.append((pool, new_cpu, new_mem))

        print(f"{pool:<32}{new_cpu:>10}{new_mem:>14}"
              f"{str(cur_cpu):>10}{str(cur_mem):>14}   {status}")

    if errors:
        for e in errors:
            print(f"\n错误: {e}", file=sys.stderr)
        return 1

    # 4) 回写
    if not args.write:
        if changes:
            print(f"\n{len(changes)} 个资源池待更新。加 --write 回写。")
        else:
            print("\n全部一致，无需更新。")
        return 0

    if not changes:
        print("\n全部一致，无需回写。")
        return 0

    for pool, new_cpu, new_mem in changes:
        pools[pool]["total_cpu"] = new_cpu
        pools[pool]["total_memory_gb"] = new_mem

    with open(args.pools, "w", encoding="utf-8") as f:
        yaml.dump(doc, f)
    print(f"\n已回写 {args.pools}（{len(changes)} 个资源池）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
