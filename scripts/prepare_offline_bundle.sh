#!/usr/bin/env bash
# Builds everything the air-gapped machine needs into one folder.
# Run on an INTERNET-CONNECTED machine with the SAME OS, CPU architecture and Python minor version
# as the target (e.g. both Linux x86_64 + Python 3.11).
#
#   TORCH=cpu  LLM_MODELS="qwen3:14b" ./scripts/prepare_offline_bundle.sh offline_bundle
#   TORCH=cuda LLM_MODELS="qwen3:32b bge-m3" ./scripts/prepare_offline_bundle.sh offline_bundle
#
# Then copy the bundle folder (and this repository) to the air-gapped machine and run
# scripts/install_offline.sh there.
set -euo pipefail

OUT="${1:-offline_bundle}"
PY="${PYTHON:-python3}"
TORCH="${TORCH:-cpu}"                       # cpu | cuda
LLM_MODELS="${LLM_MODELS:-qwen3:14b}"       # Ollama models to pre-pull (space separated)
OLLAMA_URL="${OLLAMA_URL:-https://ollama.com/download/ollama-linux-amd64.tgz}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$OUT/wheels" "$OUT/models" "$OUT/ollama"

echo "==> 1/4 Python wheels ($TORCH torch)"
EXTRA=()
if [ "$TORCH" = "cpu" ]; then
  # The CPU-only torch wheel avoids ~3 GB of CUDA libraries.
  EXTRA=(--extra-index-url https://download.pytorch.org/whl/cpu)
fi
"$PY" -m pip download --dest "$OUT/wheels" ${EXTRA[@]+"${EXTRA[@]}"} -r "$HERE/requirements.txt"
"$PY" -m pip download --dest "$OUT/wheels" pip setuptools wheel

echo "==> 2/4 Embedding + reranker models (HuggingFace)"
"$PY" -m pip install --quiet huggingface_hub
"$PY" "$HERE/scripts/download_models.py" --out "$OUT/models"

echo "==> 3/4 Ollama binary + LLM weights"
if command -v curl >/dev/null; then
  curl -fL "$OLLAMA_URL" -o "$OUT/ollama/$(basename "$OLLAMA_URL")" \
    || echo "   !! Ollama download failed - fetch it manually from https://github.com/ollama/ollama/releases"
fi
if command -v ollama >/dev/null; then
  for m in $LLM_MODELS; do
    echo "   ollama pull $m"
    ollama pull "$m"
  done
  MODELS_DIR="${OLLAMA_MODELS:-$HOME/.ollama/models}"
  [ -d /usr/share/ollama/.ollama/models ] && [ ! -d "$MODELS_DIR" ] && MODELS_DIR=/usr/share/ollama/.ollama/models
  echo "   copying $MODELS_DIR -> $OUT/ollama/models"
  cp -r "$MODELS_DIR" "$OUT/ollama/models"
else
  echo "   !! 'ollama' not found on this machine: install it, run 'ollama pull <model>' and copy ~/.ollama/models"
fi

echo "==> 4/4 Project source"
tar --exclude=.git --exclude=offline_bundle --exclude=data/index --exclude=.venv \
    -czf "$OUT/techrag-src.tgz" -C "$HERE" .

du -sh "$OUT"/* || true
echo "Bundle ready: $OUT  -> copy it to the air-gapped machine and run scripts/install_offline.sh $OUT"
