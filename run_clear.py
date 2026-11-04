import argparse
import asyncio
import glob
import os
import subprocess
import json
from omegaconf import OmegaConf

from env_sdk.compat.legacy_adapter import build_make_config   # 安装新版sdk, 新增导入该项
from env_sdk.controlplane import attach_sandbox
from execution_client.core.db import get_all_use_env_id, soft_delete_env_info

"""
根据配置文件清理pods, sandbox_id_prefix 用来过滤相关pods
清理pods, 手动 kill 脚本后，pods不会立马销毁，执行该脚本立即清理pods,释放k8s资源
python run_clear.py --del_all 删除所有pods
python run_clear.py --config config.yaml 删除指定沙箱

默认扫描 KUBECONFIG_DIR 下的全部集群(目前三个 worker 集群);
用法：
# 三集群全扫（默认）
python run_clear.py --config config.yaml

# 只清一个集群（原有能力）
python run_clear.py --config config.yaml --kubeconfig /path/to/kubeconfig.yaml

# 删本地 DB 记录的全部沙箱（不扫集群）
python run_clear.py --config config.yaml --del_all
"""

# 三个 worker 集群的 kubeconfig 目录。main/rl-agentic-cce.yaml 与
# workers/internal-green-prod-worker/rl-agentic-cce.yaml 指向同一个 server,
# 按 server 去重后实际是三个集群。
KUBECONFIG_DIR = (
    "/data/ai-infra/omni-env/team-mm-rl/region-cn-southwest-2/"
    "cluster-rl-agentic-cce/plane-default/version-v1.5.0/kubeconfigs"
)

# 单个 kubectl 查询的超时(秒)。大集群(实测 worker 10-22 那台有 3 万+ 带 label
# 的 Pod)API server 扫 etcd 就要 60s+, 原来的 30s 必然超时, 且超时后会被误报成
# "找到 0 个"。给到 180s 留足余量。
KUBECTL_TIMEOUT = 180


