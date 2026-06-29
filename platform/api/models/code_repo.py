from pydantic import BaseModel


class CodeRepoCreate(BaseModel):
    name: str
    obs_path: str
    version: str = "v1"
    description: str = ""
    main_python_file: str = "openclaw_automation.py"


class CodeRepoInfo(BaseModel):
    id: int
    name: str
    obs_path: str
    version: str
    description: str = ""
    main_python_file: str = "openclaw_automation.py"
    created_at: str
    created_by: str = ""
