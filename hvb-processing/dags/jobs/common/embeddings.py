from __future__ import annotations

from typing import Any

from common.config import get_value, load_config

_embedder: Any | None = None


def get_embedding_settings() -> dict[str, Any]:
    # Load embedding provider settings / Đọc cấu hình embedding provider
    cfg = load_config()
    return {
        "provider": get_value(cfg, "embedding", "provider", fallback="fastembed"),
        "model": get_value(
            cfg,
            "embedding",
            "model",
            fallback="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        "passage_prefix": get_value(cfg, "embedding", "passage_prefix", fallback=""),
    }


def _supported_fastembed_models() -> set[str]:
    # List model names supported by installed fastembed / Liệt kê model fastembed hỗ trợ
    from fastembed import TextEmbedding

    names: set[str] = set()
    for entry in TextEmbedding.list_supported_models():
        if isinstance(entry, dict) and entry.get("model"):
            names.add(str(entry["model"]))
    return names


def _get_fastembed_model() -> Any:
    # Lazy-load FastEmbed model / Lazy-load model FastEmbed
    global _embedder
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency 'fastembed'. Install dags/requirements.txt on Airflow."
            ) from exc
        settings = get_embedding_settings()
        model_name = settings["model"]
        supported = _supported_fastembed_models()
        if model_name not in supported:
            sample = ", ".join(sorted(supported)[:5])
            raise ValueError(
                f"Model {model_name!r} is not supported by fastembed TextEmbedding. "
                f"Update [embedding].model in config.ini. Examples: {sample}"
            )
        _embedder = TextEmbedding(model_name=model_name)
    return _embedder


def embed_texts(texts: list[str]) -> list[list[float]]:
    # Embed document texts for Qdrant upsert / Embed văn bản tài liệu để upsert Qdrant
    settings = get_embedding_settings()
    provider = settings["provider"].strip().lower()
    cleaned = [text.strip() for text in texts]
    if provider != "fastembed":
        raise ValueError(f"Unsupported embedding.provider '{settings['provider']}'. Use: fastembed")

    prefix = settings["passage_prefix"]
    prepared = [
        f"{prefix}{text}" if text and not text.startswith(("passage:", "query:")) else text
        for text in cleaned
    ]
    model = _get_fastembed_model()
    vectors: list[list[float]] = []
    for vector in model.embed(prepared):
        vectors.append(vector.tolist() if hasattr(vector, "tolist") else list(vector))
    return vectors
