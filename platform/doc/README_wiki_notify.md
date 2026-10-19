# 文档中心（Wiki）+ 全局站内通知 — 实现文档

> 状态：**已实现（P0）**（2026-10-19）。
> 目标：给「配置 / 代码 / 镜像更新了什么」一个落地的承载地与触达渠道 —— 管理员手写 Markdown
> 记录变更点，可选择是否作为全局通知推送给所有用户。

## 背景

平台侧此前有 5 套互不统一的配置实体，且都没有变更记录：

| 实体 | 真相来源 | 版本概念 | 变更历史 |
|---|---|---|---|
| `images` | SQLite | 版本内嵌在 `address` 的 tag | 无 |
| `code_repos` | SQLite + `code/{src,tar}/{name}/{version}/` | `version` 字段 | 无 |
| `harness_configs` | SQLite 索引 + `settings/{type}/{version}/` | `version` 字段 | 只能 copy 出新版本 |
| `sandbox_configs` | SQLite 索引 + `settings/sandbox/{type}_{id}.yaml` | 无 | 只有 `reset` |
| `config_templates` | SQLite（按 owner 隔离） | 无 | 无 |
| `resource_pools` | 纯 `settings/sandbox/resource_pools.yaml` | 无 | 无 |

结果是管理员更新了 harness 版本 / 代码仓 / 镜像后，用户完全无从得知改了什么，只能靠口头或 IM。
平台此前也没有任何站内通知、邮件、webhook 能力。

本次**不**做配置实体统一抽象 —— 那是另一件事。本次只补「说明」与「触达」两环。

## 架构

```
管理员
  │  Markdown 正文（可关联 harness_config:openclaw_v3 等实体）
  ▼
wiki_documents ──── POST /documents/{slug}/publish ─┐
  │                                                │ 同步请求，单事务
  │  content_md 作为快照写入                       ▼
  │                                        notifications（广播，1 行）
  │                                                │
  │                                                │ LEFT JOIN 即时算未读
  │                                                ▼
  │                                     notification_reads（谁读过，按需插行）
  │                                                │
  └── 前端 v-html 渲染（markdown-it + DOMPurify）  ├─→ 顶栏铃铛 60s 轮询未读数
                                                   └─→ /notifications 通知中心
```

### 三个关键取舍

**1. 广播表 + 已读表，不是「每用户一条通知」**
需求只有全局广播。广播表下发布一次只写 1 行，读状态按需插入天然稀疏，撤回/改标题是单行 UPDATE。
几十人、百级通知量下未读数用 `LEFT JOIN` 即时算即可，不需要物化计数列。
将来若要按角色/用户定向推送，给 `notifications` 加 `audience` + 目标表即可，已读表结构不变；
而「每用户一条」要改就得重写整个模型。

**2. 发布是普通同步请求，不挂后台循环**
`run.sh` 用 `uvicorn --workers 4`，**只有进程级后台任务**（`@app.on_event("startup")` 里的
`asyncio.create_task`、`while True`）会被 4 个 worker 各跑一遍。发布走的是普通 HTTP 请求，
只由一个 worker 处理，写一行即返回，天然不会重复。

因此本模块硬约束：**不在 `main.py` 的 startup 里加任何通知逻辑**（含补发、定时清理），
**不新增 `while True` 循环**。接收侧是拉取模型（铃铛 60s 轮询），与 worker 数量无关。

**3. Markdown 文件是内容真相，SQLite 只是可重建的索引**
正文与元数据在 `platform/doc/wiki/*.md`，由 Git 管理：有 commit 历史、diff、blame、回滚，
也能在 IDE 里直接编辑。`wiki_documents` 表只存派生索引（标题/标签/分类/关联实体/hash/mtime），
随时可以从目录重建，换机器、删库都不丢内容。

代价是失去了「正文也在一张表里」的强一致 —— 但索引是幂等重建的，且 `load_doc()` 永远从文件读正文，
所以界面显示的必然等于 Git 里的内容。通知记录与已读状态仍留在 DB：那是 per-user 运行时数据，
天然不属于 Git。

## 数据模型

DDL 在 `api/core/database.py` 的 `init_db()` 末尾，全部为 `CREATE TABLE/INDEX IF NOT EXISTS`，
多 worker 并发执行幂等。

磁盘（真相）：

```
platform/doc/wiki/
  ├── README.md                        ← slug = 文件名（不含 .md）
  └── openclaw-v3-release.md           ← YAML frontmatter + Markdown 正文
```

```markdown
---
title: openclaw_v3 发布说明
category: 发布说明
tags: [发布, harness]
summary: v3 变更点
linked_entity_type: harness_config
linked_entity_key: openclaw_v3
is_pinned: false
author: admin
created_at: '2026-10-19T11:00:00'
updated_at: '2026-10-19T11:00:00'
---

## 变更
```

