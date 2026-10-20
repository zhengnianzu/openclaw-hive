import sqlite3
import asyncio
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from .config import settings

_db_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db")


def get_db_path():
    return settings.DB_PATH


def _configure_connection(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    _configure_connection(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


async def async_execute(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_db_executor, func, *args)


def _query(sql, params=None):
    with get_connection() as conn:
        rows = conn.execute(sql, params or ()).fetchall()
        return [dict(r) for r in rows]


def _query_one(sql, params=None):
    with get_connection() as conn:
        row = conn.execute(sql, params or ()).fetchone()
        return dict(row) if row else None


def _execute(sql, params=None):
    with get_connection() as conn:
        conn.execute(sql, params or ())


def _execute_returning(sql, params=None):
    with get_connection() as conn:
        cursor = conn.execute(sql, params or ())
        return cursor.lastrowid


async def async_query(sql, params=None):
    return await async_execute(_query, sql, params)


async def async_query_one(sql, params=None):
    return await async_execute(_query_one, sql, params)


async def async_db_execute(sql, params=None):
    return await async_execute(_execute, sql, params)


async def async_execute_returning(sql, params=None):
    return await async_execute(_execute_returning, sql, params)


def init_db():
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS task_instances (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                config_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                pid INTEGER,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                stopped_at TIMESTAMP,
                total_tasks INTEGER DEFAULT 0,
                completed_tasks INTEGER DEFAULT 0,
                failed_tasks INTEGER DEFAULT 0,
                concurrent_num INTEGER DEFAULT 100,
                config_snapshot TEXT,
                error_summary TEXT,
                create_params TEXT,
                output_status TEXT          -- 点击输出/浅层完成状态: NULL未输出, done浅层完成可直接用
                , task_done_sync INTEGER NOT NULL DEFAULT 0   -- 终态(completed/finished)后最后扫描是否完成: 0待扫, 1已终扫
                , suffix_api_keys INTEGER NOT NULL DEFAULT 0  -- 创建时是否开启 key 沙箱后缀(列表标识, 只写一次)
            );

            CREATE TABLE IF NOT EXISTS task_registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT NOT NULL,
                task_name TEXT NOT NULL,
                requester TEXT DEFAULT '',
                task_path_obs TEXT DEFAULT '',
                data_total INTEGER DEFAULT 0,
                skill_dir_obs TEXT DEFAULT '',
                agent_dir_obs TEXT DEFAULT '',
                user_folder_obs TEXT DEFAULT '',
                export_path_obs TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                linked_instance_id TEXT,
                traj_path TEXT DEFAULT ''
            );
        """)
        # migrate: add create_params for existing databases
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN create_params TEXT")
        except Exception:
            pass
        # migrate: add output_status for existing databases（点击输出/浅层完成状态）
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN output_status TEXT")
        except Exception:
            pass
        # migrate: add task_done_sync for existing databases（终态后最后扫描标记）
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN task_done_sync INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        # migrate: add suffix_api_keys for existing databases（列表标识，只对新任务写入）
        # 历史实例保持默认 0，不回填、不读快照。
        try:
            conn.execute(
                "ALTER TABLE task_instances ADD COLUMN suffix_api_keys INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # migrate: add role column for existing databases
        try:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
        except Exception:
            pass
        # migrate: add harness/model fields to task_registrations
        for col in [
            "model_name TEXT DEFAULT ''",
            "eval_model_name TEXT DEFAULT ''",
            "user_proxy_model_name TEXT DEFAULT ''",
            "harness_type TEXT DEFAULT 'openclaw'",
            "base_url TEXT DEFAULT ''",
            "api_key TEXT DEFAULT ''",
            "eval_config_model TEXT DEFAULT ''",
            "eval_config_base_url TEXT DEFAULT ''",
            "eval_config_api_key TEXT DEFAULT ''",
            "eval_config_api TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE task_registrations ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (settings.ADMIN_USERNAME,))

        # 创建队列表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_queues (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                max_concurrent INTEGER NOT NULL DEFAULT 1,
                description TEXT DEFAULT '',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # migrate: 队列表补列（IF NOT EXISTS 不会给已存在的表加列）
        # max_concurrent: 队列并发量，仅作展示/记录（当前调度仍是严格串行）
        # tail_skip_enabled / tail_skip_percent: 长尾跳过。前者打开后，当 running
        #   任务的剩余任务数 < concurrent_num * tail_skip_percent / 100 时，允许
        #   队列在基础并发之上额外放行一个任务。
        for col in [
            "max_concurrent INTEGER NOT NULL DEFAULT 1",
            "description TEXT DEFAULT ''",
            "tail_skip_enabled INTEGER NOT NULL DEFAULT 0",
            "tail_skip_percent INTEGER NOT NULL DEFAULT 10",
        ]:
            try:
                conn.execute(f"ALTER TABLE task_queues ADD COLUMN {col}")
            except Exception:
                pass

        # 扩展 task_instances 表以支持队列
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN queue_id TEXT DEFAULT NULL")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN queue_position INTEGER DEFAULT 0")
        except Exception:
            pass

        # preparing 阶段的开始时间（UTC），供 recover_stuck_preparing 判定超时。
        # 不能用 created_at：那是任务创建时间，排队久的任务一进 preparing 就会被误判卡住。
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN prepare_started_at TIMESTAMP DEFAULT NULL")
        except Exception:
            pass

        # 创建队列索引
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_queue ON task_instances(queue_id, queue_position)")
        except Exception:
            pass

        # 列表/后台刷新按状态筛选、按创建时间排序。没有索引时
        # WHERE status IN (...) 与 ORDER BY created_at DESC 都是全表扫（实测 6.8s/次）。
        for ddl in (
            "CREATE INDEX IF NOT EXISTS idx_instance_status ON task_instances(status)",
            "CREATE INDEX IF NOT EXISTS idx_instance_created ON task_instances(created_at DESC)",
        ):
            try:
                conn.execute(ddl)
            except Exception:
                pass

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                harness_type TEXT NOT NULL DEFAULT 'openclaw',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS code_repos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                obs_path TEXT NOT NULL,
                version TEXT NOT NULL DEFAULT 'v1',
                description TEXT DEFAULT '',
                main_python_file TEXT DEFAULT 'openclaw_automation.py',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_by TEXT DEFAULT ''
            );
        """)

        # migrate: add main_python_file to code_repos
        try:
            conn.execute("ALTER TABLE code_repos ADD COLUMN main_python_file TEXT DEFAULT 'openclaw_automation.py'")
        except Exception:
            pass

        # migrate: add default_skills to task_registrations
        try:
            conn.execute("ALTER TABLE task_registrations ADD COLUMN default_skills TEXT DEFAULT ''")
        except Exception:
            pass

        # migrate: add config_template_id to task_registrations
        try:
            conn.execute("ALTER TABLE task_registrations ADD COLUMN config_template_id INTEGER")
        except Exception:
            pass

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS config_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '默认配置',
                owner TEXT NOT NULL,
                is_default INTEGER DEFAULT 0,
                harness_type TEXT DEFAULT 'openclaw',
                model_base_url TEXT DEFAULT '',
                invite_code TEXT DEFAULT 'pangu',
                model_api_type TEXT DEFAULT '',
                model_id TEXT DEFAULT '',
                agents_json TEXT DEFAULT '[]',
                image_name TEXT DEFAULT '',
                code_repo_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS harness_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                harness_type TEXT NOT NULL DEFAULT 'openclaw',
                version TEXT NOT NULL DEFAULT 'v1',
                description TEXT DEFAULT '',
                config_files_json TEXT DEFAULT '[]',
                is_default INTEGER DEFAULT 0,
                obs_source_path TEXT DEFAULT '',
                obs_harness_path TEXT DEFAULT '',
                obs_task_path TEXT DEFAULT '',
                obs_proxy_path TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        for col in [
            "obs_harness_path TEXT DEFAULT ''",
            "obs_task_path TEXT DEFAULT ''",
            "obs_proxy_path TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE harness_configs ADD COLUMN {col}")
            except Exception:
                pass

        # 沙箱配置索引表：**文件权威，DB 只存索引**。
        # 真正的沙箱内容在 platform/settings/sandbox/*.yaml：
        #   linux.yaml / windows.yaml            —— 模板（从 configs/*.yaml 抽出的沙箱段）
        #   <type>_<id>.yaml                     —— 每条配置的派生文件（file 列记录）
        # 创建任务时把派生文件的 global/sandbox/sandbox_namespace/s3 整段替换进实例 config.yaml。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sandbox_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,               -- 配置名称，如 default-windows
                sandbox_type TEXT NOT NULL,              -- linux | windows
                sandbox_key TEXT NOT NULL,               -- config.yaml 顶层 sandbox.<key>，如 x86_cpu / windows
                file TEXT DEFAULT '',                    -- 派生文件名（settings/sandbox/ 下），如 windows_1.yaml
                is_default INTEGER DEFAULT 0,            -- 同类型下唯一
                enabled INTEGER DEFAULT 1,
                description TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # migrate: 为已存在的 sandbox_configs 补索引列
        for col in ["file TEXT DEFAULT ''", "enabled INTEGER DEFAULT 1", "description TEXT DEFAULT ''"]:
            try:
                conn.execute(f"ALTER TABLE sandbox_configs ADD COLUMN {col}")
            except Exception:
                pass

        # 每任务明细表：把散落在文件系统里的 per-task 状态/评分聚合进 DB，便于筛选与将来着色
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,           -- 对应 task_instances.id
                task_idx INTEGER,                    -- task-<idx>.log 的 idx，可空
                config_name TEXT NOT NULL,           -- 完整配置文件名，如 018399_fin_..._q1.json
                status TEXT,                         -- 任务成功 / 任务失败 / 任务异常 / NULL(未执行)
                error_code TEXT,                     -- C001 / S001 / T001 ... 可空
                error_category TEXT,                 -- C / S / T / X（派生自 error_code 前缀），可空
                eval_score REAL,                     -- 评估计算分，可空
                eval_completion REAL,                -- completion，可空
                gate INTEGER,                        -- gate 乘积，可空
                traj_level TEXT,                     -- 预留：未来 L0/L1/L1.5/L2/L3，本次不填充
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(instance_id, config_name)
            );

            CREATE INDEX IF NOT EXISTS idx_task_records_inst ON task_records(instance_id);
            CREATE INDEX IF NOT EXISTS idx_task_records_cat  ON task_records(instance_id, error_category);
        """)

        # migrate: 为已存在的 task_records 补列（IF NOT EXISTS 不会加列），风格同上
        for col in [
            "task_idx INTEGER",
            "status TEXT",
            "error_code TEXT",
            "error_category TEXT",
            "eval_score REAL",
            "eval_completion REAL",
            "gate INTEGER",
            "traj_level TEXT",
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        ]:
            try:
                conn.execute(f"ALTER TABLE task_records ADD COLUMN {col}")
            except Exception:
                pass

        # 轨迹级明细表（输出模块）：task_records 的 1:N 展开 —— 每行 = 一个 task 的 Lx 分级 +
        # 轨迹/日志路径。行由离线 worker（offline/output_worker.py）回填；状态机见 README_outputview.md：
        #   浅层: task_records.traj_level IS NULL → running → 回填 traj_level | failed
        #   深层: status pending → downloading → done | failed（用户点开会话详情时才触发）
        # 注意：worker 回填本表与 task_records.traj_level 时用「不触碰 updated_at」的 UPDATE，
        # 否则会与在线 _sync_task_records 的 CURRENT_TIMESTAMP 触碰互相干扰（详见 worker 注释）。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_traj_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,           -- 对应 task_instances.id
                task_idx INTEGER,                    -- task-<idx>.log 的 idx（若 task_records 该行有），可空
                config_name TEXT NOT NULL,           -- task_records.config_name（config 文件名 / task 目录名）
                traj_name TEXT NOT NULL,             -- OBS task 目录名（leaf），如 acad_000012
                level TEXT NOT NULL,                 -- L0/L1/L1.5/L2/L3（compute_level 输出，dropped 归 L0）
                harness TEXT DEFAULT 'openclaw',
                passed_gate INTEGER, has_eval INTEGER, task_done INTEGER,
                completion REAL,
                tool_calls INTEGER, plain_rounds INTEGER,
                input_tokens INTEGER, output_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER,
                char_len INTEGER,
                trajectory_rel TEXT,                 -- 相对 origin 的 assistant 轨迹路径（缓存内）
                status TEXT DEFAULT 'done',          -- pending/downloading/done/failed（仅深层触发时置 pending）
                error TEXT,                          -- 深层下载/解析失败信息
                shallow_status TEXT,                 -- 浅层处理状态: NULL未处理/processed/empty(OBS无轨迹)/error
                shallow_error TEXT,                  -- 浅层错误信息
                assistant_traj_path TEXT,            -- 缓存内相对路径（深层回填）
                evaluator_traj_path TEXT,
                task_log_path TEXT,                  -- 主日志相对路径
                gateway_log_path TEXT,
                eval_log_path TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(instance_id, config_name, traj_name)
            );

            CREATE INDEX IF NOT EXISTS idx_traj_records_inst ON task_traj_records(instance_id);
            CREATE INDEX IF NOT EXISTS idx_traj_records_task ON task_traj_records(instance_id, config_name);
        """)

        # migrate: 为已存在的 task_traj_records 补列（IF NOT EXISTS 不会加列），风格同 task_records
        for col in [
            "shallow_status TEXT",
            "shallow_error TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE task_traj_records ADD COLUMN {col}")
            except Exception:
                pass
        # 迁移回填：已有行都是浅层处理过的——有分级 → processed；failed 占位 → 无轨迹 empty。
        # 无 level 的 NULL 行保持待处理，交给浅层首次分析。
        try:
            conn.execute(
                "UPDATE task_traj_records SET shallow_status='processed' "
                "WHERE level IN ('L0','L1','L1.5','L2','L3') AND shallow_status IS NULL")
            conn.execute(
                "UPDATE task_traj_records SET shallow_status='empty' "
                "WHERE level='failed' AND shallow_status IS NULL")
        except Exception:
            pass

        # 浅层按需触发登记表：点击驱动 —— 打开实例详情页 POST /{id}/shallow 登记一次，
        # worker 只消费 running/preparing + 本表登记的 finished 实例，不再全量扫历史缺口。
        # 处理完成（实例不再有缺口）后删除登记，实例出队。running 实例不入本表（持续分级）。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shallow_requests (
                instance_id TEXT PRIMARY KEY,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'queued'
            );
        """)

        # 模型配置面板「对话导出」登记表：一行 = 某实例某角色（主Agent/用户模拟/Evaluator）的导出任务。
        # 提交导出时 UPSERT（UNIQUE(instance_id, role)），外部服务返回 export_id/session_path 落库；
        # 状态轮询时回填 status/total_sessions/error_message。存完整 model_key 供 status 轮询带 key。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS model_export_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                role TEXT NOT NULL,
                model_key TEXT NOT NULL,
                model TEXT NOT NULL,
                export_id INTEGER,
                export_name TEXT,
                mode TEXT DEFAULT 'export',
                status TEXT DEFAULT 'unexported',
                session_path TEXT,
                total_sessions INTEGER,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(instance_id, role)
            );
        """)

        # [已废弃] 文档中心索引表 —— 现由 platform/wiki/index.db 独立管理，
        # 见 api/services/wiki_store.py。这两张表仅为旧库兼容保留，不再读写。
        # 保留空表不影响任何功能；确认无用后可手工 DROP。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wiki_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS wiki_documents (
                slug TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                category_id INTEGER,                          -- 派生自 frontmatter 的 category 名称
                tags TEXT NOT NULL DEFAULT '',                -- 逗号分隔
                summary TEXT NOT NULL DEFAULT '',
                linked_entity_type TEXT NOT NULL DEFAULT '',  -- image|code_repo|harness_config|...
                linked_entity_key  TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT '',
                is_pinned INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT NOT NULL DEFAULT '',        -- 文件全文 hash，用于跳过重复解析
                file_mtime REAL NOT NULL DEFAULT 0,
                last_notification_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_doc_category ON wiki_documents(category_id);
            CREATE INDEX IF NOT EXISTS idx_wiki_doc_entity   ON wiki_documents(linked_entity_type, linked_entity_key);
            CREATE INDEX IF NOT EXISTS idx_wiki_doc_updated  ON wiki_documents(updated_at DESC);
        """)

        # migrate: 早期版本把正文存在 DB（自增 id 主键）。改为文件权威后索引表结构不同，
        # 旧表里没有 slug 列 —— 直接重建。正文本来就不在 DB，重建不丢任何内容。
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(wiki_documents)").fetchall()}
            if "slug" not in cols:
                conn.execute("DROP TABLE wiki_documents")
                conn.executescript("""
                    CREATE TABLE wiki_documents (
                        slug TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        category_id INTEGER,
                        tags TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        linked_entity_type TEXT NOT NULL DEFAULT '',
                        linked_entity_key  TEXT NOT NULL DEFAULT '',
                        author TEXT NOT NULL DEFAULT '',
                        updated_by TEXT NOT NULL DEFAULT '',
                        is_pinned INTEGER NOT NULL DEFAULT 0,
                        content_hash TEXT NOT NULL DEFAULT '',
                        file_mtime REAL NOT NULL DEFAULT 0,
                        last_notification_id INTEGER,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_wiki_doc_category ON wiki_documents(category_id);
                    CREATE INDEX IF NOT EXISTS idx_wiki_doc_entity   ON wiki_documents(linked_entity_type, linked_entity_key);
                    CREATE INDEX IF NOT EXISTS idx_wiki_doc_updated  ON wiki_documents(updated_at DESC);
                """)
        except Exception:
            pass

        # 全局站内通知：**广播表 + 已读表**，不按用户复制通知行。
        # 发布一次只写 1 行 notifications，读状态按需往 notification_reads 插一行（天然稀疏），
        # 撤回/改标题都是单行 UPDATE。未读数用 LEFT JOIN 即时算，几十人量级无需物化计数列。
        # 将来要按角色/用户定向推送时，给 notifications 加 audience 字段即可，已读表不动。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content_md TEXT NOT NULL DEFAULT '',      -- 发布时快照，文档后续编辑不影响已发通知
                level TEXT NOT NULL DEFAULT 'info',       -- info|success|warning|danger
                doc_id INTEGER,                           -- 历史字段（早期自增 id 方案），新数据不再写
                doc_slug TEXT,                            -- 关联文档的 slug；文档已删则前端降级为纯文本
                created_by TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,       -- 撤回软删，保留已读记录
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_created ON notifications(created_at DESC);

            CREATE TABLE IF NOT EXISTS notification_reads (
                notification_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (notification_id, username)
            );
            CREATE INDEX IF NOT EXISTS idx_notif_reads_user ON notification_reads(username);
        """)

        # migrate: 通知改为按 slug 关联文档（文件权威后不再有自增 id）
        try:
            conn.execute("ALTER TABLE notifications ADD COLUMN doc_slug TEXT")
        except Exception:
            pass
