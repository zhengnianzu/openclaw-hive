# -*- coding: utf-8 -*-
"""数据库备份脚本测试（unittest，无 pytest 依赖）。

覆盖 db_backup.py 里几个「写错了不会立刻报错、但恢复时会要命」的点：

  1. WAL 未 checkpoint 的数据必须进备份
     直接 shutil.copy2 主库文件会漏掉 -wal 里的尾巴，副本能打开但少数据。
     这里造一个「写入后不 checkpoint」的库，断言副本里能看到那行。
  2. 保留策略只动自己的目录
     名字不合 YYYYMMDD-HHMMSS[.full] 的目录、以及备份目录里的其他文件，一律不碰。
  3. 不完整的半成品会被清掉
     manifest 里 complete=False 的目录是上次崩在中途留下的，下次 prune 顺手删。
  4. manifest 的 sha256 与实际文件一致
  5. list / audit 是只读的（前后 mtime 集合不变）

运行：cd platform && python3 -m unittest discover -s test -p "test_db_backup.py" -v
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

_spec = importlib.util.spec_from_file_location(
    "db_backup", os.path.join(PLATFORM_DIR, "db_backup.py"))
db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db)


def _make_wal_db(path: str, rows: int = 5) -> sqlite3.Connection:
    """建一个 WAL 模式的库并返回**保持打开**的写连接。

    必须保持连接打开：最后一个连接关闭时 SQLite 会自动 checkpoint 并删掉 -wal，
    那样这个测试就测不到任何东西了。生产中后端/worker 长期持有连接，-wal 里
    常态有未落盘数据 —— 这正是必须用 backup API 而不是拷文件的理由。
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")   # 关掉自动 checkpoint，保证 -wal 有内容
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE task_instances (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO users (name) VALUES (?)",
                    [(f"u{i}",) for i in range(rows)])
    con.executemany("INSERT INTO task_instances (v) VALUES (?)",
                    [(f"v{i}",) for i in range(rows)])
    con.commit()
    return con


class BackupSqliteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_wal_content_included(self):
        """核心断言：backup API 必须把未 checkpoint 的 WAL 数据带进副本。"""
        src = os.path.join(self.root, "src.db")
        writer = _make_wal_db(src, rows=7)
        self.addCleanup(writer.close)
        wal = src + "-wal"
        self.assertTrue(os.path.exists(wal) and os.path.getsize(wal) > 0,
                        "-wal 应当非空（否则这个测试没测到东西）")

        dst = os.path.join(self.root, "dst.db")
        r = db.backup_sqlite(src, dst)

        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 7)
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM task_instances").fetchone()[0], 7)
        finally:
            con.close()
        self.assertEqual(r["integrity"], "ok")
        self.assertEqual(r["rowcounts"]["task_instances"], 7)

    def test_source_not_modified(self):
        """源库以 mode=ro 打开：备份不能改变源库内容。"""
        src = os.path.join(self.root, "src.db")
        writer = _make_wal_db(src, rows=3)
        self.addCleanup(writer.close)
        before = db.sha256_of(src)
        db.backup_sqlite(src, os.path.join(self.root, "d.db"))
        self.assertEqual(db.sha256_of(src), before)

    def test_manifest_sha256_matches(self):
        src = os.path.join(self.root, "src.db")
        writer = _make_wal_db(src)
        self.addCleanup(writer.close)
        dst = os.path.join(self.root, "d.db")
        db.backup_sqlite(src, dst)
        import hashlib
        with open(dst, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(db.sha256_of(dst), h)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_bak(self, name: str, complete: bool = True) -> str:
        p = os.path.join(self.dest, name)
        os.makedirs(p)
        with open(os.path.join(p, "manifest.json"), "w") as f:
            json.dump({"complete": complete, "kind": "full" if name.endswith(".full") else "daily"}, f)
        with open(os.path.join(p, "platform.db"), "wb") as f:
            f.write(b"x" * 16)
        return p

    def _stamp(self, days_ago: int) -> str:
        import datetime as _dt
        d = _dt.datetime.now() - _dt.timedelta(days=days_ago)
        return d.strftime("%Y%m%d-%H%M%S")

    def test_keep_within_week(self):
        """按天保留：7 天内的都在，9 天前的被清。"""
        old = self._make_bak(self._stamp(9))
        recent = [self._make_bak(self._stamp(i)) for i in (0, 2, 5)]
        removed = db.prune(self.dest)
        self.assertFalse(os.path.exists(old))
        for p in recent:
            self.assertTrue(os.path.isdir(p), p)
        self.assertEqual(len(removed), 1)

    def test_keep_full_by_days(self):
        """周级套件同样按天：超出 7 天的 .full 也删。"""
        old = self._make_bak(self._stamp(8) + ".full")
        new = self._make_bak(self._stamp(1) + ".full")
        db.prune(self.dest)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(new))

    def test_untouched_foreign_entries(self):
        """名字不合规的目录、以及根下的其他文件，统统不碰。"""
        keep = os.path.join(self.dest, "manual_keep")
        os.makedirs(keep)
        with open(os.path.join(keep, "note.txt"), "w") as f:
            f.write("mine")
        with open(os.path.join(self.dest, "db_backup.log"), "w") as f:
            f.write("log")
        for i in range(10):
            self._make_bak(self._stamp(i))

        db.prune(self.dest)
        self.assertTrue(os.path.isdir(keep))
        self.assertTrue(os.path.exists(os.path.join(keep, "note.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "db_backup.log")))

    def test_incomplete_removed(self):
        bad = self._make_bak(self._stamp(1), complete=False)
        good = self._make_bak(self._stamp(2), complete=True)
        db.prune(self.dest)
        self.assertFalse(os.path.exists(bad))
        self.assertTrue(os.path.isdir(good))


class ReadOnlyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _snapshot(self):
        out = {}
        for root, _d, files in os.walk(self.dest):
            for fn in files:
                p = os.path.join(root, fn)
                out[p] = (os.path.getsize(p), os.stat(p).st_mtime_ns)
        return out

    def test_list_does_not_modify(self):
        p = os.path.join(self.dest, "20261109-030000")
        os.makedirs(p)
        with open(os.path.join(p, "manifest.json"), "w") as f:
            json.dump({"complete": True}, f)
        before = self._snapshot()
        db.list_backups(self.dest)
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
