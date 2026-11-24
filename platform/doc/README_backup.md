# 数据库备份

`platform/db_backup.py`。把「重要且不可再生」的数据复制到共享盘，保留最近
**7 天**，超期自动清理。

## 为什么需要

在加这个脚本之前，全仓**没有任何自动化备份**。仅有的备份动作散落在几个一次性
修复脚本里（`fix_missing_task_rows.py`、`cleanup_task_records.py` 的
`shutil.copy2`），只保护它们自己那一次写操作。日常运行中一旦有人手抖 `rm`、
跑错迁移、或 `VACUUM` 写坏库，就没有任何可回滚的副本。

同时 `platform/` 下堆了 **91GB** 历史手工 `.bak`（24 份，最老 7 月），根盘
已用 78%。所以这个方案刻意做两件事：**自动按天过期**，以及**不碰历史文件**。

## 备份什么

| 内容 | 位置 | 体量 | 频率 |
|---|---|---|---|
| `platform.db` | `platform/platform.db` | ~3.2G | 每日 |
| 配置与密钥 | `.env`、`config.yaml`、`configs/hive_*.yaml`、`settings/**`、`wiki/assets/` | ~2.4M gz | 每日 |
| `task_data.db` | `platform/database/task_data.db` | ~13G | 每周日（`--full`） |

`platform.db` 里真正的核心资产是 `users`(48) / `task_instances`(3112) /
`harness_configs`(83) / `batch_experiments`(22) / `code_repos` / `wiki_*`，
合计只有几十 MB —— 其余 3.2G 是 `shallow_queue`（688 万 queued 行）。
虽然队列理论上能从 OBS 重扫重建，但整库备份最省事，且避免「只备份部分表」
在恢复时出现外键/行数不一致。

## 不备份什么

| 内容 | 为什么不备 |
|---|---|
| `platform/wiki/index.db` | 由 `wiki/md/*.md` 自动重建（`wiki_store.sync_index`） |
| `platform/hive_output.db` | 已废弃，见 `api/routers/batch_output.py` 顶部注释 |
| `platform/code/` | 从代码仓 / OBS 重取 |
| `platform/downloads/` | 从 OBS 重取 |
| `OUTPUT_CACHE` 本身 | 沙箱产物，源自 OBS |
| `env_info.db`（仓库根） | 见下方「已知缺口」 |

## 落点

```
/mnt/sfs_turbo/s3-asset-b-hd-cce-aifm-nlp-exp/zhengnianzu/platform_backups/
├── 20261123-204946/           # 每日套件
│   ├── platform.db
│   ├── configs.tar.gz
│   └── manifest.json
├── 20261124-040000.full/      # 周级：额外含 task_data.db
├── db_backup.log
└── .lock
```

**放在 `OUTPUT_CACHE` 的父目录，不放其内部。** `batch_output._resolve_task_dir`
的兜底分支会拿 `OUTPUT_CACHE` 根下的子目录名当候选拼路径，没必要让备份目录
进入产物解析的搜索空间。

可用 `--dest` 或环境变量 `BACKUP_DIR` 覆盖。脚本每次启动会比对 `OUTPUT_CACHE`
的父目录，不一致时打提示 —— `OUTPUT_CACHE` 换盘后默认落点会悄悄留在旧盘上。

## 用法

```bash
cd /root/openclaw-hive/platform
./run.sh db-backup            # 立即备份（每日套件）
./run.sh db-backup-full       # 含 task_data.db（周级，较慢）
./run.sh db-backup-list       # 列出现有备份（只读）
./run.sh db-backup-audit      # 盘点存量 .bak（只读，不删除）
./run.sh db-backup-install-cron  # 幂等写入 crontab
```

也可以直接 `python3 -I db_backup.py run`。

## 设计要点

### 用 SQLite 在线备份 API，不拷文件

库跑在 WAL 模式，`-wal` 里常态有未 checkpoint 的数据。直接 `shutil.copy2`
主库文件会得到「能打开但少数据」的副本。`src.backup(dst)` 拿一致快照，
只持读锁，**备份时不需要停服务**。源库以 `mode=ro` 打开，脚本不会误写生产库。

### 本地暂存 → 校验 → 顺序写出

第一版直接在 sfs 上做校验，实测 `PRAGMA quick_check` 在 NFS 上会卡进
**D 状态**十几分钟不返回（同一文件的顺序读只要 2 秒）。现在分三段：

1. 本地 `tempfile.mkdtemp` 里生成快照并跑 `quick_check` + 行数抽样（17s）
2. 顺序 `read→write` 写到 sfs，写完不回读（5s）
3. 最后写 `manifest.json`

总耗时从 655s 降到 **25s**。

### manifest.json 是完成标记

sfs_turbo 是 NFSv3，**`os.rename` 对文件和目录都返回 EPERM**（实测），
所以「写临时名再原子改名」不可用。改为：manifest 最后写，`list` 与 `prune`
只认 `complete=true` 的目录。中途崩掉留下的半成品会被下次 `prune` 清掉。

### 保留 7 天，按天算不按份数

`KEEP_DAYS = 7`。按份数保留会在「一天跑多次」时留下跨越很久的旧备份；
按天算才是「最多维持一周」的字面意思。

## 恢复

恢复是 destructive 操作，脚本把它做成逐步确认，不做一键覆盖：

```bash
./run.sh db-backup-list                       # 1. 找到要恢复的目录
./run.sh db-backup-restore <备份目录>          # 2. 交互确认后执行
```

`db-backup-restore` 会：停服务（含全部 worker）→ 把当前库改名为
`platform.db.pre-restore.<时间戳>`（**不直接覆盖**）→ 删掉 `-wal`/`-shm`
→ 拷回 → `PRAGMA integrity_check` → 提示手动 `./run.sh start`。

回滚：`mv platform.db.pre-restore.<时间戳> platform.db`。

手工恢复时的要点：
- **必须先停服务**，否则 WAL 写入会和拷回的文件打架
- 拷回前删掉旧的 `platform.db-wal` / `platform.db-shm`，否则 SQLite 会用旧 WAL
  覆盖刚恢复的数据
- 恢复后对照 `manifest.json` 的 `rowcounts` 抽查行数

## 存量 `.bak` 的处理

`./run.sh db-backup-audit` 会列出 `platform/*.db.bak.*`、`backup_*.db`、
`database/*.bak.*` 的大小 / mtime / `task_instances` 行数，按大小倒序。

当前清单：24 份，合计 91GB。**脚本不删除它们**，看过清单后由人决定。
一般来说：同名前缀只保留最近 1~2 份（如 `taskdone.*` 三份只差几分钟），
8 月以前的可以全部清掉。

## 已知缺口

**`env_info.db`（仓库根，2.3G / 1313 万行）没有纳入备份。**
它是沙箱 Pod 注册表，由 `execution_client/core/db.py` 以
`OMNI_ENV_INFO_DB_PATH`（默认 `env_info.db`，相对 CWD）落到仓库根。
`run_clear.py` 对它只是软删除，重扫不能完全恢复。

要纳入只需在 `db_backup.py` 的备份清单里加一项，结构不用改。建议和
「Pod 泄漏排查」一起做。

## 测试

```bash
cd /root/openclaw-hive/platform
python3 -m unittest discover -s test -p "test_db_backup.py" -v
```

覆盖：WAL 未 checkpoint 的数据必须进备份、源库不被修改、sha256 一致、
按天保留、不碰外来目录、`list` 只读、半成品清理。
