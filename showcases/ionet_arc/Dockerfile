FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --upgrade pip && \
    pip3 install --no-cache-dir faster-whisper==1.2.1 ctranslate2==4.8.2
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small', revision='536b0662742c02347bc0e980a01041f333bce120', local_dir='/model')"
RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker:worker /data
COPY worker.py /app/worker.py
USER worker
EXPOSE 8080
CMD ["python3", "/app/worker.py"]
