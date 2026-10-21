"""MessageEmitter thread/path concurrency and compatibility checks; no live services."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from acps_sdk.amp.models import MessageBody, MessageDestination

from acps_sdk.amp import message_emitter as module
Emitter = module.MessageEmitter


def body(i=0):
    return MessageBody(event_type="send", system="rabbitmq",
                       destination=MessageDestination(name="synthetic", kind="exchange"),
                       message_id=f"synthetic-{i}", payload_size_bytes=128,
                       attributes={"synthetic": "中" * (400 if i % 2 else 800)})


class MessageEmitterConcurrencyTests(unittest.TestCase):
    def test_relative_absolute_and_case_aliases_keep_original_sink_after_chdir(self):
        with tempfile.TemporaryDirectory() as directory:
            first_dir, later_dir = Path(directory) / "first", Path(directory) / "later"
            first_dir.mkdir()
            later_dir.mkdir()
            path = first_dir / "records.jsonl"
            previous = Path.cwd()
            try:
                os.chdir(first_dir)
                relative = Emitter(Path("./records.jsonl"), "synthetic")
                absolute = Emitter(path, "synthetic")
                emitters = [relative, absolute]
                if sys.platform == "win32":
                    emitters.append(Emitter(Path(str(path).upper()), "synthetic"))
                os.chdir(later_dir)
                ids = [emitter.emit_sync(body(i)) for i, emitter in enumerate(emitters)]
            finally:
                os.chdir(previous)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(Counter(row["log_id"] for row in records), Counter(ids))
            self.assertFalse((later_dir / "records.jsonl").exists())

    def test_mixed_async_sync_instances_serialize_complete_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            # Two instances writing mixed sync/async records to one destination.
            first, second = Emitter(path, "synthetic"), Emitter(path.parent / "." / path.name, "synthetic")
            guard = threading.Lock()
            active, max_active = 0, 0
            original = Path.open

            @contextmanager
            def tracked_open(target, *args, **kwargs):
                nonlocal active, max_active
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    with original(target, *args, **kwargs) as stream:
                        time.sleep(0.001)  # Deliberately allow concurrent writers to meet here.
                        yield stream
                finally:
                    with guard:
                        active -= 1

            async def write_all():
                with ThreadPoolExecutor(max_workers=4) as pool:
                    sync = [pool.submit(second.emit_sync, body(i)) for i in range(40)]
                    async_ids = await asyncio.gather(*(first.emit(body(i + 40)) for i in range(40)))
                    return [future.result() for future in sync] + async_ids

            with patch.object(Path, "open", tracked_open):
                ids = asyncio.run(write_all())
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(max_active, 1)
            self.assertEqual(Counter(row["log_id"] for row in records), Counter(ids))
            self.assertEqual(len(records), 80)

    def test_different_files_can_write_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            emitters = [Emitter(Path(directory) / f"{i}.jsonl", "synthetic") for i in range(2)]
            barrier = threading.Barrier(2, timeout=3)
            original = Path.open

            @contextmanager
            def rendezvous(target, *args, **kwargs):
                with original(target, *args, **kwargs) as stream:
                    barrier.wait()
                    yield stream

            with patch.object(Path, "open", rendezvous), ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(lambda emitter: emitter.emit_sync(body()), emitters))
            for emitter, log_id in zip(emitters, ids):
                self.assertEqual(json.loads(emitter._log_file.read_text(encoding="utf-8"))["log_id"], log_id)

    def test_schema_resource_and_trace_fields_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            emitter = Emitter(path, "synthetic", resource={"service.name": "synthetic-service"})
            log_id = emitter.emit_sync(body(), trace_id="trace", span_id="span", correlation_id="session")
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["log_id"], log_id)
            self.assertEqual(record["body"]["eventType"], "send")
            self.assertEqual(record["trace_id"], "trace")
            self.assertEqual(record["correlation_id"], "session")
            self.assertEqual(record["resource"], {"service.name": "synthetic-service"})
            self.assertNotIn("integrity", record)
            self.assertNotIn("severity_text", record)

    def test_write_error_warns_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            emitter = Emitter(path, "synthetic")
            with patch.object(Path, "open", side_effect=OSError("synthetic write failure")):
                with self.assertLogs(module.__name__, level="WARNING"):
                    self.assertTrue(emitter.emit_sync(body()))
            # A second worker must acquire the same lock after the failed write.
            with ThreadPoolExecutor(max_workers=1) as pool:
                log_id = pool.submit(emitter.emit_sync, body()).result(timeout=3)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["log_id"], log_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
