"""前端轨迹解析器（frontend/src/utils/trajectory.js）的对账测试。

为什么用 unittest 驱动 node 而不是 vitest：本仓前端零测试设施
（package.json 只有 dev/build/preview，devDependencies 无 vitest/jest），
为一个工具文件引入整套前端测试链不划算。trajectory.js 是纯 ESM、
不 import Vue 或任何第三方，node 直接能跑，于是挂在现有 unittest 入口下 ——
`python3 -m unittest discover platform/test` 一条命令仍覆盖全量
（与同目录其余 17 个 test_*.py 同一套写法；本环境没装 pytest）。

核心职责：**防 Python 侧与 JS 侧的解析实现漂移**。
同一份 fixture 喂给 offline_analysis.py 与 trajectory.js，逐块逐字段比对。
解析规则存两份是「解析放前端」这个设计的代价，这个测试就是代价的对冲。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# 与 test_offline_analysis.py 同一套 import 口径（本仓无 conftest.py）
PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa  # noqa: E402

TRAJ_JS = os.path.join(PLATFORM_DIR, "frontend", "src", "utils", "trajectory.js")
HAVE_NODE = shutil.which("node") is not None and os.path.isfile(TRAJ_JS)
SKIP_REASON = "需要 node 与 frontend/src/utils/trajectory.js"


def run_js(fn, text):
    """在 node 里调 trajectory.js 的某个解析函数，返回块数组。"""
    script = """
    import { parseOpenclawTraj, parseHermesMessages, parseOpenjiuwenTraj }
      from %s
    import { readFileSync } from 'fs'
    const text = readFileSync(process.argv[2], 'utf8')
    const fns = { parseOpenclawTraj, parseHermesMessages, parseOpenjiuwenTraj }
    process.stdout.write(JSON.stringify(fns[%s](text)))
    """ % (json.dumps(TRAJ_JS), json.dumps(fn))
    with tempfile.TemporaryDirectory() as d:
        mjs = os.path.join(d, "run.mjs")
        data = os.path.join(d, "input.txt")
        with open(mjs, "w", encoding="utf-8") as f:
            f.write(script)
        with open(data, "w", encoding="utf-8") as f:
            f.write(text)
        out = subprocess.run([shutil.which("node"), mjs, data],
                             capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("node failed: %s" % out.stderr[:2000])
    return json.loads(out.stdout)


def detect_many(rels):
    """批量调 JS 侧 detectTrajKind，返回 [{kind, role}|None]。

    批量而不是逐个：每次 node 冷启动约 50-100ms，20 个用例逐个跑要好几秒。
    """
    script = """
    import { detectTrajKind } from %s
    import { readFileSync } from 'fs'
    const rels = JSON.parse(readFileSync(process.argv[2], 'utf8'))
    process.stdout.write(JSON.stringify(rels.map(r => detectTrajKind(r))))
    """ % json.dumps(TRAJ_JS)
    with tempfile.TemporaryDirectory() as d:
        mjs = os.path.join(d, "run.mjs")
        data = os.path.join(d, "rels.json")
        with open(mjs, "w", encoding="utf-8") as f:
            f.write(script)
        with open(data, "w", encoding="utf-8") as f:
            json.dump(list(rels), f)
        out = subprocess.run([shutil.which("node"), mjs, data],
                             capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("node failed: %s" % out.stderr[:2000])
    return json.loads(out.stdout)


def is_markdown_blocks(blocks):
    """批量调 JS 侧 isMarkdownBlock，返回 [bool]。"""
    script = """
    import { isMarkdownBlock } from %s
    import { readFileSync } from 'fs'
    const blocks = JSON.parse(readFileSync(process.argv[2], 'utf8'))
    process.stdout.write(JSON.stringify(blocks.map(b => isMarkdownBlock(b))))
    """ % json.dumps(TRAJ_JS)
    with tempfile.TemporaryDirectory() as d:
        mjs = os.path.join(d, "run.mjs")
        data = os.path.join(d, "blocks.json")
        with open(mjs, "w", encoding="utf-8") as f:
            f.write(script)
        with open(data, "w", encoding="utf-8") as f:
            json.dump(list(blocks), f, ensure_ascii=False)
        out = subprocess.run([shutil.which("node"), mjs, data],
                             capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("node failed: %s" % out.stderr[:2000])
    return json.loads(out.stdout)


# ============ fixtures ============

def legacy_fixture():
    """openclaw legacy：toolCall 三种真实布局 + toolResult 平级分支 + thinking/text。"""
    rows = [
        {"type": "session", "version": 3, "id": "s1"},
        {"type": "message", "message": {"role": "user", "content": "帮我查一下"}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "先看看目录"},
            {"type": "text", "text": "我来查"},
            # ① 嵌套 toolCall.function
            {"type": "toolCall", "toolCall": {
                "id": "c1", "type": "function",
                "function": {"name": "Bash", "arguments": {"command": "ls -la"}}}},
        ]}},
        # toolResult 必须与 assistant 平级（后端踩过缩进 bug）
        {"type": "message", "message": {
            "role": "toolResult", "content": "total 0",
            "tool": {"name": "Bash"}, "isError": False,
            "details": {"exitCode": 0}}},
        {"type": "message", "message": {"role": "assistant", "content": [
            # ② 嵌套 toolCall 无 function，直接 name/arguments
            {"type": "toolCall", "toolCall": {
                "id": "c2", "name": "Read", "arguments": {"path": "/a"}}},
        ]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            # ③ 扁平 {name, arguments}
            {"type": "toolCall", "id": "c3", "name": "Grep",
             "arguments": {"pattern": "x"}},
        ]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            # claude-code 的 tool_use：args 在 input 里
            {"type": "tool_use", "id": "c4", "name": "Write",
             "input": {"file_path": "/b"}},
        ]}},
        # toolResult 出错 + details 是数组（真实数据里出现过，不是只有 dict）
        {"type": "message", "message": {
            "role": "toolResult", "content": "boom",
            "tool": {"name": "Grep"}, "isError": True,
            "details": [{"name": "t1"}]}},
        {"type": "message", "message": {"role": "assistant", "content": "搞定了"}},
        # else 分支：role 非 assistant/toolResult，content 原样透传（含数组形态）
        {"type": "message", "message": {"role": "user", "content": [
            {"type": "text", "text": "再看看"}]}},
        {"type": "message", "message": {"role": "system", "content": "sys"}},
    ]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


def stream_fixture():
    """openclaw 事件流：seq 故意乱序，解析必须按 seq 重排还原时序。"""
    rows = [
        {"type": "assistant/message", "seq": 3, "data": {"message": {"content": [
            {"type": "text", "text": "第二轮"}]}}},
        {"type": "user/message", "seq": 1, "data": {"content": [
            {"type": "text", "text": "你好"}]}},
        {"type": "assistant/message", "seq": 2, "data": {"message": {"content": [
            {"type": "reasoning", "text": "想一下"},
            {"type": "tool-call", "id": "tc1", "name": "Bash",
             "arguments": {"command": "pwd"}}]}}},
        # tool/result 的 content 是 parts 数组
        {"type": "tool/result", "seq": 4, "data": {"message": {"content": [
            {"type": "tool-result", "toolCallId": "tc1",
             "content": "/home", "isError": False}]}}},
        # tool/result 的 content 是纯字符串
        {"type": "tool/result", "seq": 5, "data": {"message": {"content": "plain"}}},
    ]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"


def hermes_fixture():
    return json.dumps({
        "session_id": "s1",
        "model": "m",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "查一下天气"},
            # ⚠ 工具调用在顶层 tool_calls[]，不在 content parts 里
            {"role": "assistant", "content": "好的",
             "reasoning_content": "需要调工具",
             "tool_calls": [
                 {"id": "c1", "call_id": "c1", "type": "function",
                  "function": {"name": "get_weather",
                               "arguments": "{\"city\":\"北京\"}"}}]},
            {"role": "tool", "name": "get_weather", "tool_call_id": "c1",
             "content": "晴 25℃"},
            {"role": "assistant", "content": "北京今天晴，25 度"},
        ],
    }, ensure_ascii=False)


def openjiuwen_fixture():
    return json.dumps({
        "query": "做个报告",
        "agent_name": "a1",
        "turns": [
            {"turn": 1, "user_input": "做个报告",
             "agent_content": "我来做",
             "tool_calls": [{"tool": "write_file",
                             "input": {"path": "r.md"},
                             "output": "ok"}],
             "files": [{"name": "r.md"}, {"name": "r.pptx"}]},
            {"turn": 2, "user_input": "再改改", "agent_content": "改好了",
             "tool_calls": [], "files": []},
        ],
    }, ensure_ascii=False)


@unittest.skipUnless(HAVE_NODE, SKIP_REASON)
class TrajectoryJsBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write_tmp(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def assert_same(self, js_blocks, py_blocks, label):
        """逐块逐字段比对两侧结果。字段集也要一致（缺字段会让详情页取不到值）。"""
        self.assertEqual(len(js_blocks), len(py_blocks),
                         "%s: 块数不一致 js=%d py=%d"
                         % (label, len(js_blocks), len(py_blocks)))
        for i, (a, b) in enumerate(zip(js_blocks, py_blocks)):
            for key in sorted(set(a) | set(b)):
                self.assertEqual(
                    a.get(key), b.get(key),
                    "%s: 第 %d 块字段 %s 不一致\n  js=%r\n  py=%r"
                    % (label, i, key, a.get(key), b.get(key)))


class TestDetectTrajKind(TrajectoryJsBase):
    """路径判定。按路径而不是扩展名 —— hermes/openjiuwen 轨迹也是 .json。"""

    POSITIVE = [
        # openclaw：agents/{main,assistant*}/sessions/**/*.jsonl
        ("agents/main/sessions/abc.jsonl", "openclaw", "assistant"),
        ("agents/assistant1/sessions/abc.jsonl", "openclaw", "assistant"),
        # evaluator 也要能点开 —— 分组视图是文件浏览器，不是「挑一条主轨迹」
        ("agents/evaluator/sessions/abc.jsonl", "openclaw", "evaluator"),
        # 嵌套布局（obsutil cp -r 生成 <ws>/<session_id>/session.jsonl）
        ("task_x/task_x/agents/main/sessions/ws/sid/session.jsonl",
         "openclaw", "assistant"),
        # hermes：profiles/*/sessions/**/*.json
        ("profiles/main/sessions/session_1.json", "hermes", "assistant"),
        ("profiles/assistant1/sessions/session_1.json", "hermes", "assistant"),
        ("profiles/evaluator/sessions/session_1.json", "hermes", "evaluator"),
        # openjiuwen：logs/**/trajectories/**/query<N>.json
        ("logs/trajectories/run1/query1.json", "openjiuwen", "assistant"),
        ("logs/trajectories/run1/query12.json", "openjiuwen", "assistant"),
        # claude-code 兜底布局
        ("projects/myws/abc.jsonl", "openclaw", "assistant"),
    ]

    NEGATIVE = [
        # 普通 .json 配置：绝不能被误判成 hermes 轨迹
        "logs/traj_stats_result.json",
        "workdir/config.json",
        "traj_stats_result.json",
        # 非轨迹 jsonl
        "complete.jsonl",
        "logs/complete.jsonl",
        # 派生/摘要文件：后端 find_openclaw_sessions 明确排除含 trajectory 的文件名
        "agents/main/sessions/abc.trajectory.jsonl",
        # trajectories 目录但文件名不是 query<N>.json
        "logs/trajectories/run1/summary.json",
        # sessions 目录但不在 agents/profiles 下
        "foo/sessions/abc.jsonl",
        # 其它常见产物
        "workdir/run.log",
        "report.html",
        "",
    ]

    def test_positive(self):
        rels = [r for r, _k, _ro in self.POSITIVE]
        got = detect_many(rels)
        for (rel, kind, role), g in zip(self.POSITIVE, got):
            with self.subTest(rel=rel):
                self.assertIsNotNone(g, "%s 应被识别为轨迹" % rel)
                self.assertEqual(g["kind"], kind)
                self.assertEqual(g["role"], role)

    def test_negative(self):
        got = detect_many(self.NEGATIVE)
        for rel, g in zip(self.NEGATIVE, got):
            with self.subTest(rel=rel):
                self.assertIsNone(g, "%s 不该被识别为轨迹" % rel)


class TestOpenclawLegacy(TrajectoryJsBase):

    def test_parity(self):
        text = legacy_fixture()
        path = self.write_tmp("legacy.jsonl", text)
        self.assert_same(run_js("parseOpenclawTraj", text),
                         oa.parse_openclaw_trajectory(path), "openclaw-legacy")

    def test_toolresult_visible(self):
        """toolResult 块必须真的出现 —— 后端历史上因缩进 bug 完全看不到工具返回。"""
        blocks = run_js("parseOpenclawTraj", legacy_fixture())
        results = [b for b in blocks if b["part_type"] == "toolResult"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["content"], "total 0")
        self.assertEqual(results[0]["exitCode"], 0)
        self.assertTrue(results[1]["isError"])
        # details 是数组时 exitCode 取不到，但 details 本身要原样透传
        self.assertIsNone(results[1]["exitCode"])
        self.assertEqual(results[1]["details"], [{"name": "t1"}])

    def test_toolcall_layouts(self):
        """toolCall 三种布局 + claude-code input 都要解析出 tool_name/args。"""
        blocks = run_js("parseOpenclawTraj", legacy_fixture())
        calls = [b for b in blocks if b["part_type"] in ("toolCall", "tool_use")]
        self.assertEqual(
            [(b["tool_name"], b["args"]) for b in calls],
            [("Bash", {"command": "ls -la"}),
             ("Read", {"path": "/a"}),
             ("Grep", {"pattern": "x"}),
             ("Write", {"file_path": "/b"})])

    def test_mixed_bad_lines(self):
        """夹着坏行时，两侧都应跳过坏行、解析其余 —— 口径必须一致。"""
        text = ('{"type":"message","message":{"role":"user","content":"a"}}\n'
                'THIS IS NOT JSON\n'
                '{"type":"message","message":{"role":"assistant","content":"b"}}\n')
        path = self.write_tmp("mixed.jsonl", text)
        self.assert_same(run_js("parseOpenclawTraj", text),
                         oa.parse_openclaw_trajectory(path), "openclaw-mixed")


class TestOpenclawStream(TrajectoryJsBase):

    def test_parity(self):
        text = stream_fixture()
        path = self.write_tmp("stream.jsonl", text)
        self.assert_same(run_js("parseOpenclawTraj", text),
                         oa.parse_openclaw_trajectory(path), "openclaw-stream")

    def test_seq_order(self):
        """按 seq 重排：乱序写入也要还原成 用户→思考→工具调用→结果 的顺序。"""
        blocks = run_js("parseOpenclawTraj", stream_fixture())
        self.assertEqual([b["part_type"] for b in blocks],
                         ["text", "thinking", "toolCall", "text",
                          "toolResult", "toolResult"])
        self.assertEqual(blocks[0]["content"], "你好")


class TestHermes(TrajectoryJsBase):

    def test_parity(self):
        text = hermes_fixture()
        path = self.write_tmp("session_1.json", text)
        self.assert_same(run_js("parseHermesMessages", text),
                         oa.parse_hermes_messages(path), "hermes")

    def test_toolcalls_from_top_level(self):
        """工具调用必须从顶层 tool_calls[] 取 —— 源仓按 content parts 数导致恒 0。"""
        blocks = run_js("parseHermesMessages", hermes_fixture())
        calls = [b for b in blocks if b["part_type"] == "toolCall"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_name"], "get_weather")
        # 后端把入参放 content、整个 function 对象放 args，这里跟随同一口径
        self.assertEqual(calls[0]["content"], '{"city":"北京"}')
        self.assertEqual(calls[0]["args"],
                         {"name": "get_weather", "arguments": '{"city":"北京"}'})
        self.assertTrue(any(b["part_type"] == "thinking" for b in blocks))
        self.assertTrue(any(b["part_type"] == "toolResult" for b in blocks))


class TestOpenjiuwen(TrajectoryJsBase):

    def test_parity(self):
        text = openjiuwen_fixture()
        path = self.write_tmp("query1.json", text)
        self.assert_same(run_js("parseOpenjiuwenTraj", text),
                         oa.parse_openjiuwen_trajectory(path), "openjiuwen")

    def test_files_line(self):
        blocks = run_js("parseOpenjiuwenTraj", openjiuwen_fixture())
        self.assertTrue(any(b["content"] == "[产物]: r.md, r.pptx" for b in blocks))


class TestMalformedInput(TrajectoryJsBase):
    """坏输入返回 []，由调用方回落到原始 JSON 渲染，不能抛异常。"""

    CASES = [
        ("parseOpenclawTraj", ""),
        ("parseOpenclawTraj", "not json\n{broken"),
        ("parseHermesMessages", ""),
        ("parseHermesMessages", "{broken"),
        ("parseHermesMessages", "[]"),
        ("parseOpenjiuwenTraj", ""),
        ("parseOpenjiuwenTraj", "{broken"),
    ]

    def test_returns_empty(self):
        for fn, text in self.CASES:
            with self.subTest(fn=fn, text=text[:20]):
                self.assertEqual(run_js(fn, text), [])


class TestRealCachedTrajectory(TrajectoryJsBase):
    """真实 OBS 轨迹的对账（fixture 再全也不如真实数据）。

    默认跳过：缓存在 NFS 上（OUTPUT_CACHE 指向 sfs_turbo），递归找文件要几分钟，
    挂在默认测试流里会把整个 test suite 拖成分钟级。
    需要时显式指定文件再跑：
        TRAJ_REAL_FILE=/path/to/session.jsonl python3 test/test_trajectory_js.py
    """

    def test_parity_on_real_file(self):
        path = os.environ.get("TRAJ_REAL_FILE")
        if not path:
            self.skipTest("未设置 TRAJ_REAL_FILE（真实轨迹对账默认跳过，NFS 扫描慢）")
        if not os.path.isfile(path):
            self.fail("TRAJ_REAL_FILE 不存在: %s" % path)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if path.endswith(".jsonl"):
            js, py = run_js("parseOpenclawTraj", text), oa.parse_openclaw_trajectory(path)
        else:
            js, py = run_js("parseHermesMessages", text), oa.parse_hermes_messages(path)
        self.assertTrue(py, "后端解析出 0 块，换一个文件再试")
        self.assert_same(js, py, "real:%s" % os.path.basename(path))


class TestMarkdownBlockDetection(TrajectoryJsBase):
    """哪些块按 markdown 渲染。

    这条规则决定了什么内容会进 v-html，也决定了工具输出会不会被 markdown
    搞坏（ls 输出里的 *、_、# 被当语法吃掉、缩进被重排），必须锁住。
    """

    def _blk(self, part_type, content, role="assistant"):
        return {"role": role, "part_type": part_type, "content": content,
                "tool_name": None, "args": None, "isError": None,
                "exitCode": None, "details": None}

    def test_renders_natural_language_with_markup(self):
        cases = [
            self._blk("text", "- 第一项\n- 第二项"),
            self._blk("text", "1. 一\n2. 二"),
            self._blk("thinking", "我要做：\n- a\n- b"),
            self._blk("text", "用 `foo.pptx` 这个文件"),
            self._blk("text", "**重点**在这里"),
            self._blk("text", "# 标题"),
            self._blk("text", "> 引用"),
            self._blk("text", "| a | b |\n| - | - |"),
            self._blk("text", "```\ncode\n```"),
            self._blk("text", "- 列表", role="user"),
        ]
        self.assertEqual(is_markdown_blocks(cases), [True] * len(cases))

    def test_skips_tool_output_and_args(self):
        """toolResult / toolCall 一律不渲染 —— 即便正文里有 markdown 样的字符。"""
        cases = [
            # ls -la 输出：含 - 开头的行（权限位）与 * ，渲染会被当列表/强调
            self._blk("toolResult", "-rw-r--r-- 1 u g 15275 a.json\n-rw-r--r-- 1 u g 80 b.py",
                      role="toolResult"),
            self._blk("toolResult", '{"a": 1, "b": [2, 3]}', role="toolResult"),
            self._blk("toolResult", "# comment\n- item", role="toolResult"),
            self._blk("toolCall", "- 看起来像列表但这是工具正文"),
        ]
        self.assertEqual(is_markdown_blocks(cases), [False] * len(cases))

    def test_skips_plain_text_without_markup(self):
        """没有 markdown 标记的纯文本不渲染：markdown 会合并空行、吃掉缩进。"""
        cases = [
            self._blk("text", "就是一句普通的话。"),
            self._blk("text", "第一段\n\n第二段\n\n第三段"),
            self._blk("thinking", "我先想想，然后再决定怎么做。"),
            self._blk("text", ""),
        ]
        self.assertEqual(is_markdown_blocks(cases), [False] * len(cases))

    def test_skips_non_string_content(self):
        """content 是数组/对象（真实 toolResult 的 parts 形态）时不渲染。"""
        cases = [
            self._blk("text", [{"type": "text", "text": "- a"}]),
            self._blk("text", {"k": "- a"}),
            self._blk("text", None),
        ]
        self.assertEqual(is_markdown_blocks(cases), [False] * len(cases))

    def test_real_trajectory_targeting(self):
        """真实轨迹上的定向：只命中 text/thinking，绝不碰 toolCall/toolResult。"""
        path = os.environ.get("TRAJ_REAL_FILE")
        if not path or not os.path.isfile(path):
            self.skipTest("未设置 TRAJ_REAL_FILE")
        blocks = oa.parse_openclaw_trajectory(path)
        flags = is_markdown_blocks(blocks)
        for b, f in zip(blocks, flags):
            if f:
                self.assertIn(b["part_type"], ("text", "thinking"),
                              "不该渲染 %s 块" % b["part_type"])
                self.assertIsInstance(b["content"], str)


if __name__ == "__main__":
    unittest.main()
