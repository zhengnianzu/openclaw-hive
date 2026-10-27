# -*- coding: utf-8 -*-
"""沙箱交付物本地导航测试（unittest，无 pytest 依赖，与 test_offline_analysis.py 同风格）。

当前架构是「全量预下载 + 本地遍历」：worker 用 `obsutil cp -r` 把整个 task 前缀下到
OUTPUT_CACHE/<batch>/<leaf>/，本模块只读磁盘。所以这里测的全是纯本地、无副作用的函数：
路径安全、目录遍历（跳过平台中间目录）、文本分页、类型判定、PDF 缓存命名。

运行：cd platform && python -m unittest discover -s test -v
"""
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from api.services import artifact_store as store  # noqa: E402


class CleanRelTest(unittest.TestCase):
    def test_accepts_normal_paths(self):
        self.assertEqual(store.clean_rel("workspace/a/b.html"), "workspace/a/b.html")
        # "./" 与重复斜杠被规整
        self.assertEqual(store.clean_rel("./workspace//x.md"), "workspace/x.md")

    def test_rejects_traversal_and_oddities(self):
        for bad in ("../etc/passwd", "a/../../b", "/abs/path", "a\\b", "a\x00b", ""):
            with self.assertRaises(ValueError, msg=bad):
                store.clean_rel(bad)

    def test_rejects_overlong_segment(self):
        with self.assertRaises(ValueError):
            store.clean_rel("a/" + "x" * 300)


class SafeJoinTest(unittest.TestCase):
    def test_containment(self):
        with tempfile.TemporaryDirectory() as root:
            joined = store.safe_join(root, "a/b.txt")
            self.assertTrue(joined.startswith(os.path.realpath(root)))
            with self.assertRaises(ValueError):
                store.safe_join(root, "../escape.txt")

    def test_symlink_escape_rejected(self):
        """软链逃逸：realpath 校验必须拦下。"""
        with tempfile.TemporaryDirectory() as root:
            outside = tempfile.mkdtemp()
            link = os.path.join(root, "link")
            os.symlink(outside, link)
            try:
                with self.assertRaises(ValueError):
                    store.safe_join(root, "link/secret.txt")
            finally:
                os.remove(link)
                os.rmdir(outside)


class WalkTreeTest(unittest.TestCase):
    def _mk(self, root, rel, size=1):
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def test_flat_listing_with_rel_paths(self):
        with tempfile.TemporaryDirectory() as root:
            self._mk(root, "workdir/run.log", 10)
            self._mk(root, "workspace/slides.pptx", 2048)
            entries = store.walk_tree(root)
            rels = sorted(e["rel"] for e in entries)
            self.assertEqual(rels, ["workdir/run.log", "workspace/slides.pptx"])
            ppt = next(e for e in entries if e["rel"].endswith("slides.pptx"))
            self.assertEqual(ppt["name"], "slides.pptx")
            self.assertEqual(ppt["size"], 2048)      # 字节数，不是格式化字符串

    def test_internal_pdf_dir_skipped(self):
        """_pdf/ 是 LibreOffice 转换缓存，不属于沙箱交付件，遍历必须跳过。"""
        with tempfile.TemporaryDirectory() as root:
            self._mk(root, "workspace/demo.pptx")
            self._mk(root, "_pdf/workspace__demo.pptx.pdf")
            rels = [e["rel"] for e in store.walk_tree(root)]
            self.assertEqual(rels, ["workspace/demo.pptx"])

    def test_max_entries_truncates(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(5):
                self._mk(root, f"f{i}.txt")
            self.assertEqual(len(store.walk_tree(root, max_entries=3)), 3)

    def test_missing_root_returns_empty(self):
        self.assertEqual(store.walk_tree("/nonexistent/path/for/test"), [])


class ListSubdirsTest(unittest.TestCase):
    def test_only_directories(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "task_000001"))
            os.makedirs(os.path.join(root, "task_000002"))
            open(os.path.join(root, "stray.txt"), "w").close()
            self.assertEqual(store.list_subdirs(root), ["task_000001", "task_000002"])

    def test_missing_root_returns_empty(self):
        self.assertEqual(store.list_subdirs("/nonexistent/path/for/test"), [])


class ReadSliceTest(unittest.TestCase):
    def test_char_paging(self):
        """按字符分页：trajectory.jsonl 单行十几万字符，按行分页拆不开。"""
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write('{"a":1}\n{"b":2}\n')
            p = f.name
        try:
            r = store.read_text_slice(p, 0, 5)
            self.assertEqual(r["text"], '{"a":')
            self.assertTrue(r["has_more"])
            self.assertEqual(r["total_chars"], 16)
            self.assertEqual(r["next_offset"], 5)

            r2 = store.read_text_slice(p, r["next_offset"], 100)
            self.assertFalse(r2["has_more"])
            self.assertEqual(r["text"] + r2["text"], '{"a":1}\n{"b":2}\n')
        finally:
            os.remove(p)

    def test_offset_past_end_is_clamped(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("abc")
            p = f.name
        try:
            r = store.read_text_slice(p, 999, 10)
            self.assertEqual(r["text"], "")
            self.assertFalse(r["has_more"])
        finally:
            os.remove(p)


class MediaTypeTest(unittest.TestCase):
    def test_office_needs_conversion(self):
        for name in ("a.pptx", "a.doc", "a.xlsx", "a.ODS"):
            self.assertTrue(store.is_office(name), name)
            # 非浏览器原生类型 → 不允许内联（前端走 /pdf 转换接口）
            self.assertFalse(store.media_type(name)[1], name)
            self.assertEqual(store.media_type(name)[0], "application/octet-stream")

    def test_native_types_inline(self):
        self.assertEqual(store.media_type("a.html")[0], "text/html; charset=utf-8")
        self.assertEqual(store.media_type("a.svg")[0], "image/svg+xml")
        self.assertEqual(store.media_type("a.pdf")[0], "application/pdf")
        self.assertEqual(store.media_type("a.png")[0], "image/png")

    def test_csv_is_text_not_office(self):
        """csv/tsv 既是表格也是纯文本：必须按纯文本返回，不能拖进 LibreOffice。

        后端 media_type 与前端 previewKindOf 的判定顺序必须一致，否则
        「后端给 octet-stream、前端当表格渲染」会白屏。
        """
        for name in ("a.csv", "a.tsv"):
            self.assertEqual(store.media_type(name), ("text/plain; charset=utf-8", True))
            self.assertFalse(store.is_office(name))

    def test_unknown_is_binary(self):
        self.assertEqual(store.media_type("a.bin"), ("application/octet-stream", False))


class PdfCacheTest(unittest.TestCase):
    def test_name_is_flat_and_inside_pdf_dir(self):
        """PDF 缓存放在 task 目录的 _pdf/ 下，名字把路径的 "/" 压平，防止多级目录。"""
        p = store.pdf_cache_path("/cache/batch/task_1", "workspace/sub/demo deck.pptx")
        self.assertTrue(p.startswith("/cache/batch/task_1/_pdf/"))
        self.assertTrue(p.endswith(".pdf"))
        self.assertEqual(os.path.basename(p).count(os.sep), 0)

    def test_cache_path_is_stable(self):
        a = store.pdf_cache_path("/t", "a/b.pptx")
        b = store.pdf_cache_path("/t", "a/b.pptx")
        self.assertEqual(a, b)

    def test_flattening_does_not_collide(self):
        """压平后的路径必须仍然一一对应，否则两个文件会共用一份 PDF。"""
        a = store.pdf_cache_path("/t", "a b/x.pptx")
        b = store.pdf_cache_path("/t", "a_b/x.pptx")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
