import os
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "OpenClaw Hive Platform"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    ALGORITHM: str = "HS256"

    # config.py -> core -> api -> platform -> openclaw-hive
    HIVE_ROOT: str = str(Path(__file__).resolve().parent.parent.parent.parent)
    DB_PATH: str = ""
    OBSUTIL_PATH: str = ""
    OBS_BUCKET: str = "obs://rl-agentdata"
    CONFIG_TEMPLATE: str = ""
    SETTINGS_DIR: str = ""
    OUTPUT_CACHE: str = ""
    INSTANCE_DIR: str = ""
    # 文档中心的正文目录（Git 管理；SQLite 只存索引，可随时重建）
    WIKI_DIR: str = ""

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # 队列调度配置（已取消全局沙箱上限 MAX_TOTAL_SANDBOXES）
    ORPHAN_PREPARING_GRACE_SECONDS: int = 120

    # 全局并发上限：任何队列同时占用的任务数都不能超过它。
    # 队列实际并发 = min(QUEUE_MAX_CONCURRENT, 该队列的 max_concurrent)。
    # 默认 2；设为 1 则所有队列都只能跑 1 个（严格串行）。
    QUEUE_MAX_CONCURRENT: int = 2

    # preparing 状态超时（秒）：准备阶段会做 OBS 下载（单次最长 600s，可能两次）
    # 与打包（120s）。若协程异常消失（reload/事件循环错误），实例会永久卡在
    # preparing 占住队列槽位；后台 refresher 超过此阈值仍未拿到 pid 的实例判为
    # 卡住，标记 failed 并放行下一个任务。
    PREPARE_TIMEOUT_SECONDS: int = 1800

    # 空跑模式（调试队列用）：True 时用「sleep N 秒后写 complete.jsonl」的空跑进程
    # 替代真实 hive.py，准备阶段仍走真实流程，因此 pid/停止/完成检测/队列续跑
    # 全部走生产路径，只有任务执行本身是空的。
    MOCK_TASK_RUN: bool = False
    MOCK_TASK_SECONDS: int = 10

    # obsutil 凭据（Windows 辅助 tar 打包用）。utils/win_util.py 直接解析
    # .env 读取这三个键；这里必须声明，否则 pydantic-settings 的 extra=forbid
    # 会让带 IK/KK/EK 的 .env 在 import 阶段直接报错。
    IK: str = ""
    KK: str = ""
    EK: str = ""

    # 对话导出服务（模型配置面板「浏览/导出」对接，.env 配置）
    EXPORT_BASE: str = ""
    EXPORT_ACCESS_KEY: str = ""

    class Config:
        env_file = ".env"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        platform_dir = os.path.join(self.HIVE_ROOT, "platform")
        if not self.DB_PATH:
            self.DB_PATH = os.path.join(platform_dir, "platform.db")
        if not self.CONFIG_TEMPLATE:
            self.CONFIG_TEMPLATE = os.path.join(platform_dir, "settings", "config.yaml")
        if not self.OBSUTIL_PATH:
            self.OBSUTIL_PATH = os.path.join(platform_dir, "obsutil", "obsutil")
        if not self.SETTINGS_DIR:
            self.SETTINGS_DIR = os.path.join(platform_dir, "settings")
        if not self.OUTPUT_CACHE:
            self.OUTPUT_CACHE = os.path.join(platform_dir, "output_cache")
        if not self.INSTANCE_DIR:
            self.INSTANCE_DIR = os.path.join(platform_dir, "instances")
        if not self.WIKI_DIR:
            self.WIKI_DIR = os.path.join(platform_dir, "doc", "wiki")


settings = Settings()