def _kubeconfig_server(path: str) -> str:
    """读取 kubeconfig 里第一个 cluster 的 server, 用于跨文件去重。"""
    try:
        result = subprocess.run(
            ["kubectl", "--kubeconfig", path, "config", "view",
             "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def discover_kubeconfigs(kubeconfig_dir: str) -> list:
    """递归找出目录下所有 kubeconfig, 按 server 去重, 返回文件路径列表。

    同一集群可能有多份副本(main/ 与 internal-green-prod-worker/ 就是), 重复
    扫描会重复执行 kubectl, 还会让同一个 sandbox_id 被收集两次。
    """
    paths = sorted(glob.glob(os.path.join(kubeconfig_dir, "**", "*.yaml"), recursive=True))
    if not paths:
        raise FileNotFoundError(f"no kubeconfig found under {kubeconfig_dir}")

    seen_servers = {}
    picked = []
    for path in paths:
        server = _kubeconfig_server(path)
        if server and server in seen_servers:
            # print(f"[skip] {path} 与 {seen_servers[server]} 指向同一集群 ({server})")
            continue
        if server:
            seen_servers[server] = path
        picked.append(path)
    return picked


def get_sandbox_ids_from_k8s_old(sandbox_id_prefix, sandbox_namespace, kubeconfig=None):
    prefix = f"sandbox-{sandbox_id_prefix}-"
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += ["get", "pods", "-n", sandbox_namespace, "--no-headers"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=KUBECTL_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"kubectl get pods 执行失败：{result.stderr.strip()}")
        sandbox_ids = []
        for line in result.stdout.strip().splitlines():
            pod_name = line.split()[0]
            if pod_name.startswith(prefix):
                sandbox_ids.append(pod_name.removeprefix("sandbox-"))
        return sandbox_ids
    except subprocess.TimeoutExpired:
        print("错误： kubectl 执行超时")
        return []


def get_sandbox_ids_from_k8s(sandbox_id_prefix, sandbox_namespace, kubeconfig=None,
                             timeout=KUBECTL_TIMEOUT):
    """
    从 k8s 获取 sandbox_id。
    真正的 sandbox_id 存储在 Pod label 'omni-env.ai/sandbox-id' 中，
    而不是从 Pod 名推导。

    kubeconfig 为 None 时走默认 ~/.kube/config; 指定时只查那一个集群。

    输出用 go-template 只取 label 值, 不用 -o json: 大集群里带该 label 的 Pod
    可能有 3 万个, 全量 JSON 有 800MB+, 传输和解析都是纯浪费。实测第三个集群
    全量 JSON 需 60s+, 只取 label 也要 ~60s —— 瓶颈是 API server 扫 etcd,
    所以超时必须给足(见 KUBECTL_TIMEOUT), 光缩小输出不够。
    """
    # 用 label selector 直接过滤，避免拉取全部 Pod
    label_selector = "omni-env.ai/sandbox-id"
    cmd = ["kubectl"]
    if kubeconfig:
        cmd += ["--kubeconfig", kubeconfig]
    cmd += [
        "get", "pods",
        "-n", sandbox_namespace,
        "-l", label_selector,
        "-o",
        'go-template={{range .items}}{{index .metadata.labels "omni-env.ai/sandbox-id"}}{{"\\n"}}{{end}}',
        "--request-timeout=120s",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            print(f"kubectl get pods 执行失败：{result.stderr.strip()}")
            return []

        sandbox_ids = []
        for line in result.stdout.splitlines():
            sid = line.strip()
            if not sid:
                continue
            # 如果指定了前缀，只保留匹配的
            if sandbox_id_prefix and not sid.startswith(sandbox_id_prefix):
                continue
            sandbox_ids.append(sid)
        return sandbox_ids
    except subprocess.TimeoutExpired:
        # 带上集群名和实际超时值, 否则多集群下看不出是哪个集群卡住
        print(f"错误： kubectl 执行超时 ({timeout}s, 集群={kubeconfig or '~/.kube/config'})")
        return []


def collect_sandbox_ids(sandbox_id_prefix, sandbox_namespace, kubeconfigs):
    """在多个集群里分别查 sandbox, 合并去重。

    一个 sandbox 只可能在一个集群里, 但保留跨集群去重以防 kubeconfig 有重复。
    单个集群查询失败不影响其余集群 —— 继续收集, 只打印警告。
    """
    found = {}          # sandbox_id -> 来源 kubeconfig
    total = len(kubeconfigs)
    for idx, kubeconfig in enumerate(kubeconfigs, 1):
        cluster_label = kubeconfig or "~/.kube/config"
        ids = get_sandbox_ids_from_k8s(
            sandbox_id_prefix, sandbox_namespace, kubeconfig=kubeconfig
        )
        print(f"[{idx}/{total}] {cluster_label}: 找到 {len(ids)} 个")
        for sid in ids:
            found.setdefault(sid, cluster_label)
    return found


async def close_sandboxes(sandbox_ids, config):
    """按 sandbox_id 走新 controlplane 关沙箱: POST /api/v1/sandboxes/<id>/close。"""

    async def close_one(sid):
        try:
            client = await attach_sandbox(sandbox_id=sid, config=config)
            result = await client.close()
            print(f"[close_ok] sandbox_id={sid} result={getattr(result, 'code', None)}")
        except Exception as e:
            print(f"[close_fail] sandbox_id={sid} error={e}")
        finally:
            # 无论成功失败, 都把本地 DB 标记为已删, 避免下次误清理
            try:
                soft_delete_env_info(sid)
            except Exception:
                pass

    await asyncio.gather(*(close_one(sid) for sid in sandbox_ids))


def main() -> None:
    parser = argparse.ArgumentParser(description="Openclaw distillation Clear Pods")
    parser.add_argument('--config', help="配置文件路径 (JSON/YAML)")
    parser.add_argument('--del_all', action='store_true', help='删除全部pods')
    parser.add_argument('--kubeconfig', help="只清理该 kubeconfig 对应的集群")
    parser.add_argument('--kubeconfig-dir', default=KUBECONFIG_DIR,
                        help=f"多集群 kubeconfig 目录 (默认 {KUBECONFIG_DIR})")
    args = parser.parse_args()

    if not args.config:
        raise ValueError("not found --config")
    config_dict = OmegaConf.load(args.config)

    # --del_all 走本地 DB 记录, 与集群无关, 不需要扫 kubeconfig
    if args.del_all:
        sandbox_ids = get_all_use_env_id()
    else:
        if args.kubeconfig:
            kubeconfigs = [args.kubeconfig]
        elif os.path.isdir(args.kubeconfig_dir):
            kubeconfigs = discover_kubeconfigs(args.kubeconfig_dir)
        else:
            print(f"警告: kubeconfig 目录不存在 {args.kubeconfig_dir}, 回退默认 ~/.kube/config")
            kubeconfigs = [None]
        print(f"待扫描集群: {len(kubeconfigs)} 个")
        for kc in kubeconfigs:
            print(f"  - {kc or '~/.kube/config'}")

        sandbox_id_prefix = config_dict.sandbox_id_prefix
        sandbox_namespace = config_dict.get("sandbox_namespace", "omni-env-default-worker")
        found = collect_sandbox_ids(sandbox_id_prefix, sandbox_namespace, kubeconfigs)
        sandbox_ids = list(found)
        if not sandbox_ids:
            print(f"sandbox_id_prefix={sandbox_id_prefix} 下没有找到任何运行中的 Pod")
            return
        print(f"sandbox_id_prefix={sandbox_id_prefix} 共找到 {len(sandbox_ids)} 个 Pod")

    asyncio.run(close_sandboxes(sandbox_ids, config_dict))


if __name__ == "__main__":
    main()
