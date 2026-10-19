---
title: 文档中心使用说明
category: 平台使用
tags:
- 入门
- wiki
summary: 文档正文就是 Git 里的 Markdown 文件，可在界面编辑，也可用 IDE 直接改。
is_pinned: true
author: admin
created_at: '2026-10-19T00:00:00'
updated_at: '2026-10-19T00:00:00'
---

# 文档中心使用说明

## 正文存在哪里

每篇文档对应 `platform/doc/wiki/<文件名>.md`。**文件是内容的唯一真相**，SQLite 里只有一份可随时重建的索引。

- 界面里的编辑器读写的就是这个文件，保存即刻生效，不需要重启。
- 也可以直接用 IDE 改文件、用 `git add` / `commit` 提交；下次打开页面会自动同步（按 mtime 检测）。
- `git pull` 拉到新文档或删掉的文档，界面会随之增减，无需手工导入。
- 想回滚、看 diff、查谁改了什么，都用 Git：

```bash
git log -p -- platform/doc/wiki/
git diff HEAD~1 -- platform/doc/wiki/openclaw-v3-release.md
git checkout HEAD~1 -- platform/doc/wiki/openclaw-v3-release.md
```

## 文件格式

文件开头是一段 YAML frontmatter，下面是正文（标准 Markdown）：

```markdown
---
title: openclaw_v3 发布说明
category: 发布说明
tags:
- 发布
- harness
summary: v3 变更点
linked_entity_type: harness_config
linked_entity_key: openclaw_v3
is_pinned: false
author: admin
created_at: '2026-10-19T11:00:00'
updated_at: '2026-10-19T11:00:00'
---

## 变更

- 新增 `--concurrent`
```

- `title` 缺省时用文件名。
- `category` 写分类**名称**（不是 id），这样 diff 里可读；界面上新建分类时会自动生成。
- `tags` 用 YAML 列表或逗号分隔字符串都行。
- `linked_entity_type` 可选值：`image` / `code_repo` / `harness_config` / `sandbox_config` / `resource_pool` / `config_template`。
- 文件名即 URL：`#/wiki/openclaw-v3-release`。创建后不建议改名（会让已有通知的链接失效）。

## 发布为通知

文档写完后，管理员可在详情页点「发布为通知」，选择是否推送给所有用户。

- 通知存的是**发布时的正文快照**，之后修改文档不会改动已发出的公告。
- 通知记录和「谁读过」存在 SQLite 里 —— 那是运行时数据，不属于 Git。
- 撤回是软删，保留已读记录。

## 已知限制

- 暂无文档版本历史 —— 用 `git log` 代替。
- 暂不支持上传附件；图片请用 http/https 外链。
- 正文里的 HTML 标签会被转义（防 XSS），只渲染标准 Markdown。
