import asyncio
import base64
import tempfile
import unittest
from pathlib import Path

from omegaconf import OmegaConf

import sandbox_log_tap as tap


class SandboxLogExporterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = tap.ExportSettings(
            env_id="env-test",
            log_name="run.log",
            sandbox_type="x86_cpu",
            remote_path="/logs/run.log",
            local_path=Path(self.temp_dir.name) / "log_exports" / "env-test" / "run.log",
        )

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_snapshot_exports_complete_file_in_chunks(self):
        content = ("first\n第二行\nlast\n" * 100).encode("utf-8")
        calls = []
        original = tap._read_remote_chunk

        async def fake_read(client, settings, next_byte, max_bytes):
            calls.append((next_byte, max_bytes))
            data = content[next_byte:next_byte + max_bytes]
            return tap.RemoteChunk(True, len(content), next_byte, data)

        tap._read_remote_chunk = fake_read
        try:
            result = await tap.export_with_client(
                object(), self.settings, chunk_bytes=1024
            )
        finally:
            tap._read_remote_chunk = original

        self.assertEqual(result.read_bytes(), content)
        self.assertGreater(len(calls), 1)
        self.assertEqual(calls[0][0], 0)

    async def test_initial_snapshot_has_fixed_boundary_then_follow_appends(self):
        initial = b"initial-complete\n"
        appended = b"later\n"
        remote = bytearray(initial)
        snapshot_done = asyncio.Event()
        stop = asyncio.Event()
        original = tap._read_remote_chunk

        async def fake_read(client, settings, next_byte, max_bytes):
            data = bytes(remote[next_byte:next_byte + max_bytes])
            return tap.RemoteChunk(True, len(remote), next_byte, data)

        async def progress(event):
            if event["event"] == "chunk" and event["local_size"] == len(initial):
                snapshot_done.set()
            if event["event"] == "chunk" and event["local_size"] == len(initial) + len(appended):
                stop.set()

        tap._read_remote_chunk = fake_read
        task = asyncio.create_task(tap.export_with_client(
            object(), self.settings, follow=True, chunk_bytes=1024,
            poll_interval=0.01, stop_event=stop, progress=progress,
        ))
        try:
            await asyncio.wait_for(snapshot_done.wait(), timeout=1)
            remote.extend(appended)
            result = await asyncio.wait_for(task, timeout=1)
        finally:
            tap._read_remote_chunk = original
            if not task.done():
                task.cancel()

        self.assertEqual(result.read_bytes(), initial + appended)

    async def test_missing_snapshot_log_fails_without_empty_file(self):
        original = tap._read_remote_chunk

        async def fake_read(client, settings, next_byte, max_bytes):
            return tap.RemoteChunk(False, 0, 0, b"")

        tap._read_remote_chunk = fake_read
        try:
            with self.assertRaises(tap.LogExportError):
                await tap.export_with_client(object(), self.settings)
        finally:
            tap._read_remote_chunk = original

        self.assertFalse(self.settings.local_path.exists())

    async def test_execute_probe_supports_sync_exec_command(self):
        class SyncOnlyClient:
            def exec_command(self, command, timeout_seconds):
                self.call = (command, timeout_seconds)
                return {
                    "response": {
                        "exit_code": 0,
                        "stdout": "probe-output",
                        "stderr": "",
                    }
                }

        client = SyncOnlyClient()
        stdout = await tap._execute_probe(client, ["echo", "test"])
        self.assertEqual(stdout, "probe-output")
        self.assertEqual(client.call, (["echo", "test"], 30))

    async def test_export_attaches_by_env_id_and_never_closes_sandbox(self):
        config_path = Path(self.temp_dir.name) / "demo.yaml"
        config_path.write_text(
            "sandbox:\n  x86_cpu: {}\n"
            "run_config:\n"
            "  sandbox:\n    result_workdir: /workdir\n"
            "  task:\n    task_output_path: outputs\n",
            encoding="utf-8",
        )

        class FakeClient:
            def __init__(self):
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1

        fake_client = FakeClient()
        attached = []
        original_attach = tap._attach_client
        original_read = tap._read_remote_chunk

        async def fake_attach(env_id, config):
            attached.append((env_id, config.global_ if "global_" in config else None))
            return fake_client

        async def fake_read(client, settings, next_byte, max_bytes):
            return tap.RemoteChunk(True, 3, next_byte, b"abc"[next_byte:])

        tap._attach_client = fake_attach
        tap._read_remote_chunk = fake_read
        try:
            result = await tap.export_sandbox_log(
                config_path=config_path,
                env_id="env-test",
                output_dir=Path(self.temp_dir.name) / "exports",
            )
        finally:
            tap._attach_client = original_attach
            tap._read_remote_chunk = original_read

        self.assertEqual(attached[0][0], "env-test")
        self.assertEqual(result.read_bytes(), b"abc")
        self.assertEqual(fake_client.close_calls, 0)


