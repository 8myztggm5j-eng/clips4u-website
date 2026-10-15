FROM node:22-bookworm-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
# Download the tiny speech model at image build time, not while the 512 MiB web server is running.
# This avoids Hugging Face downloads and temporary load spikes during requests.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-tiny', local_dir='/opt/clips4u-models/tiny', allow_patterns=['model.bin','config.json','tokenizer.json','vocabulary.json','preprocessor_config.json'])"
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false HF_HUB_OFFLINE=1
# Verify the CTranslate2 model opens on the same Debian image used in production.
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('/opt/clips4u-models/tiny', device='cpu', compute_type='int8', cpu_threads=1, num_workers=1); print('Whisper model load verified')"
COPY app.py /app/app.py
COPY highlights.py /app/highlights.py
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1"]
