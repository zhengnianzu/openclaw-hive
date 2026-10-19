"""系统设置总览 —— 「当前能配置哪些选项」的统一入口。

平台里配置实体分散在 5 张表 + 2 个 YAML 文件里，各自有独立的页面与权限口径。
这里把它们收成一份清单，一次请求返回全部入口 + 计数，避免前端为一张总览页打六七个请求。

计数只用于展示，读失败 / 表为空一律降级为 0，不让一个坏掉的配置阻塞整页。
"""
from fastapi import APIRouter, Depends

from ..core.database import get_connection
from ..core.security import get_current_user
from .resource_pools import load_pools

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _count(conn, sql: str, params=()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0
    except Exception:
        return 0


@router.get("/overview")
def settings_overview(user: dict = Depends(get_current_user)):
    with get_connection() as conn:
        images = _count(conn, "SELECT COUNT(*) AS cnt FROM images")
        code_repos = _count(conn, "SELECT COUNT(*) AS cnt FROM code_repos")
        harness_configs = _count(conn, "SELECT COUNT(*) AS cnt FROM harness_configs")
        sandbox_configs = _count(conn, "SELECT COUNT(*) AS cnt FROM sandbox_configs")
        config_templates = _count(
            conn, "SELECT COUNT(*) AS cnt FROM config_templates WHERE owner = ?",
            (user["username"],),
        )
        wiki_docs = _count(conn, "SELECT COUNT(*) AS cnt FROM wiki_documents")
        unread = _count(
            conn,
            """SELECT COUNT(*) AS cnt FROM notifications n
               LEFT JOIN notification_reads r
                      ON r.notification_id = n.id AND r.username = ?
               WHERE n.revoked = 0 AND r.notification_id IS NULL""",
            (user["username"],),
        )
        users = _count(conn, "SELECT COUNT(*) AS cnt FROM users")

    pools = load_pools()

    items = [
        {"key": "images", "group": "运行配置", "title": "镜像管理",
         "desc": "实例使用的镜像地址（版本即 address 里的 tag）",
         "route": "/images", "count": images, "role": "operator"},
        {"key": "code_repos", "group": "运行配置", "title": "代码管理",
         "desc": "代码仓库与版本（OBS 路径 + version）",
         "route": "/code-repos", "count": code_repos, "role": "operator"},
        {"key": "harness_configs", "group": "运行配置", "title": "Harness 配置",
         "desc": "每种 harness 类型的各版本配置文件（settings/<type>/<version>/）",
         "route": "/harness-configs", "count": harness_configs, "role": "user"},
        {"key": "sandbox_configs", "group": "运行配置", "title": "沙箱配置",
         "desc": "沙箱模板派生文件（settings/sandbox/*.yaml），按类型设默认",
         "route": "/sandbox-configs", "count": sandbox_configs, "role": "operator"},
        {"key": "resource_pools", "group": "运行配置", "title": "资源池",
         "desc": "池总量在 settings/sandbox/resource_pools.yaml（文件编辑，页面只读）",
         "route": "/analytics", "count": len(pools), "role": "operator"},
        {"key": "config_templates", "group": "个人配置", "title": "配置模板",
         "desc": "仅自己可见的创建任务默认参数",
         "route": "/config-templates", "count": config_templates, "role": "user"},
        {"key": "wiki", "group": "文档与通知", "title": "文档中心",
         "desc": "更新说明 / 使用手册；管理员可发布为全局通知",
         "route": "/wiki", "count": wiki_docs, "role": "user"},
        {"key": "notifications", "group": "文档与通知", "title": "通知中心",
         "desc": "全局公告与未读状态",
         "route": "/notifications", "count": unread, "role": "user"},
        {"key": "users", "group": "系统", "title": "用户管理",
         "desc": "账号、角色（admin / operator / viewer）与启用状态",
         "route": "/users", "count": users, "role": "admin"},
    ]
    return {"items": items, "role": user.get("role", "viewer")}
