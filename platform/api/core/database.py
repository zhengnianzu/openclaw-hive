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
                create_params TEXT
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