DB（派生索引 + 运行时状态）：

```sql
wiki_categories(id, name UNIQUE, sort_order, created_at)

-- 索引表，可从目录完整重建
wiki_documents(slug PRIMARY KEY, title, category_id, tags, summary,
               linked_entity_type, linked_entity_key,
               author, updated_by, is_pinned,
               content_hash, file_mtime, last_notification_id,
               created_at, updated_at)

notifications(id, title, content_md, level, doc_id, doc_slug,
              created_by, revoked, created_at)

notification_reads(notification_id, username, read_at,
                   PRIMARY KEY (notification_id, username))
```

- **slug = 文件名**，作为跨机器稳定的路由参数（`#/wiki/openclaw-v3-release`），不像自增 id 会因重建顺序漂移。
- **category 在 frontmatter 里存分类名**，不是 id —— diff 可读。改分类名时后端会同步改写所有引用它的文件。
- `sync_index()` 只重新解析 mtime 变化的文件；目录里没有的 slug 会从索引删除（响应 `git rm` / `git pull`）。
- 索引表若缺失可直接 `DROP` 重建；正文不在 DB，重建不丢任何内容。
- `tags` 逗号分隔（DB 侧）；按整词匹配用 `(',' || tags || ',') LIKE '%,x,%'`，避免 `harness` 命中 `harness2`。
- `notifications.content_md` 是**发布时快照**，文档后续编辑不影响已发出的通知。
- `revoked=1` 为软删，保留已读记录。
- `doc_slug` 指向文档；文档被删后通知仍保留，前端降级为纯文本。
- **文件写入是原子的**：临时文件 + `os.replace`，避免并发读到半截内容。

## 后端

新增文件：

| 文件 | 内容 |
|---|---|
| `api/models/wiki.py` | 分类/文档模型；`LINKED_ENTITY_TYPES` 白名单 |
| `api/models/notification.py` | 通知模型；`NotificationSentItem` 多两个统计字段 |
| `api/routers/wiki.py` | 文档与分类 CRUD、`/publish` |
| `api/routers/notifications.py` | 未读数、列表、已读、撤回 |
| `api/routers/settings_overview.py` | 「当前能配置哪些选项」总览 |

`main.py` 追加三行 `include_router`。**`requirements.txt` 无变化**（后端不做 Markdown 渲染）。

### 端点

Wiki（读：`get_current_user`；写：`WIKI_WRITE`）

- `GET/POST /api/wiki/categories`、`PUT/DELETE /api/wiki/categories/{id}`
  删除分类只解除归类（`category_id = NULL`），不删文档。
- `GET /api/wiki/documents` — 支持 `q` / `category_id` / `tag` / `linked_entity_type` /
  `linked_entity_key` / `pinned` / `page` / `page_size`。**响应不含 `content_md`**（显式列名），
  避免列表体积随正文长度增长。
- `GET/POST /api/wiki/documents`、`GET/PUT/DELETE /api/wiki/documents/{slug}`
- `POST /api/wiki/documents/{slug}/publish` — body `{push, title?, level?, content_md?}`；
  `push=false` 只返回 `{notification_id: null}`。

通知

- `GET /api/notifications?unread_only=&page=&page_size=`
- `GET /api/notifications/unread-count`
- `POST /api/notifications/{id}/read`、`POST /api/notifications/read-all`
- `GET /api/notifications/sent`（admin，含 `read_count / total_users`）
- `DELETE /api/notifications/{id}`（admin，软删）

> ⚠️ 路由顺序：`/unread-count`、`/read-all`、`/sent` 必须注册在 `/{notification_id}` **之前**，
> 否则会被当作 int 路径参数报 422。

设置总览

- `GET /api/settings/overview` — 一次返回 9 个入口 + 计数 + 各自所需角色，
  前端据此过滤卡片。计数读失败降级为 0，不让单个坏配置阻塞整页。

### 权限

| 操作 | viewer | operator | admin |
|---|---|---|---|
| 读文档 / 分类 / 通知 | ✅ | ✅ | ✅ |
| 文档、分类增删改 / 发布通知 | ❌ | ❌ | ✅ |
| 通知已读 / 全部已读 | ✅ | ✅ | ✅ |
| 「我发出的」/ 撤回通知 | ❌ | ❌ | ✅ |
| 设置总览 | ✅ | ✅ | ✅ |

写权限收敛为 `api/routers/wiki.py` 的 `WIKI_WRITE = require_admin`。
P1 若要放开给 operator，只改这一行 + 前端按钮的 `v-if`。

## 前端

新增依赖：`markdown-it` + `dompurify`（`package.json`）。

