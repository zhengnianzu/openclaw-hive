from pydantic import BaseModel
from typing import Optional


class CodeRepoCreate(BaseModel):
    name: str
    obs_path: str
    version: str = "v1"
    description: str = ""
    main_python_file: str = "harness_automation.py"
    tag: str = ""


class CodeRepoUpdate(BaseModel):
    # 没有 name / version —— 下载目录 code/src/{name}/{version} 与 tar 路径都由它们
    # 拼成（instances.py 生成实例配置时），改了会让已打包的 tar 对不上。
    obs_path: Optional[str] = None
    description: Optional[str] = None
    main_python_file: Optional[str] = None
    tag: Optional[str] = None


class CodeRepoInfo(BaseModel):
    id: int
    name: str
    obs_path: str
    version: str
    description: str = ""
    main_python_file: str = "harness_automation.py"
    tag: str = ""
    created_at: str
    created_by: str = ""


class CodeRepoTagCreate(BaseModel):
    name: str


class CodeRepoTagInfo(BaseModel):
    id: int
    name: str
    created_at: str
    created_by: str = ""
