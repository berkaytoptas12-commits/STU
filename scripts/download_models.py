#!/usr/bin/env python3
"""Download the embedding and reranker models for offline use.

Run on an INTERNET-CONNECTED machine (pip install huggingface_hub), then copy the output folder to the
air-gapped machine as ./models (paths in config/config.yaml: models/bge-m3, models/bge-reranker-v2-m3).

    python scripts/download_models.py --out offline_bundle/models
    python scripts/download_models.py --out models --extra intfloat/multilingual-e5-large
"""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_MODELS = {
    "bge-m3": "BAAI/bge-m3",                            # multilingual embeddings (TR <-> EN), 1024-dim, 8k ctx
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",    # multilingual cross-encoder reranker
}
# Files that sentence-transformers does not need (ONNX exports, extra BGE-M3 heads, images).
IGNORE = ["onnx/*", "*.onnx", "*.onnx_data", "colbert_linear.pt", "sparse_linear.pt", "imgs/*", "*.png", "*.jpg"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="models", help="output folder")
    ap.add_argument("--extra", action="append", default=[], help="additional HF repo id(s) to download")
    ap.add_argument("--skip-reranker", action="store_true")
    args = ap.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit("pip install huggingface_hub")

    models = dict(DEFAULT_MODELS)
    if args.skip_reranker:
        models.pop("bge-reranker-v2-m3")
    for repo in args.extra:
        models[repo.split("/")[-1]] = repo

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, repo in models.items():
        target = out / name
        print(f"==> {repo} -> {target}")
        snapshot_download(repo_id=repo, local_dir=str(target), ignore_patterns=IGNORE)

    print("\nVerifying the models load from disk (offline) ...")
    try:
        import os

        os.environ["HF_HUB_OFFLINE"] = "1"
        from sentence_transformers import CrossEncoder, SentenceTransformer

        if (out / "bge-m3").exists():
            m = SentenceTransformer(str(out / "bge-m3"), device="cpu")
            print("   bge-m3 dim:", m.get_sentence_embedding_dimension())
        if (out / "bge-reranker-v2-m3").exists():
            r = CrossEncoder(str(out / "bge-reranker-v2-m3"), device="cpu")
            print("   reranker score:", r.predict([("PCIe link training", "The LTSSM controls link training.")]))
    except ImportError:
        print("   (sentence-transformers not installed here; skipped load check)")
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