| 文件 | 职责 |
|---|---|
| `src/utils/markdown.js` | `renderMarkdown()`：markdown-it(`html:false, linkify, breaks`) → DOMPurify |
| `src/components/MarkdownView.vue` | **全项目唯一的 `v-html` 使用点** |
| `src/components/NotificationBell.vue` | 顶栏铃铛 + 未读角标 |
| `src/stores/notification.js` | 未读数轮询（60s，`silent`）、已读操作 |
| `src/views/WikiList.vue` | 列表 + 筛选 + 分类管理 |
| `src/views/WikiDetail.vue` | 详情 + 发布对话框 |
| `src/views/WikiEdit.vue` | 编辑/预览双 tab + 关联实体选择器 |
| `src/views/NotificationCenter.vue` | 通知中心 + admin「我发出的」 |
| `src/views/SettingsOverview.vue` | 设置入口卡片总览 |

改动的既有文件：

- `src/router/index.js` — 新增 6 条路由（`wiki/new` 必须排在 `wiki/:id` 之前）；补 `adminOnly` 守卫
- `src/views/Layout.vue` — 侧栏加「文档中心」「系统设置」；顶栏挂铃铛；`activeMenu` computed
  修掉详情页父菜单不高亮的老问题
- `src/App.vue` — 追加**全局** `.markdown-body` 样式
- `src/main.js` — 注册新用到的图标

### Markdown 安全

- `html: false`：原始 HTML 一律转义成文本，这是最强的防线。
- `validateLink` 默认拒绝 `javascript:` / `vbscript:` / 非图片 `data:`。
- DOMPurify 作为第二道闸（管理员账号被借用的场景），并禁掉 `style/iframe/form/input/object/embed`。
- `.markdown-body` 样式必须放 `App.vue` 的全局段 —— `<style scoped>` 命不中 `v-html` 生成的节点。
- 图片只允许 http/https 外链。P0 没有附件上传（`frontend/dist` 每次 build 会清空）。

## 验证

```bash
BASE=http://127.0.0.1:8087
TOKEN=$(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"***"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
AUTH="Authorization: Bearer $TOKEN"

curl -s -X POST $BASE/api/wiki/categories -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"发布说明","sort_order":10}'

curl -s -X POST $BASE/api/wiki/documents -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "title":"openclaw_v3 发布说明","slug":"openclaw-v3-release","category_id":1,"tags":"发布,harness",
  "summary":"v3 变更点","content_md":"## 变更\n\n- 新增 `--concurrent`\n",
  "linked_entity_type":"harness_config","linked_entity_key":"openclaw_v3"}'

curl -s "$BASE/api/wiki/documents?tag=发布" -H "$AUTH"                       # 不含 content_md
curl -s -X POST $BASE/api/wiki/documents/openclaw-v3-release/publish -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"push":true,"level":"success"}'   # {"notification_id":1}
curl -s $BASE/api/notifications/unread-count -H "$AUTH"                      # 1
curl -s -X POST $BASE/api/notifications/read-all -H "$AUTH"
curl -s $BASE/api/notifications/unread-count -H "$AUTH"                      # 0
curl -s $BASE/api/settings/overview -H "$AUTH" | python3 -m json.tool
```

手工验收：

1. admin 新建文档（标题/列表/表格/代码块/外链图）→ 详情渲染正确；
   XSS 探针 `<img src=x onerror=alert(1)>`、`[x](javascript:alert(1))` 应被转义/剥除。
2. 发布时勾选推送 → 另开 viewer 隐身窗口，铃铛 60s 内出现未读；标记已读后刷新仍为 0（服务端状态）。
3. 编辑文档但**不勾选**推送 → viewer 未读数不变。
4. viewer 直接访问 `#/wiki/new` → 被拦回列表；`POST /api/wiki/documents` 返回 403。
5. admin 撤回通知 → viewer 侧 60s 内列表消失。
6. 访问 `/wiki/1` 时侧栏「文档中心」高亮。
7. `sqlite3 <DB_PATH> "SELECT COUNT(*) FROM notification_reads"` 只有被点开过的通知才有行。

## 已知限制 / 后续

- **无文档版本历史**。P1：`wiki_document_revisions`（PUT 前写旧版）+ 详情页 diff/回滚。
- **无附件上传**。P1：`POST /api/wiki/assets`（扩展名白名单 + 5MB），UUID 文件名 + 无鉴权 GET。
- **无定向推送**。P1：`notifications.audience` + 目标表。
- **配置页无反查入口**。P1：Harness/代码管理列表行加「相关文档」，用
  `?linked_entity_type=&linked_entity_key=` 查询。
- **无代码块高亮**（避免引入 highlight.js 增大包体）。
- `users.is_active` 在登录与 `get_current_user` 中均未校验（既有问题），
  `unread-count` 的 `total_users` 统计因此可能略偏高。
