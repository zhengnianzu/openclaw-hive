import sqlite3
import asyncio
import os
import time
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


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _index_names(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,))}


# task_traj_records 上必须存在的业务索引。
# 注意：这三个索引原来只写在「放开 level NOT NULL」的重建脚本里，而那段脚本是
# RENAME → CREATE → INSERT → DROP → CREATE INDEX 的一串 executescript，
# 中间任何一步被打断（4 个 uvicorn worker 并发跑 init_db + database is locked），
# 就会出现「表建好了、索引没建」的半迁移状态。第 96 行的早退条件只看 level 约束，
# 于是永久不再修复。现在把「补索引」独立出来，每次启动都校验。
TRAJ_INDEXES = (
    ("idx_traj_records_inst",
     "CREATE INDEX IF NOT EXISTS idx_traj_records_inst ON task_traj_records(instance_id)"),
    ("idx_traj_records_task",
     "CREATE INDEX IF NOT EXISTS idx_traj_records_task "
     "ON task_traj_records(instance_id, config_name)"),
    ("idx_traj_artifacts_status",
     "CREATE INDEX IF NOT EXISTS idx_traj_artifacts_status "
     "ON task_traj_records(artifacts_status)"),
    # 队列领取：ORDER BY 要能走索引，否则每轮都是全表扫 + 临时 B 树排序。
    # 有 artifact_priority（用户点开会置 100）后排序键变成两列，索引也要跟上。
    ("idx_traj_artifact_queue",
     "CREATE INDEX IF NOT EXISTS idx_traj_artifact_queue "
     "ON task_traj_records(artifact_priority DESC, updated_at) "
     "WHERE artifacts_status='queued' AND artifact_complete=1"),
    # 看板 _queue_counts 里的 artifact_complete=0 计数（350 万行全表扫 ≈2.5s）
    ("idx_traj_artifact_complete",
     "CREATE INDEX IF NOT EXISTS idx_traj_artifact_complete "
     "ON task_traj_records(artifact_complete)"),
)

# 其它表上被看板高频查询打到、但一直没建索引的列。
OTHER_INDEXES = (
    # shallow_pending = WHERE traj_level IS NULL，330 万行全表扫 ≈2.3s
    ("idx_task_records_level",
     "CREATE INDEX IF NOT EXISTS idx_task_records_level ON task_records(traj_level)"),
)


def _ensure_traj_indexes(conn) -> list[str]:
    """补齐 task_traj_records 的业务索引，返回本次新建的索引名。

    产物下载队列每轮都要跑
      WHERE artifacts_status='queued' AND artifact_complete=1 ORDER BY updated_at
    索引缺失时会退化成全表扫 + 临时 B 树排序（3.5M 行实测 0.33~0.38s/次），
    在 4 uvicorn + 2 worker 共享写锁的情况下会持续放大 database is locked。

    **索引名在 SQLite 里是全局唯一的**：重建表时 RENAME 会把原索引一并搬到
    _ttr_old 名下，此时 `CREATE INDEX IF NOT EXISTS idx_traj_records_inst ON
    task_traj_records(...)` 会静默 no-op（名字被占，IF NOT EXISTS 不报错也不建）。
    所以必须先清掉挂在别的表上的同名索引，否则「补索引」永远补不上。
    """
    if not _table_exists(conn, "task_traj_records"):
        return []
    have = _index_names(conn, "task_traj_records")
    created = []
    for name, ddl in TRAJ_INDEXES:
        if name in have:
            continue
        shadow = conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            (name,)).fetchone()
        if shadow and shadow[0] != "task_traj_records":
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute(ddl)
        created.append(name)

    # 其它表的看板索引：同样要防「索引名被 _ttr_old 之类占用」的静默 no-op
    for name, ddl in OTHER_INDEXES:
        shadow = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (name,)).fetchone()
        if shadow:
            continue
        conn.execute(ddl)
        created.append(name)
    return created


