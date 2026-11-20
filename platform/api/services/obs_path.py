"""公共 OBS 路径工具。

约定：数据库与前端表单一律存完整路径（obs://bucket/a/b/c/）；
      写入 harness config.yaml 前必须剥桶名得到相对路径（a/b/c）。

这里只做字符串变换，不涉及网络或文件系统。
"""

import re


def strip_bucket(path: str) -> str:
    """把 obs://bucket/a/b 或 bucket/a/b 或 a/b 归一成 a/b（无首尾斜杠）。

    等价于前端 obsConfig.js 中的 stripBucket，但不依赖运行时桶配置——
    凡是以 obs:// 开头的路径均剥离桶段，其余直接去首尾斜杠。
    """
    p = str(path or "").strip()
    if p.startswith("obs://"):
        # obs://bucket-name/rest/of/path  →  rest/of/path
        p = re.sub(r'^obs://[^/]+/?', '', p)
    return p.strip("/")


def join_bucket(bucket_root: str, rel: str) -> str:
    """把相对路径拼回完整 OBS 路径（结尾带 /）。

    bucket_root 形如 obs://bucket-name/ 或 obs://bucket-name。
    """
    root = bucket_root.rstrip("/") + "/"
    clean = rel.strip("/")
    return root + (clean + "/" if clean else "")
