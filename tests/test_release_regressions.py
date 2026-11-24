"""Regression tests for the v0.2.2 review findings."""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from a2a_hub import cli
from a2a_hub.adapters.base import Adapter, CallResult
from a2a_hub.core.hub_app import Hub
from a2a_hub.core.registry import AgentRecord, Registry
from a2a_hub.core.router import Router
from a2a_hub.core.store import Store


class Recorder(Adapter):
    async def probe(self):
        return "ok"

    async def call(self, prompt, *, context_id, session_id, timeout):
        self.calls.append((prompt, context_id, session_id, timeout))
        if prompt in ("error", "expired") and session_id:
            return CallResult(ok=False, error="request failed", session_invalid=prompt == "expired")
        return CallResult(ok=prompt != "fail", text="output", session_id="saved")


def step(name, prompt=None, **options):
    return dict(id=name, prompt=prompt or name, agent="stub", **options)


class PlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / "test.db")
        self.addCleanup(self.store.close)
        reg = Registry(self.store)
        self.adapter = Recorder("stub")
        self.adapter.calls = []
        reg.register(AgentRecord(name="stub", kind="test"))
        self.hub = Hub(self.store, reg, Router(reg), {"stub": self.adapter},
                       workspace_dir=str(self.root))

    async def test_contexts_and_replay(self):
        params = {"steps": [step("a")]}
        await self.hub._run_plan(params)
        await self.hub._run_plan({**params, "planId": "unique"})
        with self.assertRaisesRegex(ValueError, "planId"):
            await self.hub._run_plan({**params, "planId": "unique"})
        first, second = self.adapter.calls
        self.assertNotEqual(first[1], second[1])
        self.assertIsNone(second[2])
        self.assertEqual(len(self.store.list_plan_tasks("unique")), 1)
        await self.hub._run_plan({"steps": [
            step("a", sharedContext=True), step("b", dependsOn=["a"], sharedContext=True)
        ]})
        self.assertEqual(self.adapter.calls[-1][2], "saved")

    async def test_dependencies_and_timeouts(self):
        for invalid in (0, -1, "nan", "bad"):
            with self.assertRaises(ValueError):
                await self.hub._run_plan({"steps": [step("bad", timeout=invalid)]})
        self.assertEqual(self.store.count_tasks(), 0)
        run = await self.hub._run_plan({"steps": [
            step("a", "fail", onError="continue"), step("x"),
            step("b", dependsOn=["a"]), step("c", dependsOn=["x"], timeout=13),
        ]})
        states = {r["step_id"]: r["state"] for r in self.store.list_plan_tasks(run["planId"])}
        self.assertEqual(states, {"a": "failed", "b": "canceled", "x": "completed", "c": "completed"})
        self.assertIsNone(self.adapter.calls[0][3])
        self.assertEqual(self.adapter.calls[-1][3], 13)

    async def test_retry_only_explicit_expiry(self):
        for prompt, attempts in [("error", 1), ("expired", 2)]:
            self.store.set_context_session("ctx", "stub", "old")
            self.adapter.calls.clear()
            await self.hub.dispatch_task(prompt, context_id="ctx", agent="stub")
            self.assertEqual(len(self.adapter.calls), attempts)

    async def test_queued_cancel_finishes_stream_and_preserves_state(self):
        task_id = self.store.create_task(context_id="ctx", state="submitted")["id"]
        stream = (await self.hub._send_streaming_message("subscriber", {"taskId": task_id})).body_iterator
        with patch("a2a_hub.core.hub_app.SSE_HEARTBEAT_SECONDS", 0.02):
            self.assertIn("keep-alive", await anext(stream))
            await self.hub._cancel_task({"id": task_id})
            self.assertIn("TASK_STATE_CANCELED", await asyncio.wait_for(anext(stream), 2))
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
        await self.hub.dispatch_task("queued", agent="gone", task_id=task_id)
        self.assertEqual(self.store.get_task(task_id)["state"], "canceled")

    async def test_artifact_failure_stops_dependents(self):
        with patch.object(self.store, "add_artifact", side_effect=OSError("write failed")):
            run = await self.hub._run_plan({"steps": [step("a"), step("b", "{{steps.a}}")]})
        self.assertFalse(run["ok"])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.store.get_task(run["steps"][0]["taskId"])["state"], "failed")

    async def test_persisted_card_and_updated_route(self):
        def record(port, path):
            url = f"http://127.0.0.1:{port}"
            return AgentRecord(name="http", kind="a2a_http", endpoint=url,
                               card={"supportedInterfaces": [{"url": url + path}]})
        self.hub.registry.register(record(8101, "/rpc"))
        with patch.object(cli, "ROOT", self.root):
            hub = cli.build_hub(self.root / "test.db")
        self.addCleanup(hub.store.close)
        for port, path in [(8101, "/rpc"), (8101, "/other"), (8102, "/rpc")]:
            rec = record(port, path)
            hub.registry.register(rec)
            self.assertEqual(hub._get_adapter(hub.registry.get("http")).rpc_url, rec.endpoint + path)


class ServerTests(unittest.TestCase):
    def test_second_server_leaves_active_tasks_alone(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
            reserve.close()
            db = Path(tmp) / "hub.db"
            cmd = [sys.executable, str(ROOT / "hub.py"), "--db", str(db), "serve", "--host", "127.0.0.1"]
            env = {**os.environ, "HUB_TOKEN": ""}
            proc = subprocess.Popen(cmd + ["--port", str(port)], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("first server did not start")
                store = Store(db)
                try:
                    tid = store.create_task(context_id="test", state="working")["id"]
                    for other_port in (port, 0):
                        second = subprocess.run(cmd + ["--port", str(other_port)], env=env,
                                                capture_output=True, timeout=10)
                        self.assertNotEqual(second.returncode, 0)
                        self.assertEqual(store.get_task(tid)["state"], "working")
                finally:
                    store.close()
            finally:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
