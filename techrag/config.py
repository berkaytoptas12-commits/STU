"""Configuration.

Layering (later wins): built-in defaults -> YAML file -> per-user settings.json (written by the desktop
Settings dialog) -> environment variables ``TECHRAG_<SECTION>__<KEY>`` (e.g. ``TECHRAG_LLM__MODEL``).

YAML lookup: explicit path > $TECHRAG_CONFIG > ./config/config.yaml (optional).

All model access goes through OpenAI-compatible HTTP endpoints (vLLM / SGLang / ...): chat, vision,
embeddings and rerank can each point at a different server. Nothing heavy runs locally.
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_CONFIG_PATH = Path("config/config.yaml")
APP_NAME = "TechRAG"


def resource_path(*parts: str) -> Path:
    """Path of a file shipped inside the package (works from source and from a PyInstaller bundle)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    candidate = base.joinpath("techrag", *parts)
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parent.joinpath(*parts)


def user_config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME.lower()


def user_cache_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME.lower()


# ----------------------------------------------------------------------------- sections

@dataclass
class ServiceConfig:
    """One OpenAI-compatible endpoint. base_url should end with /v1 (it is added when missing)."""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 300.0
    # Last resort for test setups: skip HTTPS certificate verification for this endpoint.
    verify_ssl: bool = True


@dataclass
class LLMConfig(ServiceConfig):
    base_url: str = "http://localhost:8000/v1"
    temperature: float = 0.1
    max_tokens: int = 4096
    # Thinking for multi-part / comparison questions: auto (planner decides) | on | off
    thinking: str = "auto"
    # How thinking is switched: chat_template (Qwen: chat_template_kwargs.enable_thinking)
    # | reasoning_effort (sends reasoning_effort) | none
    thinking_control: str = "chat_template"
    reasoning_effort: str = "medium"
    # Tool calling (vLLM needs --enable-auto-tool-choice --tool-call-parser ...): auto | on | off
    tools: str = "auto"
    max_tool_rounds: int = 4
    # Curated context budget (tokens of source text given to the model). Deliberately far below the
    # model's maximum: a small reranked context is more faithful than a dump of raw pages.
    context_tokens: int = 16000


@dataclass
class VisionConfig(ServiceConfig):
    """Multimodal model used to read tables at ingestion and page images at answer time.
    Empty base_url / model / api_key fall back to the chat LLM's values."""
    enabled: bool = True
    dpi: int = 150
    max_tokens: int = 6000
    concurrency: int = 4
    # Only pages whose table score reaches this are sent to the VLM (eager, filtered).
    min_page_score: float = 4.0
    max_pages_per_doc: int = 600


@dataclass
class EmbeddingConfig(ServiceConfig):
    backend: str = "api"            # api | hash (model-free, tests only)
    base_url: str = "http://localhost:8001/v1"
    batch_size: int = 32
    concurrency: int = 4
    # "auto" derives the query instruction from the model name (Qwen3-Embedding, E5, BGE ...).
    query_instruction: str = "auto"
    passage_prefix: str = "auto"


@dataclass
class RerankerConfig(ServiceConfig):
    enabled: bool = True
    base_url: str = "http://localhost:8002/v1"
    template: str = "auto"          # auto | qwen3 | none
    endpoint: str = "auto"          # auto | rerank | score
    candidates: int = 40
    instruction: str = ("Given a question about a hardware interface or design standard, judge whether the "
                        "passage contains information that answers it")


def _default_library() -> str:
    # The packaged desktop app may start from any working directory; keep its library in the user's home.
    if getattr(sys, "frozen", False):
        return str(Path.home() / APP_NAME / "library")
    return "data/library"


@dataclass
class PathsConfig:
    # The library holds sources/<collection>/*.pdf, index.sqlite and cache/vlm. Copy or share the
    # whole folder; others can open it read-only.
    library_dir: str = field(default_factory=_default_library)
    cache_dir: str = ""             # local render cache; "" = per-user cache dir
    domains_file: str = ""          # "" = bundled techrag/resources/domains.yaml


@dataclass
class ChunkingConfig:
    target_tokens: int = 380
    max_tokens: int = 650
    overlap_tokens: int = 60
    min_tokens: int = 40
    extract_tables: bool = True     # PyMuPDF ruled-table detection (fallback when VLM is off)
    strip_headers_footers: bool = True
    skip_toc_pages: bool = True


@dataclass
class RetrievalConfig:
    bm25_top_k: int = 50
    dense_top_k: int = 50
    rrf_k: int = 60
    final_top_k: int = 8
    # Small-to-big: a hit is expanded to its whole section when the section is at most this long,
    # otherwise to neighbouring chunks of the same section.
    section_expand_tokens: int = 1800
    neighbor_window: int = 1
    domain_routing: bool = True
    entity_filter: bool = True      # hard filter on standards/versions named in the question (never widened)
    min_results_for_filter: int = 1  # (unused since 0.2: a named standard's scope is never widened)
    query_rewrite: bool = True
    # Ask back instead of answering when no standard is named and the evidence for a value question comes
    # from sibling standards/versions (DDR4 and DDR5 ...) whose values may differ.
    clarify_ambiguous: bool = True
    parameter_rows: int = 12        # typed parameter rows pre-fetched for numeric questions


