from pydantic import BaseModel, model_validator
from typing import Optional


class CodeRepoCreate(BaseModel):
    name: str
    # 来源：obs(默认) = 填 obs_path 由 obsutil 拉；git = 填 repo_url/branch 由平台 clone。
    source_type: str = "obs"
    obs_path: str = ""
    repo_url: str = ""
    branch: str = ""
    # 仓内子目录；空 = 仓根。只对 git 来源有意义。
    subdir: str = ""
    version: str = "v1"
    description: str = ""
    main_python_file: str = "harness_automation.py"
    tag: str = ""
    # 全局默认代码仓(唯一): 高级配置选完 harness 后自动填入
    is_default: bool = False

    @model_validator(mode="after")
    def _check_source(self):
        """按来源校验必填项，并把 source_type / branch 归一化。

        放在模型里而不是 router：创建接口和将来可能的批量导入都该走同一套判据。
        """
        self.source_type = (self.source_type or "obs").strip().lower()
        if self.source_type not in ("obs", "git"):
            raise ValueError("source_type 只能是 obs 或 git")
        if self.source_type == "git":
            if not (self.repo_url or "").strip():
                raise ValueError("Git 来源必须填写仓库地址")
            # 缺省补 main：绝大多数仓是它，让用户少填一格。
            self.branch = (self.branch or "").strip() or "main"
        elif not (self.obs_path or "").strip():
            raise ValueError("OBS 来源必须填写 OBS 路径")
        return self


class CodeRepoUpdate(BaseModel):
    # 没有 name / version —— 下载目录 code/src/{name}/{version} 与 tar 路径都由它们
    # 拼成（instances.py 生成实例配置时），改了会让已打包的 tar 对不上。
    #
    # 同理没有 source_type：切换来源会让已打包产物与目录规则对不上
    # （obs 不带 commit 层、git 带），要换来源应当新建一条记录。
    obs_path: Optional[str] = None
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    subdir: Optional[str] = None
    description: Optional[str] = None
    main_python_file: Optional[str] = None
    tag: Optional[str] = None
    is_default: Optional[bool] = None
    # 创建页可见性开关: 停用后高级配置下拉不再列出(存量实例引用不受影响)
    enabled: Optional[bool] = None


class CodeRepoInfo(BaseModel):
    id: int
    name: str
    source_type: str = "obs"
    obs_path: str = ""
    repo_url: str = ""
    branch: str = ""
    subdir: str = ""
    resolved_commit: str = ""
    synced_at: Optional[str] = None
    # 此刻是否有同步在跑（由同步锁的占用状态推出）。只对 git 来源有意义。
    syncing: bool = False
    version: str
    description: str = ""
    main_python_file: str = "harness_automation.py"
    tag: str = ""
    is_default: bool = False
    enabled: bool = True
    created_at: str
    created_by: str = ""


class CodeRepoTagCreate(BaseModel):
    name: str


class CodeRepoTagInfo(BaseModel):
    id: int
    name: str
    created_at: str
    created_by: str = ""
