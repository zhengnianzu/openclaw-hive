import sqlite3
import asyncio
import os
import re
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from .config import settings

_db_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="db")


def get_db_path():
    return settings.DB_PATH


# ── 分库：两张巨表搬到独立文件，用 ATTACH 挂回同一个连接 ──
#
# 为什么分：task_records(1030 万行) + task_traj_records(915 万行) 连同索引占了
# platform.db 的 85%（9.1GB / 10.6GB）。SQLite 的 WAL checkpoint 要把 WAL 回写进
# 主库，库越大越久，而 checkpoint **独占写锁** —— 10GB 的库在 NFS 盘上轻易超过
# busy_timeout(15s)，于是 queue_scheduler / 各 worker 到处报 `database is locked`
# （实测连待办 0 行的 worker 也在撞锁）。把大表移出去，主库缩到约 1.5GB，
# checkpoint 回到秒级，而两张大表自己的 WAL 由离线 worker 低频写入。
#
# 为什么用 ATTACH 而不是独立连接：全仓有 58 处 SQL 引用这两张表，大量是跨表
# JOIN（如 `task_records tr LEFT JOIN task_traj_records ttr`、shallow_obs 的
# EXISTS 子查询）。ATTACH 后这些 SQL **一行都不用改** —— SQLite 允许跨 attached
# 库 JOIN，表名不带库前缀时会在 main 和已挂载库里依次查找。
TASK_DATA_SCHEMA = "taskdata"
# 搬到独立库的表。只搬「行数巨大且只被离线链路高频写」的，不搬 task_instances
# （仅 2167 行，且是在线路径的核心，留在主库最省事）。
TASK_DATA_TABLES = ("task_records", "task_traj_records")


def get_task_data_path():
    """两张巨表所在的库文件路径。默认 <主库同级>/database/task_data.db。

    settings.TASK_DATA_DB_PATH 可覆盖（测试用临时库时会设）。
    """
    override = getattr(settings, "TASK_DATA_DB_PATH", None)
    if override:
        return override
    main = get_db_path()
    return os.path.join(os.path.dirname(os.path.abspath(main)),
                        "database", "task_data.db")


def _attach_task_data(conn):
    """把 task_data.db 挂到当前连接的 `taskdata` schema 上。

    **文件不存在就不挂**：迁移前（或全新部署还没建库）两张表仍在主库里，
    照旧能查到；迁移后主库的同名表被删除，查询自动落到 attached 库。
    这样代码可以先上线、迁移另行安排，不存在「必须同时切换」的窗口。

    失败静默：ATTACH 不该让整个连接不可用（比如并发建目录时的瞬时竞争）。
    真正用不到这两张表的请求（登录、wiki、队列）不受影响。
    """
    path = get_task_data_path()
    if not os.path.exists(path):
        return False
    try:
        conn.execute(f"ATTACH DATABASE ? AS {TASK_DATA_SCHEMA}", (path,))
        # attached 库要单独设 PRAGMA —— journal_mode 是 per-database 的。
        # 不设的话新库会用默认的 delete 模式，读写互斥，比不分库还糟。
        conn.execute(f"PRAGMA {TASK_DATA_SCHEMA}.journal_mode=WAL")
        conn.execute(f"PRAGMA {TASK_DATA_SCHEMA}.synchronous=NORMAL")
        return True
    except sqlite3.Error:
        return False


