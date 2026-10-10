"""Multi-job HTTP transcription worker for io.net CaaS.

Audio is uploaded directly: no server-side fetching of caller-supplied URLs.
Only public/non-sensitive audio is intended for this pilot. Output provenance
is operational evidence, not a zkVM proof of transcription correctness.
"""

import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MODEL_ID = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MAX_AUDIO_BYTES = 64 * 1024 * 1024
LOGGER = logging.getLogger(__name__)

_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_LOCK = threading.Lock()


def get_model(model_dir: str | Path, device: str = "cuda", compute_type: str = "float16") -> Any:
    """Retrieve or lazily instantiate a WhisperModel cached by (model_dir, device, compute_type)."""
    key = (str(model_dir), device, compute_type)
    with _MODEL_LOCK:
        if key not in _MODEL_CACHE:
            from faster_whisper import WhisperModel

            _MODEL_CACHE[key] = WhisperModel(
                str(model_dir), device=device, compute_type=compute_type, local_files_only=True
            )
        return _MODEL_CACHE[key]


def clear_model_cache() -> None:
    """Clear cached WhisperModel instances (useful for testing or releasing VRAM)."""
    with _MODEL_LOCK:
        _MODEL_CACHE.clear()


def gpu_metrics() -> dict[str, Any]:
    """Return a best-effort GPU utilization snapshot without failing a job."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError):
        return {"available": False, "reason": "nvidia_smi_unavailable"}
    except subprocess.TimeoutExpired:
        return {"available": False, "reason": "nvidia_smi_timed_out"}
    except subprocess.CalledProcessError:
        return {"available": False, "reason": "nvidia_smi_failed"}

    metrics = []
    for line in result.stdout.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) != 6:
            continue
        metrics.append(
            {
                "index": values[0],
                "utilization_percent": values[1],
                "memory_used_mib": values[2],
                "memory_total_mib": values[3],
                "temperature_celsius": values[4],
                "power_watts": values[5],
            }
        )
    return {"available": True, "gpus": metrics}


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.2f} MiB"


def _format_gpu(gpu: dict[str, Any] | None) -> str:
    if not gpu:
        return "N/A"
    if not gpu.get("available"):
        return f"N/A ({gpu.get('reason', 'unavailable')})"
    entries = []
    for g in gpu.get("gpus", []):
        entries.append(
            f"GPU {g['index']}: {g['utilization_percent']}% util, "
            f"{g['memory_used_mib']}/{g['memory_total_mib']} MiB, {g['temperature_celsius']}°C"
        )
    return ", ".join(entries) if entries else "N/A"


def format_event(event: str, **fields: Any) -> str:
    """Format operational events into pretty human-readable log messages."""
    job_id = fields.get("job_id", "")
    short_id = f"[{job_id[:12]}] " if job_id else ""
    gpu_str = _format_gpu(fields.get("gpu"))

    if event == "worker_started":
        count = fields.get("job_count", 0)
        return f"Worker started | Existing jobs: {count} | GPU: {gpu_str}"
    if event == "jobs_interrupted_after_restart":
        count = fields.get("count", 0)
        return f"Worker restart: marked {count} incomplete job(s) as interrupted"
    if event == "job_accepted":
        size = _format_bytes(fields.get("audio_bytes", 0))
        depth = fields.get("queue_depth", 1)
        lang = fields.get("requested_language") or "auto"
        return f"Job {short_id}accepted | Size: {size} | Queue depth: {depth} | Language: {lang}"
    if event == "job_duplicate":
        status = fields.get("status", "unknown")
        return f"Job {short_id}duplicate submitted | Status: {status}"
    if event == "job_started":
        return f"Job {short_id}started transcription | GPU: {gpu_str}"
    if event == "job_running":
        return f"Job {short_id}in progress | GPU: {gpu_str}"
    if event == "job_completed":
        elapsed = fields.get("elapsed_seconds", 0.0)
        return f"Job {short_id}completed successfully in {elapsed:.2f}s | GPU: {gpu_str}"
    if event == "job_failed":
        return f"Job {short_id}transcription failed | GPU: {gpu_str}"

    parts = [f"Event: {event}"]
    for k, v in sorted(fields.items()):
        parts.append(f"{k}={v}")
    return " | ".join(parts)


def log_event(event: str, **fields: Any) -> None:
    """Emit pretty human-readable log message, or JSON if WARRANT_LOG_JSON=1."""
    if os.getenv("WARRANT_LOG_JSON", "").lower() in ("1", "true"):
        LOGGER.info("%s", json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True))
        return
    LOGGER.info("%s", format_event(event, **fields))


def gpu_log_interval() -> float:
    try:
        return max(float(os.getenv("WARRANT_GPU_LOG_INTERVAL_SECONDS", "10")), 1.0)
    except ValueError:
        return 10.0


def transcribe(audio: str | Path, model_dir: str | Path, language: str | None = None) -> dict[str, Any]:
    """Transcribe audio file using cached WhisperModel instance."""
    device = os.getenv("WARRANT_DEVICE", "cuda")
    compute_type = os.getenv("WARRANT_COMPUTE_TYPE", "float16" if device == "cuda" else "default")
    lang = language or os.getenv("WARRANT_LANGUAGE")
    model = get_model(model_dir, device=device, compute_type=compute_type)
    segments, info = model.transcribe(str(audio), beam_size=5, language=lang)
    return {
        "language": info.language,
        "audio_seconds": info.duration,
        "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments],
    }


class Worker:
    def __init__(self, root: str | Path, token: str, engine=transcribe, model_dir: str | Path = "/model"):
        if not token or len(token) < 32:
            raise ValueError("Set WARRANT_WORKER_TOKEN to a random token of at least 32 characters")
        self.root, self.token = Path(root), token
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine, self.model_dir = engine, model_dir
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self._closed = False
        self._queue: queue.Queue = queue.Queue()
        self.jobs = self.load_jobs()

        # A restarted process must not silently rerun an already accepted job.
        interrupted = False
        for job in self.jobs.values():
            if job["status"] in {"queued", "running"}:
                job["status"] = "interrupted"
                interrupted = True
        if interrupted:
            self.persist()
            log_event(
                "jobs_interrupted_after_restart",
                count=sum(job["status"] == "interrupted" for job in self.jobs.values()),
            )

        self._worker_thread = threading.Thread(target=self._process_queue, daemon=True)
        self._worker_thread.start()

    def _process_queue(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            job_id, audio, language = item
            try:
                self.run(job_id, audio, language)
            except Exception:
                LOGGER.exception("Unexpected error processing queued job %s", job_id)
            finally:
                self._queue.task_done()

    def close(self) -> None:
        """Shut down the background job worker thread."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def job(self) -> dict[str, Any] | None:
        """Compatibility view for callers of the original single-job worker."""
        with self.lock:
            if len(self.jobs) == 1:
                return next(iter(self.jobs.values())).copy()
            return None

    def load_jobs(self) -> dict[str, dict[str, Any]]:
        state = self.root / "jobs.json"
        if state.exists():
            try:
                return json.loads(state.read_text()).get("jobs", {})
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("Could not read %s, starting fresh: %s", state, exc)
                return {}
        # Upgrade state written by the original single-job pilot without rerunning it.
        legacy = self.root / "job.json"
        if not legacy.exists():
            return {}
        try:
            job = json.loads(legacy.read_text())
            job_id = job["input_sha256"]
            job["job_id"] = job_id
            return {job_id: job}
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            LOGGER.warning("Could not read legacy state %s: %s", legacy, exc)
            return {}

    def persist(self) -> None:
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"jobs": self.jobs}, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "jobs.json")
        finally:
            if os.path.exists(name):
                with contextlib.suppress(OSError):
                    os.unlink(name)

    def submit(self, body: bytes, expected_hash: str, language: str | None = None) -> tuple[int, dict[str, Any]]:
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            return 400, {"error": "audio hash mismatch"}
        with self.lock:
            if self._closed:
                return 503, {"error": "worker is shut down"}
            if actual_hash in self.jobs:
                job = self.jobs[actual_hash].copy()
                log_event("job_duplicate", job_id=actual_hash, status=job["status"])
                return 200, job
            inputs = self.root / "inputs"
            inputs.mkdir(exist_ok=True)
            audio = inputs / f"{actual_hash}.audio"
            audio.write_bytes(body)
            self.jobs[actual_hash] = {
                "job_id": actual_hash,
                "input_sha256": actual_hash,
                "status": "queued",
                "model": MODEL_ID,
                "model_revision": MODEL_REVISION,
            }
            if language:
                self.jobs[actual_hash]["requested_language"] = language
            self.persist()
            self._queue.put((actual_hash, audio, language))
            job = self.jobs[actual_hash].copy()
            queue_depth = sum(item["status"] == "queued" for item in self.jobs.values())
        log_event(
            "job_accepted",
            job_id=actual_hash,
            audio_bytes=len(body),
            queue_depth=queue_depth,
            requested_language=language,
        )
        return 202, job

    def _call_engine(self, audio: Path, language: str | None = None) -> Any:
        try:
            sig = inspect.signature(self.engine)
            if "language" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                return self.engine(audio, self.model_dir, language=language)
        except (ValueError, TypeError):
            pass
        return self.engine(audio, self.model_dir)

    def run(self, job_id: str, audio: str | Path, language: str | None = None) -> None:
        audio_path = Path(audio)
        with self.run_lock:
            with self.lock:
                self.jobs[job_id]["status"] = "running"
                self.persist()
            log_event("job_started", job_id=job_id, gpu=gpu_metrics())
            started = time.monotonic()
            telemetry_stop = threading.Event()
            telemetry = threading.Thread(
                target=self.log_running_gpu_metrics,
                args=(job_id, telemetry_stop),
                daemon=True,
            )
            try:
                telemetry.start()
                try:
                    output = self._call_engine(audio_path, language=language)
                    result_bytes = json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                    update = {
                        "status": "complete",
                        "result": output,
                        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                        "elapsed_seconds": time.monotonic() - started,
                    }
                except Exception as exc:
                    LOGGER.exception("Transcription error for job %s: %s", job_id, exc)
                    update = {"status": "failed", "error": "Transcription failed"}
                finally:
                    telemetry_stop.set()
                    telemetry.join(timeout=3)
            finally:
                if not os.getenv("WARRANT_KEEP_INPUTS"):
                    try:
                        if audio_path.exists():
                            audio_path.unlink()
                    except OSError as exc:
                        LOGGER.warning("Failed to remove input audio %s: %s", audio_path, exc)

            with self.lock:
                self.jobs[job_id].update(update)
                self.persist()
            if update["status"] == "complete":
                log_event(
                    "job_completed",
                    job_id=job_id,
                    elapsed_seconds=update["elapsed_seconds"],
                    gpu=gpu_metrics(),
                )
            else:
                log_event("job_failed", job_id=job_id, gpu=gpu_metrics())

    def log_running_gpu_metrics(self, job_id: str, stop: threading.Event) -> None:
        while not stop.wait(gpu_log_interval()):
            log_event("job_running", job_id=job_id, gpu=gpu_metrics())

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return job.copy() if job else None

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self.jobs[job_id].copy() for job_id in sorted(self.jobs)]


