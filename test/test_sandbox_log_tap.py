import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import sandbox_log_tap as tap


class _FakeClient:
    async def extend(self, args):  # pragma: no cover - probe is replaced in these tests
        raise AssertionError("unexpected real sandbox request")


class SandboxLogTapTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = tap.SandboxLogService(self.temp_dir.name)
        await self.service.start()
        self.service.register(
            env_id="env-test",
            task_idx=7,
            config_name="task-seven.json",
            client=_FakeClient(),
            sandbox_type="x86_cpu",
            run_log_path="/logs/run.log",
            gateway_log_path="/logs/gateway.log",
        )

    async def asyncTearDown(self):
        await self.service.close()
        self.temp_dir.cleanup()

    async def test_idle_service_does_not_read_sandbox_and_snapshot_writes_local_file(self):
        content = "第一行\nsecond line\n".encode("utf-8")
        calls = []
        original = tap._read_remote_chunk

        async def fake_read(handle, path, cursor, max_bytes):
            calls.append((handle.env_id, path, cursor))
            start = max(0, cursor)
            data = content[start:start + 7]
            return tap.RemoteChunk(True, len(content), start, data)

        # 服务启动和任务注册本身不能访问沙箱。
        self.assertEqual(calls, [])
        tap._read_remote_chunk = fake_read
        try:
            events = []
            async for event in tap.iter_log_request(
                self.service.control_file,
                task_idx=7,
                log="run",
            ):
                events.append(event)
        finally:
            tap._read_remote_chunk = original

        self.assertGreater(len(calls), 1)
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual(events[-1]["event"], "done")
        local_path = Path(events[-1]["local_path"])
        self.assertEqual(local_path.read_bytes(), content)
        streamed = "".join(
            event.get("text", "") for event in events if event["event"] == "chunk"
        )
        self.assertEqual(streamed, content.decode("utf-8"))

    async def test_list_returns_only_active_sandboxes(self):
        events = []
        async for event in tap.iter_log_request(
            self.service.control_file,
            action="list",
        ):
            events.append(event)
        self.assertEqual(events[0]["event"], "tasks")
        self.assertEqual(events[0]["tasks"][0]["env_id"], "env-test")
        self.assertEqual(events[0]["tasks"][0]["task_idx"], 7)

        await self.service.unregister("env-test")
        events = []
        async for event in tap.iter_log_request(
            self.service.control_file,
            action="list",
        ):
            events.append(event)
        self.assertEqual(events[0]["tasks"], [])

    async def test_rejects_non_allowlisted_sandbox_path(self):
        metadata = json.loads(self.service.control_file.read_text(encoding="utf-8"))
        reader, writer = await asyncio.open_connection(metadata["host"], metadata["port"])
        writer.write((json.dumps({
            "token": metadata["token"],
            "action": "fetch",
            "task_idx": 7,
            "log": "run",
            "sandbox_path": "/etc/passwd",
        }) + "\n").encode("utf-8"))
        await writer.drain()
        response = json.loads((await reader.readline()).decode("utf-8"))
        writer.close()
        await writer.wait_closed()
        self.assertEqual(response["event"], "error")
        self.assertIn("allowlist", response["error"])

    async def test_unregister_stops_active_follow_before_client_close(self):
        first_read = asyncio.Event()
        original = tap._read_remote_chunk

        async def fake_read(handle, path, cursor, max_bytes):
            first_read.set()
            return tap.RemoteChunk(True, 0, 0, b"")

        async def consume():
            return [
                event
                async for event in tap.iter_log_request(
                    self.service.control_file,
                    task_idx=7,
                    log="run",
                    follow=True,
                )
            ]

        tap._read_remote_chunk = fake_read
        consumer = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(first_read.wait(), timeout=1)
            await self.service.unregister("env-test")
            events = await asyncio.wait_for(consumer, timeout=1)
        finally:
            tap._read_remote_chunk = original
            if not consumer.done():
                consumer.cancel()
        self.assertNotIn("env-test", self.service.handles)
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["reason"], "task_closing")

    async def test_control_file_is_kept_as_stopped_and_overwritten_on_restart(self):
        control_file = self.service.control_file
        first = json.loads(control_file.read_text(encoding="utf-8"))
        self.assertEqual(first["status"], "running")

        await self.service.close()
        stopped = json.loads(control_file.read_text(encoding="utf-8"))
        self.assertEqual(stopped["status"], "stopped")

        with self.assertRaises(tap.LogTapError):
            async for _event in tap.iter_log_request(control_file, action="list"):
                pass

        self.service = tap.SandboxLogService(self.temp_dir.name)
        await self.service.start()
        restarted = json.loads(control_file.read_text(encoding="utf-8"))
        self.assertEqual(restarted["status"], "running")
        self.assertNotEqual(restarted["token"], first["token"])


class ProbeParserTest(unittest.TestCase):
    def test_parse_probe(self):
        payload = tap.PROBE_PREFIX + "1:9:3:" + "YWJj"
        parsed = tap._parse_probe(payload)
        self.assertTrue(parsed.exists)
        self.assertEqual(parsed.size, 9)
        self.assertEqual(parsed.start, 3)
        self.assertEqual(parsed.data, b"abc")
        self.assertEqual(parsed.next_cursor, 6)


if __name__ == "__main__":
    unittest.main()