def qualify_task_data_ddl(sql: str, schema: str) -> str:
    """把 SQL 里对两张巨表的引用加上 schema 前缀（schema='main' 时原样返回）。

    为什么要在**执行时改写**而不是把前缀写进每条 DDL：全仓有 20 多处 DDL
    （CREATE TABLE / CREATE INDEX / ALTER TABLE）和 58 处查询引用这两张表，
    逐条硬编码前缀既啰嗦又容易漏，漏一处就是一张空表遮蔽 1030 万行。
    这里只在一个地方做替换，DDL 字符串保持可读。

    注意只改写 DDL，**查询不需要改写** —— 查询不带前缀时 SQLite 会按
    main → attached 顺序查找，迁移后 main 里没有这两张表，自然落到 taskdata。
    DDL 不行的原因见 task_data_schema 的说明（IF NOT EXISTS 只看 main）。
    """
    if schema == "main":
        return sql
    out = sql
    # CREATE INDEX 的 schema 必须加在**索引名**上，不是表名上：
    #   对   CREATE INDEX taskdata.idx_x ON task_records(col)
    #   错   CREATE INDEX idx_x ON taskdata.task_records(col)   → near ".": syntax error
    # 索引隐式属于它所在的库，所以表名反而不能带前缀。
    out = re.sub(
        r"\b(CREATE\s+(?:UNIQUE\s+)?INDEX\s+)(IF\s+NOT\s+EXISTS\s+)?(\w+)\s+ON\s+(\w+)",
        lambda m: (f"{m.group(1)}{m.group(2) or ''}{schema}.{m.group(3)} ON {m.group(4)}"
                   if m.group(4) in TASK_DATA_TABLES
                   else m.group(0)),
        out, flags=re.IGNORECASE)
    for t in TASK_DATA_TABLES:
        # 其余语句（CREATE TABLE / ALTER TABLE / INSERT INTO / FROM / UPDATE）
        # 把前缀加在表名上。关键字与表名之间可能夹着 `IF NOT EXISTS` ——
        # 必须一起匹配，否则这条最危险的 DDL 恰好漏掉，在 main 里建出空表
        # 把 attached 库的 1030 万行遮蔽掉。
        out = re.sub(
            rf"\b(TABLE|INTO|FROM|UPDATE)\s+(IF\s+NOT\s+EXISTS\s+)?{t}\b",
            lambda m: f"{m.group(1)} {m.group(2) or ''}{schema}.{t}",
            out, flags=re.IGNORECASE)
    return out


def exec_task_data_ddl(conn, sql: str) -> None:
    """执行一条针对巨表的 DDL，自动加上当前的 schema 前缀。失败静默（自愈补列用）。"""
    try:
        conn.execute(qualify_task_data_ddl(sql, task_data_schema(conn)))
    except sqlite3.Error:
        pass


def task_data_schema(conn) -> str:
    """这两张巨表当前实际所在的 schema 名（`main` 或 `taskdata`）。

    **DDL 必须用它做前缀**，不能裸写表名。原因：`CREATE TABLE IF NOT EXISTS
    task_records` 在 ATTACH 之后仍然会建到 **main** 里（SQLite 的 IF NOT EXISTS
    只看 main，不看 attached 库），而不带前缀的查询又是 main 优先 ——
    结果就是一张空表把 attached 库里的 1030 万行**整个遮蔽掉**，
    而且不报任何错。实测验证过这个行为。

    所以：迁移后 taskdata 里有表 → 返回 'taskdata'；否则返回 'main'（迁移前）。
    """
    if not os.path.exists(get_task_data_path()):
        return "main"
    try:
        row = conn.execute(
            f"SELECT 1 FROM {TASK_DATA_SCHEMA}.sqlite_master "
            "WHERE type='table' AND name='task_records' LIMIT 1").fetchone()
        return TASK_DATA_SCHEMA if row else "main"
    except sqlite3.Error:
        return "main"


# SQLite 写锁等待上限（毫秒）。默认 5s 对「uvicorn 4 worker + 3 个离线进程」
# 偏小：产物发现一轮要写十几万行，其它连接 5s 等不到就报 database is locked。
# 放长只是等待、不是失败，真正的解法是让写事务变短（见 output_worker 的产物队列）。
BUSY_TIMEOUT_MS = int(os.environ.get("DB_BUSY_TIMEOUT", "15000"))


