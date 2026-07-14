import argparse
import asyncio
import subprocess
from omegaconf import OmegaConf

from env_sdk.compat.legacy_adapter import build_make_config   # 安装新版sdk, 新增导入该项
from env_sdk.controlplane import attach_sandbox
from execution_client.core.db import get_all_use_env_id, soft_delete_env_info

"""
根据配置文件清理pods, sandbox_id_prefix 用来过滤相关pods
清理pods, 手动 kill 脚本后，pods不会立马销毁，执行该脚本立即清理pods,释放k8s资源
python run_clear.py --del_all 删除所有pods
python run_clear.py --config config.yaml 删除指定沙箱
"""

def get_sandbox_ids_from_k8s(sandbox_id_prefix):
    prefix = f"sandbox-{sandbox_id_prefix}-"
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", "omni-env-default-worker", "--no-headers"],
            capture_output=True, text=True, timeout=30,
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
    args = parser.parse_args()

    if not args.config:
        raise ValueError("not found --config")
    config_dict = OmegaConf.load(args.config)

    if args.del_all:
        sandbox_ids = get_all_use_env_id()
    else:
        sandbox_id_prefix = config_dict.sandbox_id_prefix
        sandbox_ids = get_sandbox_ids_from_k8s(sandbox_id_prefix)
        if not sandbox_ids:
            print(f"sandbox_id_prefix={sandbox_id_prefix} 下没有找到任何运行中的 Pod")
            return
        print(f"sandbox_id_prefix={sandbox_id_prefix} 找到 {len(sandbox_ids)}个 Pod")

    asyncio.run(close_sandboxes(sandbox_ids, config_dict))


if __name__ == "__main__":
    main()