def _finish_interrupted_traj_rebuild(conn) -> bool:
    """收尾被打断的重建表迁移。返回是否做了收尾动作。

    现场可能处在任意一步：
      a) RENAME 成功、CREATE/INSERT 未完成  → task_traj_records 不存在或刚建好没数据
      b) INSERT 完成、DROP 未执行          → 新表数据齐全，_ttr_old 残留（占半个库）
    判断依据用行数：新表行数 >= 旧表行数即认为拷贝完成（迁移后新写入的行只会更多）。
    无法判断或数据不全时**保留 _ttr_old 不动**，宁可占空间也不能丢数据。
    """
    if not _table_exists(conn, "_ttr_old"):
        return False

    if not _table_exists(conn, "task_traj_records"):
        # a) 旧表已被 RENAME 走、新表没建成 —— 直接改回名字，数据原封不动
        conn.execute("ALTER TABLE _ttr_old RENAME TO task_traj_records")
        print("[db] 检测到中断的重建迁移：task_traj_records 缺失，已从 _ttr_old 还原")
        return True

    old_n = conn.execute("SELECT COUNT(*) FROM _ttr_old").fetchone()[0]
    new_n = conn.execute("SELECT COUNT(*) FROM task_traj_records").fetchone()[0]
    if new_n < old_n:
        print(f"[db] 警告: _ttr_old 残留但新表行数不足（old={old_n} new={new_n}），"
              f"保留 _ttr_old 未删除，请人工核对")
        return False

    conn.execute("DROP TABLE _ttr_old")
    print(f"[db] 已收尾中断的重建迁移：删除残留表 _ttr_old（{old_n} 行）")
    return True


def self_heal_traj_level_nullable(conn):
    """把 task_traj_records.level 的 NOT NULL 放开（老库升级）+ 收尾半迁移现场。

    为什么需要：产物下载的发现阶段按「实例的 config 列表」建行，此时还没有分级
    （浅层可能从未跑过——比如从别的机器拷来的实例）。level 若仍 NOT NULL，
    这些行插不进去。

    SQLite 不支持 ALTER COLUMN，只能重建表搬数据。

    **这个函数必须是幂等且可重入的**：init_db() 在 --workers 4 下被 4 个进程并发
    调用，重建脚本一旦被打断就会留下 _ttr_old + 缺索引的半成品，而旧版的早退条件
    （只看 level 是否可空）让这种现场永久无法自愈。
    """
    try:
        info = conn.execute("PRAGMA table_info(task_traj_records)").fetchall()
    except Exception:
        return

    # 无论走哪条分支，先把半迁移现场收尾、索引补齐 —— 这两件事与 level 约束无关
    _finish_interrupted_traj_rebuild(conn)
    _ensure_traj_indexes(conn)

    level_col = next((r for r in info if r[1] == "level"), None)
    if level_col is None:
        return
    # notnull 是第 4 列（cid, name, type, notnull, dflt_value, pk）
    if not level_col[3]:
        return                      # 已经是可 NULL，无需重建

    cols = [r[1] for r in info]
    col_list = ", ".join(cols)
    # 整个重建必须是一个原子单元。
    # **BEGIN IMMEDIATE 必须写在 executescript 脚本内部**：executescript 在执行前
    # 会隐式 COMMIT 掉当前事务（已实测：BEGIN 之后 conn.in_transaction 为 True，
    # executescript 之后变成 False），所以外面那句 BEGIN IMMEDIATE 形同虚设，
    # 后面的 rollback() 也回滚不了任何已执行的 DDL/DML —— 这正是线上留下
    # `_ttr_old` 350 万行残留的机械原因，不是「偶发中断」。
    # executescript 内部会自动 COMMIT，把 BEGIN 放进脚本即可获得真正的原子性。
    try:
        conn.executescript(f"""
            BEGIN IMMEDIATE;
            ALTER TABLE task_traj_records RENAME TO _ttr_old;
            CREATE TABLE task_traj_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                task_idx INTEGER,
                config_name TEXT NOT NULL,
                traj_name TEXT NOT NULL,
                level TEXT,
                harness TEXT DEFAULT 'openclaw',
                passed_gate INTEGER, has_eval INTEGER, task_done INTEGER,
                completion REAL,
                tool_calls INTEGER, plain_rounds INTEGER,
                input_tokens INTEGER, output_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER,
                char_len INTEGER,
                trajectory_rel TEXT,
                traj_status TEXT DEFAULT 'done',
                artifacts_status TEXT DEFAULT 'queued',
                artifact_complete INTEGER DEFAULT 0,
                artifact_priority INTEGER DEFAULT 0,
                artifact_bytes INTEGER,
                artifact_files INTEGER,
                artifact_downloaded_at REAL,
                artifact_worker TEXT,
                artifact_started_at REAL,
                artifact_attempts INTEGER DEFAULT 0,
                status TEXT DEFAULT 'done',
                error TEXT,
                shallow_status TEXT,
                shallow_error TEXT,
                assistant_traj_path TEXT,
                evaluator_traj_path TEXT,
                task_log_path TEXT,
                gateway_log_path TEXT,
                eval_log_path TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(instance_id, config_name, traj_name)
            );
            INSERT INTO task_traj_records ({col_list})
                SELECT {col_list} FROM _ttr_old;
            DROP TABLE _ttr_old;
            COMMIT;
        """)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    conn.commit()

    _ensure_traj_indexes(conn)
    conn.commit()
    print("[db] task_traj_records.level 已放开 NOT NULL（重建表迁移完成）")


