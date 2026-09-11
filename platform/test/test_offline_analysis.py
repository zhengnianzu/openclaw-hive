# -*- coding: utf-8 -*-
"""离线分析引擎测试（unittest，无 pytest 依赖）。

覆盖 README_trajdb.md「阶段性测试案例」对应的断言：
  S3  openclaw L1 gate（tool_calls>=3 且 plain_rounds>0）
  S4  hermes 真实会话 → analyze_hermes_messages 得 tool_calls==24 / plain_rounds==1 / assistant_rounds==25
  S5  compute_level 全级别判定（L0/L1/L1.5/L2/L3）
  S6  detect_harness 布局判定（profiles/*/sessions/*.json → hermes；agents/*/sessions/*.jsonl → openclaw）
  S8  过期 tsr 过滤（task_level "none" 的全零 entry 判 stale，快路径回退）
      openclaw/hermes 轨迹逐块解析（thinking/toolCall/toolResult 字段正确）
      批次漏斗比率（stats_from_per_task_compact）

运行：cd /root/openclaw-hive/platform && python -m unittest discover -s test -v
真实 hermes 会话文件存在时自动加跑 24/1/25 断言（缺失则 skip）。
"""
import json
import os
import sys
import tempfile
import unittest

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLATFORM_DIR not in sys.path:
    sys.path.insert(0, PLATFORM_DIR)

from src import offline_analysis as oa  # noqa: E402
import src.traj_pipeline as tp  # noqa: E402
from src.traj_pipeline import harness_tsr_to_entries  # noqa: E402

HERMES_REAL = "/tmp/hermes_probe/hermes_session.json"
OC_TSR_REAL = "/tmp/ocprobe/tsr.json"
HERMES_TSR_STALE = "/tmp/hermes_probe/traj_stats_result.json"


def _write(path: str, data: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return path


def _make_hermes_session(path: str, n_tool_msgs: int = 2, n_plain: int = 1) -> str:
    """构造 OpenAI-chat 式 hermes 会话（顶层 tool_calls[]，与真实格式一致）。"""
    msgs = [{"role": "user", "content": "你好"}]
    for i in range(n_tool_msgs):
        msgs.append({"role": "assistant",
                     "content": f"第{i}轮思考",
                     "reasoning_content": "thinking...",
                     "tool_calls": [{"id": f"call_{i}", "type": "function",
                                     "function": {"name": "search",
                                                  "arguments": json.dumps({"q": str(i)})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}",
                     "name": "search", "content": f"结果{i}"})
    for _ in range(n_plain):
        msgs.append({"role": "assistant", "content": "无工具收尾"})
    return _write(path, json.dumps({"messages": msgs}))


def _make_openclaw_traj(path: str) -> str:
    """构造 openclaw 事件流轨迹（message.message.content parts，真实格式）。"""
    lines = [
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "查关键词"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "先搜索"},
            {"type": "text", "text": "我来查"},
            {"type": "toolCall", "toolCall": {"id": "t1", "type": "function",
                                              "function": {"name": "search",
                                                           "arguments": "{\"q\":\"kw\"}"}}},
        ]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "t1",
                                        "content": [{"type": "text", "text": "无结果"}],
                                        "details": {"exitCode": 0}, "isError": False}},
        {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "结束"}]}},
    ]
    return _write(path, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))


def _make_openclaw_traj_flat(path: str) -> str:
    """扁平 toolCall：{type,id,name,arguments} 直接在 part 上（真实 oc_memory 子任务格式）。"""
    lines = [
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "读文件"}]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "t1", "name": "exec",
             "arguments": {"command": "find / -name 'x.docx'"}},
        ]}},
        {"type": "message", "message": {"role": "toolResult", "toolCallId": "t1",
                                        "content": [{"type": "text", "text": "找到"}],
                                        "details": {"exitCode": 0}, "isError": False}},
    ]
    return _write(path, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))


