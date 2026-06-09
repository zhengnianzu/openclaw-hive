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

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    class Config:
        env_file = ".env"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.DB_PATH:
            self.DB_PATH = os.path.join(self.HIVE_ROOT, "platform", "platform.db")
        if not self.CONFIG_TEMPLATE:
            self.CONFIG_TEMPLATE = os.path.join(self.HIVE_ROOT, "config.yaml")
        if not self.OBSUTIL_PATH:
            self.OBSUTIL_PATH = os.path.join(self.HIVE_ROOT, "platform", "obsutil", "obsutil")


settings = Settings()