@contextmanager
def init_db_lock(timeout: float = 120.0):
    """init_db 的跨进程互斥锁（带超时）。

    uvicorn 以 --workers 4 启动时，4 个进程会各自 import main 并执行迁移。
    库里有重建表迁移（RENAME/CREATE/INSERT/DROP），并发执行会互相抢写锁，
    表现为 database is locked + 进程起不来，且可能留下「表搬了一半」的现场
    （实测线上就留下过 _ttr_old 350 万行 + 业务索引全丢）。
    这里用 flock 排他锁串行化，后到的进程等前一个跑完即可。

    **必须带超时**：flock 的阻塞等待没有上限，一旦有进程拿到锁后卡死不释放
    （或同进程重复加锁导致自死锁），后续所有 worker 会永久挂在
    locks_lock_inode_wait —— 服务再也起不来，而且 ps 上看不出任何异常。
    超时后放弃拿锁、直接往下走：迁移语句本身是幂等的，并发最坏结果是
    database is locked 重试，比整个服务起不来好得多。
    """
    path = os.path.join(os.path.dirname(get_db_path()) or ".", ".init_db.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            import fcntl
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        print(f"[db] 警告: 等待 init_db 锁超过 {timeout:.0f}s 未拿到，"
                              f"跳过等待继续启动（迁移语句幂等，并发最多报 locked）",
                              flush=True)
                        break
                    time.sleep(0.2)
        except ImportError:      # 非 POSIX 平台：退化为无锁（单 worker 场景够用）
            pass
        yield
    finally:
        if acquired:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        os.close(fd)


def init_db():
    os.makedirs(os.path.dirname(get_db_path()), exist_ok=True)
    with init_db_lock():
        init_db_unlocked()


# task_traj_records 的「新增可选列」。建表语句、重建表语句、ADD COLUMN 迁移三处
# 必须同步，否则重建表迁移一旦发生就会把列丢掉。这里集中一份，供轻量自愈复用。
TRAJ_EXTRA_COLUMNS = (
    "shallow_status TEXT",
    "shallow_error TEXT",
    "artifacts_status TEXT DEFAULT 'queued'",
    "artifact_complete INTEGER DEFAULT 0",
    "artifact_priority INTEGER DEFAULT 0",
    "artifact_bytes INTEGER",
    "artifact_files INTEGER",
    "artifact_downloaded_at REAL",
    "artifact_worker TEXT",
    "artifact_started_at REAL",
    "artifact_attempts INTEGER DEFAULT 0",
)

# task_instances 的「新增可选列」。
# 注意：产物发现的轮转时间戳属于**实例**维度（一个实例记一次即可），
# 不属于 task 行。曾经误放进 TRAJ_EXTRA_COLUMNS，结果是列加到了 task_traj_records
# 上、而 SQL 查的是 task_instances，worker 一启动就 no such column 直接崩。
INSTANCE_EXTRA_COLUMNS = (
    "last_artifact_discover_at REAL",
)


def ensure_traj_extra_columns() -> None:
    """幂等补列，不建表、不重建、不做重迁移。

    offline/output_worker.py 是**独立进程**，从不调用 init_db()，它只靠 uvicorn
    启动时把库迁移好。于是「新增一列 + 只重启 worker」的组合会让 worker 的 SQL
    直接报 no such column（列还没被加过），而症状是全轮发现失败、日志只有一句
    笼统的错误。worker 启动时调一次本函数即可自愈。

    与 self_heal_traj_level_nullable 的区别：那个会重建整张表（3.5M 行，分钟级），
    这里只做 ALTER TABLE ADD COLUMN（纯元数据操作，秒级），可以每次启动都跑。
    """
    for table, columns in (("task_traj_records", TRAJ_EXTRA_COLUMNS),
                           ("task_instances", INSTANCE_EXTRA_COLUMNS)):
        try:
            with get_connection() as conn:
                if not _table_exists(conn, table):
                    continue
                have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                added = []
                for col in columns:
                    name = col.split()[0]
                    if name in have:
                        continue
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                        added.append(name)
                    except Exception:
                        pass
                if added:
                    conn.commit()
                    print(f"[db] {table} 补列: {', '.join(added)}")
                if table == "task_traj_records":
                    _ensure_traj_indexes(conn)
                    conn.commit()
        except Exception as e:
            print(f"[db] {table} 补列失败（worker 可能仍能用已有列）: {str(e)[:150]}")


def init_db_unlocked():
    """建表+迁移主体。**调用方必须已经持有 init_db_lock()**。

    不要在这里再加锁：flock 不按进程重入，同一进程两个 fd 打开同一把锁会自死锁
    （实测卡在 locks_lock_inode_wait 永不返回，该 uvicorn worker 直接起不来）。
    """
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
                -- 产物发现轮转：上次被发现阶段扫到的时间（epoch）。
                -- 按「最久没被发现」排序，避免固定取最新的 N 个实例导致其余永远饿死。
                last_artifact_discover_at REAL,
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
                default_skill_dir_obs TEXT DEFAULT '',
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

        # migrate: add an independent source directory for default skills
        try:
            conn.execute(
                "ALTER TABLE task_registrations ADD COLUMN default_skill_dir_obs TEXT DEFAULT ''"
            )
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
                -- 方案标识：同一 harness_type 内唯一，用于拼磁盘目录 settings/<type>_<slug>/
                slug TEXT DEFAULT '',
                -- 方案目录相对 HARNESS_DIR 的路径，如 "harness/openclaw/harness-5"。
                -- 为空时按 harness-<id> 推导（仓库历史默认布局）。
                harness_dir TEXT DEFAULT '',
                -- 兼容旧模型：老行用 version 当目录名；新行该列保留为"迁移前的版本"
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

            -- 方案下的不可变版本快照。草稿在方案目录里可编辑，
            -- 只有「创建版本」时才把草稿冻结成这里的一行 + .versions/<version>/ 目录。
            CREATE TABLE IF NOT EXISTS harness_config_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id INTEGER NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',   -- draft | testing | published
                description TEXT DEFAULT '',
                files_json TEXT DEFAULT '[]',
                obs_harness_path TEXT DEFAULT '',
                obs_task_path TEXT DEFAULT '',
                obs_proxy_path TEXT DEFAULT '',
                created_by TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published_at TIMESTAMP,
                published_by TEXT DEFAULT '',
                UNIQUE(config_id, version)
            );
        """)

        for col in [
            "obs_harness_path TEXT DEFAULT ''",
            "obs_task_path TEXT DEFAULT ''",
            "obs_proxy_path TEXT DEFAULT ''",
            "slug TEXT DEFAULT ''",
            "harness_dir TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(f"ALTER TABLE harness_configs ADD COLUMN {col}")
            except Exception:
                pass

        # UNIQUE 索引只能在表建好之后补；已有重复数据时建索引会失败，忽略即可
        # （ensure_defaults 里的迁移会先修复重复的 is_default）
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_hcv_config_version "
                "ON harness_config_versions(config_id, version)"
            )
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
                -- level 允许 NULL：产物下载的发现阶段按「实例的 config 列表」建行，
                -- 那时还没有分级结果（浅层可能从未跑过，比如从别的机器拷来的实例）。
                -- 漏斗/分级统计一律按 level IN (L0..L3) 过滤，NULL 自然被排除。
                level TEXT,                          -- L0/L1/L1.5/L2/L3（compute_level 输出，dropped 归 L0）
                harness TEXT DEFAULT 'openclaw',
                passed_gate INTEGER, has_eval INTEGER, task_done INTEGER,
                completion REAL,
                tool_calls INTEGER, plain_rounds INTEGER,
                input_tokens INTEGER, output_tokens INTEGER,
                reasoning_tokens INTEGER, total_tokens INTEGER,
                char_len INTEGER,
                trajectory_rel TEXT,                 -- 相对 origin 的 assistant 轨迹路径（缓存内）
                -- 轨迹下载状态（deep 详情按需下载固定几个文件）
                traj_status TEXT DEFAULT 'done',     -- NULL/pending/downloading/done/failed
                -- 产物下载状态（工作区 tab 全量下载整个 OBS 前缀）
                artifacts_status TEXT DEFAULT 'queued',  -- queued/downloading/done/failed/skipped
                artifact_complete INTEGER DEFAULT 0, -- 该 config 是否出现在本地 complete.jsonl
                artifact_priority INTEGER DEFAULT 0, -- 越大越优先；用户点「沙箱交付物」=100
                artifact_bytes INTEGER,              -- 产物落盘字节数
                artifact_files INTEGER,              -- 产物文件数
                artifact_downloaded_at REAL,
                artifact_worker TEXT,                -- 领取者 hostname:pid，便于排查
                artifact_started_at REAL,            -- 领取时间，超时回收用它
                artifact_attempts INTEGER DEFAULT 0, -- 重试次数
                status TEXT DEFAULT 'done',          -- 兼容列：等同于 traj_status
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

        # 沙箱产物下载记账 —— 已并入 task_traj_records（artifacts_status 等列）。
        # 原 task_artifacts 表废弃：它与 task_traj_records 是同一套「待下载→领取→完成」
        # 状态机，且两者下载到同一个 task 目录，拆两张表等于把同一模式写两遍。
        # 保留一行 DROP 让老库自愈；新库从未建过该表，DROP IF EXISTS 无副作用。
        conn.execute("DROP TABLE IF EXISTS task_artifacts")

        # Worker 心跳（资源看板「调度监控」）。
        # 之前 worker 跑起来在界面上完全不可见：只有 pid 文件、且日志只在有产出时才打印
        # （空闲时一行都不追加，实测空跑 21 小时后日志 mtime 仍停在启动那一刻）。
        # 进程重启、空闲、卡死三种状态在数据上完全无法区分，因此每轮落一行心跳。
        # 只保留最近 2000 行（写入时顺带裁剪），够画 1 小时趋势即可。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS worker_heartbeats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                worker      TEXT NOT NULL,        -- shallow | deep | artifacts
                pid         INTEGER,
                round_start REAL NOT NULL,        -- epoch；启动标记行写 0
                round_end   REAL,
                duration    REAL,
                processed   INTEGER DEFAULT 0,
                failed      INTEGER DEFAULT 0,
                note        TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_heartbeat_worker
                ON worker_heartbeats(worker, round_start);
        """)

        # migrate: 为已存在的 task_traj_records 补列（IF NOT EXISTS 不会加列）。
        # 列清单集中在 TRAJ_EXTRA_COLUMNS，worker 启动时的 ensure_traj_extra_columns()
        # 复用同一份 —— 三处（建表 / 重建表 / ADD COLUMN）不同步时，重建一次就会丢列。
        for col in TRAJ_EXTRA_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE task_traj_records ADD COLUMN {col}")
            except Exception:
                pass
        # migrate: 产物下载相关列（老库升级路径）。
        # level 的 NOT NULL 放开需要重建表；SQLite 不能 ALTER COLUMN，只能建新表搬数据。
        # 只在检测到旧约束时做一次，正常库不受影响。
        self_heal_traj_level_nullable(conn)

        # self_heal 可能触发重建（新表按字面量建，可能缺列）或从 _ttr_old 还原，
        # 两种情况都要再过一遍补列 —— 必须放在 self_heal 之后。
        for col in TRAJ_EXTRA_COLUMNS:
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

        # 补统计信息 + 回收 WAL。
        #
        # ANALYZE：全库从未跑过，sqlite_stat1 不存在 → 优化器无法评估部分索引的
        # 收益，产物队列的专用索引 idx_traj_artifact_queue 建了却从未被选中
        # （实测走了 idx_traj_artifact_complete + USE TEMP B-TREE FOR ORDER BY，
        # 领取查询 67ms；强制走队列索引后 0ms）。
        #
        # wal_checkpoint：全仓没有任何 checkpoint 调用，WAL 曾涨到 2.37GB。
        # WAL 越大 checkpoint 越贵，而 checkpoint 又要等所有 reader 让出读锁 ——
        # 一个慢查询就能让 WAL 在那段时间里无法推进，形成正反馈。
        # 这里只在启动时做一次 TRUNCATE（4 个 uvicorn worker 由 init_db_lock 串行化），
        # 不影响运行期。
        for stmt, label in (
            ("ANALYZE task_traj_records", "ANALYZE"),
            ("PRAGMA wal_checkpoint(TRUNCATE)", "wal_checkpoint"),
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception as e:
                # 失败不能影响启动：ANALYZE 只是优化收益，checkpoint 拿不到锁是常态。
                print(f"[db] {label} 跳过: {str(e)[:120]}")