class SettingsAndProbeTest(unittest.TestCase):
    def test_resolve_linux_paths_and_default_output(self):
        config = OmegaConf.create({
            "sandbox": {"x86_cpu": {}},
            "run_config": {
                "sandbox": {"result_log": "custom.log"},
                "task": {"task_output_path": "my-outputs"},
            },
        })
        settings = tap.resolve_export_settings(
            config,
            config_path="configs/demo.yaml",
            env_id="zx-123",
            log_name="run.log",
        )
        self.assertEqual(settings.remote_path, "/home/ma-user/workspace/workdir/custom.log")
        self.assertEqual(
            settings.local_path,
            (Path("my-outputs") / "demo" / "log_exports" / "zx-123" / "run.log").resolve(),
        )

    def test_rejects_unsafe_env_id(self):
        with self.assertRaises(tap.LogExportError):
            tap.resolve_export_settings(
                OmegaConf.create({}), config_path="config.yaml",
                env_id="../escape", log_name="run.log",
            )

    def test_rejects_legacy_log_name(self):
        with self.assertRaises(tap.LogExportError):
            tap.resolve_export_settings(
                OmegaConf.create({}), config_path="config.yaml",
                env_id="zx-123", log_name="run",
            )

    def test_parse_probe(self):
        payload = base64.b64encode(b"abc").decode("ascii")
        parsed = tap._parse_probe(tap.PROBE_PREFIX + f"1:9:3:{payload}")
        self.assertTrue(parsed.exists)
        self.assertEqual(parsed.size, 9)
        self.assertEqual(parsed.start, 3)
        self.assertEqual(parsed.data, b"abc")
        self.assertEqual(parsed.next_byte, 6)


if __name__ == "__main__":
    unittest.main()


