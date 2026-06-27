import sqlite3
import os
from contextlib import contextmanager

from .config import settings


def get_db_path():
    return settings.DB_PATH


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
        ]:
            try:
                conn.execute(f"ALTER TABLE task_registrations ADD COLUMN {col}")
            except Exception:
                pass
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (settings.ADMIN_USERNAME,))


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