def _make_openjiuwen_query(path: str, n_tool_turns: int = 2, n_plain_turns: int = 1) -> str:
    """构造 openjiuwen 轨迹（logs/trajectories/<run_id>/query<N>.json，turns[] 顶层 tool_calls[]）。"""
    turns = []
    turn_no = 1
    for i in range(n_tool_turns):
        turns.append({
            "turn": turn_no, "user_input": f"第{turn_no}轮问", "agent_content": f"第{i}轮答",
            "tool_calls": [{"tool": "search", "input": {"q": str(i)}, "output": f"结果{i}"}],
            "files": [], "stop_reason": "complete", "evidence_incomplete": False,
        })
        turn_no += 1
    for _ in range(n_plain_turns):
        turns.append({
            "turn": turn_no, "user_input": "继续", "agent_content": "无工具收尾",
            "tool_calls": [], "files": [], "stop_reason": "complete", "evidence_incomplete": False,
        })
        turn_no += 1
    data = {"query": "Q", "agent_name": "main", "turns": turns,
            "outcome": "done", "evaluations": []}
    return _write(path, json.dumps(data, ensure_ascii=False))


class TestComputeLevel(unittest.TestCase):
    """S5: compute_level 单 authority 判定。"""

    def test_openclaw_gate(self):
        # L1 门槛: openclaw = tool_calls>=3 且 plain_rounds>0
        self.assertEqual(oa.compute_level("openclaw", 3, 1), "L1")
        self.assertEqual(oa.compute_level("openclaw", 5, 2), "L1")
        # 未过门槛 → L0
        self.assertEqual(oa.compute_level("openclaw", 2, 1), "L0")
        self.assertEqual(oa.compute_level("openclaw", 5, 0), "L0")
        self.assertEqual(oa.compute_level("openclaw", 0, 0), "L0")

    def test_hermes_gate(self):
        # hermes 门槛: plain_rounds>0（有产出即可）
        self.assertEqual(oa.compute_level("hermes", 24, 1), "L1")
        self.assertEqual(oa.compute_level("hermes", 0, 1), "L1")
        self.assertEqual(oa.compute_level("hermes", 0, 0), "L0")

    def test_completion_levels(self):
        # L1.5 = L1 + 数值 completion；L2 = >=0.5；L3 = ==1
        self.assertEqual(oa.compute_level("openclaw", 5, 1, completion=None), "L1")
        self.assertEqual(oa.compute_level("openclaw", 5, 1, completion=0.3), "L1.5")
        self.assertEqual(oa.compute_level("openclaw", 5, 1, completion=0.5), "L2")
        self.assertEqual(oa.compute_level("openclaw", 5, 1, completion=0.99), "L2")
        self.assertEqual(oa.compute_level("openclaw", 5, 1, completion=1.0), "L3")
        # 无门槛即便 completion=1 也还是 L0
        self.assertEqual(oa.compute_level("openclaw", 2, 0, completion=1.0), "L0")


class TestDetectHarness(unittest.TestCase):
    """S6: harness 判定按文件布局，不用 harness_home。"""

    def test_hermes_layout(self):
        self.assertEqual(
            oa.detect_harness(["profiles/assistant1/sessions/session_1.json",
                               "profiles/main/sessions/session_2.json"]), "hermes")
        self.assertEqual(oa.detect_harness(["profiles/main/sessions/session_1.json",
                                            "logs/run.log"]), "hermes")

    def test_openclaw_layout(self):
        self.assertEqual(
            oa.detect_harness(["agents/main/sessions/2026-01-01.jsonl",
                               "agents/evaluator/sessions/x.jsonl"]), "openclaw")

    def test_openjiuwen_layout(self):
        self.assertEqual(
            oa.detect_harness(["logs/trajectories/20260910T170528/query1.json",
                               "logs/traj_stats_result.json"]), "openjiuwen")

    def test_empty_fallback(self):
        self.assertIn(oa.detect_harness([]), ("openclaw", "hermes", "openjiuwen", "unknown"))