def _handle_get(request: BaseHTTPRequestHandler, worker: Worker) -> None:
    parsed = urllib.parse.urlsplit(request.path)
    if parsed.path == "/health":
        return request.reply(200, {"status": "ok"})
    if not request.authorized():
        return
    if parsed.path == "/jobs":
        return request.reply(200, {"jobs": worker.list()})
    if parsed.path.startswith("/job/"):
        job_id = parsed.path.removeprefix("/job/")
        if not re.fullmatch(r"[0-9a-f]{64}", job_id):
            return request.reply(404, {"error": "not found"})
        job = worker.get(job_id)
        return request.reply(200, job) if job else request.reply(404, {"error": "not found"})
    if parsed.path != "/job":
        return request.reply(404, {"error": "not found"})
    jobs = worker.list()
    if not jobs:
        return request.reply(200, {"status": "empty"})
    if len(jobs) == 1:
        return request.reply(200, jobs[0])
    return request.reply(400, {"error": "job_id required; use GET /job/<job_id> or GET /jobs"})


def _handle_post(request: BaseHTTPRequestHandler, worker: Worker) -> None:
    parsed = urllib.parse.urlsplit(request.path)
    if not request.authorized():
        return
    if parsed.path != "/job":
        return request.reply(404, {"error": "not found"})
    query = urllib.parse.parse_qs(parsed.query)
    language = query.get("language", [None])[0] or request.headers.get("X-Language")
    if language:
        language = language.strip().lower()
        if not re.fullmatch(r"[a-z]{2,3}", language):
            return request.reply(400, {"error": "Language must be a 2 or 3 letter ISO code (e.g. en, es)"})
    size_str = request.headers.get("Content-Length", "")
    expected = request.headers.get("X-Audio-SHA256", "")
    if not re.fullmatch(r"[0-9]{1,9}", size_str) or not (0 < int(size_str) <= MAX_AUDIO_BYTES):
        return request.reply(413, {"error": "Audio must be between 1 byte and 64 MiB"})
    size = int(size_str)
    if request.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return request.reply(400, {"error": "Need Content-Length and X-Audio-SHA256"})
    body = request.rfile.read(size)
    if len(body) != size:
        return request.reply(400, {"error": "incomplete upload"})
    code, result = worker.submit(body, expected, language=language)
    request.reply(code, result)


def handler(worker: Worker):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # Never log authorization headers or audio/transcript content.

        def reply(self, code: int, data: dict[str, Any]):
            body = json.dumps(data, allow_nan=False).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.debug("Client disconnected before response could be sent")

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + worker.token).encode()):
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            return _handle_get(self, worker)

        def do_POST(self):
            return _handle_post(self, worker)

    return Handler


def main():
    log_format = os.getenv("WARRANT_LOG_FORMAT", "[%(asctime)s] [%(levelname)s] %(message)s")
    logging.basicConfig(
        level=getattr(logging, os.getenv("WARRANT_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format=log_format,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    worker = Worker(
        os.getenv("WARRANT_WORK_DIR", "/data"),
        os.environ.get("WARRANT_WORKER_TOKEN"),
        model_dir=os.getenv("WARRANT_MODEL_DIR", "/model"),
    )
    log_event("worker_started", job_count=len(worker.jobs), gpu=gpu_metrics())
    server = ThreadingHTTPServer(("0.0.0.0", 8080), handler(worker))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.close()


if __name__ == "__main__":
    main()
