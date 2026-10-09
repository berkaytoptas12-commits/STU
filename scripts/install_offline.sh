#!/usr/bin/env bash
# Installs techrag on the AIR-GAPPED machine from a bundle made by prepare_offline_bundle.sh.
# Nothing here touches the network.
#
#   ./scripts/install_offline.sh /media/usb/offline_bundle
set -euo pipefail

BUNDLE="${1:?usage: install_offline.sh <bundle dir>}"
PY="${PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

echo "==> Python virtualenv (.venv)"
"$PY" -m venv .venv
PIP=".venv/bin/pip"
$PIP install --no-index --find-links "$BUNDLE/wheels" --upgrade pip setuptools wheel
$PIP install --no-index --find-links "$BUNDLE/wheels" -r requirements.txt
$PIP install --no-index --find-links "$BUNDLE/wheels" --no-build-isolation --no-deps -e .

echo "==> Embedding / reranker models -> ./models"
mkdir -p models
cp -rn "$BUNDLE/models/"* models/ 2>/dev/null || cp -r "$BUNDLE/models/"* models/

echo "==> Ollama"
TGZ="$(ls "$BUNDLE"/ollama/ollama-linux-*.tgz 2>/dev/null | head -n1 || true)"
if [ -n "$TGZ" ] && ! command -v ollama >/dev/null; then
  echo "   extracting $TGZ to /usr (needs sudo)"
  sudo tar -C /usr -xzf "$TGZ"
fi
if [ -d "$BUNDLE/ollama/models" ]; then
  DEST="${OLLAMA_MODELS:-$HOME/.ollama/models}"
  mkdir -p "$DEST"
  cp -rn "$BUNDLE/ollama/models/"* "$DEST/" 2>/dev/null || cp -r "$BUNDLE/ollama/models/"* "$DEST/"
  echo "   LLM weights copied to $DEST"
fi

cat <<'EOF'

Kurulum tamam. Sonraki adımlar:
  1) LLM sunucusunu başlatın:          ollama serve      (ayrı bir terminalde / servis olarak)
  2) Kontrol:                           .venv/bin/techrag doctor
  3) Standart PDF'lerini koyun:         data/sources/<koleksiyon>/*.pdf
  4) İndeksleyin:                       .venv/bin/techrag ingest
  5) Web arayüzü:                       .venv/bin/techrag serve   -> http://127.0.0.1:8000
EOF
