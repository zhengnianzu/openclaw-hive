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

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # 队列调度配置
    MAX_TOTAL_SANDBOXES: int = 100

    # 空跑模式（调试队列用）：True 时用「sleep N 秒后写 complete.jsonl」的空跑进程
    # 替代真实 hive.py，准备阶段仍走真实流程，因此 pid/停止/完成检测/队列续跑
    # 全部走生产路径，只有任务执行本身是空的。
    MOCK_TASK_RUN: bool = False
    MOCK_TASK_SECONDS: int = 10

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


settings = Settings()