class TestOpenjiuwenParser(unittest.TestCase):
    """openjiuwen query<N>.json → analyze_openjiuwen_turns 逐轮统计。"""

    def test_synthetic(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openjiuwen_query(os.path.join(d, "query1.json"))
            r = oa.analyze_openjiuwen_turns(p)
            self.assertEqual(r["tool_calls"], 2)
            self.assertEqual(r["plain_rounds"], 1)
            self.assertEqual(r["assistant_rounds"], 3)

    def test_plain_rounds_requires_no_toolcalls(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openjiuwen_query(os.path.join(d, "query1.json"),
                                       n_tool_turns=0, n_plain_turns=2)
            r = oa.analyze_openjiuwen_turns(p)
            self.assertEqual(r["tool_calls"], 0)
            self.assertEqual(r["plain_rounds"], 2)

    def test_gate_openjiuwen_is_not_openclaw(self):
        # openjiuwen 落顶层 tool_calls[]，旧统计常得 0；L1 门槛应「有产出即过」，不是 openclaw 的 ≥3。
        self.assertEqual(oa.compute_level("openjiuwen", 0, 1), "L1")
        self.assertEqual(oa.compute_level("openjiuwen", 2, 1), "L1")
        self.assertEqual(oa.compute_level("openjiuwen", 0, 0), "L0")

    def test_find_and_plan(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openjiuwen_query(
                os.path.join(d, "logs", "trajectories", "20260910T170528", "query1.json"),
                n_tool_turns=2, n_plain_turns=1)
            self.assertEqual(oa.find_openjiuwen_sessions(d), [p])
            self.assertEqual(oa.find_primary_assistant_trajectory(d), p)
            # OBS 对象枚举 → 精确 plan 相对路径
            rel = "logs/trajectories/20260910T170528/query1.json"
            keys = [f"obs://b/x/task_task_30/{rel}", "obs://b/x/task_task_30/workdir/run.log"]
            self.assertEqual(oa._plan_traj_rels(keys, "obs://b/x/task_task_30/"), [rel])

    def test_parse_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openjiuwen_query(os.path.join(d, "query1.json"))
            blocks = oa.parse_openjiuwen_trajectory(p)
            types = [b["part_type"] for b in blocks]
            # 每轮: user(text) → assistant(text) → toolCall(s)；构造 2 工具轮 + 1 纯轮
            self.assertEqual(types.count("toolCall"), 2)
            self.assertEqual(types[0], "text")


class TestHermesParser(unittest.TestCase):
    """S4: hermes 真实会话 → 24/1/25；构造会话 → 2/1/3。"""

    def test_synthetic(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_hermes_session(os.path.join(d, "s.json"))
            r = oa.analyze_hermes_messages(p)
            self.assertEqual(r["tool_calls"], 2)
            self.assertEqual(r["plain_rounds"], 1)
            self.assertEqual(r["assistant_rounds"], 3)

    def test_plain_rounds_requires_no_toolcalls(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_hermes_session(os.path.join(d, "s.json"), n_tool_msgs=0, n_plain=2)
            r = oa.analyze_hermes_messages(p)
            self.assertEqual(r["tool_calls"], 0)
            self.assertEqual(r["plain_rounds"], 2)

    @unittest.skipUnless(os.path.isfile(HERMES_REAL), "真实 hermes 会话探针不存在，跳过")
    def test_real_session_counts(self):
        r = oa.analyze_hermes_messages(HERMES_REAL)
        # 50 消息 = 1 user + 25 assistant + 24 tool；24 次工具调用、1 轮纯文本
        self.assertEqual(r["tool_calls"], 24)
        self.assertEqual(r["plain_rounds"], 1)
        self.assertEqual(r["assistant_rounds"], 25)

    @unittest.skipUnless(os.path.isfile(HERMES_REAL), "真实 hermes 会话探针不存在，跳过")
    def test_real_session_blocks(self):
        blocks = oa.parse_hermes_messages(HERMES_REAL)
        self.assertGreater(len(blocks), 0)
        # 首块是 user/text
        self.assertEqual(blocks[0]["role"], "user")
        # 工具调用块带 tool_name + args
        tc = [b for b in blocks if b["part_type"] == "toolCall"]
        self.assertEqual(len(tc), 24)
        self.assertTrue(all(b.get("tool_name") for b in tc))


class TestOpenclawParser(unittest.TestCase):
    """openclaw 轨迹逐块解析 + 统计。"""

    def test_parse_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openclaw_traj(os.path.join(d, "t.jsonl"))
            blocks = oa.parse_openclaw_trajectory(p)
            self.assertEqual([b["part_type"] for b in blocks],
                             ["text", "thinking", "text", "toolCall", "toolResult", "text"])
            tc = blocks[3]
            self.assertEqual(tc["tool_name"], "search")
            self.assertEqual(tc["args"], '{"q":"kw"}')
            tr = blocks[4]
            self.assertFalse(tr["isError"])
            self.assertEqual(tr["exitCode"], 0)

    def test_parse_blocks_flat_toolcall(self):
        # 真实 oc_memory 子任务：toolCall 扁平 {id,name,arguments}，无 toolCall.function 嵌套
        with tempfile.TemporaryDirectory() as d:
            p = _make_openclaw_traj_flat(os.path.join(d, "t.jsonl"))
            blocks = oa.parse_openclaw_trajectory(p)
            self.assertEqual([b["part_type"] for b in blocks],
                             ["text", "toolCall", "toolResult"])
            tc = blocks[1]
            self.assertEqual(tc["tool_name"], "exec")
            self.assertEqual(tc["args"], {"command": "find / -name 'x.docx'"})

    def test_analyze_counts(self):
        with tempfile.TemporaryDirectory() as d:
            p = _make_openclaw_traj(os.path.join(d, "t.jsonl"))
            r = oa.analyze_trajectory(p)
            self.assertEqual(r["tool_calls"], 1)
            self.assertEqual(r["plain_rounds"], 1)   # 收尾轮无 toolCall
            self.assertEqual(r["assistant_rounds"], 2)

    def test_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(os.path.join(d, "big.jsonl"),
                       "".join('{"type":"message","message":{"role":"assistant","content":[]}}\n'
                               for _ in range(100)))
            blocks = oa.parse_openclaw_trajectory(p, max_lines=10)
            self.assertTrue(any(b["part_type"] == "truncated" for b in blocks))


class TestNestedDiscovery(unittest.TestCase):
    """obsutil cp 到已存在目录会生成 task/<leaf>/ 嵌套布局，find/list 需递归。"""

    def test_nested_openclaw(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "user_p_q1", "agents", "main", "sessions")
            os.makedirs(sub)
            traj = _make_openclaw_traj(os.path.join(sub, "s.jsonl"))
            sess = oa.find_openclaw_sessions(d)
            self.assertEqual(sess, [traj])
            self.assertEqual(oa.find_primary_assistant_trajectory(d), traj)
            self.assertEqual(len(oa.list_task_trajectories(d)), 1)
            self.assertEqual(oa.list_task_trajectories(d)[0]["role"], "assistant")

    def test_nested_hermes(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "task_x", "profiles", "main", "sessions")
            os.makedirs(sub)
            s = _make_hermes_session(os.path.join(sub, "session_1.json"))
            self.assertEqual(oa.find_hermes_sessions(d), [s])
            self.assertEqual(oa.find_primary_assistant_trajectory(d), s)
            self.assertEqual(len(oa.list_task_trajectories(d)), 1)


class TestCacheLayout(unittest.TestCase):
    """S9: 缓存目录布局 —— 丢桶名 + 丢属主段，保留 batch（用户示例逐字断言）。"""

    def test_user_example_layout(self):
        url = ("obs://s3-asset-b-hd-cce-aifm-nlp-exp/zhangchen/"
               "smoke_test_z009213392608211055_oc_traj/acad_000012")
        want = "smoke_test_z009213392608211055_oc_traj/acad_000012"
        self.assertEqual(oa._cache_subdir_for(url), want)
        self.assertEqual(tp.cache_subdir_for(url), want)

    def test_owner_dropped(self):
        self.assertEqual(oa._cache_subdir_for("obs://b/zhangchen/x/y"),
                         "x/y")
        self.assertEqual(tp.cache_subdir_for("obs://b/zhangchen/x/y"),
                         "x/y")

    def test_single_segment_kept(self):
        # 桶内仅 1 段（直接传 batch/任务根，无 owner）→ 整段保留
        self.assertEqual(oa._cache_subdir_for("obs://b/batch_one"), "batch_one")
        self.assertEqual(tp.cache_subdir_for("obs://b/batch_one"), "batch_one")

    def test_traj_rel_maps_batch_path(self):
        # harness_tsr_to_entries 的 trajectory 现在映射为 <batch>/<leaf>/容器相对路径
        d = {"task": "t1",
             "harness_home": "/h/.openclaw",
             "agents": [{"trajectory": "/h/.openclaw/agents/main/sessions/x.jsonl"}]}
        e = tp.harness_tsr_to_entries(d, task_obs="obs://b/zhangchen/batch_x/t1")
        self.assertEqual(e[0]["trajectory"],
                         "batch_x/t1/agents/main/sessions/x.jsonl")
        # 无 task_obs → 旧式 <task>/相对路径（backward compat）
        e2 = tp.harness_tsr_to_entries(d)
        self.assertEqual(e2[0]["trajectory"], "agents/main/sessions/x.jsonl")


class TestObsBaseFromSnapshot(unittest.TestCase):
    """config_snapshot → OBS 根 → 本地缓存目录（logs-only 任务的详情定位兜底）。"""

    SNAP = (
        "global:\n  logger:\n    name: test\n"
        "run_config:\n  harness_type: openjiuwen\n"
        "  obs:\n"
        "    traj_save_bucket: obs://s3-asset-b-hd-cce-aifm-nlp-exp\n"
        "    traj_save_path: openjiuwen_trajs/traj_admin2609101705\n"
    )

    def test_base_from_snapshot(self):
        self.assertEqual(oa.obs_base_from_snapshot(self.SNAP),
                         "obs://s3-asset-b-hd-cce-aifm-nlp-exp/"
                         "openjiuwen_trajs/traj_admin2609101705/")

    def test_base_trailing_slash_normalized(self):
        snap = self.SNAP.replace("traj_admin2609101705",
                                 "openjiuwen_trajs/").replace(
            "traj_save_path: openjiuwen_trajs/openjiuwen_trajs/",
            "traj_save_path: /openjiuwen_trajs/")
        self.assertTrue(oa.obs_base_from_snapshot(snap).endswith("/"))

    def test_base_none_on_missing(self):
        self.assertIsNone(oa.obs_base_from_snapshot(None))
        self.assertIsNone(oa.obs_base_from_snapshot(""))
        self.assertIsNone(oa.obs_base_from_snapshot("run_config: {harness_type: pi}\n"))
        self.assertIsNone(oa.obs_base_from_snapshot("not: [valid: yaml"))

    def test_task_cache_dir_matches_download_rule(self):
        # 与 download_task_detail / _cache_subdir_for 同规则：丢桶名 + 丢属主段。
        # obs_base 恒含属主前缀（<bucket>/<owner>/<batch>/…），故首段总是被丢弃。
        base = "obs://b/owner/batch_x/"
        self.assertEqual(oa.task_cache_dir("/cache", base, "task_task_28204"),
                         os.path.join("/cache", "batch_x", "task_task_28204"))
        # 多级 batch 前缀（属主/多段批次）整段保留在 batch 之后
        base2 = "obs://b/owner/a/b/c/"
        self.assertEqual(oa.task_cache_dir("/cache", base2, "t1"),
                         os.path.join("/cache", "a", "b", "c", "t1"))


class TestExtraLogs(unittest.TestCase):
    """其他日志清单：只列非固定三类，去重主日志副本，大小降序。"""

    def _mk(self, d, rel, content=""):
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_lists_only_non_primary(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk(d, "workdir/run.log", "main")
            self._mk(d, "workdir/gateway.log", "gw")
            self._mk(d, "logs/evaluator_use.log", "eval")
            self._mk(d, "logs/task_task_28204.log", "truncated copy")
            self._mk(d, "task_task_28204/logs/logs/run/jiuwen.log", "j" * 100)
            self._mk(d, "task_task_28204/logs/logs/llm.log", "l" * 50)
            names = [os.path.basename(e["path"])
                     for e in oa.list_extra_logs(d, "task_task_28204")]
            self.assertEqual(names, ["jiuwen.log", "llm.log"])  # size desc
            # 固定三类 + 主日志候选副本均不在列
            self.assertNotIn("run.log", names)
            self.assertNotIn("gateway.log", names)
            self.assertNotIn("evaluator_use.log", names)
            self.assertNotIn("task_task_28204.log", names)

    def test_primary_fallback_also_excluded(self):
        # 无 workdir/run.log 时主日志回退 logs/<task>.log —— 该文件本身也不得出现在其他日志
        with tempfile.TemporaryDirectory() as d:
            self._mk(d, "logs/t1.log", "main via fallback")
            self._mk(d, "logs/run/jiuwen.log", "j" * 10)
            names = [os.path.basename(e["path"])
                     for e in oa.list_extra_logs(d, "t1")]
            self.assertEqual(names, ["jiuwen.log"])

    def test_empty_when_no_extra(self):
        with tempfile.TemporaryDirectory() as d:
            self._mk(d, "workdir/run.log", "main")
            self.assertEqual(oa.list_extra_logs(d, "t1"), [])


class TestLogFallbackGrade(unittest.TestCase):
    """无有效 tsr 时的日志兜底分级：Task_Done → ≥L1.5，家族门不适用，分数抬级。"""

    def _mklog(self, d, body):
        p = os.path.join(d, "task-4.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def test_no_marker_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mklog(d, "[Evaluator] turn=1 agent=main 输出\n")
            self.assertIsNone(oa.log_fallback_grade(p, "openjiuwen"))

    def test_missing_file_returns_none(self):
        self.assertIsNone(oa.log_fallback_grade("/no/such/log", "openjiuwen"))

    def test_marker_no_eval_is_l1_5(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._mklog(d, "【Task_Done】\n")
            e, lv = oa.log_fallback_grade(p, "openjiuwen")
            self.assertEqual(lv, "L1.5")
            self.assertTrue(e["task_done"])
            self.assertTrue(e["passed_gate"])
            self.assertFalse(e["has_eval"])
            self.assertIsNone(e["evaluator_completion"])

    def test_openclaw_family_never_below_l1_5(self):
        # 家族门（tool_calls>=3）在兜底路径不适用：tool_calls 无从统计，不得打回 L0/L1
        with tempfile.TemporaryDirectory() as d:
            p = self._mklog(d, "【Task_Done】\n")
            for ht in ("openclaw", "opencode", "claude-code", "hermes", "openjiuwen"):
                _, lv = oa.log_fallback_grade(p, ht)
                self.assertIn(lv, ("L1.5", "L2", "L3"),
                              msg=f"{ht} 兜底不得低于 L1.5，实际 {lv}")

    def test_score_maps_up_but_floor_l1_5(self):
        body_tpl = (
            "【Task_Done】\n"
            "[Evaluator] turn=1 agent=main 输出\n"
            "{\n"
            '  "completion": %s\n'
            "}\n"
        )
        cases = {"0.3": "L1.5", "0.5": "L2", "0.8": "L2", "1.0": "L3"}
        for sc, want in cases.items():
            with tempfile.TemporaryDirectory() as d:
                p = self._mklog(d, body_tpl % sc)
                e, lv = oa.log_fallback_grade(p, "openclaw")
                self.assertEqual(lv, want, msg=f"score={sc} 期望 {want} 实际 {lv}")
                self.assertTrue(e["has_eval"])
                self.assertEqual(e["verdict_source"], "log")
                self.assertEqual(e["evaluator_completion"], float(sc))

    def test_legacy_marker_requires_success_status(self):
        # 旧版 openclaw harness 用「所有任务执行完成!」；单用不可靠，须与「任务成功」互证
        with tempfile.TemporaryDirectory() as d:
            p = self._mklog(d, "所有任务执行完成!\n")
            self.assertIsNone(oa.log_fallback_grade(p, "openclaw"),
                              "无 status 时不得采信旧标识")
            self.assertIsNone(oa.log_fallback_grade(p, "openclaw", "任务异常"),
                              "非任务成功时不得采信旧标识")
            e, lv = oa.log_fallback_grade(p, "openclaw", "任务成功")
            self.assertEqual(lv, "L1.5")
            self.assertTrue(e["task_done"])

    def test_new_marker_ignores_status(self):
        # 【Task_Done】是权威信号：无需 status 印证，任何 status 下都判完成
        with tempfile.TemporaryDirectory() as d:
            p = self._mklog(d, "【Task_Done】\n")
            for st in (None, "任务成功", "任务异常", "任务失败"):
                fb = oa.log_fallback_grade(p, "openclaw", st)
                self.assertIsNotNone(fb, msg=f"status={st} 下新标识应判完成")
                self.assertEqual(fb[1], "L1.5")


class TestStaleTsr(unittest.TestCase):
    """S8: 过期 stats（task_level "none" / 全零 entry）判 stale，快路径回退。"""

    def test_stale_real(self):
        if not os.path.isfile(HERMES_TSR_STALE):
            self.skipTest("hermes 过期 tsr 探针不存在")
        entries = harness_tsr_to_entries(json.load(open(HERMES_TSR_STALE)), task_obs="obs://b/batch_x/task_y")
        self.assertTrue(len(entries) >= 1)
        self.assertTrue(all(oa._entry_is_stale(e) for e in entries))
        self.assertEqual(entries[0]["trajectory"], None)
        self.assertEqual(entries[0]["tool_calls"], 0)

    def test_valid_real_not_stale(self):
        if not os.path.isfile(OC_TSR_REAL):
            self.skipTest("openclaw tsr 探针不存在")
        entries = harness_tsr_to_entries(json.load(open(OC_TSR_REAL)), task_obs="obs://b/batch_x/task_y")
        self.assertTrue(len(entries) >= 1)
        self.assertFalse(any(oa._entry_is_stale(e) for e in entries))

    def test_entry_is_stale_rules(self):
        self.assertTrue(oa._entry_is_stale({"tool_calls": 0, "plain_rounds": 0,
                                            "trajectory": None}))
        self.assertTrue(oa._entry_is_stale({"tool_calls": 0, "plain_rounds": 0,
                                            "trajectory": "x.jsonl"}))
        self.assertFalse(oa._entry_is_stale({"tool_calls": 5, "plain_rounds": 1,
                                             "trajectory": "x.jsonl"}))


class TestFunnel(unittest.TestCase):
    """批次漏斗比率与汇总。"""

    def test_stats_from_per_task_compact(self):
        from src.traj_pipeline import stats_from_per_task
        per_task = [
            {"task": "t1", "harness": "openclaw", "tool_calls": 5, "plain_rounds": 1,
             "passed_gate": True, "has_eval": True, "evaluator_completion": 0.5,
             "char_len": 100},
            {"task": "t2", "harness": "hermes", "tool_calls": 24, "plain_rounds": 1,
             "passed_gate": True, "has_eval": False, "evaluator_completion": None,
             "char_len": 200},
            {"task": "t3", "harness": "openclaw", "tool_calls": 1, "plain_rounds": 0,
             "passed_gate": False, "has_eval": False, "evaluator_completion": None,
             "char_len": 50},
        ]
        stats = stats_from_per_task(per_task, source_type="workspace")
        comp = oa.stats_from_per_task_compact(stats)
        self.assertEqual(comp["total"], 3)
        self.assertEqual(comp["L1"], 2)          # t1, t2
        self.assertEqual(comp["L1.5"], 1)        # t1 (completion=0.5)
        self.assertEqual(comp["L2"], 1)          # t1 (0.5>=0.5)
        self.assertEqual(comp["L3"], 0)
        self.assertEqual(comp["dropped"], 1)     # t3
        # 逐级通过率（compact 输出 round 到 4 位小数）
        self.assertAlmostEqual(comp["ratio"]["L1/L0"], 2 / 3, places=3)
        self.assertAlmostEqual(comp["ratio"]["L1.5/L1"], 0.5, places=3)
        self.assertEqual(comp["ratio"]["L2/L1.5"], 1)
        self.assertEqual(comp["ratio"]["L3/L2"], 0)


if __name__ == "__main__":
    unittest.main()
