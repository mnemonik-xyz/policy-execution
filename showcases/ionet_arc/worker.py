"""Multi-job HTTP transcription worker for io.net CaaS.

Audio is uploaded directly: no server-side fetching of caller-supplied URLs.
Only public/non-sensitive audio is intended for this pilot. Output provenance
is operational evidence, not a zkVM proof of transcription correctness.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse

MODEL_ID = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MAX_AUDIO_BYTES = 64 * 1024 * 1024
LOGGER = logging.getLogger(__name__)


def gpu_metrics():
    """Return a best-effort GPU utilization snapshot without failing a job."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,"
             "temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
            capture_output=True, check=True, text=True, timeout=2)
    except FileNotFoundError:
        return {"available": False, "reason": "nvidia_smi_unavailable"}
    except OSError:
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
        metrics.append({"index": values[0], "utilization_percent": values[1],
                        "memory_used_mib": values[2], "memory_total_mib": values[3],
                        "temperature_celsius": values[4], "power_watts": values[5]})
    return {"available": True, "gpus": metrics}


def log_event(event, **fields):
    LOGGER.info("%s", json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True))


def gpu_log_interval():
    try:
        return max(float(os.getenv("WARRANT_GPU_LOG_INTERVAL_SECONDS", "10")), 1)
    except ValueError:
        return 10


def transcribe(audio, model_dir, language=None):
    from faster_whisper import WhisperModel
    device = os.getenv("WARRANT_DEVICE", "cuda")
    compute_type = os.getenv("WARRANT_COMPUTE_TYPE", "float16" if device == "cuda" else "default")
    lang = language or os.getenv("WARRANT_LANGUAGE")
    model = WhisperModel(model_dir, device=device, compute_type=compute_type, local_files_only=True)
    segments, info = model.transcribe(str(audio), beam_size=5, language=lang)
    return {"language": info.language, "audio_seconds": info.duration,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments]}


class Worker:
    def __init__(self, root, token, engine=transcribe, model_dir="/model"):
        if not token or len(token) < 32:
            raise ValueError("Set WARRANT_WORKER_TOKEN to a random token of at least 32 characters")
        self.root, self.token = Path(root), token
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine, self.model_dir = engine, model_dir
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.jobs = self.load_jobs()
        # A restarted process must not silently rerun an already accepted job.
        interrupted = False
        for job in self.jobs.values():
            if job["status"] in {"queued", "running"}:
                job["status"] = "interrupted"
                interrupted = True
        if interrupted:
            self.persist()
            log_event("jobs_interrupted_after_restart", count=sum(
                job["status"] == "interrupted" for job in self.jobs.values()))

    @property
    def job(self):
        """Compatibility view for callers of the original single-job worker."""
        if len(self.jobs) == 1:
            return next(iter(self.jobs.values()))
        return None

    def load_jobs(self):
        state = self.root / "jobs.json"
        if state.exists():
            return json.loads(state.read_text())["jobs"]
        # Upgrade state written by the original single-job pilot without rerunning it.
        legacy = self.root / "job.json"
        if not legacy.exists():
            return {}
        job = json.loads(legacy.read_text())
        job_id = job["input_sha256"]
        job["job_id"] = job_id
        return {job_id: job}

    def persist(self):
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"jobs": self.jobs}, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "jobs.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def submit(self, body, expected_hash, language=None):
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            return 400, {"error": "audio hash mismatch"}
        with self.lock:
            if actual_hash in self.jobs:
                job = self.jobs[actual_hash].copy()
                log_event("job_duplicate", job_id=actual_hash, status=job["status"])
                return 200, job
            inputs = self.root / "inputs"
            inputs.mkdir(exist_ok=True)
            audio = inputs / f"{actual_hash}.audio"
            audio.write_bytes(body)
            self.jobs[actual_hash] = {"job_id": actual_hash, "input_sha256": actual_hash,
                                      "status": "queued", "model": MODEL_ID,
                                      "model_revision": MODEL_REVISION}
            if language:
                self.jobs[actual_hash]["requested_language"] = language
            self.persist()
            threading.Thread(target=self.run, args=(actual_hash, audio, language), daemon=True).start()
            job = self.jobs[actual_hash].copy()
            queue_depth = sum(item["status"] == "queued" for item in self.jobs.values())
        log_event("job_accepted", job_id=actual_hash, audio_bytes=len(body),
                  queue_depth=queue_depth, requested_language=language)
        return 202, job

    def run(self, job_id, audio, language=None):
        with self.run_lock:
            with self.lock:
                self.jobs[job_id]["status"] = "running"
                self.persist()
            log_event("job_started", job_id=job_id, gpu=gpu_metrics())
            started = time.monotonic()
            telemetry_stop = threading.Event()
            telemetry = threading.Thread(target=self.log_running_gpu_metrics,
                                         args=(job_id, telemetry_stop), daemon=True)
            telemetry.start()
            try:
                import inspect
                sig = inspect.signature(self.engine)
                if "language" in sig.parameters:
                    output = self.engine(audio, self.model_dir, language=language)
                else:
                    output = self.engine(audio, self.model_dir)
                result_bytes = json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
                update = {"status": "complete", "result": output,
                          "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                          "elapsed_seconds": time.monotonic() - started}
            except Exception as exc:
                import sys
                print(f"Transcription error: {exc}", file=sys.stderr, flush=True)
                update = {"status": "failed", "error": "Transcription failed"}
            finally:
                telemetry_stop.set()
                telemetry.join(timeout=3)
            with self.lock:
                self.jobs[job_id].update(update)
                self.persist()
            if update["status"] == "complete":
                log_event("job_completed", job_id=job_id,
                          elapsed_seconds=update["elapsed_seconds"], gpu=gpu_metrics())
            else:
                log_event("job_failed", job_id=job_id, gpu=gpu_metrics())

    def log_running_gpu_metrics(self, job_id, stop):
        while not stop.wait(gpu_log_interval()):
            log_event("job_running", job_id=job_id, gpu=gpu_metrics())

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return job.copy() if job else None

    def list(self):
        with self.lock:
            return [self.jobs[job_id].copy() for job_id in sorted(self.jobs)]


def _handle_get(request, worker):
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


def _handle_post(request, worker):
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
    size = request.headers.get("Content-Length", "")
    expected = request.headers.get("X-Audio-SHA256", "")
    if not re.fullmatch(r"[0-9]{1,9}", size) or not 0 < int(size) <= MAX_AUDIO_BYTES:
        return request.reply(413, {"error": "Audio must be between 1 byte and 64 MiB"})
    if request.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return request.reply(400, {"error": "Need Content-Length and X-Audio-SHA256"})
    body = request.rfile.read(int(size))
    if len(body) != int(size):
        return request.reply(400, {"error": "incomplete upload"})
    code, result = worker.submit(body, expected, language=language)
    request.reply(code, result)


def handler(worker):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *args):
            pass  # Never log authorization headers or audio/transcript content.

        def reply(self, code, data):
            body = json.dumps(data, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

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
    logging.basicConfig(level=getattr(logging, os.getenv("WARRANT_LOG_LEVEL", "INFO").upper(), logging.INFO),
                        format="%(message)s")
    worker = Worker(os.getenv("WARRANT_WORK_DIR", "/data"), os.environ.get("WARRANT_WORKER_TOKEN"),
                    model_dir=os.getenv("WARRANT_MODEL_DIR", "/model"))
    log_event("worker_started", job_count=len(worker.jobs), gpu=gpu_metrics())
    ThreadingHTTPServer(("0.0.0.0", 8080), handler(worker)).serve_forever()


if __name__ == "__main__":
    main()
