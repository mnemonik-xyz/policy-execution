"""Single-job HTTP transcription worker for io.net CaaS.

Audio is uploaded directly: no server-side fetching of caller-supplied URLs.
Only public/non-sensitive audio is intended for this pilot. Output provenance
is operational evidence, not a zkVM proof of transcription correctness.
"""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time

MODEL_ID = "Systran/faster-whisper-small"
MODEL_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MAX_AUDIO_BYTES = 64 * 1024 * 1024


def transcribe(audio, model_dir):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_dir, device="cuda", compute_type="float16", local_files_only=True)
    segments, info = model.transcribe(str(audio), beam_size=5)
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
        self.job = None
        # A restarted process must not silently rerun an already accepted job.
        if (self.root / "job.json").exists():
            self.job = json.loads((self.root / "job.json").read_text())
            if self.job["status"] == "running":
                self.job["status"] = "interrupted"
                self.persist()

    def persist(self):
        fd, name = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self.job, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.root / "job.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def submit(self, body, expected_hash):
        actual_hash = hashlib.sha256(body).hexdigest()
        if actual_hash != expected_hash:
            return 400, {"error": "audio hash mismatch"}
        with self.lock:
            if self.job:
                if self.job["input_sha256"] != actual_hash:
                    return 409, {"error": "This pilot worker accepts only one unique job"}
                return 200, self.job.copy()
            audio = self.root / "input.audio"
            audio.write_bytes(body)
            self.job = {"input_sha256": actual_hash, "status": "running",
                        "model": MODEL_ID, "model_revision": MODEL_REVISION}
            self.persist()
            threading.Thread(target=self.run, args=(audio,), daemon=True).start()
            return 202, self.job.copy()

    def run(self, audio):
        started = time.monotonic()
        try:
            output = self.engine(audio, self.model_dir)
            result_bytes = json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            update = {"status": "complete", "result": output,
                      "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                      "elapsed_seconds": time.monotonic() - started}
        except Exception:
            update = {"status": "failed", "error": "Transcription failed"}
        with self.lock:
            self.job.update(update)
            self.persist()


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

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + worker.token).encode()):
                self.reply(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if self.path == "/health":
                return self.reply(200, {"status": "ok"})
            if not self.authorized():
                return
            if self.path != "/job":
                return self.reply(404, {"error": "not found"})
            with worker.lock:
                self.reply(200, worker.job or {"status": "empty"})

        def do_POST(self):
            if not self.authorized():
                return
            if self.path != "/job":
                return self.reply(404, {"error": "not found"})
            size = self.headers.get("Content-Length", "")
            expected = self.headers.get("X-Audio-SHA256", "")
            if not re.fullmatch(r"[0-9]{1,9}", size) or not 0 < int(size) <= MAX_AUDIO_BYTES:
                return self.reply(413, {"error": "Audio must be between 1 byte and 64 MiB"})
            if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9a-f]{64}", expected):
                return self.reply(400, {"error": "Need Content-Length and X-Audio-SHA256"})
            body = self.rfile.read(int(size))
            if len(body) != int(size):
                return self.reply(400, {"error": "incomplete upload"})
            code, result = worker.submit(body, expected)
            self.reply(code, result)
    return Handler


def main():
    worker = Worker(os.getenv("WARRANT_WORK_DIR", "/data"), os.environ.get("WARRANT_WORKER_TOKEN"),
                    model_dir=os.getenv("WARRANT_MODEL_DIR", "/model"))
    ThreadingHTTPServer(("0.0.0.0", 8080), handler(worker)).serve_forever()


if __name__ == "__main__":
    main()
