# Multi-stage build for io.net transcription worker
# Stage 1: Builder for python virtual environment and model download
FROM nvidia/cuda:12.3.2-base-ubuntu22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir faster-whisper==1.2.1 ctranslate2==4.8.2

RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120', local_dir='/model')" \
    && rm -rf /model/.cache /model/.git* /model/README.md /root/.cache

# Prune pip, setuptools, wheel, and __pycache__ from the venv
RUN pip uninstall -y pip setuptools wheel && \
    find /opt/venv -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true && \
    find /opt/venv -name "*.pyc" -delete

# Stage 2: Minimal runtime image
FROM nvidia/cuda:12.3.2-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install only the runtime libraries strictly required by CTranslate2 (cuBLAS and cuDNN 9) and Python runtime.
# Omits ~1.5 GB of unused libraries (cuFFT, cuSOLVER, cuSPARSE, NPP, NCCL).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    libcublas-12-3 \
    libcudnn9-cuda-12 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker:worker /data

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /model /model
COPY worker.py /app/worker.py

ENV PATH="/opt/venv/bin:$PATH"
ENV LD_LIBRARY_PATH="/usr/local/cuda-12.3/targets/x86_64-linux/lib:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH}"

USER worker
EXPOSE 8080
CMD ["python3", "/app/worker.py"]
