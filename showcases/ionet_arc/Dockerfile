FROM nvidia/cuda:12.3.2-base-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_TELEMETRY=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# cuBLAS and cuDNN 9 come from pip wheels, instead of the cudnn-runtime base image
RUN pip3 install \
      faster-whisper==1.2.1 ctranslate2==4.8.2 \
      nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" \
    && find /usr/local/lib/python3.10/dist-packages -name '__pycache__' -prune -exec rm -rf {} +

ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib

RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120', local_dir='/model', allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.*','preprocessor_config.json'])" \
    && rm -rf /model/.cache /root/.cache

RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker:worker /data
COPY worker.py /app/worker.py
USER worker
EXPOSE 8080
CMD ["python3", "/app/worker.py"]
