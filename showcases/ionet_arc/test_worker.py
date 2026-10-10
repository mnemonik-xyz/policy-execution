"""Unit tests for multi-job transcription Worker and helper routines."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from showcases.ionet_arc.worker import (
    Worker,
    clear_model_cache,
    format_event,
    get_model,
    gpu_metrics,
    log_event,
    transcribe,
)


class WorkerModelCacheTests(unittest.TestCase):
    def setUp(self):
        clear_model_cache()

    def tearDown(self):
        clear_model_cache()

    def test_get_model_caches_instances(self):
        mock_fw = MagicMock()
        mock_whisper = mock_fw.WhisperModel
        mock_whisper.side_effect = lambda *args, **kwargs: MagicMock()

        with patch.dict(sys.modules, {"faster_whisper": mock_fw}):
            model1 = get_model("/models/whisper", device="cuda", compute_type="float16")
            model2 = get_model("/models/whisper", device="cuda", compute_type="float16")
            self.assertIs(model1, model2)
            mock_whisper.assert_called_once_with(
                "/models/whisper", device="cuda", compute_type="float16", local_files_only=True
            )

            # Different configuration instantiates new model
            model3 = get_model("/models/whisper", device="cpu", compute_type="default")
            self.assertIsNot(model1, model3)
            self.assertEqual(mock_whisper.call_count, 2)

            # clear_model_cache clears instances
            clear_model_cache()
            model4 = get_model("/models/whisper", device="cuda", compute_type="float16")
            self.assertEqual(mock_whisper.call_count, 3)
            self.assertIsNot(model1, model4)

    def test_transcribe_uses_cached_model(self):
        mock_fw = MagicMock()
        mock_whisper = mock_fw.WhisperModel
        mock_model = MagicMock()
        mock_segment = MagicMock(start=0.0, end=1.2, text="hello world")
        mock_info = MagicMock(language="en", duration=1.2)
        mock_model.transcribe.return_value = ([mock_segment], mock_info)
        mock_whisper.return_value = mock_model

        with patch.dict(sys.modules, {"faster_whisper": mock_fw}):
            result = transcribe("dummy.wav", "/models/test", language="en")
            self.assertEqual(result["language"], "en")
            self.assertEqual(result["audio_seconds"], 1.2)
            self.assertEqual(result["segments"][0]["text"], "hello world")
            mock_model.transcribe.assert_called_once_with("dummy.wav", beam_size=5, language="en")


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.token = "a" * 32

    def tearDown(self):
        self.tmp.cleanup()

    def test_token_validation(self):
        with self.assertRaises(ValueError):
            Worker(self.root, "short-token")

    def test_sequential_queue_processing(self):
        executed_order = []
        lock = threading.Lock()

        def engine(audio, model_dir, language=None):
            with lock:
                executed_order.append(audio.read_bytes())
            time.sleep(0.02)
            return {"language": language or "en", "segments": []}

        with Worker(self.root, self.token, engine=engine) as worker:
            audio1 = b"audio payload one"
            h1 = hashlib.sha256(audio1).hexdigest()
            code1, _ = worker.submit(audio1, h1, language="en")
            self.assertEqual(code1, 202)

            audio2 = b"audio payload two"
            h2 = hashlib.sha256(audio2).hexdigest()
            code2, _ = worker.submit(audio2, h2, language="es")
            self.assertEqual(code2, 202)

            # Wait for both jobs to finish
            for _ in range(100):
                j1 = worker.get(h1)
                j2 = worker.get(h2)
                if j1 and j1["status"] == "complete" and j2 and j2["status"] == "complete":
                    break
                time.sleep(0.01)

            self.assertEqual(worker.get(h1)["status"], "complete")
            self.assertEqual(worker.get(h2)["status"], "complete")
            self.assertEqual(executed_order, [audio1, audio2])

    def test_audio_cleanup_on_completion_and_failure(self):
        def failing_engine(audio, model_dir):
            raise RuntimeError("transcription failed intentionally")

        with Worker(self.root, self.token, engine=failing_engine) as worker:
            payload = b"audio that will trigger error"
            h = hashlib.sha256(payload).hexdigest()
            code, _ = worker.submit(payload, h)
            self.assertEqual(code, 202)

            for _ in range(100):
                res = worker.get(h)
                if res and res["status"] in {"complete", "failed"}:
                    break
                time.sleep(0.01)

            self.assertEqual(worker.get(h)["status"], "failed")
            audio_path = self.root / "inputs" / f"{h}.audio"
            self.assertFalse(audio_path.exists(), "Temporary audio must be unlinked after job ends")

    def test_warrant_keep_inputs_preserves_audio(self):
        def echo_engine(audio, model_dir):
            return {"language": "en", "segments": []}

        with patch.dict(os.environ, {"WARRANT_KEEP_INPUTS": "1"}):
            with Worker(self.root, self.token, engine=echo_engine) as worker:
                payload = b"audio to keep on disk"
                h = hashlib.sha256(payload).hexdigest()
                worker.submit(payload, h)

                for _ in range(100):
                    res = worker.get(h)
                    if res and res["status"] == "complete":
                        break
                    time.sleep(0.01)

                audio_path = self.root / "inputs" / f"{h}.audio"
                self.assertTrue(audio_path.exists(), "Audio must be retained when WARRANT_KEEP_INPUTS is set")

    def test_duplicate_submission_does_not_reprocess(self):
        call_count = 0

        def count_engine(audio, model_dir):
            nonlocal call_count
            call_count += 1
            return {"language": "en", "segments": []}

        with Worker(self.root, self.token, engine=count_engine) as worker:
            payload = b"unique audio for duplicate check"
            h = hashlib.sha256(payload).hexdigest()
            code1, _ = worker.submit(payload, h)
            self.assertEqual(code1, 202)

            for _ in range(100):
                if worker.get(h)["status"] == "complete":
                    break
                time.sleep(0.01)

            # Re-submit identical audio
            code2, job2 = worker.submit(payload, h)
            self.assertEqual(code2, 200)
            self.assertEqual(job2["status"], "complete")
            self.assertEqual(call_count, 1)

    def test_restart_marks_uncompleted_jobs_interrupted(self):
        state_file = self.root / "jobs.json"
        unfinished_id = "0" * 64
        state_file.write_text(
            json.dumps(
                {
                    "jobs": {
                        unfinished_id: {"job_id": unfinished_id, "input_sha256": unfinished_id, "status": "running"},
                    }
                }
            )
        )

        worker = Worker(self.root, self.token)
        try:
            self.assertEqual(worker.get(unfinished_id)["status"], "interrupted")
        finally:
            worker.close()

    def test_closed_worker_rejects_new_submissions(self):
        worker = Worker(self.root, self.token)
        worker.close()
        code, err = worker.submit(b"test", hashlib.sha256(b"test").hexdigest())
        self.assertEqual(code, 503)
        self.assertIn("shut down", err["error"])

    def test_gpu_metrics(self):
        mock_res = MagicMock()
        mock_res.stdout = "0, 78, 1024, 24576, 65, 201.5\n"
        with patch("subprocess.run", return_value=mock_res):
            metrics = gpu_metrics()
            self.assertTrue(metrics["available"])
            self.assertEqual(
                metrics["gpus"],
                [
                    {
                        "index": "0",
                        "utilization_percent": "78",
                        "memory_used_mib": "1024",
                        "memory_total_mib": "24576",
                        "temperature_celsius": "65",
                        "power_watts": "201.5",
                    }
                ],
            )

        with patch("subprocess.run", side_effect=FileNotFoundError):
            metrics = gpu_metrics()
            self.assertFalse(metrics["available"])
            self.assertEqual(metrics["reason"], "nvidia_smi_unavailable")


class WorkerLoggingTests(unittest.TestCase):
    def test_format_event_human_readable(self):
        gpu_info = {
            "available": True,
            "gpus": [
                {
                    "index": "0",
                    "utilization_percent": "75",
                    "memory_used_mib": "1024",
                    "memory_total_mib": "24576",
                    "temperature_celsius": "62",
                    "power_watts": "180.0",
                }
            ],
        }

        # worker_started
        ws = format_event("worker_started", job_count=3, gpu=gpu_info)
        self.assertIn("Worker started", ws)
        self.assertIn("Existing jobs: 3", ws)
        self.assertIn("GPU 0: 75% util", ws)

        # job_accepted
        ja = format_event(
            "job_accepted", job_id="abcdef1234567890", audio_bytes=2048, queue_depth=1, requested_language="en"
        )
        self.assertIn("Job [abcdef123456] accepted", ja)
        self.assertIn("Size: 2.0 KiB", ja)
        self.assertIn("Language: en", ja)

        # job_started
        js = format_event("job_started", job_id="abcdef1234567890", gpu=gpu_info)
        self.assertIn("Job [abcdef123456] started transcription", js)
        self.assertIn("GPU 0: 75% util", js)

        # job_completed
        jc = format_event("job_completed", job_id="abcdef1234567890", elapsed_seconds=1.234, gpu=gpu_info)
        self.assertIn("Job [abcdef123456] completed successfully in 1.23s", jc)

        # job_failed
        jf = format_event("job_failed", job_id="abcdef1234567890", gpu=None)
        self.assertIn("Job [abcdef123456] transcription failed | GPU: N/A", jf)

        # job_duplicate
        jd = format_event("job_duplicate", job_id="abcdef1234567890", status="complete")
        self.assertIn("Job [abcdef123456] duplicate submitted | Status: complete", jd)

        # jobs_interrupted_after_restart
        ji = format_event("jobs_interrupted_after_restart", count=2)
        self.assertIn("marked 2 incomplete job(s) as interrupted", ji)

    @patch("showcases.ionet_arc.worker.LOGGER.info")
    def test_log_event_modes(self, mock_info):
        # Default mode: human-readable string
        log_event("worker_started", job_count=0, gpu=None)
        mock_info.assert_called_with("%s", "Worker started | Existing jobs: 0 | GPU: N/A")

        # JSON mode with WARRANT_LOG_JSON=1
        with patch.dict(os.environ, {"WARRANT_LOG_JSON": "1"}):
            log_event("worker_started", job_count=0)
            args = mock_info.call_args[0]
            self.assertEqual(args[0], "%s")
            parsed = json.loads(args[1])
            self.assertEqual(parsed["event"], "worker_started")
            self.assertEqual(parsed["job_count"], 0)


if __name__ == "__main__":
    unittest.main()
