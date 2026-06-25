"""
Optional TwelveLabs Marengo embedding backend.

The topic-cluster / semantic-search feature normally talks to an
OpenAI-compatible ``/embeddings`` endpoint. Marengo is *not* OpenAI-compatible
(different request/response shape), so when the user opts in by setting
``EMBEDDING_MODEL`` to a Marengo model name (e.g. ``marengo3.0``) we route text
embedding through the TwelveLabs SDK instead.

This is fully opt-in: if ``EMBEDDING_MODEL`` is not a Marengo model, nothing in
this module runs and the existing OpenAI-compatible path is used unchanged.

Marengo embeds text, image, audio and video into one shared 512-dim latent
space, which makes it a natural fit for the project's existing cosine-similarity
search over image prompts.

Get a free API key (generous free tier) at https://twelvelabs.io .
"""

from typing import List, Optional

from fastapi import HTTPException

from scripts.iib.logger import logger

# Default Marengo model. Older retrieval models (e.g. "Marengo-retrieval-2.7")
# are rejected by the current embed endpoint; "marengo3.0" is the supported one.
DEFAULT_MARENGO_MODEL = "marengo3.0"

# Marengo has a hard 500-token limit per text input. Use 450 to leave headroom
# because we can only estimate tokens (4 ASCII chars ≈ 1 token, 1 CJK ≈ 1 token)
# without access to Marengo's actual tokenizer.
_MARENGO_MAX_TOKENS = 450


def _truncate_to_token_budget(text: str, max_tokens: int = _MARENGO_MAX_TOKENS) -> str:
    """Conservative token-aware truncation, same algorithm as topic_cluster."""
    if not text or not text.strip():
        return " "
    tokens = 0
    ascii_bucket = 0
    for i, ch in enumerate(text):
        if ord(ch) < 128:
            ascii_bucket += 1
            if ascii_bucket >= 4:
                tokens += 1
                ascii_bucket = 0
        else:
            tokens += 1
        if tokens >= max_tokens:
            return text[: i + 1].strip()
    return text


def is_marengo_model(model: Optional[str]) -> bool:
    """True if the configured embedding model should be served by Marengo."""
    return bool(model) and str(model).strip().lower().startswith("marengo")


def marengo_text_embeddings(
    *,
    inputs: List[str],
    model: str,
    api_key: str,
) -> List[List[float]]:
    """
    Create text embeddings via TwelveLabs Marengo.

    Mirrors the contract of the OpenAI-compatible path in topic_cluster.py:
    returns one float vector per input string, in input order.

    The TwelveLabs embed endpoint takes a single ``text`` per call, so we issue
    one call per input. Per-item failures (SDK errors, empty results from content
    filtering) return ``None`` for that item so a single bad prompt doesn't kill
    the whole batch.
    """
    if not api_key:
        raise HTTPException(
            status_code=500,
            detail="TwelveLabs API key not configured (set TWELVELABS_API_KEY)",
        )

    try:
        from twelvelabs import TwelveLabs
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail="twelvelabs SDK not installed. Run: pip install 'twelvelabs>=1.2.8'",
        ) from e

    client = TwelveLabs(api_key=api_key)
    vectors: List[Optional[List[float]]] = []
    for text in inputs:
        # Marengo rejects empty text; use a single space as a harmless placeholder
        # so vector count stays aligned with the input list.
        safe_text = text if (text and text.strip()) else " "
        # Pre-truncate to stay under Marengo's 500-token limit. The caller
        # (topic_cluster) already truncates with a generous OpenAI budget,
        # but Marengo is much stricter and uses its own tokenizer.
        safe_text = _truncate_to_token_budget(safe_text)
        try:
            res = client.embed.create(model_name=model, text=safe_text)
        except Exception as e:  # per-item failure: log, skip, don't kill the batch
            logger.error("[marengo] embed.create failed for text len=%d: %s",
                         len(safe_text), str(e)[:200])
            vectors.append(None)
            continue

        seg = getattr(getattr(res, "text_embedding", None), "segments", None)
        if not seg or getattr(seg[0], "float_", None) is None:
            # Marengo returns empty segments when the prompt exceeds the 500-token
            # limit, or for certain content it refuses to embed. Include the
            # error_message (e.g. "The text token length should be less than or
            # equal to 500.") so the failure record is actionable.
            err_msg = getattr(
                getattr(res, "text_embedding", None), "error_message", None
            ) or "empty embedding"
            logger.warning(
                "[marengo] empty embedding for text len=%d: %s",
                len(safe_text), err_msg,
            )
            vectors.append(None)
            continue
        vectors.append([float(x) for x in seg[0].float_])

    ok = sum(1 for v in vectors if v is not None)
    logger.info(
        "[marengo] embedded %s/%s input(s), dim=%s",
        ok,
        len(vectors),
        len(vectors[0]) if ok else 0,
    )
    return vectors
