#!/usr/bin/env bash
# Reference: serving the four endpoints TechRAG uses with vLLM inside the closed network.
# Flags change between vLLM releases and model families -- check `vllm serve --help` and each model card.
# TechRAG only needs OpenAI-compatible endpoints; any of these can live on separate GPUs/servers.
set -euo pipefail

# 1) Chat + vision model (answers, query planning, judge, VLM table extraction).
#    Tool calling must be enabled for the agent tools; a reasoning parser keeps thinking out of the answer.
vllm serve "${CHAT_MODEL:?set CHAT_MODEL to your Qwen model path}" \
  --port 8000 --served-model-name chat \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --reasoning-parser qwen3 \
  --max-model-len 65536 \
  --limit-mm-per-prompt '{"image": 4}' &

# 2) Embeddings (pooling runner). Older vLLM: --task embed
vllm serve "${EMBED_MODEL:-Qwen/Qwen3-Embedding-0.6B}" --port 8001 --runner pooling &

# 3) Reranker. Qwen3-Reranker must be loaded as a sequence classifier; TechRAG adds its prompt template.
vllm serve "${RERANK_MODEL:-Qwen/Qwen3-Reranker-0.6B}" --port 8002 --runner pooling \
  --hf_overrides '{"architectures":["Qwen3ForSequenceClassification"],"classifier_from_token":["no","yes"],"is_original_qwen3_reranker":true}' &
#    bge-reranker-v2-m3 needs no overrides:  vllm serve BAAI/bge-reranker-v2-m3 --port 8002 --runner pooling

wait
