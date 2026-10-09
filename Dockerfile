# techrag (CPU). Build on an internet-connected machine, then move the image across the air gap:
#   docker compose build && docker save techrag:latest ollama/ollama:latest | gzip > techrag-images.tgz
#   (air-gapped) docker load < techrag-images.tgz && docker compose up -d
# Model folders (./models) and documents (./data) are mounted as volumes, not baked into the image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app
COPY requirements.txt pyproject.toml README.md ./
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

COPY techrag ./techrag
COPY config ./config
COPY eval ./eval
RUN pip install --no-deps --no-build-isolation .

EXPOSE 8000
CMD ["techrag", "serve", "--host", "0.0.0.0", "--port", "8000"]
