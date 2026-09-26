import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import tc  # noqa: E402


class TaskCheckpointRegressionTests(unittest.TestCase):
    def make_api(self):
        temp = tempfile.TemporaryDirectory()
        workspace = Path(temp.name) / "workspace"
        workspace.mkdir()
        api = tc.TaskCheckpoint(workspace)
        api.init(str(workspace), "test")
        self.addCleanup(temp.cleanup)
        return api, workspace

    def test_restore_never_deletes_new_sensitive_file(self):
        api, workspace = self.make_api()
        (workspace / "tracked.txt").write_text("before", encoding="utf-8")
        api.save(str(workspace), "initial")
        (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")

        preview = api.restore(str(workspace), index=1, apply=False)

        self.assertNotIn(".env", preview["plan"]["to_delete"])
        self.assertIn(".env", preview["plan"]["unrestorable"])
        api.restore(str(workspace), index=1, apply=True)
        self.assertTrue((workspace / ".env").exists())

    def test_save_semantics_are_part_of_idempotency(self):
        api, workspace = self.make_api()
        (workspace / "tracked.txt").write_text("same", encoding="utf-8")

        first = api.save(str(workspace), "step", conclusion="first", next_step="one")
        second = api.save(str(workspace), "step", conclusion="second", next_step="two")

        self.assertFalse(second["idempotent"])
        self.assertEqual(second["index"], first["index"] + 1)
        self.assertEqual(api.resume(str(workspace))["last"]["conclusion"], "second")

    def test_compress_preserves_payloads_referenced_by_other_tasks(self):
        store = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(store, ignore_errors=True))
        workspace_a = store.parent / (store.name + "-a")
        workspace_b = store.parent / (store.name + "-b")
        workspace_a.mkdir()
        workspace_b.mkdir()
        self.addCleanup(lambda: shutil.rmtree(workspace_a, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(workspace_b, ignore_errors=True))
        api_a = tc.TaskCheckpoint(workspace_a)
        api_b = tc.TaskCheckpoint(workspace_b)
        api_a.init(str(workspace_a), "a", store=str(store))
        api_b.init(str(workspace_b), "b", store=str(store))
        (workspace_a / "same.txt").write_text("shared payload", encoding="utf-8")
        (workspace_b / "same.txt").write_text("shared payload", encoding="utf-8")
        api_a.save(str(workspace_a), "a-step")
        api_b.save(str(workspace_b), "b-step")

        info = api_a.compress(keep_seconds=0, minimum=0, maximum=0)
        payloads = list((store / "payload").glob("*/*"))

        self.assertGreaterEqual(info["eligible"], 0)
        self.assertTrue(payloads)
        self.assertEqual(api_b.restore(str(workspace_b), index=1, apply=False)["plan"]["unrestorable"], [])

    def test_mcp_stdio_initialize_and_tools_list(self):
        env = os.environ.copy()
        env["TC_WATCH"] = "off"
        request = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "",
        ])
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "tc_mcp.py")],
            input=request,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(replies[1]["result"]["tools"]), 9)


if __name__ == "__main__":
    unittest.main()