class SandboxGoneTest(unittest.IsolatedAsyncioTestCase):
    """沙箱在导出过程中关闭时的行为。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = tap.ExportSettings(
            env_id="env-gone",
            log_name="run.log",
            sandbox_type="x86_cpu",
            remote_path="/logs/run.log",
            local_path=Path(self.temp_dir.name) / "log_exports" / "env-gone" / "run.log",
        )
        self.original = tap._read_remote_chunk

    async def asyncTearDown(self):
        tap._read_remote_chunk = self.original
        self.temp_dir.cleanup()

    def _fake_read(self, content: bytes, fail_after: int):
        """读满 content 后第 fail_after 次调用起模拟沙箱消失。"""
        state = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            state["n"] += 1
            if state["n"] > fail_after:
                raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")
            data = content[next_byte:next_byte + max_bytes]
            return tap.RemoteChunk(True, len(content), next_byte, data)

        return fake_read, state

    async def test_readtimeout_is_classified_as_sandbox_unreachable(self):
        """httpx.ReadTimeout 必须被归一成 SandboxUnreachable，不能穿透。"""
        httpx = __import__("httpx")

        class _Client:
            async def async_exec_command(self, command, timeout_seconds=None):
                raise httpx.ReadTimeout("")

        with self.assertRaises(tap.SandboxUnreachable):
            await tap._execute_probe(_Client(), ["echo", "x"], 1.0)

        # 这是原来 bug 的核心：ReadTimeout 不属于 CLI 捕获的那几类。
        self.assertFalse(issubclass(httpx.ReadTimeout, (OSError, ValueError)))
        self.assertTrue(issubclass(tap.SandboxUnreachable, tap.LogExportError))

    async def test_follow_finishes_gracefully_when_sandbox_closes(self):
        content = b"complete-run-log\n" * 10
        fake_read, _state = self._fake_read(content, fail_after=2)
        tap._read_remote_chunk = fake_read

        events = []
        result = await tap.export_with_client(
            object(), self.settings, follow=True, chunk_bytes=1024,
            poll_interval=0.01, progress=lambda e: events.append(e),
        )

        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertEqual(done["reason"], "sandbox_gone")
        self.assertIn("ReadTimeout", done["detail"])
        # 已经读到的完整日志必须保留。
        self.assertEqual(result.read_bytes(), content)

    async def test_unreachable_before_complete_snapshot_still_fails(self):
        """一个字节都没读到就连不上 —— 这是真失败，不能伪装成成功。"""
        async def fake_read(client, settings, next_byte, max_bytes):
            raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")

        tap._read_remote_chunk = fake_read
        with self.assertRaises(tap.SandboxUnreachable):
            await tap.export_with_client(
                object(), self.settings, follow=True, poll_interval=0.01,
            )
        self.assertFalse(self.settings.local_path.exists())

    async def test_transient_blip_is_retried_not_treated_as_gone(self):
        """一次抖动之后沙箱还活着，follow 应该继续而不是收尾。"""
        content = b"abc\n"
        state = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            state["n"] += 1
            if state["n"] == 1:
                raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")
            return tap.RemoteChunk(True, len(content), next_byte,
                                   content[next_byte:next_byte + max_bytes])

        tap._read_remote_chunk = fake_read
        result = await tap.export_with_client(
            object(), self.settings, follow=False,
        )
        self.assertEqual(result.read_bytes(), content)
        self.assertGreaterEqual(state["n"], 2)


class ControlPlaneStateTest(unittest.IsolatedAsyncioTestCase):
    """以控制面状态为准判定沙箱是否关闭。"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = tap.ExportSettings(
            env_id="env-cp", log_name="run.log", sandbox_type="x86_cpu",
            remote_path="/logs/run.log",
            local_path=Path(self.temp_dir.name) / "log_exports" / "env-cp" / "run.log",
        )
        self.original = tap._read_remote_chunk

    async def asyncTearDown(self):
        tap._read_remote_chunk = self.original
        self.temp_dir.cleanup()

    @staticmethod
    def _state(status, detail=""):
        async def check():
            return tap.SandboxState(status, detail)
        return check

    def test_sandbox_state_terminal_classification(self):
        self.assertTrue(tap.SandboxState("closed").is_terminal)
        self.assertTrue(tap.SandboxState("deleted", "sandbox_not_found").is_terminal)
        self.assertFalse(tap.SandboxState("running").is_terminal)
        # 问不出来 != 已关闭，这是重试判定的关键。
        self.assertFalse(tap.SandboxState("").is_terminal)
        self.assertFalse(tap.SandboxState("").is_known)

    async def test_closed_sandbox_skips_retries(self):
        """控制面说 closed，就不该再浪费重试次数。"""
        calls = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            calls["n"] += 1
            raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")

        tap._read_remote_chunk = fake_read
        with self.assertRaises(tap.SandboxClosed):
            await tap._read_chunk_with_retry(
                object(), self.settings, 0, 1024,
                retries=5, stop_event=asyncio.Event(),
                state_check=self._state("closed", "close_reason=manual_close"),
            )
        self.assertEqual(calls["n"], 1)

    async def test_running_sandbox_still_retries(self):
        """控制面说 running，说明是抖动，重试次数必须照用。"""
        calls = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            calls["n"] += 1
            raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")

        tap._read_remote_chunk = fake_read
        with self.assertRaises(tap.SandboxUnreachable) as ctx:
            await tap._read_chunk_with_retry(
                object(), self.settings, 0, 1024,
                retries=2, stop_event=asyncio.Event(),
                state_check=self._state("running"),
            )
        self.assertNotIsInstance(ctx.exception, tap.SandboxClosed)
        self.assertEqual(calls["n"], 3)  # 首次 + 2 次重试

    async def test_idle_follow_never_queries_control_plane(self):
        """follow 空闲轮询不产生控制面请求 —— 只在探测失败时才查。"""
        content = b"steady-log\n"
        probes = {"n": 0}
        state_calls = {"n": 0}
        stop = asyncio.Event()

        async def fake_read(client, settings, next_byte, max_bytes):
            probes["n"] += 1
            if probes["n"] >= 4:       # 空转几轮后收手，避免测试挂住
                stop.set()
            return tap.RemoteChunk(True, len(content), next_byte,
                                   content[next_byte:next_byte + max_bytes])

        async def counting_state():
            state_calls["n"] += 1
            return tap.SandboxState("running")

        tap._read_remote_chunk = fake_read
        await tap.export_with_client(
            object(), self.settings, follow=True, poll_interval=0.01,
            stop_event=stop, state_check=counting_state,
        )
        self.assertGreaterEqual(probes["n"], 4)   # 确实空转了几轮
        self.assertEqual(state_calls["n"], 0)     # 但一次控制面都没问

    async def test_closed_detected_on_probe_failure_finishes_cleanly(self):
        """探测失败 + 控制面确认关闭 -> reason=sandbox_closed，日志保留。"""
        content = b"done-log\n"
        probes = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            probes["n"] += 1
            if probes["n"] > 1:
                raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")
            return tap.RemoteChunk(True, len(content), next_byte,
                                   content[next_byte:next_byte + max_bytes])

        tap._read_remote_chunk = fake_read
        events = []
        result = await tap.export_with_client(
            object(), self.settings, follow=True, poll_interval=0.01,
            progress=lambda e: events.append(e),
            state_check=self._state("closed", "close_reason=manual_close"),
        )

        done = events[-1]
        self.assertEqual(done["reason"], "sandbox_closed")
        self.assertIn("close_reason=manual_close", done["detail"])
        self.assertEqual(result.read_bytes(), content)
        # 控制面给了确定答案，不该再消耗重试：第 2 次探测失败后就收尾。
        self.assertEqual(probes["n"], 2)

    async def test_unknown_state_falls_back_to_heuristic(self):
        """控制面问不出来时，退回原来的启发式，reason 标成 sandbox_gone。"""
        content = b"partial-but-complete\n"
        state = {"n": 0}

        async def fake_read(client, settings, next_byte, max_bytes):
            state["n"] += 1
            if state["n"] > 1:
                raise tap.SandboxUnreachable("sandbox is unreachable (ReadTimeout)")
            return tap.RemoteChunk(True, len(content), next_byte,
                                   content[next_byte:next_byte + max_bytes])

        tap._read_remote_chunk = fake_read
        events = []
        result = await tap.export_with_client(
            object(), self.settings, follow=True, poll_interval=0.01,
            progress=lambda e: events.append(e),
            state_check=self._state(""),
        )
        self.assertEqual(events[-1]["reason"], "sandbox_gone")
        self.assertEqual(result.read_bytes(), content)

    async def test_state_check_failure_never_propagates(self):
        """状态查询自己抛异常，不能变成新的失败来源。"""
        async def boom():
            raise RuntimeError("controlplane unreachable")

        got = await tap._query_state(boom)
        self.assertEqual(got.status, "")
        self.assertIn("controlplane unreachable", got.detail)