def _configure_connection(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    # 挂载巨表所在的独立库（文件不存在时静默跳过，见 _attach_task_data）
    _attach_task_data(conn)
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
    """表是否存在。两张巨表要查它们实际所在的 schema（迁移后在 taskdata）。

    裸 `sqlite_master` 只看 main —— 迁移后查 task_records 会得到「不存在」，
    于是 _ensure_traj_indexes 直接 return、索引永远补不上。
    """
    if name in TASK_DATA_TABLES:
        sch = task_data_schema(conn)
        return conn.execute(
            f"SELECT 1 FROM {sch}.sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _index_names(conn, table: str) -> set[str]:
    """表上已有的索引名。同 _table_exists，巨表要查对应 schema。"""
    sch = task_data_schema(conn) if table in TASK_DATA_TABLES else "main"
    return {r[0] for r in conn.execute(
        f"SELECT name FROM {sch}.sqlite_master WHERE type='index' AND tbl_name=?",
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
    # 首页「成功轨迹」：按实例聚合 task_done=1。
    # 全表 GROUP BY 要 1.97s（360 万行 × 每行回表判断 task_done），首页每次轮询都跑不起。
    # 部分索引只收 task_done=1 的行（约 290 万），且 instance_id 就在索引里，
    # COUNT(*) 可走 covering index，实测降到 0.10s。
    ("idx_traj_task_done_inst",
     "CREATE INDEX IF NOT EXISTS idx_traj_task_done_inst "
     "ON task_traj_records(instance_id) WHERE task_done=1"),
)

# 其它表上被看板高频查询打到、但一直没建索引的列。
OTHER_INDEXES = (
    # shallow_pending = WHERE traj_level IS NULL，330 万行全表扫 ≈2.3s
    ("idx_task_records_level",
     "CREATE INDEX IF NOT EXISTS idx_task_records_level ON task_records(traj_level)"),
    # 浅层 worker 每轮挑实例：WHERE EXISTS (SELECT 1 FROM task_records
    #   WHERE instance_id=? AND traj_level IS NULL) ORDER BY ...
    # 没有它时每个实例都要回表判断 traj_level，实测整条查询 68.6s（928 个实例）。
    # 部分索引只收未分级的行（当前约 346 万，清完积压后会迅速缩小），
    # EXISTS 能直接命中，不再扫已分级的 500 万行。
    ("idx_task_records_ungraded",
     "CREATE INDEX IF NOT EXISTS idx_task_records_ungraded "
     "ON task_records(instance_id) WHERE traj_level IS NULL"),
    # 首页「DONE」列：按实例聚合 task_done=1。task_records 有 984 万行，
    # 全表 COUNT 太慢；部分索引只收已判定为 done 的行（且 instance_id 就在索引里，
    # COUNT(*) 走 covering index）。
    ("idx_task_records_done",
     "CREATE INDEX IF NOT EXISTS idx_task_records_done "
     "ON task_records(instance_id) WHERE task_done=1"),
    # 首页「DONE」列的回退判定：_done_counts 要区分「扫过且为 0」与「压根没扫」，
    # 为此查 WHERE task_done IS NOT NULL 的 DISTINCT instance_id。
    # 上面的 idx_task_records_done 是 task_done=1 的部分索引，服务不了 IS NOT NULL，
    # 于是走 idx_task_records_inst 把 1038 万行全扫一遍（其中 892 万行 task_done 为
    # NULL），实测 2.4s —— 占了 /api/instances 整体 3.1s 的大头，而首页每 20s 轮询一次。
    # 换成同谓词的部分索引后只收已扫过的行（约 146 万），实测 1447ms → 2ms。
    ("idx_task_records_scanned",
     "CREATE INDEX IF NOT EXISTS idx_task_records_scanned "
     "ON task_records(instance_id) WHERE task_done IS NOT NULL"),
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
    sch = task_data_schema(conn)
    have = _index_names(conn, "task_traj_records")
    created = []
    for name, ddl in TRAJ_INDEXES:
        if name in have:
            continue
        shadow = conn.execute(
            f"SELECT tbl_name FROM {sch}.sqlite_master WHERE type='index' AND name=?",
            (name,)).fetchone()
        if shadow and shadow[0] != "task_traj_records":
            conn.execute(f"DROP INDEX IF EXISTS {sch}.{name}")
        conn.execute(qualify_task_data_ddl(ddl, sch))
        created.append(name)

    # 其它表的看板索引：同样要防「索引名被 _ttr_old 之类占用」的静默 no-op。
    # OTHER_INDEXES 里混着 task_records（巨表，可能在 taskdata）和别的表，
    # 所以要按每条 DDL 实际命中的表决定去哪个 schema 查重名。
    for name, ddl in OTHER_INDEXES:
        tgt = sch if any(t in ddl for t in TASK_DATA_TABLES) else "main"
        shadow = conn.execute(
            f"SELECT name FROM {tgt}.sqlite_master WHERE type='index' AND name=?",
            (name,)).fetchone()
        if shadow:
            continue
        conn.execute(qualify_task_data_ddl(ddl, sch))
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
    # 重跑来源：本实例是「重跑失败」新建出来的，指向源实例 id；普通实例为 NULL。
    # 原先只存在 create_params JSON 里，汇总 SQL 每行都要 json_extract 一次，
    # 提成真实列后可以直接建索引式过滤。语义见 _leaf_instance_filter()。
    "copied_from TEXT",
    # 展示用序号：按创建时间升序，最早=1，最新最大。首页表格首列显示，
    # 用来替代又长又像的实例名（2611032107-wanyi26110321070534-d5b7）口头指认。
    #
    # **为什么是持久列而不是查询时 ROW_NUMBER()**：要求「保持不变」。实时排名在
    # 删除旧实例后会让其后所有行的序号集体 −1（该库历史上已删掉 43% 的实例），
    # 截图/工单里写的「第 1203 条」第二天就对不上了。代价是删除会留空洞，
    # 这是「稳定」的必然结果，取舍上稳定 > 连续。
    #
    # 不复用 rowid：rowid 同样单调（实测 2321 行零逆序对），但空洞更大
    # （最大 4076 vs 实际 2321 行），一上来就显示四位数很怪。
    "seq INTEGER",
)

# task_records 的「新增可选列」。
# task_done = 本地 task-N.log 是否含【Task_Done】标记（agent 自己声明做完）。
#   NULL = 还没扫过；0/1 = 已判定。
# 与 task_traj_records.task_done 是**两个口径**：后者来自 OBS 的 tsr / 慢路径，
# 且补全快路径刻意不拉（output_worker._fetch_tsr_entries_fast 注释），导致 254 个实例
# 的该列恒为 0。这一列是worker 直接扫本地日志得到的，不依赖 OBS。
# 之所以落在 task_records 而不是 task_traj_records：前者 UNIQUE(instance_id, config_name)
# 一任务一行，增量扫描只需 WHERE task_done IS NULL；后者一个 config 可能多行（多轨迹），
# 会把同一个日志判多次。
TASK_EXTRA_COLUMNS = (
    "task_done INTEGER",
    # 任务描述（config json 的 queries[].text），供「会话列表」的内容检索用。
    #
    # 为什么落库而不是搜的时候现场读文件：配置是 task_input_path 下每任务一个的
    # json，不在库里。现场 grep 单实例 1 万个文件要 0.8s（32 并发）、20MB 占在
    # uvicorn 进程内存里（4 个 worker 各一份），而且命中几千条时要拼几批 IN，
    # 没法和其它过滤、排序、分页一起下沉到 SQL。落库后就是一个普通 LIKE 条件。
    #
    # **存完整不截断**：实测平均 129 字，而截断到 100 字会丢 54~66% 的命中
    # （「保存到工作区」这类关键信息常在句尾），而存储只省 23% —— 不划算。
    # 真正的省法是按实例 LRU 只保留最近 20 个（见 offline/shallow_query.py）。
    "query_text TEXT",
)

# shallow_queue 的「新增可选列」。产物下载维（artifact_*）是为了让 task_tracker
# 成为唯一的发现方：它在 complete.jsonl 增量读时把 artifact_state 标 'queued'，
# output_worker 只领取、不再全量 UPSERT task_traj_records。
# 单独一套而不是复用 local_state/obs_state：三套状态机的进度完全独立
# （本地判定 900/s、OBS 0.4~2s、产物 cp 1~100s），挤在一起快的会被慢的 claimed 卡住。
SHALLOW_EXTRA_COLUMNS = (
    # local / obs 两维是浅层重构时用 ALTER 加到线上库的，建表 DDL 里一直没补，
    # 于是全新库（init_db 从零建）会缺这两维，shallow-local / shallow-obs 直接
    # no such column。这里一并纳入，保证新库与线上库同构。
    "local_state TEXT NOT NULL DEFAULT 'queued'",
    "obs_state TEXT NOT NULL DEFAULT 'queued'",
    "obs_claimed_by TEXT",
    "obs_claimed_at REAL",
    "obs_attempts INTEGER NOT NULL DEFAULT 0",
    "artifact_state TEXT NOT NULL DEFAULT 'queued'",
    "artifact_claimed_by TEXT",
    "artifact_claimed_at REAL",
    "artifact_attempts INTEGER NOT NULL DEFAULT 0",
    "artifact_retry_at REAL",
    "artifact_note TEXT",
    # 产物维自己的优先级。**不能复用 priority**：priority 是浅层「用户点了输出，
    # 整实例插队」的语义，一个实例一旦被点过，它的全部 config 都变成 100。
    # 若产物车道也用 priority 分流，那 9000+ 行会整批涌进实时车道，
    # 真正的「下载此任务」点击反而排在它们后面（实测 9572 行 priority=100）。
    # artifact_priority 只由「下载此任务 / 会话详情下载」写 100，tracker 一律留 0。
    "artifact_priority INTEGER NOT NULL DEFAULT 0",
)

# shallow_queue 上的部分索引。索引名在 SQLite 里是全局唯一的，所以同样要防
# 「名字被别的表占着 → IF NOT EXISTS 静默 no-op」（见 _ensure_traj_indexes 的说明）。
SHALLOW_INDEXES = (
    ("idx_shallow_queue_claim",
     "CREATE INDEX IF NOT EXISTS idx_shallow_queue_claim "
     "ON shallow_queue(priority DESC, id) WHERE state='queued'"),
    ("idx_shallow_queue_state",
     "CREATE INDEX IF NOT EXISTS idx_shallow_queue_state "
     "ON shallow_queue(state, claimed_at)"),
    ("idx_shallow_queue_artifact",
     "CREATE INDEX IF NOT EXISTS idx_shallow_queue_artifact "
     "ON shallow_queue(artifact_priority DESC, id) WHERE artifact_state='queued'"),
    ("idx_shallow_queue_artifact_claimed",
     "CREATE INDEX IF NOT EXISTS idx_shallow_queue_artifact_claimed "
     "ON shallow_queue(artifact_state, artifact_claimed_at)"),
)


def ensure_shallow_extra_columns() -> None:
    """shallow_queue 的幂等补列 + 补索引。

    和 ensure_traj_extra_columns 同样的理由：task_tracker / shallow-local /
    shallow-obs / output_worker 都是独立进程、不跑 init_db，只靠 uvicorn 启动时迁移。
    「加了列 + 只重启 worker」会让 SQL 直接报 no such column，而症状只是日志里一句
    笼统的「本轮异常」——所以每个离线进程启动时都该调一次。
    """
    try:
        with get_connection() as conn:
            if not _table_exists(conn, "shallow_queue"):
                return
            have = {r[1] for r in conn.execute("PRAGMA table_info(shallow_queue)")}
            added = []
            for col in SHALLOW_EXTRA_COLUMNS:
                name = col.split()[0]
                if name in have:
                    continue
                try:
                    conn.execute(f"ALTER TABLE shallow_queue ADD COLUMN {col}")
                    added.append(name)
                except Exception:
                    pass
            if added:
                conn.commit()
                print(f"[db] shallow_queue 补列: {', '.join(added)}")
            for name, ddl in SHALLOW_INDEXES:
                shadow = conn.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                    (name,)).fetchone()
                if shadow and shadow[0] != "shallow_queue":
                    conn.execute(f"DROP INDEX IF EXISTS {name}")
                conn.execute(ddl)
            conn.commit()
    except Exception as e:
        print(f"[db] shallow_queue 补列失败（worker 可能仍能用已有列）: {str(e)[:150]}")


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
                           ("task_instances", INSTANCE_EXTRA_COLUMNS),
                           ("task_records", TASK_EXTRA_COLUMNS)):
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
                # seq 刚加出来时是全 NULL，按 created_at 升序回填一次（最早=1）。
                # 只在本次真的新增了该列时跑，之后的启动不再动历史值 —— 否则
                # 删过实例的库每次重启都会重新压缩序号，就不「保持不变」了。
                if table == "task_instances" and "seq" in added:
                    conn.execute("""
                        UPDATE task_instances SET seq = (
                            SELECT COUNT(*) FROM task_instances AS t
                            WHERE t.created_at <= task_instances.created_at
                        )
                    """)
                    conn.commit()
                    n = conn.execute(
                        "SELECT COUNT(*) FROM task_instances WHERE seq IS NOT NULL"
                    ).fetchone()[0]
                    print(f"[db] task_instances.seq 回填 {n} 行")
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

        # 插队优先级：越大的越先放行，0 表示正常排队（默认值，入队/重新入队都是 0）。
        # 排序口径统一为 ORDER BY queue_priority DESC, queue_position ASC：
        # 插队任务互相之间仍按 queue_position 先来后到，不会因为都是「插队」而顺序随机。
        # 「取消插队」= 置回 0，任务回到普通排队序列里原来的位置（position 从未被改过）。
        try:
            conn.execute("ALTER TABLE task_instances ADD COLUMN queue_priority INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        # 创建队列索引
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_queue ON task_instances(queue_id, queue_position)")
        except Exception:
            pass

        # 调度抢占按 (queue_id, queue_priority DESC, queue_position) 取队首，给这条路径单独建索引
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_queue_priority "
                "ON task_instances(queue_id, queue_priority DESC, queue_position)"
            )
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
                -- 运行该镜像的操作系统：linux | windows。绝大多数镜像是 linux，
                -- 所以默认 linux，Windows 镜像在注册时显式选择。
                os TEXT NOT NULL DEFAULT 'linux',
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

        # migrate: add os to images
        # 新列对历史行一律是默认值 linux，所以紧接着按名称/地址里的 windows 关键字
        # 回填一次（只在刚加完列时跑，之后用户手工改的值不会被覆盖）。
        try:
            conn.execute("ALTER TABLE images ADD COLUMN os TEXT NOT NULL DEFAULT 'linux'")
        except Exception:
            pass
        else:
            conn.execute(
                "UPDATE images SET os='windows' "
                "WHERE LOWER(name) LIKE '%windows%' OR LOWER(address) LIKE '%windows%'"
            )

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
        conn.executescript(qualify_task_data_ddl("""
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
        """, task_data_schema(conn)))

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
                exec_task_data_ddl(conn, f"ALTER TABLE task_records ADD COLUMN {col}")
            except Exception:
                pass

        # 轨迹级明细表（输出模块）：task_records 的 1:N 展开 —— 每行 = 一个 task 的 Lx 分级 +
        # 轨迹/日志路径。行由离线 worker（offline/output_worker.py）回填；状态机见 README_outputview.md：
        #   浅层: task_records.traj_level IS NULL → running → 回填 traj_level | failed
        #   深层: status pending → downloading → done | failed（用户点开会话详情时才触发）
        # 注意：worker 回填本表与 task_records.traj_level 时用「不触碰 updated_at」的 UPDATE，
        # 否则会与在线 _sync_task_records 的 CURRENT_TIMESTAMP 触碰互相干扰（详见 worker 注释）。
        conn.executescript(qualify_task_data_ddl("""
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
        """, task_data_schema(conn)))

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
                exec_task_data_ddl(conn, f"ALTER TABLE task_traj_records ADD COLUMN {col}")
            except Exception:
                pass
        # migrate: task_records 的可选列（task_done 等）。
        # 必须放在 self_heal_traj_level_nullable **之前**：它内部会调
        # _ensure_traj_indexes 建 OTHER_INDEXES，其中
        # `ON task_records(instance_id) WHERE task_done=1` 是部分索引 ——
        # 列不存在时 init_db 会直接死在 no such column（全新库实测必现）。
        for col in TASK_EXTRA_COLUMNS:
            try:
                exec_task_data_ddl(conn, f"ALTER TABLE task_records ADD COLUMN {col}")
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
                exec_task_data_ddl(conn, f"ALTER TABLE task_traj_records ADD COLUMN {col}")
            except Exception:
                pass

        # migrate: task_instances 的可选列。
        # 原先只有 worker 进程的 ensure_traj_extra_columns() 会补这些列，
        # 于是「只重启 API、不重启 worker」时 API 侧写入就会 no such column
        # （copied_from 是创建实例时写的，必须由 API 进程自己保证列存在）。
        for col in INSTANCE_EXTRA_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE task_instances ADD COLUMN {col}")
            except Exception:
                pass

        # migrate: task_records 的可选列（task_done 等）。
        # 必须放在 _ensure_traj_indexes / OTHER_INDEXES 建索引**之前**：
        # OTHER_INDEXES 里有 `ON task_records(instance_id) WHERE task_done=1`
        # 这类部分索引，列不存在时 init_db 会直接死在 no such column。
        for col in TASK_EXTRA_COLUMNS:
            try:
                exec_task_data_ddl(conn, f"ALTER TABLE task_records ADD COLUMN {col}")
            except Exception:
                pass

        # 回填 copied_from：历史重跑实例只在 create_params JSON 里记过来源。
        # 只填 NULL 行，幂等；json_extract 在 create_params 为空/非法时返回 NULL，
        # 不会写进脏值。json1 是 SQLite 3.38+ 的默认编译项，生产实测 3.45.1 可用。
        try:
            conn.execute(
                "UPDATE task_instances "
                "SET copied_from = json_extract(create_params, '$.copy_complete_from') "
                "WHERE copied_from IS NULL AND create_params IS NOT NULL"
            )
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

        # ── 浅层分析重构：config 级领取队列 + tracker 增量读状态 ──
        #
        # shallow_queue 与上面的 shallow_requests 粒度不同，不能混用：
        #   shallow_requests = 实例级（PRIMARY KEY instance_id，一个实例一行），
        #                      仅供详情页轮询「我这个实例排上了没」
        #   shallow_queue    = config 级（一个实例 N 行），worker 从这里 CAS 领取
        #
        # state 是领取状态机（借鉴 artifacts_status 的做法）：
        #   queued   待领取
        #   claimed  已被某个 worker 领走（claimed_by / claimed_at 用于回收超时）
        #   done     处理完毕
        #   skipped  无法处理（本地日志缺失等），不再重试
        #
        # 一行为三套**互相独立**的状态机（各自 claimed_by/claimed_at/attempts）：
        #   local/local_state    —— shallow-local 判 task_done
        #   obs/obs_state        —— shallow-obs 定级
        #   artifact/artifact_*  —— 产物下载（output_worker --deep-only / --realtime-only）
        # 拆维的理由同三个进程的拆分：快慢相差一个数量级，挤在一个 state 上会互相拖住
        # （快的被慢的 claimed 占住）。产物下载由 task_tracker 在 complete.jsonl 增量
        # 投递时标 artifact_state='queued'，worker 只领取、不再自己做全量发现。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS shallow_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                config_name TEXT NOT NULL,
                task_idx    INTEGER,
                -- 用户点击 = 100，普通 = 0。领取时 ORDER BY priority DESC, id，
                -- 保证「点开的任务」插队，其余按发现顺序（即最新优先）
                priority    INTEGER NOT NULL DEFAULT 0,
                state       TEXT NOT NULL DEFAULT 'queued',
                claimed_by  TEXT,
                claimed_at  REAL,
                attempts    INTEGER NOT NULL DEFAULT 0,
                note        TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- 本地判定维（shallow-local）
                local_state TEXT NOT NULL DEFAULT 'queued',
                -- OBS 定级维（shallow-obs）
                obs_state       TEXT NOT NULL DEFAULT 'queued',
                obs_claimed_by  TEXT,
                obs_claimed_at  REAL,
                obs_attempts    INTEGER NOT NULL DEFAULT 0,
                -- 产物下载维（第三套状态机，与 local/obs 互不干扰）
                artifact_state      TEXT NOT NULL DEFAULT 'queued',
                artifact_claimed_by TEXT,
                artifact_claimed_at REAL,
                artifact_attempts   INTEGER NOT NULL DEFAULT 0,
                artifact_retry_at   REAL,
                artifact_note       TEXT,
                artifact_priority   INTEGER NOT NULL DEFAULT 0,
                UNIQUE(instance_id, config_name)
            );

            -- 领取走这个部分索引：只收 queued 行，随队列消化自动变小
            CREATE INDEX IF NOT EXISTS idx_shallow_queue_claim
                ON shallow_queue(priority DESC, id) WHERE state='queued';
            -- 回收超时 claimed（worker 被 kill）
            CREATE INDEX IF NOT EXISTS idx_shallow_queue_state
                ON shallow_queue(state, claimed_at);
            -- 产物下载的领取索引，与 idx_shallow_queue_claim 同构：
            -- 只收 artifact_state='queued' 的行，队列消化后索引自动变小。
            CREATE INDEX IF NOT EXISTS idx_shallow_queue_artifact
                ON shallow_queue(artifact_priority DESC, id) WHERE artifact_state='queued';
            -- 产物下载的超时回收（worker 被 kill 时行会停在 claimed）
            CREATE INDEX IF NOT EXISTS idx_shallow_queue_artifact_claimed
                ON shallow_queue(artifact_state, artifact_claimed_at);
        """)

        # tracker 的增量读状态：每个 (实例, jsonl 文件) 记一条。
        # offset 只读新增部分；inode + head_hash 两个指纹用来判断「文件被换掉了」，
        # 任一变化都要把 offset 归零，否则会漏读。
        # 为什么需要 head_hash：hive 的 _rewrite_failed_record 是**原地 truncate 重写**
        # （NFSv3 不支持 rename），inode 不变。只靠 inode 时，重写后文件变长会让旧
        # offset 指到新内容的中间，前面的行被整段跳过。
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracker_state (
                instance_id TEXT NOT NULL,
                file_path   TEXT NOT NULL,
                inode       INTEGER,
                offset      INTEGER NOT NULL DEFAULT 0,
                head_hash   TEXT,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (instance_id, file_path)
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