@dataclass
class AnswerConfig:
    verify: bool = True             # deterministic number/unit/citation check (cheap, always useful)
    judge: bool = True              # fresh-context entailment judge per claim (extra LLM calls)
    judge_batch: int = 8
    regenerate: bool = True         # one regeneration pass for failing claims
    failed_claims: str = "strip"    # strip | flag  (after the regeneration pass)
    history_turns: int = 3


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    allow_upload: bool = True
    api_token: str = ""


@dataclass
class TLSConfig:
    """HTTPS trust for the model endpoints (see techrag/tls.py)."""
    system_store: bool = True       # trust certificates in the Windows store (company CAs deployed by IT)
    ca_bundle: str = ""             # extra CA / server certificate files (PEM or DER), ';'-separated
    use_system_proxy: bool = False  # model servers are on the LAN; a system/corporate proxy is usually wrong


@dataclass
class UIConfig:
    language: str = "tr"            # tr | en


@dataclass
class Config:
    offline: bool = True
    read_only: bool = False         # open a shared library without writing to it
    paths: PathsConfig = field(default_factory=PathsConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    answer: AnswerConfig = field(default_factory=AnswerConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    tls: TLSConfig = field(default_factory=TLSConfig)
    ui: UIConfig = field(default_factory=UIConfig)

    # ------------------------------------------------------------------ derived paths
    @property
    def library_dir(self) -> Path:
        return Path(self.paths.library_dir).expanduser()

    @property
    def sources_dir(self) -> Path:
        return self.library_dir / "sources"

    @property
    def db_path(self) -> Path:
        return self.library_dir / "index.sqlite"

    @property
    def vlm_cache_dir(self) -> Path:
        return self.library_dir / "cache" / "vlm"

    @property
    def page_cache_dir(self) -> Path:
        base = Path(self.paths.cache_dir).expanduser() if self.paths.cache_dir else user_cache_dir()
        return base / "pages"

    @property
    def domains_file(self) -> Path:
        return Path(self.paths.domains_file) if self.paths.domains_file else resource_path("resources", "domains.yaml")

    def vision_service(self) -> ServiceConfig:
        """Effective vision endpoint (falls back to the chat LLM)."""
        v = self.vision
        same = not v.base_url
        return ServiceConfig(base_url=v.base_url or self.llm.base_url, api_key=v.api_key or self.llm.api_key,
                             model=v.model or self.llm.model, timeout=v.timeout,
                             verify_ssl=self.llm.verify_ssl if same else v.verify_ssl)

    def to_dict(self) -> dict:
        return asdict(self)

    def copy(self) -> "Config":
        return copy.deepcopy(self)


# ----------------------------------------------------------------------------- loading

def _coerce(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            return float(value)
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


def apply_dict(obj: Any, data: dict, where: str = "", strict: bool = True) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in (data or {}).items():
        if key not in known:
            if strict:
                raise ValueError(f"Unknown config key '{where}{key}'")
            continue
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config section '{where}{key}' must be a mapping")
            apply_dict(current, value, f"{where}{key}.", strict)
        else:
            setattr(obj, key, _coerce(value, current))


def _apply_env(cfg: Config) -> None:
    prefix = "TECHRAG_"
    for name, value in os.environ.items():
        if not name.startswith(prefix) or name in ("TECHRAG_CONFIG", "TECHRAG_DEBUG", "TECHRAG_NO_USER_SETTINGS"):
            continue
        parts = name[len(prefix):].lower().split("__")
        target: Any = cfg
        for part in parts[:-1]:
            target = getattr(target, part, None)
            if target is None:
                break
        if target is None or not is_dataclass(target) or not hasattr(target, parts[-1]):
            continue
        setattr(target, parts[-1], _coerce(value, getattr(target, parts[-1])))


def load_config(path: Optional[str | Path] = None, user_settings: Optional[bool] = None) -> Config:
    cfg = Config()
    candidate = Path(path) if path else Path(os.environ.get("TECHRAG_CONFIG", DEFAULT_CONFIG_PATH))
    if candidate.exists():
        with open(candidate, encoding="utf-8") as fh:
            apply_dict(cfg, yaml.safe_load(fh) or {})
    elif path:
        raise FileNotFoundError(f"Config file not found: {candidate}")
    if user_settings is None:
        user_settings = not os.environ.get("TECHRAG_NO_USER_SETTINGS")
    if user_settings:
        from techrag.settings import load_user_settings

        apply_dict(cfg, load_user_settings(), strict=False)
    _apply_env(cfg)
    if cfg.offline:
        enforce_offline()
    from techrag.api import configure_tls

    configure_tls(cfg.tls)
    return cfg


def enforce_offline() -> None:
    """No library used here phones home, but make sure nothing that might be imported later does."""
    for key, value in {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "DO_NOT_TRACK": "1", "ANONYMIZED_TELEMETRY": "False"}.items():
        os.environ.setdefault(key, value)
