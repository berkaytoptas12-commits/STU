"""Configuration: YAML file + environment overrides.

Lookup order for the config file: explicit path > $TECHRAG_CONFIG > ./config/config.yaml.
Any value can be overridden with an environment variable of the form
``TECHRAG_<SECTION>__<KEY>``, e.g. ``TECHRAG_LLM__MODEL=qwen3:32b``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_PATH = Path("config/config.yaml")


@dataclass
class PathsConfig:
    sources_dir: str = "data/sources"
    index_dir: str = "data/index"
    domains_file: str = "config/domains.yaml"


@dataclass
class LLMConfig:
    # "ollama"  -> Ollama native API (supports num_ctx; recommended with Ollama)
    # "openai"  -> any OpenAI-compatible server (vLLM, llama.cpp server, LM Studio, TGI ...);
    #              base_url must then include the /v1 suffix.
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    api_key: str = ""
    temperature: float = 0.1
    max_tokens: int = 2048
    num_ctx: int = 16384
    timeout: float = 600.0
    # Thinking/reasoning models (qwen3, deepseek-r1, gpt-oss ...). None = do not send the flag.
    think: Optional[bool] = False


@dataclass
class EmbeddingConfig:
    # "sentence_transformers" (local model dir), "ollama", "openai" (OpenAI-compatible /v1/embeddings)
    # "hash" is a model-free bag-of-words hasher meant ONLY for tests / smoke checks.
    backend: str = "sentence_transformers"
    model: str = "models/bge-m3"
    base_url: str = "http://localhost:11434"
    api_key: str = ""
    device: str = "cpu"
    batch_size: int = 16
    max_seq_length: int = 1024
    query_prefix: str = ""
    passage_prefix: str = ""
    timeout: float = 300.0


@dataclass
class RerankerConfig:
    enabled: bool = True
    backend: str = "sentence_transformers"
    model: str = "models/bge-reranker-v2-m3"
    device: str = "cpu"
    batch_size: int = 16
    max_length: int = 1024
    candidates: int = 40


@dataclass
class ChunkingConfig:
    target_tokens: int = 380
    max_tokens: int = 650
    overlap_tokens: int = 60
    min_tokens: int = 40
    extract_tables: bool = True
    strip_headers_footers: bool = True
    skip_toc_pages: bool = True


@dataclass
class RetrievalConfig:
    bm25_top_k: int = 40
    dense_top_k: int = 40
    rrf_k: int = 60
    final_top_k: int = 8
    neighbor_window: int = 1
    max_context_tokens: int = 7000
    # Restrict search to the standards explicitly named in the question (e.g. "PCIe", "ARINC 429").
    domain_routing: bool = True
    # Fall back to an unrestricted search when the routed search returns fewer hits than this
    # (e.g. the question names a standard whose documents are not indexed yet).
    min_results_for_filter: int = 1
    # LLM-based query planning: Turkish -> English translation, follow-up resolution, keyword expansion.
    query_rewrite: bool = True


@dataclass
class AnswerConfig:
    verify: bool = True
    # One extra LLM pass that rewrites the answer when verification finds unsupported values.
    self_correct: bool = False
    history_turns: int = 3


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    allow_upload: bool = True
    # If set, every /api call needs "Authorization: Bearer <token>".
    api_token: str = ""


@dataclass
class Config:
    offline: bool = True
    paths: PathsConfig = field(default_factory=PathsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    answer: AnswerConfig = field(default_factory=AnswerConfig)
    server: ServerConfig = field(default_factory=ServerConfig)

    @property
    def sources_dir(self) -> Path:
        return Path(self.paths.sources_dir)

    @property
    def index_dir(self) -> Path:
        return Path(self.paths.index_dir)

    @property
    def db_path(self) -> Path:
        return self.index_dir / "index.sqlite"

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce(value: Any, default: Any) -> Any:
    """Convert a string (from env) to the type of the default value."""
    if not isinstance(value, str):
        return value
    if isinstance(default, bool) or default is None:
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        if low in ("", "none", "null"):
            return None
        return value
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def _apply(obj: Any, data: dict, where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in known:
            raise ValueError(f"Unknown config key '{where}{key}'")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config section '{where}{key}' must be a mapping")
            _apply(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, _coerce(value, current))


def _apply_env(cfg: Config) -> None:
    prefix = "TECHRAG_"
    for name, value in os.environ.items():
        if not name.startswith(prefix) or name == "TECHRAG_CONFIG":
            continue
        parts = name[len(prefix):].lower().split("__")
        target: Any = cfg
        for part in parts[:-1]:
            if not hasattr(target, part):
                target = None
                break
            target = getattr(target, part)
        if target is None or not is_dataclass(target) or not hasattr(target, parts[-1]):
            continue
        setattr(target, parts[-1], _coerce(value, getattr(target, parts[-1])))


def load_config(path: Optional[str | Path] = None) -> Config:
    cfg = Config()
    candidate = Path(path) if path else Path(os.environ.get("TECHRAG_CONFIG", DEFAULT_CONFIG_PATH))
    if candidate.exists():
        with open(candidate, encoding="utf-8") as fh:
            _apply(cfg, yaml.safe_load(fh) or {}, "")
    elif path:
        raise FileNotFoundError(f"Config file not found: {candidate}")
    _apply_env(cfg)
    if cfg.offline:
        enforce_offline()
    return cfg


def enforce_offline() -> None:
    """Make sure HuggingFace / transformers never try to reach the network."""
    for key, value in {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "ANONYMIZED_TELEMETRY": "False",
    }.items():
        os.environ.setdefault(key, value)
