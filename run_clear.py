import argparse

from omegaconf import OmegaConf

from execution_client.client.client import delete_env, get_all_use_env_id

"""
根据配置文件清理pods, user_id 用来过滤相关pods
清理pods, 手动 kill 脚本后，pods不会立马销毁，执行该脚本立即清理pods,释放k8s资源
"""

parser = argparse.ArgumentParser(description="Openclaw distillation Clear Pods")
parser.add_argument('--config', help="配置文件路径 (JSON/YAML)")
parser.add_argument('--delete', action='store_true', help='删除pods')
args = parser.parse_args()

if args.delete:
    if not args.config:
        raise ValueError("not found --config")

    config_dict = OmegaConf.load(args.config)
    delete_env(config_dict)
else:
    env_ids = get_all_use_env_id()
    print(f"总计有 {len(env_ids)} 个 pods")
    print(env_ids)


