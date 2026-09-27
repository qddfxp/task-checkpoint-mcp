"""阶段一验收。只使用标准库。运行：python -m unittest tests.test_tc"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import tc
import tc_mcp
from tc import TaskCheckpoint, init_logger


def write_file(root: Path, name: str, content: str = "hello") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def active_task_id(checkpoints: Path) -> str | None:
    """读 current.json 的活动指针表。活动指针按 root 分键，单工作区只有一项。"""
    table = json.loads((checkpoints / "current.json").read_text(encoding="utf-8"))
    active = table.get("active")
    if isinstance(active, dict) and len(active) == 1:
        return next(iter(active.values()))
    return table.get("active_task_id")


class Phase1(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.api = TaskCheckpoint(self.root)
        store = self.root / ".checkpoints"
        store.mkdir(exist_ok=True)
        init_logger(store)
        self.api.logger = store
        self.init = self.api.init(str(self.root), "test-task", goal="do stuff", constraints="none")
        self.tid = self.init["task_id"]

    def tearDown(self):
        self._tmp.cleanup()

    def test_01_three_saves(self):
        write_file(self.root, "a.txt", "aaa")
        r1 = self.api.save(str(self.root), "step 1")
        write_file(self.root, "b.txt", "bbb")
        r2 = self.api.save(str(self.root), "step 2")
        write_file(self.root, "c.txt", "ccc")
        r3 = self.api.save(str(self.root), "step 3")
        self.assertEqual([r1["index"], r2["index"], r3["index"]], [1, 2, 3])
        show = self.api.show(str(self.root))
        self.assertEqual([s["index"] for s in show["steps"]], [1, 2, 3])
        self.assertEqual([s["title"] for s in show["steps"]], ["step 1", "step 2", "step 3"])

    def test_02_preview_does_not_change_files(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        write_file(self.root, "b.txt", "b1")
        self.api.save(str(self.root), "s2")
        write_file(self.root, "c.txt", "c1")
        self.api.save(str(self.root), "s3")
        plan = self.api.restore(str(self.root), index=2, apply=False)
        self.assertFalse(plan["applied"])
        self.assertIsNone(plan["recovered"])
        self.assertEqual((self.root / "c.txt").read_text(encoding="utf-8"), "c1")
        drifts = self.root / ".checkpoints" / "tasks" / self.tid / "drifts"
        kinds = []
        if drifts.exists():
            for path in drifts.glob("*.json"):
                kinds.append(json.loads(path.read_text(encoding="utf-8")).get("kind"))
        self.assertNotIn("recovered", kinds)

    def test_03_apply_deletes_later_file_and_records_recovered(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        write_file(self.root, "b.txt", "b1")
        self.api.save(str(self.root), "s2")
        write_file(self.root, "c.txt", "c1")
        self.api.save(str(self.root), "s3")
        result = self.api.restore(str(self.root), index=2, apply=True)
        self.assertTrue(result["applied"])
        self.assertFalse((self.root / "c.txt").exists())
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "a1")
        rec = self.root / ".checkpoints" / "tasks" / self.tid / "drifts" / f"{result['recovered']}.json"
        self.assertTrue(rec.exists())

    def test_04_restore_forward_brings_file_back(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        write_file(self.root, "b.txt", "b1")
        self.api.save(str(self.root), "s2")
        write_file(self.root, "c.txt", "c1")
        self.api.save(str(self.root), "s3")
        self.api.restore(str(self.root), index=2, apply=True)
        self.api.restore(str(self.root), index=3, apply=True)
        self.assertEqual((self.root / "c.txt").read_text(encoding="utf-8"), "c1")

    def test_05_restore_across_several_steps(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        write_file(self.root, "b.txt", "b1")
        self.api.save(str(self.root), "s2")
        for name in ("c.txt", "d.txt", "e.txt"):
            write_file(self.root, name, "x")
            self.api.save(str(self.root), f"s_{name}")
        self.api.restore(str(self.root), index=2, apply=True)
        for name in ("c.txt", "d.txt", "e.txt"):
            self.assertFalse((self.root / name).exists(), name)

    def test_06_resume_reports_drift_without_writing(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1", conclusion="done first")
        write_file(self.root, "uncommitted.txt", "uc")
        fresh = TaskCheckpoint(self.root)
        before = list((self.root / ".checkpoints" / "tasks" / self.tid / "drifts").glob("*.json")) if (
            self.root / ".checkpoints" / "tasks" / self.tid / "drifts"
        ).exists() else []
        resume = fresh.resume(str(self.root))
        after = list((self.root / ".checkpoints" / "tasks" / self.tid / "drifts").glob("*.json")) if (
            self.root / ".checkpoints" / "tasks" / self.tid / "drifts"
        ).exists() else []
        self.assertEqual(resume["head"], 1)
        self.assertEqual(resume["goal"], "do stuff")
        self.assertIn("uncommitted.txt", resume["drift_paths"])
        self.assertEqual(resume["last"]["conclusion"], "done first")
        self.assertEqual(before, after)

    def test_07_recovered_can_undo_restore(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        write_file(self.root, "b.txt", "b1")
        self.api.save(str(self.root), "s2")
        write_file(self.root, "c.txt", "c1")
        self.api.save(str(self.root), "s3")
        result = self.api.restore(str(self.root), index=2, apply=True)
        self.assertFalse((self.root / "c.txt").exists())
        self.api.restore(str(self.root), drift_id=result["recovered"], apply=True)
        self.assertEqual((self.root / "c.txt").read_text(encoding="utf-8"), "c1")

    def test_08_switch_to_closed_is_rejected(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1", close=True)
        with self.assertRaises(PermissionError):
            self.api.switch(str(self.root), self.tid)
        self.assertEqual(active_task_id(self.root / ".checkpoints"), self.tid)
        self.assertEqual(self.api._state(self.tid)["status"], "closed")

    def test_09_same_title_same_content_is_idempotent(self):
        write_file(self.root, "a.txt", "x")
        first = self.api.save(str(self.root), "same title")
        second = self.api.save(str(self.root), "same title")
        self.assertEqual(second["index"], first["index"])
        write_file(self.root, "a.txt", "y")
        third = self.api.save(str(self.root), "same title")
        self.assertEqual(third["index"], first["index"] + 1)

    def test_10_missing_companion_is_reported_not_fatal(self):
        write_file(self.root, "m.p3d", "data")
        # 配套缺失只是上报，不能中断整个 save
        saved = self.api.save(str(self.root), "missing companion")
        self.assertEqual(saved["groups"][0]["status"], "missing")
        self.assertEqual(saved["groups"][0]["companion"], "m.p3dat")
        (self.root / "m.p3dat").mkdir()
        write_file(self.root, "m.p3dat/info.txt", "info")
        saved = self.api.save(str(self.root), "with group")
        self.assertEqual(saved["groups"][0]["status"], "included")

    def test_10b_unrelated_p3d_does_not_block_save(self):
        """通用工作区里一个孤立的 .p3d 不能把存档搞挂。"""
        write_file(self.root, "note.txt", "hi")
        write_file(self.root, "stray.p3d", "x")
        saved = self.api.save(str(self.root), "stray model")
        self.assertEqual(saved["index"], 1)
        self.assertEqual(saved["groups"][0]["status"], "missing")

    def test_11_env_is_not_copied(self):
        write_file(self.root, ".env", "SECRET=123")
        write_file(self.root, "normal.txt", "hi")
        self.api.save(str(self.root), "env step")
        payload = self.root / ".checkpoints" / "payload"
        blob = b"".join(p.read_bytes() for p in payload.rglob("*") if p.is_file())
        self.assertNotIn(b"SECRET=123", blob)
        step = json.loads(next((self.root / ".checkpoints" / "tasks" / self.tid / "steps").glob("*.json")).read_text(encoding="utf-8"))
        manifest = json.loads((self.root / ".checkpoints" / "tasks" / self.tid / "manifest" / step["manifest_ref"]).read_text(encoding="utf-8"))
        entry = (manifest.get("full_baseline") or manifest["entries"])[".env"]
        self.assertNotIn("sha256", entry)
        self.assertIn(".env", self.api.resume(str(self.root))["skipped"])

    def test_12_partial_state_file_is_reported(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        state_path = self.root / ".checkpoints" / "tasks" / self.tid / "state.json"
        state_path.with_suffix(".json.tmp").write_text("{", encoding="utf-8")
        fresh = TaskCheckpoint(self.root)
        self.assertEqual(fresh._state(self.tid)["head"], 1)
        health = fresh.resume(str(self.root))["health"]
        self.assertFalse(health["ok"])
        self.assertTrue(health["errors"])

    def test_13_second_init_suspends_previous(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1")
        second = self.api.init(str(self.root), "task-2")
        self.assertEqual(second["prev_suspended"], self.tid)
        self.assertEqual(self.api._state(self.tid)["status"], "suspended")
        self.api.switch(str(self.root), self.tid)
        write_file(self.root, "b.txt", "b1")
        saved = self.api.save(str(self.root), "s2")
        self.assertEqual(saved["task_id"], self.tid)

    def test_14_closed_task_rejects_save(self):
        write_file(self.root, "a.txt", "a1")
        self.api.save(str(self.root), "s1", close=True)
        with self.assertRaises(PermissionError):
            self.api.save(str(self.root), "after close")

    def test_15_identical_content_is_stored_once(self):
        content = "same content" * 1000
        write_file(self.root, "a.txt", content)
        self.api.save(str(self.root), "first")
        write_file(self.root, "b.txt", content)
        self.api.save(str(self.root), "second")
        files = [p for p in (self.root / ".checkpoints" / "payload").rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)

    def test_17_outside_symlink_is_rejected(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "link_outside"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlink not permitted")
        self.api.save(str(self.root), "link test")
        health = self.api.resume(str(self.root))["health"]
        self.assertFalse(health["ok"])
        self.assertTrue(any("link_outside" in item for item in health["errors"]))

    def test_18_over_limit_file_is_not_copied(self):
        big = self.root / "big.bin"
        size = 101 * 1024 * 1024
        with open(big, "wb") as fh:
            fh.write(b"\0" * size)
        self.api.save(str(self.root), "big step")
        payload = self.root / ".checkpoints" / "payload"
        files = [p for p in payload.rglob("*") if p.is_file()] if payload.exists() else []
        self.assertEqual(files, [])
        self.assertIn("big.bin", self.api.resume(str(self.root))["skipped"])


class Phase2(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.api = TaskCheckpoint(self.root)
        self.api.init(str(self.root), "phase2", goal="watch")

    def tearDown(self):
        self._tmp.cleanup()

    def test_second_tick_does_not_read_unchanged_files(self):
        for index in range(20):
            (self.root / f"f{index}.txt").write_text("same", encoding="utf-8")
        self.api.save(str(self.root), "base")
        self.api._watch["content_reads"] = 0
        result = self.api.tick(now=1)
        self.assertFalse(result["captured"])
        self.assertEqual(self.api._watch.get("content_reads", 0), 0)

    def test_quiet_period_has_a_maximum(self):
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "base")
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        self.assertFalse(self.api.tick(now=10)["captured"])
        (self.root / "a.txt").write_text("three", encoding="utf-8")
        self.api.tick(now=20)
        (self.root / "a.txt").write_text("four", encoding="utf-8")
        waiting = self.api.tick(now=50)
        self.assertFalse(waiting["captured"])
        (self.root / "a.txt").write_text("five-more", encoding="utf-8")
        forced = self.api.tick(now=90)
        self.assertTrue(forced["captured"])
        self.assertTrue(forced["partial"])
        (self.root / "a.txt").write_text("settled", encoding="utf-8")
        self.api.tick(now=100)
        settled = self.api.tick(now=101, content_changed=False)
        self.assertTrue(settled["captured"])
        self.assertFalse(settled["partial"])

    def test_capture_result_shape_is_stable(self):
        required = {"captured", "drift_id", "partial", "reads", "waiting"}
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "base")
        unchanged = self.api.capture(str(self.root), now=1, force=True)
        self.assertTrue(required <= set(unchanged))
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        waiting = self.api.capture(str(self.root), now=2)
        self.assertTrue(required <= set(waiting))
        captured = self.api.capture(str(self.root), now=3, force=True)
        self.assertTrue(required <= set(captured))
        self.assertTrue(captured["captured"])

    def test_tick_detects_stability_without_a_flag(self):
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "base")
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        first = self.api.tick(now=10)
        self.assertFalse(first["captured"])
        second = self.api.tick(now=11)
        self.assertTrue(second["captured"])
        self.assertFalse(second["partial"])

    def test_save_to_another_task_switches_without_deadlock(self):
        first = self.api.init(str(self.root), "first")["task_id"]
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "first")
        second = self.api.init(str(self.root), "second")["task_id"]
        started = time.monotonic()
        saved = self.api.save(str(self.root), "into first", task_id=first)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(saved["task_id"], first)
        self.assertEqual(active_task_id(self.root / ".checkpoints"), first)
        self.api.save(str(self.root), "close second", task_id=second, close=True)
        with self.assertRaises(PermissionError):
            self.api.save(str(self.root), "rejected", task_id=second)
        self.assertEqual(active_task_id(self.root / ".checkpoints"), first)

    def test_external_store_is_reused_by_new_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            store = Path(tmp) / "outside"
            root.mkdir()
            TaskCheckpoint(root).init(str(root), "external", store=str(store))
            (root / "a.txt").write_text("one", encoding="utf-8")
            fresh = TaskCheckpoint(root)
            self.assertEqual(fresh.store, store.resolve())
            self.assertFalse(fresh.tick(now=1)["captured"])
            captured = fresh.tick(now=2)
            self.assertTrue(captured["captured"])
            self.assertTrue((store / "tasks").exists())

    def test_deleted_file_is_reported_and_captured(self):
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "base")
        (self.root / "a.txt").unlink()
        resume = TaskCheckpoint(self.root).resume(str(self.root))
        self.assertIn("a.txt", resume["drift_paths"])
        captured = TaskCheckpoint(self.root).capture(str(self.root), force=True)
        self.assertTrue(captured["captured"])
        self.assertTrue(captured["drift_id"])
        shown = TaskCheckpoint(self.root).show(str(self.root), include_drift=True)
        self.assertIn(captured["drift_id"], {item["drift_index"] for item in shown["drifts"]})
        TaskCheckpoint(self.root).restore(str(self.root), index=1, apply=True)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "one")

    def test_lock_timeout_leaves_valid_json(self):
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        self.api.save(str(self.root), "base")
        errors = []

        def worker(text):
            try:
                (self.root / "a.txt").write_text(text, encoding="utf-8")
                self.api.save(str(self.root), text)
            except TimeoutError:
                errors.append("timeout")

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for name in ("state.json", "index.json"):
            path = next((self.root / ".checkpoints").rglob(name))
            json.loads(path.read_text(encoding="utf-8"))
        self.assertLessEqual(len(errors), 1)

    def test_recovered_survives_compression(self):
        (self.root / "a.txt").write_text("v1", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        (self.root / "a.txt").write_text("v2", encoding="utf-8")
        self.api.save(str(self.root), "s2")
        restored = self.api.restore(str(self.root), index=1, apply=True)
        for index in range(51):
            (self.root / "a.txt").write_text(f"n{index}", encoding="utf-8")
            self.api.capture(str(self.root), now=index, partial=True)
        self.api.compress(keep_seconds=0, minimum=1, maximum=1)
        self.api.restore(str(self.root), drift_id=restored["recovered"], apply=True)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "v2")

    def test_drift_reports_added_modified_and_deleted_paths(self):
        (self.root / "a.txt").write_text("v1", encoding="utf-8")
        self.api.save(str(self.root), "base")
        (self.root / "a.txt").write_text("v2", encoding="utf-8")
        (self.root / "b.txt").write_text("new", encoding="utf-8")
        result = self.api.capture(str(self.root), force=True)
        (self.root / "a.txt").unlink()
        (self.root / "b.txt").unlink()
        (self.root / "c.txt").write_text("replacement", encoding="utf-8")
        deleted = self.api.capture(str(self.root), force=True)
        shown = self.api.show(str(self.root), include_drift=True)["drifts"]
        first_changes = {item["path"]: item["change_type"] for item in shown if item["drift_index"] == result["drift_id"] for item in item["changes"]}
        second_changes = {item["path"]: item["change_type"] for item in shown if item["drift_index"] == deleted["drift_id"] for item in item["changes"]}
        self.assertEqual(first_changes["a.txt"], "modified")
        self.assertEqual(first_changes["b.txt"], "added")
        self.assertEqual(second_changes["a.txt"], "deleted")
        self.assertEqual(second_changes["b.txt"], "deleted")
        self.assertEqual(second_changes["c.txt"], "added")
        self.assertTrue(result["drift_id"] and deleted["drift_id"])

    def test_compressed_drift_restore_fails_without_writing(self):
        (self.root / "a.txt").write_text("v1", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        for index, text in enumerate(("v2", "v3", "v4"), start=1):
            (self.root / "a.txt").write_text(text, encoding="utf-8")
            self.api.tick(now=index)
            saved = self.api.capture(str(self.root), now=70 + index, partial=True)
            if index == 1:
                first = saved
        self.api.compress(keep_seconds=0, minimum=1, maximum=1)
        shown = self.api.show(str(self.root), include_drift=True)
        drifted = [item for item in shown["drifts"] if item["drift_index"] == first["drift_id"]][0]
        self.assertFalse(drifted["restorable"])
        before = (self.root / "a.txt").read_text(encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.api.restore(str(self.root), drift_id=first["drift_id"], apply=True)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), before)

    def test_switch_does_not_rewrite_index(self):
        (self.root / "a.txt").write_text("v", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        index = self.root / ".checkpoints" / "tasks" / active_task_id(self.root / ".checkpoints") / "index.json"
        stamp = index.stat().st_mtime_ns
        second = self.api.init(str(self.root), "other")
        self.api.switch(str(self.root), second["prev_suspended"])
        self.assertEqual(index.stat().st_mtime_ns, stamp)

    def test_git_ref_does_not_change_worktree(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        (self.root / "a.txt").write_text("v", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        result = self.api.save(str(self.root), "s2")["git"]
        self.assertTrue(result["ok"])
        self.assertTrue(result["unchanged"])
        self.assertTrue(result["ref"].startswith("refs/checkpoints/"))


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        TaskCheckpoint(self.root).init(str(self.root), "review")

    def tearDown(self):
        self._tmp.cleanup()

    def test_shared_payload_is_not_overwritten(self):
        root = str(self.root)
        (self.root / "a.txt").write_text("P", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "base")
        for text in ("Q", "P"):
            (self.root / "a.txt").write_text(text, encoding="utf-8")
            TaskCheckpoint(self.root).tick(now=1)
            TaskCheckpoint(self.root).capture(root, now=70, partial=True)
        before = {path.read_bytes() for path in (self.root / ".checkpoints" / "payload").rglob("*") if path.is_file()}
        TaskCheckpoint(self.root).compress(minimum=1, maximum=1)
        after = {path.name: path.read_bytes() for path in (self.root / ".checkpoints" / "payload").rglob("*") if path.is_file()}
        self.assertEqual([p for p in (self.root / ".checkpoints" / "payload").rglob("*.diff")], [],
                         "compress 不应再留下没人读的 .diff 产物")
        self.assertTrue(all(value in before for value in after.values()))
        (self.root / "a.txt").write_text("Z", encoding="utf-8")
        TaskCheckpoint(self.root).restore(root, index=1, apply=True)
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "P")

    def test_restore_moves_head_without_reusing_numbers(self):
        root = str(self.root)
        for index, text in enumerate(("a", "b", "c"), start=1):
            (self.root / "a.txt").write_text(text, encoding="utf-8")
            TaskCheckpoint(self.root).save(root, f"s{index}")
        TaskCheckpoint(self.root).restore(root, index=2, apply=True)
        self.assertEqual(TaskCheckpoint(self.root).resume(root)["head"], 2)
        (self.root / "a.txt").write_text("d", encoding="utf-8")
        saved = TaskCheckpoint(self.root).save(root, "s4")
        self.assertEqual(saved["index"], 4)

    def test_new_instance_can_finish_quiet_period(self):
        root = str(self.root)
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "base")
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        self.assertFalse(TaskCheckpoint(self.root).capture(root, now=1)["captured"])
        result = TaskCheckpoint(self.root).capture(root, now=70)
        self.assertTrue(result["captured"])

    def test_manifest_is_incremental_and_restores_across_chain(self):
        root = str(self.root)
        for index in range(1, 6):
            (self.root / f"f{index}.txt").write_text(f"v{index}", encoding="utf-8")
            TaskCheckpoint(self.root).save(root, f"s{index}")
        task = sorted((self.root / ".checkpoints" / "tasks").iterdir())[0]
        manifests = sorted((task / "manifest").glob("m*.json"))
        modes = [json.loads(path.read_text(encoding="utf-8")).get("mode") for path in manifests]
        self.assertEqual(modes[0], "full")
        self.assertTrue(all(mode == "delta" for mode in modes[1:]))
        sizes = [len(json.loads(path.read_text(encoding="utf-8"))["entries"]) for path in manifests]
        self.assertLess(max(sizes[1:]), sizes[0] + 5)
        (self.root / "f2.txt").unlink()
        TaskCheckpoint(self.root).save(root, "after-delete")
        plan = TaskCheckpoint(self.root).restore(root, index=5, apply=True)
        self.assertEqual(plan["plan"]["to_write"], ["f2.txt"])
        self.assertTrue((self.root / "f2.txt").exists())
        self.assertEqual((self.root / "f2.txt").read_text(encoding="utf-8"), "v2")

    def test_manifest_chain_has_periodic_full_anchor(self):
        root = str(self.root)
        task = sorted((self.root / ".checkpoints" / "tasks").iterdir())[0]
        for index in range(1, tc.MANIFEST_CHAIN_MAX * 2 + 5):
            (self.root / "a.txt").write_text(f"v{index}", encoding="utf-8")
            TaskCheckpoint(self.root).save(root, f"s{index}")
        manifests = sorted((task / "manifest").glob("m*.json"))
        fulls = [path.name for path in manifests
                 if json.loads(path.read_text(encoding="utf-8")).get("mode") == "full"]
        self.assertEqual(fulls, ["m0001.json", f"m{tc.MANIFEST_CHAIN_MAX + 1:04d}.json",
                                f"m{tc.MANIFEST_CHAIN_MAX * 2 + 1:04d}.json"])
        self.assertEqual(TaskCheckpoint(self.root).restore(root, index=1, apply=False)["plan"]["to_replace"], ["a.txt"])
        self.assertEqual(TaskCheckpoint(self.root).restore(root, index=2, apply=False)["plan"]["to_replace"], ["a.txt"])

    def test_git_ref_builds_tree_from_workspace_in_empty_repo(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        root = str(self.root)
        (self.root / "a.txt").write_text("v1", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "s1")
        (self.root / "a.txt").write_text("v2-not-added", encoding="utf-8")
        (self.root / "new.txt").write_text("fresh", encoding="utf-8")
        result = TaskCheckpoint(self.root).save(root, "s2")["git"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                                        capture_output=True).returncode != 0, True)
        listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["ref"]], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8")
        names = sorted(listed.stdout.split())
        self.assertEqual(names, ["a.txt", "new.txt"])
        blob = subprocess.run(["git", "show", f"{result['ref']}:a.txt"], cwd=self.root,
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(blob.stdout, "v2-not-added")
        unstaged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.root,
                                  capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(unstaged.stdout.split(), [])
        self.assertFalse((self.root / ".checkpoints" / "git-index.tmp").exists())

    def test_git_ref_keeps_sensitive_content_out_of_the_tree(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        root = str(self.root)
        (self.root / "a.txt").write_text("public", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=secret", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "s1")
        (self.root / "a.txt").write_text("v2", encoding="utf-8")
        result = TaskCheckpoint(self.root).save(root, "s2")["git"]
        self.assertTrue(result["ok"], result)
        listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["ref"]], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(sorted(listed.stdout.split()), ["a.txt"])

    def test_legacy_manifest_without_mode_still_loads(self):
        root = str(self.root)
        (self.root / "a.txt").write_text("legacy", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "s1")
        task = sorted((self.root / ".checkpoints" / "tasks").iterdir())[0]
        entries = {"a.txt": {"mtime": 1, "size": 6, "sha256": tc._sha(b"legacy")}}
        legacy = {"version": "1", "entries": {}, "full_baseline": entries}
        (task / "manifest" / "m0002.json").write_text(json.dumps(legacy), encoding="utf-8")
        step = json.loads((task / "steps" / "s0001.json").read_text(encoding="utf-8"))
        self.assertEqual(TaskCheckpoint(self.root)._load_manifest(task.name, "m0002.json"), entries)
        self.assertEqual(step["manifest_ref"], "m0001.json")

    def test_every_branch_returns_all_declared_fields(self):
        """反向断言：声明的字段必须在每个分支都出现，不只是实际返回⊆声明。"""
        declared_tools = {tool["name"]: tool.get("outputSchema") or {} for tool in tc_mcp.TOOL_SPECS}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("external", encoding="utf-8")
            try:
                os.symlink(outside, root / "link.txt")
            except (OSError, NotImplementedError):
                pass
            path = str(root)
            write_file(root, "a.txt", "one")
            seen: dict = {}

            def run(name: str, arguments: dict) -> dict:
                result = tc_mcp.dispatch_tool(name, arguments)
                seen.setdefault(name, []).append(result)
                return result

            first = run("tc_init", {"root": path, "name": "branches"})
            run("tc_init", {"root": path, "name": "other"})
            run("tc_switch", {"root": path, "task_id": first["task_id"]})
            run("tc_show", {"root": path})
            run("tc_save", {"root": path, "title": "s1"})
            run("tc_save", {"root": path, "title": "s1"})
            run("tc_capture", {"root": path})
            run("tc_restore", {"root": path, "index": 1})
            run("tc_restore", {"root": path, "index": 1, "apply": True})
            run("tc_resume", {"root": path})
            exported = run("tc_export", {"root": path, "to": tmp})
            second = Path(tmp) / "ws2"
            second.mkdir()
            run("tc_import", {"root": str(second), "package": exported["path"]})
            run("tc_compress", {"root": path})
            run("tc_save", {"root": path, "title": "done", "close": True})
            self.assertEqual(sorted(seen), sorted(declared_tools))
            for name, results in seen.items():
                declared = set(declared_tools[name].get("properties") or {})
                required = set(declared_tools[name].get("required") or [])
                for index, result in enumerate(results):
                    self.assertEqual(sorted(set(result) - declared), [], f"{name} 第 {index} 个分支多返回了未声明字段")
                    self.assertEqual(sorted(required - set(result)), [], f"{name} 第 {index} 个分支少了声明字段")

    def test_declared_and_actual_output_keys_agree_for_save_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            path = str(root)
            api = TaskCheckpoint(root)
            api.init(path, "save-branches")
            (root / "a.txt").write_text("one", encoding="utf-8")
            first = api.save(path, "s1")
            repeated = api.save(path, "s1")
            closed = api.save(path, "done", close=True)
            self.assertEqual(sorted(first), sorted(repeated))
            self.assertEqual(sorted(first), sorted(closed))
            self.assertTrue(repeated["idempotent"])
            self.assertFalse(first["idempotent"])
            self.assertTrue(closed["closed"])
            self.assertEqual(closed["status"], "closed")

    def test_git_ref_without_any_step_returns_reason_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            path = str(root)
            api = TaskCheckpoint(root)
            api.init(path, "fresh")
            result = api.git_ref(path)
            self.assertTrue(result["git"])
            self.assertIsNone(result["ref"])
            self.assertEqual(result["reason"], "no step yet")
            self.assertTrue(result["unchanged"])

    def test_git_ref_self_check_is_truthful_and_leaves_user_index_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            path = str(root)
            api = TaskCheckpoint(root)
            api.init(path, "selfcheck")
            (root / "a.txt").write_text("v1", encoding="utf-8")
            api.save(path, "s1")
            (root / "a.txt").write_text("v2-not-added", encoding="utf-8")
            (root / "new.txt").write_text("fresh", encoding="utf-8")
            api.save(path, "s2")
            before = subprocess.run(["git", "ls-files", "-s"], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout
            result = api.save(path, "s3")["git"]
            after = subprocess.run(["git", "ls-files", "-s"], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout
            self.assertTrue(result["unchanged"], "自检必须为真：连同它自动建 ref 也不该动索引或 HEAD")
            self.assertEqual(before, after)
            self.assertEqual(after, "", "用户索引必须保持为空")
            tree = subprocess.run(["git", "ls-tree", "-r", "--name-only", result["ref"]], cwd=root,
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(sorted(tree.stdout.split()), ["a.txt", "new.txt"])
            blob = subprocess.run(["git", "show", f"{result['ref']}:a.txt"], cwd=root,
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(blob.stdout, "v2-not-added")
            self.assertFalse((root / ".checkpoints" / "git-index.tmp").exists())

    def test_self_check_can_be_switched_off_for_speed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            path = str(root)
            api = TaskCheckpoint(root)
            api.init(path, "noverify")
            (root / "a.txt").write_text("v1", encoding="utf-8")
            api.save(path, "s1")
            os.environ["TC_GIT_VERIFY"] = "off"
            try:
                result = api.save(path, "s2")["git"]
            finally:
                os.environ.pop("TC_GIT_VERIFY", None)
            self.assertTrue(result["ok"])
            self.assertIsNone(result["unchanged"], "跳过自证时不能谎报工作区没被动过")
            # ref 照旧建出来，只是没跑前后指纹对比
            self.assertTrue(result["ref"])
            subprocess.run(["git", "show-ref", "--verify", "--quiet", result["ref"]], cwd=root, check=True)

    def test_watch_errors_do_not_grow_stderr_linearly(self):
        """watch 持续报错时 stderr 只能按对数增长，恢复时要吐一行。"""
        import io
        import contextlib

        root = str(self.root)
        store = self.root / ".checkpoints"
        TaskCheckpoint(root)._write_active("t1")
        # 必须清掉别的测试留下的 root：_watch_once() 遍历 tc._WATCH 里的**全部**
        # root，残留 N 个就会把每行同样的内容重复写 N 遍，而这个测试断言的是
        # 精确行数。不清的话它在"前面某个测试碰过 _WATCH"的机器上会假失败。
        saved_watch = dict(tc._WATCH)
        tc._WATCH.clear()
        tc._WATCH[root] = {"root": root, "store": str(store)}
        tc_mcp._WATCH_ERRORS.clear()
        original = tc.TaskCheckpoint.tick
        bursts = [
            ("boom", 100, ["task-checkpoint watch skipped: RuntimeError: boom",
                           "task-checkpoint watch skipped (10x): RuntimeError: boom",
                           "task-checkpoint watch skipped (100x): RuntimeError: boom"]),
            ("different", 1, ["task-checkpoint watch skipped (101x): ValueError: different"]),
        ]
        captured = io.StringIO()
        try:
            for message, times, expected in bursts:
                def broken(self, _message=message):
                    raise (RuntimeError if _message == "boom" else ValueError)(_message)

                tc.TaskCheckpoint.tick = broken
                with contextlib.redirect_stderr(captured):
                    for _ in range(times):
                        tc_mcp._watch_once()
                lines = [line for line in captured.getvalue().splitlines() if line]
                self.assertEqual(lines, expected, f"{message} 突发 {times} 次后的 stderr 行不符")
                captured.seek(0)
                captured.truncate()
            tc.TaskCheckpoint.tick = original
            with contextlib.redirect_stderr(captured):
                tc_mcp._watch_once()
            self.assertEqual(captured.getvalue().splitlines(),
                             ["task-checkpoint watch recovered after 101 failed ticks"])
        finally:
            tc.TaskCheckpoint.tick = original
            tc._WATCH.clear()
            tc._WATCH.update(saved_watch)
            tc_mcp._WATCH_ERRORS.clear()

    def test_watch_once_stays_silent_when_the_tick_succeeds(self):
        import io
        import contextlib

        root = str(self.root)
        # setUp 里的 init 已经写好 current.json，这里直接用它。
        self.assertTrue((self.root / ".checkpoints" / "current.json").exists())
        tc._WATCH[root] = {"root": root, "store": str(self.root / ".checkpoints")}
        tc_mcp._WATCH_ERRORS.clear()
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured):
                for _ in range(3):
                    tc_mcp._watch_once()
            self.assertEqual(captured.getvalue(), "")
        finally:
            tc._WATCH.pop(root, None)
            tc_mcp._WATCH_ERRORS.clear()

    def test_sensitive_file_is_skipped_but_siblings_still_restore(self):
        """一个 .env 不应该把整个回退连坐掉。"""
        root = str(self.root)
        (self.root / "app.py").write_text("v1", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=one", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "sensitive")
        (self.root / "app.py").write_text("v2", encoding="utf-8")
        (self.root / ".env").write_text("TOKEN=two", encoding="utf-8")
        preview = TaskCheckpoint(self.root).restore(root, index=1, apply=False)
        self.assertIn(".env", preview["plan"]["unrestorable"])
        self.assertNotIn(".env", preview["plan"]["to_replace"])
        done = TaskCheckpoint(self.root).restore(root, index=1, apply=True)
        self.assertTrue(done["applied"])
        # 不可回退的那个跳过，能回退的照退
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "v1")
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "TOKEN=two")
        self.assertIn(".env", done["plan"]["unrestorable"])

    def test_restore_preview_reports_skipped_paths_too(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("external", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        root = str(self.root)
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        TaskCheckpoint(self.root).save(root, "s1")
        preview = TaskCheckpoint(self.root).restore(root, index=1, apply=False)
        applied = TaskCheckpoint(self.root).restore(root, index=1, apply=True)
        self.assertIn("skipped_paths", preview)
        self.assertIn("skipped_paths", applied)
        self.assertEqual(preview["skipped_paths"], applied["skipped_paths"])
        self.assertTrue(any("link.txt" in item for item in preview["skipped_paths"]))

    def test_save_reports_skipped_symlink(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("external", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = TaskCheckpoint(self.root).save(str(self.root), "with-link")
        self.assertIn("skipped_paths", result)
        self.assertTrue(any("link.txt" in item for item in result["skipped_paths"]))


class ProtocolSmoke(unittest.TestCase):
    def test_real_process_answers_protocol_messages(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tc_resume", "arguments": {"root": "Z:/missing"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "nope"},
        ]
        payload = "".join(json.dumps(item) + "\n" for item in messages)
        result = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, env={**os.environ, "TC_WATCH": "off"})
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(lines[1]["result"]["tools"]), len(tc_mcp.TOOLS))
        self.assertTrue(lines[2]["result"]["isError"])
        self.assertEqual(lines[3]["error"]["code"], -32601)

    def test_malformed_requests_return_errors_and_server_survives(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        payload = "".join(json.dumps(item) + "\n" for item in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None},
            [1, 2, 3],
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        ])
        result = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10,
                                env={**os.environ, "TC_WATCH": "off"})
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines[0]["error"]["code"], -32602)
        self.assertEqual(lines[1]["error"]["code"], -32600)
        self.assertEqual(lines[2]["result"]["protocolVersion"], "2025-06-18")

    def test_cancel_notification_does_not_produce_a_response(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        payload = "".join(json.dumps(item) + "\n" for item in [
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        ])
        result = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=10,
                                env={**os.environ, "TC_WATCH": "off"})
        lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual([line["id"] for line in lines], [2])

    def test_server_survives_a_watch_tick(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "TC_WATCH_INTERVAL": "0.2", "TC_DRIFT_MAX_WAIT": "0.2"}
            process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env)
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}) + "\n")
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "tc_init", "arguments": {"root": tmp, "name": "watch"}}}) + "\n")
            process.stdin.flush()
            time.sleep(0.8)
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}) + "\n")
            process.stdin.flush()
            process.stdin.close()
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotIn("tc' is not defined", stderr)
            ids = [json.loads(line).get("id") for line in stdout.splitlines() if line.strip()]
            self.assertEqual(ids, [1, 2, 3])

    def test_server_writes_utf8_frames(self):
        """MCP 帧必须是 UTF-8；本地代码页下中文描述会被写成乱码。"""
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in messages).encode("utf-8")
        result = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True,
                                timeout=10, env={**os.environ, "TC_WATCH": "off"})
        text = result.stdout.decode("utf-8")
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        description = next(tool for tool in lines[1]["result"]["tools"] if tool["name"] == "tc_init")["description"]
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in description))

    def test_structured_content_matches_declared_output_keys(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "tc_mcp.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            outside = Path(tmp) / "outside.txt"
            outside.write_text("external", encoding="utf-8")
            try:
                os.symlink(outside, root / "link.txt")
            except (OSError, NotImplementedError):
                pass
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "tc_init", "arguments": {"root": str(root), "name": "schema"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "tc_save", "arguments": {"root": str(root), "title": "s1"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "tc_capture", "arguments": {"root": str(root)}}},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "tc_resume", "arguments": {"root": str(root)}}},
            ]
            payload = "".join(json.dumps(item) + "\n" for item in messages)
            result = subprocess.run([sys.executable, str(script)], input=payload, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=20, env={**os.environ, "TC_WATCH": "off"})
            lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            ids = [item.get("id") for item in messages]
            specs = {tool["name"]: tool.get("outputSchema") or {} for tool in lines[1]["result"]["tools"]}
            for response in lines[2:]:
                name = messages[ids.index(response["id"])]["params"]["name"]
                structured = response["result"].get("structuredContent") or {}
                declared = set((specs[name].get("properties") or {}))
                self.assertEqual(sorted(set(structured) - declared), [], f"{name} 返回了未声明的字段")
                self.assertTrue(set(specs[name].get("required") or []) <= set(structured), f"{name} 缺少必需字段")


class ClosedRestore(unittest.TestCase):
    def test_closed_task_restore_apply_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "close")
            write_file(root, "a.txt", "a1")
            api.save(str(root), "close", close=True)
            with self.assertRaises(PermissionError):
                api.restore(str(root), index=0, apply=True)


class ToolSchema(unittest.TestCase):
    def test_invalid_tool_arguments_use_jsonrpc_invalid_params(self):
        missing = tc_mcp.handle_tools_call({"name": "tc_save", "arguments": {"root": "x"}}, 1)
        self.assertEqual(missing["error"]["code"], -32602)
        extra = tc_mcp.handle_tools_call({"name": "tc_resume", "arguments": {"root": "x", "extra": True}}, 2)
        self.assertEqual(extra["error"]["code"], -32602)
        wrong = tc_mcp.handle_tools_call({"name": "tc_show", "arguments": {"root": "x", "limit": "10"}}, 3)
        self.assertEqual(wrong["error"]["code"], -32602)
        non_object = tc_mcp.handle_tools_call({"name": "tc_resume", "arguments": []}, 4)
        self.assertEqual(non_object["error"]["code"], -32602)

    def test_tools_list_declares_real_parameters(self):
        result = tc_mcp.handle_tools_list({}, 1)["result"]["tools"]
        names = [tool["name"] for tool in result]
        self.assertEqual(names, tc_mcp.TOOLS)
        for tool in result:
            self.assertIn("root", tool["inputSchema"]["properties"])
            self.assertIn("root", tool["inputSchema"]["required"])
            self.assertNotEqual(tool["description"], f"tool {tool['name']}")
        save = next(tool for tool in result if tool["name"] == "tc_save")
        self.assertIn("title", save["inputSchema"]["required"])
        restore = next(tool for tool in result if tool["name"] == "tc_restore")
        self.assertIn("drift_id", restore["inputSchema"]["properties"])


class Phase3(unittest.TestCase):
    def test_store_equal_to_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            with self.assertRaises(ValueError):
                TaskCheckpoint(root, store=str(root))
            with self.assertRaises(ValueError):
                TaskCheckpoint(root).init(str(root), "bad-store", store=str(root))

    def test_save_paths_are_explicitly_rejected_until_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "paths")
            with self.assertRaises(ValueError):
                api.save(str(root), "scoped", paths=["src"])
            result = tc_mcp.handle_tools_call({"name": "tc_save", "arguments": {"root": str(root), "title": "scoped", "paths": ["src"]}}, 1)
            self.assertEqual(result["error"]["code"], -32602)

    def test_custom_workspace_store_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            store = root / "archive"
            api = TaskCheckpoint(root)
            api.init(str(root), "custom-store", store=str(store))
            (root / "a.txt").write_text("visible", encoding="utf-8")
            api.save(str(root), "s1")
            state = api._active()
            scanned = api._scan(state)
            self.assertNotIn("archive/current.json", scanned)
            self.assertNotIn("archive/tasks", scanned)

    def test_save_records_pending_drift_before_next_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "drift")
            write_file(root, "a.txt", "v1")
            api.save(str(root), "s1")
            write_file(root, "a.txt", "v2")
            api.save(str(root), "s2")
            shown = api.show(str(root), include_drift=True)
            self.assertTrue(any(d["kind"] == "drift" for d in shown["drifts"]))

    def test_export_can_resume_elsewhere_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            other = Path(tmp) / "other"
            output = Path(tmp) / "out"
            root.mkdir(); other.mkdir(); output.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "handoff", goal="finish the report")
            (root / "a.txt").write_text("visible", encoding="utf-8")
            (root / ".env").write_text("SECRET=123", encoding="utf-8")
            api.save(str(root), "s1", next_step="write conclusion")
            exported = api.export(str(root), str(output))
            blob = b"".join(path.read_bytes() for path in Path(exported["path"]).rglob("*") if path.is_file())
            self.assertNotIn(b"SECRET=123", blob)
            self.assertIn(".env", exported["filtered"])
            TaskCheckpoint(other).import_handoff(exported["path"], str(other))
            resumed = TaskCheckpoint(other).resume(str(other))
            self.assertEqual(resumed["goal"], "finish the report")
            self.assertEqual(resumed["head"], 1)

    def test_export_resolves_delta_chain_and_writes_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            other = Path(tmp) / "other"
            output = Path(tmp) / "out"
            root.mkdir(); other.mkdir(); output.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "chain")
            for index in range(1, 5):
                (root / f"f{index}.txt").write_text(f"v{index}", encoding="utf-8")
                api.save(str(root), f"s{index}")
            (root / "f1.txt").unlink()
            api.save(str(root), "s5")
            exported = api.export(str(root), str(output))
            package = Path(exported["path"])
            files = {path.relative_to(package / "files").as_posix()
                     for path in (package / "files").rglob("*") if path.is_file()}
            self.assertEqual(sorted(files), ["f2.txt", "f3.txt", "f4.txt"])
            self.assertEqual(list(package.rglob("*.tmp")), [])
            TaskCheckpoint(other).import_handoff(exported["path"], str(other))
            imported = TaskCheckpoint(other)
            self.assertEqual(imported.resume(str(other))["head"], 1)
            self.assertEqual((other / "f4.txt").read_text(encoding="utf-8"), "v4")
            self.assertEqual(list((other / ".checkpoints").rglob("*.tmp")), [])
            self.assertFalse((other / "f1.txt").exists())

    def test_import_does_not_leave_partial_files_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "pkg"
            (package / "files").mkdir(parents=True)
            (package / "files" / "ok.txt").write_text("good", encoding="utf-8")
            (package / "handoff.json").write_text(json.dumps({"schema_version": "1", "task": {
                "task_id": "t1", "name": "n", "goal": "g", "constraints": "", "head": 1, "status": "open"},
                "resume": {}, "filtered": []}), encoding="utf-8")
            other = Path(tmp) / "other"
            other.mkdir()
            TaskCheckpoint(other).import_handoff(str(package), str(other))
            self.assertEqual((other / "ok.txt").read_text(encoding="utf-8"), "good")
            self.assertEqual(list(other.rglob("*.tmp")), [])
            self.assertEqual(list(other.rglob("*.bak")), [])

    def test_import_is_cleaned_up_after_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "pkg"
            (package / "files").mkdir(parents=True)
            (package / "files" / "one.txt").write_text("one", encoding="utf-8")
            (package / "files" / "two.txt").write_text("two", encoding="utf-8")
            (package / "handoff.json").write_text(json.dumps({
                "schema_version": "1",
                "task": {"task_id": "t-fault", "name": "fault", "goal": "", "constraints": ""},
                "resume": {}, "filtered": [],
            }), encoding="utf-8")
            root = Path(tmp) / "root"
            root.mkdir()
            api = TaskCheckpoint(root)
            original = api._copy_file
            calls = {"count": 0}

            def fail_second(source, destination):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("injected import failure")
                return original(source, destination)

            api._copy_file = fail_second
            with self.assertRaises(OSError):
                api.import_handoff(str(package), str(root))
            self.assertFalse((root / "one.txt").exists())
            self.assertFalse((root / "two.txt").exists())
            self.assertFalse((root / ".checkpoints" / "tasks" / "t-fault").exists())
            self.assertFalse((root / ".checkpoints" / "current.json").exists())
            self.assertFalse(list((root / ".checkpoints").glob(".import-*")))

    def test_import_rejects_unsafe_or_duplicate_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "pkg"
            (package / "files").mkdir(parents=True)
            (package / "files" / "ok.txt").write_text("good", encoding="utf-8")
            handoff = {"schema_version": "1", "task": {"task_id": "t1", "name": "n", "goal": "g", "constraints": ""}, "resume": {}, "filtered": []}
            (package / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
            other = Path(tmp) / "other"
            other.mkdir()
            api = TaskCheckpoint(other)
            api.import_handoff(str(package), str(other))
            with self.assertRaises(FileExistsError):
                api.import_handoff(str(package), str(other))
            handoff["task"]["task_id"] = "../escape"
            (package / "handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
            with self.assertRaises(ValueError):
                TaskCheckpoint(Path(tmp) / "third").import_handoff(str(package), str(Path(tmp) / "third"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            other = Path(tmp) / "other"
            output = Path(tmp) / "out"
            root.mkdir(); other.mkdir(); output.mkdir()
            api = TaskCheckpoint(root)
            api.init(str(root), "handoff")
            (root / "a.txt").write_text("original", encoding="utf-8")
            api.save(str(root), "s1")
            exported = api.export(str(root), str(output))
            imported = TaskCheckpoint(other)
            imported.import_handoff(exported["path"], str(other))
            (other / "a.txt").write_text("changed", encoding="utf-8")
            result = imported.restore(str(other), index=1, apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual((other / "a.txt").read_text(encoding="utf-8"), "original")


class Phase4Packaging(unittest.TestCase):
    """阶段四交付物：`python -m` 入口、打包契约、SKILL 规范、README 与实际一致。

    这些断言把“文档说的”和“代码实际能做的”钉在一起，防止以后改名、加依赖、
    或者把入口点写错而没人发现。
    """

    @property
    def project(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _run_server(self, argv, messages, timeout=30):
        payload = "".join(json.dumps(m) + "\n" for m in messages)
        env = {**os.environ, "TC_WATCH": "off", "PYTHONPATH": str(self.project / "scripts")}
        return subprocess.run([sys.executable, *argv], input=payload, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)

    def test_module_entry_point_serves_the_whole_loop(self):
        """python -m tc_mcp 要能跑完整链路：初始化 → 存步 → 读回。

        用 -m 而不是脚本路径，走的是“装进环境后”的那种启动方式，
        能顺带暴露脚本写法里才可以依赖的相对路径。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            (root / "a.txt").write_text("v1", encoding="utf-8")
            res = self._run_server(["-m", "tc_mcp"], [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "tc_init", "arguments": {"root": str(root), "name": "pkg"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "tc_save", "arguments": {
                     "root": str(root), "title": "first", "conclusion": "c", "next": "n"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "tc_resume", "arguments": {"root": str(root)}}},
            ])
            self.assertEqual(res.returncode, 0, res.stderr)
            lines = [json.loads(l) for l in res.stdout.splitlines() if l.strip()]
            # stdout 只能是 MCP 帧，不能混进任何非 JSON 的杂输出
            self.assertEqual(len(lines), 5)
            self.assertEqual([m["id"] for m in lines], [1, 2, 3, 4, 5])
            self.assertEqual(lines[0]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(len(lines[1]["result"]["tools"]), len(tc_mcp.TOOLS))
            for m in lines[2:5]:
                self.assertNotIn("error", m, m)
                self.assertFalse(m["result"].get("isError"), m)
            # 存的那一步要真读得回来，否则入口点算跑通但功能没通
            self.assertEqual(lines[4]["result"]["structuredContent"]["head"], 1)

    def test_pyproject_matches_the_module_contract(self):
        try:
            import tomllib
        except ModuleNotFoundError:  # 3.10 没有 tomllib；服务器本体仍支持 3.10
            self.skipTest("tomllib requires Python 3.11+")

        cfg = tomllib.loads((self.project / "pyproject.toml").read_text(encoding="utf-8"))
        st = cfg["tool"]["setuptools"]
        # 源码留在 scripts/，不改造成包目录
        self.assertEqual(st["package-dir"], {"": "scripts"})
        self.assertEqual(st["py-modules"], ["tc", "tc_mcp"])
        for mod in st["py-modules"]:
            self.assertTrue((self.project / "scripts" / f"{mod}.py").is_file(), mod)
        # “只用标准库”这条承诺，从这里开始是被机器检查的
        self.assertEqual(cfg["project"]["dependencies"], [])
        floor = tuple(int(p) for p in cfg["project"]["requires-python"].lstrip(">=").split("."))
        self.assertGreaterEqual(floor, (3, 10))
        # 命令名 → 真实可调用的对象
        entry = cfg["project"]["scripts"]["tc-mcp"]
        mod_name, attr = entry.split(":")
        self.assertIn(mod_name, st["py-modules"])
        self.assertTrue(callable(getattr(tc_mcp, attr)), entry)
        # 包名不能和模块名撞车
        self.assertNotEqual(cfg["project"]["name"].replace("-", "_"), mod_name)

    def test_skill_frontmatter_is_loadable(self):
        """SKILL.md 要符合 Agent Skills 规范，否则 Agent 根本加载不到它。"""
        import re

        src = (self.project / "SKILL.md").read_text(encoding="utf-8")
        m = re.match(r"---\n(.*?)\n---\n", src, re.S)
        self.assertIsNotNone(m, "SKILL.md 缺少 frontmatter")
        fm = m.group(1)
        name = re.search(r"^name:\s*(\S+)\s*$", fm, re.M).group(1)
        desc = re.search(r"^description:\s*(.+)$", fm, re.M).group(1)
        self.assertLessEqual(len(name), 64)
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertTrue(desc.strip(), "description 不能为空，否则技能不会被加载")
        self.assertLessEqual(len(desc), 1024)

    def test_skill_states_the_hard_rules_the_product_depends_on(self):
        """任务层只能靠模型主动调用产生，所以 SKILL 必须把这几条写成硬规则。

        少了这份约束，前几个阶段做的存储可靠性没有出口。
        """
        src = (self.project / "SKILL.md").read_text(encoding="utf-8")
        for tool in tc_mcp.TOOLS:
            self.assertIn(tool, src, f"SKILL.md 没提到 {tool}")
        # 每步存一次 + 交接字段 + 新会话先续上
        self.assertIn("conclusion", src)
        self.assertIn("next", src)
        # 回退前先预览（apply: false 出现在文档里）
        self.assertIn("apply", src)
        self.assertIn("tc_resume", src)

    def test_readme_documents_the_packaged_entry_points_only_now(self):
        """阶段四之前 README 不该写 pipx / python -m（那时还没有 pyproject）；
        到了阶段四之后，三种启动方式必须都写全。"""
        src = (self.project / "README.md").read_text(encoding="utf-8")
        self.assertIn("pipx install", src)
        self.assertIn("python -m tc_mcp", src)
        self.assertIn("tc-mcp", src)
        self.assertIn("mcpServers", src)
        # 自检命令要能真跑：里面引用的测试文件必须存在
        self.assertIn("tests", src)
        self.assertIn("test_tc.py", src)


class GitRefIsReachable(unittest.TestCase):
    """`git_ref` 必须能从 MCP 界面被用到。

    真实工作区演练发现的问题：`git_ref` 实现了、也测过安全性，但 8 个工具里没有它、
    `save`/`capture` 也不调它，于是它在 MCP 界面上不可达——设计好的增强等于没做。
    方案 §4 把 ref 命名为 `s<任务层步骤号>`，说明它属于 `save` 这个边界。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        (self.root / "tracked.txt").write_text("v1", encoding="utf-8")
        self.tc = TaskCheckpoint(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _reinit(self, git=True):
        if git:
            run = lambda *a: subprocess.run(["git", *a], cwd=str(self.root), capture_output=True,
                                            text=True, encoding="utf-8", errors="replace")
            run("init", "-q")
            run("config", "user.email", "t@localhost")
            run("config", "user.name", "t")
            run("add", "tracked.txt")
            run("commit", "-q", "-m", "init")
            (self.root / "untracked.txt").write_text("untracked", encoding="utf-8")
            (self.root / "tracked.txt").write_text("v2-not-added", encoding="utf-8")
        self.tc.init(str(self.root), "git-task")

    def _git(self, *args):
        r = subprocess.run(["git", *args], cwd=str(self.root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.stdout

    def test_save_creates_a_checkpoint_ref_in_a_repo(self):
        self._reinit(git=True)
        head_before = self._git("rev-parse", "HEAD")
        index_before = self._git("ls-files", "-s")
        res = self.tc.save(str(self.root), "step one", conclusion="c")
        self.assertTrue(res["git"]["ok"], res["git"])
        self.assertTrue(res["git"]["ref"], res["git"])
        refs = self._git("for-each-ref", "--format=%(refname)", "refs/checkpoints").split()
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0].endswith("/s0001"), refs)
        # 承诺：不碰用户仓库状态
        self.assertEqual(self._git("rev-parse", "HEAD"), head_before)
        self.assertEqual(self._git("ls-files", "-s"), index_before)
        # 树里要有未 add 的改动和未跟踪文件
        tree = self._git("ls-tree", "--name-only", "-r", refs[0])
        self.assertIn("untracked.txt", tree)
        # 存档目录不能进树
        self.assertNotIn(".checkpoints", tree)

    def test_save_still_works_without_a_repo(self):
        self._reinit(git=False)
        res = self.tc.save(str(self.root), "no git here")
        self.assertEqual(res["index"], 1)
        self.assertEqual(res["git"]["reason"], "not a git repo")
        self.assertTrue(res["git"]["ok"])

    def test_git_can_be_turned_off(self):
        self._reinit(git=True)
        os.environ["TC_GIT"] = "off"
        try:
            res = self.tc.save(str(self.root), "git off")
        finally:
            os.environ.pop("TC_GIT", None)
        self.assertEqual(res["git"]["reason"], "TC_GIT=off")
        self.assertEqual(self._git("for-each-ref", "--format=%(refname)", "refs/checkpoints").split(), [])

    def test_a_git_failure_never_breaks_the_save(self):
        """git 是增强不是基础路径：它挂了，存档必须照旧成功。"""
        self._reinit(git=True)
        original = tc.TaskCheckpoint.git_ref

        def boom(self, root, _state=None):
            raise OSError("git 不见了")

        tc.TaskCheckpoint.git_ref = boom
        try:
            res = self.tc.save(str(self.root), "git broken")
        finally:
            tc.TaskCheckpoint.git_ref = original
        self.assertEqual(res["index"], 1)
        self.assertFalse(res["git"]["ok"])
        self.assertIn("git 不见了", res["git"]["error"])
        # 存档本身真写下去了
        self.assertEqual(len(TaskCheckpoint(self.root).show(str(self.root))["steps"]), 1)

    def test_step_numbers_never_repeat_so_refs_never_collide(self):
        """步骤号由 step_count 只增不复用，所以回退后再存不会撞名。

        这一点值得钉住：它意味着自动建 ref 不会因为回退而失效。
        """
        self._reinit(git=True)
        self.tc.save(str(self.root), "one")
        (self.root / "tracked.txt").write_text("v3", encoding="utf-8")
        self.tc.save(str(self.root), "two")
        self.tc.restore(str(self.root), index=1, apply=True)
        (self.root / "tracked.txt").write_text("v4", encoding="utf-8")
        res = self.tc.save(str(self.root), "three after rollback")
        self.assertTrue(res["git"]["ok"], res["git"])
        self.assertTrue(res["git"]["ref"].endswith("/s0003"), res["git"])
        self.assertEqual(res["index"], 3)

    def test_an_existing_ref_is_refused_and_save_still_succeeds(self):
        """方案要求同名 ref 拒绝覆盖；撞上了也只是丢一个指针，不能弄掛存档。"""
        self._reinit(git=True)
        self.tc.save(str(self.root), "one")
        tid = active_task_id(self.root / ".checkpoints")
        # 抢先占住下一个步骤号的 ref 名
        occupied = f"refs/checkpoints/{tid}/s0002"
        subprocess.run(["git", "update-ref", occupied, "HEAD"], cwd=str(self.root), check=True,
                       capture_output=True)
        (self.root / "tracked.txt").write_text("v3", encoding="utf-8")
        res = self.tc.save(str(self.root), "two")
        self.assertEqual(res["git"]["ok"], False)
        self.assertIn("FileExistsError", res["git"]["error"])
        self.assertEqual(res["index"], 2)
        # 没被覆盖
        self.assertEqual(self._git("rev-parse", occupied).strip(),
                         self._git("rev-parse", "HEAD").strip())

    def test_other_save_branches_also_report_git(self):
        """分支结构要一致：close 和幂等分支也得有 git 字段。"""
        self._reinit(git=True)
        self.tc.save(str(self.root), "one")
        again = self.tc.save(str(self.root), "one")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["git"]["reason"], "no new step")
        closed = self.tc.save(str(self.root), "end", close=True)
        self.assertTrue(closed["closed"])
        self.assertEqual(closed["git"]["reason"], "no new step")


class StoreIsolation(unittest.TestCase):
    """同一个 store 被多个工作区共用时，活动指针与基线索引必须互不串味。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.a = base / "wsA"
        self.b = base / "wsB"
        self.a.mkdir()
        self.b.mkdir()
        self.store = base / "shared-store"
        self.store.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_other_workspace_files_never_leak_into_drift(self):
        self.a.joinpath("a.txt").write_text("a1", encoding="utf-8")
        self.b.joinpath("b.txt").write_text("b1", encoding="utf-8")

        api_a = TaskCheckpoint(self.a)
        api_a.init(str(self.a), "taskA", store=str(self.store))
        api_a.save(str(self.a), "stepA")

        api_b = TaskCheckpoint(self.b)
        api_b.init(str(self.b), "taskB", store=str(self.store))
        drift = api_b.resume(str(self.b))["drift_paths"]
        self.assertNotIn("a.txt", drift)
        self.assertEqual(drift, ["b.txt"])

    def test_active_pointer_is_per_workspace(self):
        self.a.joinpath("a.txt").write_text("a1", encoding="utf-8")
        self.b.joinpath("b.txt").write_text("b1", encoding="utf-8")

        api_a = TaskCheckpoint(self.a)
        task_a = api_a.init(str(self.a), "taskA", store=str(self.store))["task_id"]
        api_a.save(str(self.a), "stepA")

        api_b = TaskCheckpoint(self.b)
        task_b = api_b.init(str(self.b), "taskB", store=str(self.store))["task_id"]
        self.assertNotEqual(task_a, task_b)

        # B 的 init 不能把 A 的活动指针顶掉
        self.assertEqual(TaskCheckpoint(self.a)._active_id(), task_a)
        self.assertEqual(TaskCheckpoint(self.b)._active_id(), task_b)

    def test_index_is_written_per_task(self):
        self.a.joinpath("a.txt").write_text("a1", encoding="utf-8")
        self.b.joinpath("b.txt").write_text("b1", encoding="utf-8")
        api_a = TaskCheckpoint(self.a)
        task_a = api_a.init(str(self.a), "taskA", store=str(self.store))["task_id"]
        api_a.save(str(self.a), "stepA")
        api_b = TaskCheckpoint(self.b)
        task_b = api_b.init(str(self.b), "taskB", store=str(self.store))["task_id"]
        api_b.save(str(self.b), "stepB")

        self.assertFalse((self.store / "index.json").exists())
        index_a = json.loads((self.store / "tasks" / task_a / "index.json").read_text(encoding="utf-8"))
        index_b = json.loads((self.store / "tasks" / task_b / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(index_a), ["a.txt"])
        self.assertEqual(sorted(index_b), ["b.txt"])

    def test_new_task_starts_with_empty_baseline(self):
        self.a.joinpath("a.txt").write_text("a1", encoding="utf-8")
        api = TaskCheckpoint(self.a)
        api.init(str(self.a), "first", store=str(self.store))
        api.save(str(self.a), "step")
        api.init(str(self.a), "second", store=str(self.store))
        # 新任务不能继承上个任务的基线，否则 resume 会把已存在的文件当成无改动
        self.assertEqual(api.resume(str(self.a))["drift_paths"], ["a.txt"])

    def test_legacy_single_key_pointer_is_still_read(self):
        """0.1.1 的 current.json 只有一个 active_task_id，仍要能读出来。"""
        (self.store / "current.json").write_text(
            json.dumps({"schema_version": "1", "active_task_id": "t-legacy"}), encoding="utf-8")
        self.assertEqual(TaskCheckpoint(self.a, store=str(self.store))._active_id(), "t-legacy")
        # 读过一次后按新格式重写，且不动别的键
        TaskCheckpoint(self.a, store=str(self.store))._write_active("t-new")
        table = json.loads((self.store / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(list(table["active"].values()), ["t-new"])


class IncrementalScan(unittest.TestCase):
    """sha 复用是显式选项；默认必须按内容算。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.api = TaskCheckpoint(self.root)
        self.api.init(str(self.root), "inc")
        self._saved_reuse = os.environ.get("TC_SHA_REUSE")
        os.environ.pop("TC_SHA_REUSE", None)

    def tearDown(self):
        if self._saved_reuse is None:
            os.environ.pop("TC_SHA_REUSE", None)
        else:
            os.environ["TC_SHA_REUSE"] = self._saved_reuse
        self._tmp.cleanup()

    def _writes(self, name: str, content: str = "hello") -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _sixty_files(self):
        for index in range(30):
            self._writes(f"f{index}.txt")

    def test_default_hashes_content_every_time(self):
        self._sixty_files()
        self.api.save(str(self.root), "base")
        self.api._watch["content_reads"] = 0
        self.api.save(str(self.root), "again")
        self.assertEqual(self.api._watch["content_reads"], 30, "默认不看 mtime，一律按内容算")

    def test_sha_reuse_opt_in_skips_unchanged_reads(self):
        self._sixty_files()
        self.api.save(str(self.root), "base")
        os.environ["TC_SHA_REUSE"] = "on"
        self.api._watch["content_reads"] = 0
        self.api.save(str(self.root), "again")
        self.assertEqual(self.api._watch["content_reads"], 0)

    def test_reuse_opt_in_still_rereads_the_changed_file(self):
        self._sixty_files()
        self.api.save(str(self.root), "base")
        self._writes("f7.txt", "changed")
        os.environ["TC_SHA_REUSE"] = "on"
        self.api._watch["content_reads"] = 0
        self.api.save(str(self.root), "one change")
        self.assertEqual(self.api._watch["content_reads"], 1)

    def test_resume_does_not_read_content(self):
        self._sixty_files()
        self.api.save(str(self.root), "base")
        self.api._watch["content_reads"] = 0
        self.api.resume(str(self.root))
        self.assertEqual(self.api._watch["content_reads"], 0)

    def test_mtime_preserving_change_is_not_missed(self):
        """mtime 被还原且长度不变时，默认仍必须记录真实内容。

        这是 TC_SHA_REUSE 关着的理由：那是唯一一条会静默把错内容记进步骤的路径。
        """
        target = self.root / "a.txt"
        target.write_text("AAAA", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        stat = target.stat()
        target.write_text("BBBB", encoding="utf-8")
        os.utime(target, ns=(stat.st_mtime_ns, stat.st_mtime_ns))
        self.api.save(str(self.root), "s2")

        task_dir = self.root / ".checkpoints" / "tasks" / active_task_id(self.root / ".checkpoints")
        ref = json.loads((task_dir / "steps" / "s0002.json").read_text(encoding="utf-8"))["manifest_ref"]
        recorded = self.api._load_manifest(task_dir.name, ref)["a.txt"]["sha256"]
        self.assertEqual(recorded, tc._sha(b"BBBB"), "步骤必须记真实内容，不能复用旧 sha")

        target.write_text("CCCCCCCC", encoding="utf-8")
        self.api.restore(str(self.root), index=2, apply=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "BBBB", "回退第 2 步必须给回当时的 BBBB")

    def test_index_keeps_sha_for_the_reuse_path(self):
        self._writes("a.txt", "one")
        self.api.save(str(self.root), "base")
        index = json.loads((self.root / ".checkpoints" / "tasks" / active_task_id(self.root / ".checkpoints") / "index.json").read_text(encoding="utf-8"))
        self.assertIn("sha256", index["a.txt"])
        os.environ["TC_SHA_REUSE"] = "on"
        fresh = TaskCheckpoint(self.root)
        fresh._watch["content_reads"] = 0
        fresh.save(str(self.root), "again")
        self.assertTrue(fresh._watch["content_reads"] == 0, "新实例应该能从索引里复用 sha")

    def test_sensitive_file_never_gets_sha_or_payload(self):
        self._writes(".env", "TOKEN=x")
        self.api.save(str(self.root), "base")
        index = json.loads((self.root / ".checkpoints" / "tasks" / active_task_id(self.root / ".checkpoints") / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn("sha256", index[".env"])
        self.assertTrue(index[".env"]["sensitive"])

    def test_payload_write_is_verified_against_sha(self):
        self._writes("a.txt", "real content")
        api = TaskCheckpoint(self.root)
        # sha 对不上盘上内容时，宁可少存一个 payload，也不能存错内容
        api._store_payloads({"a.txt": {"mtime": 1, "size": 12, "sha256": "0" * 64}})
        self.assertFalse((api.store / "payload" / "00" / ("0" * 64)).exists())
        self.assertTrue(any("changed mid-save" in item for item in api._health))
        # 对得上就正常写
        from tc import _sha
        digest = _sha(b"real content")
        api._store_payloads({"a.txt": {"mtime": 1, "size": 12, "sha256": digest}})
        self.assertEqual((api.store / "payload" / digest[:2] / digest).read_bytes(), b"real content")
    def test_restore_puts_back_the_file_mode(self):
        """POSIX 验可执行位；Windows 只有只读位，就验只读位。"""
        target = self.root / "run.sh"
        target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        keep = 0o755 if os.name != "nt" else 0o444
        other = 0o600 if os.name != "nt" else 0o666
        os.chmod(target, keep)
        self.api.save(str(self.root), "with mode")
        # 内容和权限都改了，回退时两个都要回来
        os.chmod(target, other)  # 先放开写位（Windows 只读时写不进去）
        target.write_text("changed", encoding="utf-8")
        self.api.restore(str(self.root), index=1, apply=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "#!/bin/sh\necho hi\n")
        self.assertEqual(os.stat(target).st_mode & 0o777, keep, "权限位应该被还原")
        os.chmod(target, 0o666 if os.name == "nt" else 0o644)  # 让 tearDown 能清理

    def test_restore_can_replace_a_read_only_file(self):
        """Windows 上 os.replace 盖不掉只读文件，restore 不能因此挂掉。"""
        target = self.root / "locked.txt"
        target.write_text("v1", encoding="utf-8")
        self.api.save(str(self.root), "base")
        target.write_text("v2", encoding="utf-8")
        os.chmod(target, 0o444)
        try:
            self.api.restore(str(self.root), index=1, apply=True)
            self.assertEqual(target.read_text(encoding="utf-8"), "v1")
        finally:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)

    def test_sensitive_directory_is_detected(self):
        """只看文件名会漏掉 secrets/api.txt 这种——目录名也要看。"""
        for rel in ("secrets/api.txt", "a/b/.ssh/config", "credentials/prod.json"):
            self._writes(rel, "sk-REAL")
        self._writes("src/tokenizer.py", "normal code")
        self.api.save(str(self.root), "base")
        index = json.loads((self.root / ".checkpoints" / "tasks" / active_task_id(self.root / ".checkpoints") / "index.json").read_text(encoding="utf-8"))
        for rel in ("secrets/api.txt", "a/b/.ssh/config", "credentials/prod.json"):
            self.assertTrue(index[rel]["sensitive"], f"{rel} 应被判为敏感")
            self.assertNotIn("sha256", index[rel], f"{rel} 不应该有 sha（内容未入库）")
        self.assertFalse(index["src/tokenizer.py"].get("sensitive"))
        self.assertIn("sha256", index["src/tokenizer.py"])
        blob = b"".join(p.read_bytes() for p in (self.root / ".checkpoints" / "payload").rglob("*") if p.is_file())
        self.assertNotIn(b"sk-REAL", blob)


class RestoreSafety(unittest.TestCase):
    """回退路径的三个“静默出错”风险：丢文件、被 .env 连坐、清单跨 store 串味。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()
        self.api = TaskCheckpoint(self.root)
        self.api.init(str(self.root), "safety")

    def tearDown(self):
        self._tmp.cleanup()

    def _three_steps(self, api=None):
        api = api or self.api
        for index, text in enumerate(("one", "two", "three"), 1):
            (self.root / "a.txt").write_text(text, encoding="utf-8")
            (self.root / f"f{index}.txt").write_text(text, encoding="utf-8")
            api.save(str(self.root), f"s{index}")

    def _flaky_at(self, api, ordinal):
        """让第 ordinal 次 _copy_file 调用抛错。"""
        original = api._copy_file
        seen = {"n": 0}

        def flaky(source, destination):
            seen["n"] += 1
            if seen["n"] == ordinal:
                raise OSError("injected")
            return original(source, destination)

        api._copy_file = flaky
        return original, seen

    def test_backup_phase_failure_does_not_lose_files(self):
        self._three_steps()
        (self.root / "a.txt").write_text("MODIFIED", encoding="utf-8")
        (self.root / "f3.txt").unlink()
        (self.root / "new.txt").write_text("new", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

        original, _ = self._flaky_at(self.api, 2)   # 第 2 个文件备份时就失败
        try:
            with self.assertRaises(OSError):
                self.api.restore(str(self.root), index=1, apply=True)
        finally:
            self.api._copy_file = original

        after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(before, after, "备份阶段失败后工作区必须原样不动")
        self.assertFalse(list((self.root / ".checkpoints").glob(".restore-*")))

    def test_failure_after_backups_still_rolls_back_exactly(self):
        self._three_steps()
        (self.root / "a.txt").write_text("MODIFIED", encoding="utf-8")
        (self.root / "f3.txt").unlink()
        (self.root / "new.txt").write_text("new", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}

        # 3 个受影响的文件各备份 1 次，第 4 次调用已经是真正写工作区那一步了
        original, _ = self._flaky_at(self.api, 4)
        try:
            with self.assertRaises(OSError):
                self.api.restore(str(self.root), index=1, apply=True)
        finally:
            self.api._copy_file = original

        after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(before, after, "真正改过工作区后失败，回滚必须逐字节完美")

    def test_preview_reports_file_whose_payload_is_gone(self):
        target = self.root / "a.txt"
        target.write_text("AAAA", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        # 直接把 payload 删掉：不管它是被回收、被手工清掉还是写入校验没过
        digest = tc._sha(b"AAAA")
        (self.root / ".checkpoints" / "payload" / digest[:2] / digest).unlink()
        target.write_text("DDDDDDDD", encoding="utf-8")

        preview = self.api.restore(str(self.root), index=1, apply=False)
        self.assertIn("a.txt", preview["plan"]["unrestorable"], "payload 不在就必须报不可回退")
        result = self.api.restore(str(self.root), index=1, apply=True)
        self.assertIn("a.txt", result["plan"]["unrestorable"])
        self.assertEqual(target.read_text(encoding="utf-8"), "DDDDDDDD")

    def test_manifest_cache_is_scoped_by_store(self):
        base = Path(self._tmp.name)
        other = base / "other"
        other.mkdir()
        (self.root / "alpha.txt").write_text("ALPHA", encoding="utf-8")
        self.api.save(str(self.root), "s1")          # 真实 store 里也落一份 m0001
        task_id = active_task_id(self.root / ".checkpoints")
        manifest_dir = other / ".checkpoints" / "tasks" / task_id / "manifest"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "m0001.json").write_text(json.dumps({
            "version": "1", "mode": "full",
            "entries": {"beta.txt": {"mtime": 1, "size": 5, "mode": 420, "sha256": "b" * 64}},
        }), encoding="utf-8")
        tc._MANIFEST_CACHE.clear()
        mine = TaskCheckpoint(self.root)._load_manifest(task_id, "m0001.json")
        theirs = TaskCheckpoint(other)._load_manifest(task_id, "m0001.json")
        self.assertNotEqual(sorted(mine), sorted(theirs), "两个 store 的同名清单不能互相污染")
        self.assertEqual(sorted(mine), ["alpha.txt"])
        self.assertEqual(sorted(theirs), ["beta.txt"])

    def test_import_handoff_honours_the_configured_store(self):
        base = Path(self._tmp.name)
        out = base / "out"
        out.mkdir()
        (self.root / "alpha.txt").write_text("ALPHA", encoding="utf-8")
        self.api.save(str(self.root), "s1")
        package = self.api.export(str(self.root), str(out))["path"]
        target = base / "target"
        target.mkdir()
        external = base / "extstore"
        TaskCheckpoint(target, store=str(external)).import_handoff(package, str(target))
        self.assertTrue(external.exists(), "显式指定的 store 不能被忽略")
        self.assertFalse((target / ".checkpoints").exists())


class SensitiveDetection(unittest.TestCase):
    """敏感判定两头都要管：漏判 = 凭据明文进 payload；误拦 = 源码回退静默失效。"""

    # 真凭据，必须拦
    MUST_BLOCK = (
        ".env", "prod.env", "my.secret.env.local", ".envrc", ".npmrc", ".pypirc", ".pgpass",
        ".htpasswd", ".netrc", ".my.cnf", "my.cnf", "passwd", "shadow", "kubeconfig",
        "id_rsa", "id_ed25519", "app.key", "server.pem", "certificat.p12", "keystore.jks",
        "ssh_host_rsa_key", "client_key", "terraform.tfstate", "prod/terraform.tfvars",
        "auth.jwt", "settings.xml", "wp-config.php", "web.config", "settings.py",
        "wrangler.toml", ".dev.vars", "credentials", "creds", "secret.json",
        "prod_credentials.json", "credentials.xml", "mypassword.txt", "auth_token.txt",
        "service-account.json", "appsettings.json", "gcp-creds.json", "creds.json", "my_creds.txt",
        "mytoken", "oauth_token",
        "secrets/api.txt", ".ssh/id_rsa", ".aws/credentials", "keys/id_rsa", "credentials/prod.json",
    )
    # 源码 / 非凭据，不能拦
    MUST_PASS = (
        "src/app.py", "src/tokenizer.py", "src/tokens.py", "src/keystore.py", "src/keyboard.py",
        "tests/test_token.py", "src/password_policy.py", "docs/secrets.md",
        "src/my_secrets_helper.py", "src/api_key_utils.py", "lib/credentials_manager.py",
        "id_rsa.pub", "author.json",
    )

    def test_credentials_are_blocked(self):
        for rel in self.MUST_BLOCK:
            self.assertTrue(tc._sensitive(rel), f"{rel} 是凭据，必须判敏感")

    def test_source_files_are_not_blocked(self):
        for rel in self.MUST_PASS:
            self.assertFalse(tc._sensitive(rel), f"{rel} 不是凭据，拦了会让它回退时静默失效")

    def test_innocuous_names_are_a_known_gap(self):
        """名字无辜的凭据按名字抓不到：这是黑名单的固有局限，只能靠文档说清 + exclude。"""
        for rel in ("config/database.yaml", "docker-compose.yml", "config.json"):
            self.assertFalse(tc._sensitive(rel))

    def test_blocked_file_content_never_reaches_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            marker = b"sk-REAL-SECRET"
            blocked = ("prod.env", "kubeconfig", ".my.cnf", "credentials/prod.json", ".envrc")
            for name in blocked:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(marker)
            (root / "app.py").write_bytes(b"normal")
            api = TaskCheckpoint(root)
            api.init(str(root), "t")
            api.save(str(root), "s1")
            blob = b"".join(p.read_bytes() for p in (root / ".checkpoints" / "payload").rglob("*")
                            if p.is_file())
            self.assertNotIn(marker, blob)


class PostReviewFixes(unittest.TestCase):
    """独立评审挖出的具体缺陷，逐条钉住。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_git_fingerprint_really_covers_head(self):
        """自证必须真的含 HEAD。v1 的 -b 只给 `## master`，拿不到 oid；v2 才有 branch.oid。

        曾经为了省一次 git 调用改用 v1 --branch，HEAD 就从自证里默默消失了。
        """
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        v2 = subprocess.run(["git", "status", "--porcelain=v2", "--branch", "-z"], cwd=self.root,
                            capture_output=True, text=True).stdout
        v1 = subprocess.run(["git", "status", "--porcelain=v1", "--branch", "-z"], cwd=self.root,
                            capture_output=True, text=True).stdout
        self.assertNotIn(head, v1, "v1 -b 不含 HEAD 的 oid，不能再用它拿 HEAD")
        self.assertNotIn(head, v2, "未提交时 HEAD 还没指向任何 commit")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                       cwd=self.root, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root,
                              capture_output=True, text=True).stdout.strip()
        v2 = subprocess.run(["git", "status", "--porcelain=v2", "--branch", "-z"], cwd=self.root,
                            capture_output=True, text=True).stdout
        v1 = subprocess.run(["git", "status", "--porcelain=v1", "--branch", "-z"], cwd=self.root,
                            capture_output=True, text=True).stdout
        self.assertIn(head, v2, "v2 必须带 HEAD 的 oid")
        self.assertNotIn(head, v1)

    def test_git_self_check_still_reports_unchanged(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        api = TaskCheckpoint(self.root)
        api.init(str(self.root), "t")
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        api.save(str(self.root), "s1")
        (self.root / "a.txt").write_text("two", encoding="utf-8")
        result = api.save(str(self.root), "s2")["git"]
        self.assertTrue(result["ok"])
        self.assertTrue(result["unchanged"], "自证要报工作区/索引/HEAD 都没动")

    def test_export_skips_files_whose_payload_is_gone(self):
        """和 restore 同一套降级：丢了的 payload 只跳过并上报，不让整次导出失败。"""
        api = TaskCheckpoint(self.root)
        api.init(str(self.root), "t")
        (self.root / "keep.txt").write_text("keep", encoding="utf-8")
        (self.root / "gone.bin").write_bytes(b"gone-content")
        api.save(str(self.root), "s1")
        digest = tc._sha(b"gone-content")
        (self.root / ".checkpoints" / "payload" / digest[:2] / digest).unlink()
        out = Path(self._tmp.name) / "out"
        out.mkdir()
        result = api.export(str(self.root), str(out))
        self.assertIn("gone.bin", result["filtered"])
        self.assertIn("keep.txt", result["files"])
        self.assertFalse((Path(result["path"]) / "files" / "gone.bin").exists())


class ExplicitReturns(unittest.TestCase):
    """会改变状态、或什么都不做的调用，要在返回值里说清楚，别让调用方去猜。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_reports_when_it_switched_the_active_task(self):
        api = TaskCheckpoint(self.root)
        first = api.init(str(self.root), "first")["task_id"]
        (self.root / "a.txt").write_text("one", encoding="utf-8")
        api.save(str(self.root), "s1")
        second = api.init(str(self.root), "second")["task_id"]

        # 存回 first 会把活动任务切过去（既有行为），但这个副作用必须显式报出来
        back = api.save(str(self.root), "into first", task_id=first)
        self.assertTrue(back["active_task_changed"], "切了活动任务就要说")
        self.assertEqual(active_task_id(self.root / ".checkpoints"), first)

        api.switch(str(self.root), second)
        same = api.save(str(self.root), "into second")
        self.assertFalse(same["active_task_changed"])

    def test_compress_explains_a_zero_result(self):
        """converted=0 不能是个谜：默认保留 7 天，多数情况下当然什么都不回收。"""
        api = TaskCheckpoint(self.root)
        api.init(str(self.root), "t")
        (self.root / "a.txt").write_text("v1", encoding="utf-8")
        api.save(str(self.root), "s1")
        result = api.compress()
        self.assertEqual(result["converted"], 0)
        self.assertEqual(result["keep_seconds"], 7 * 24 * 3600)
        self.assertEqual(result["bytes_freed"], 0)
        for key in ("eligible", "already_gone", "minimum", "maximum"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
