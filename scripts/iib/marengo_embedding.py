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
    one call per input. Errors are surfaced as HTTPException to match the rest
    of the embedding pipeline.
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
    vectors: List[List[float]] = []
    for text in inputs:
        # Marengo rejects empty text; use a single space as a harmless placeholder
        # so vector count stays aligned with the input list.
        safe_text = text if (text and text.strip()) else " "
        try:
            res = client.embed.create(model_name=model, text=safe_text)
        except Exception as e:  # SDK raises typed errors; normalize for the pipeline
            logger.error("[marengo] embed.create failed: %s", str(e)[:300])
            raise HTTPException(
                status_code=502, detail=f"Marengo embedding failed: {e}"
            ) from e

        seg = getattr(getattr(res, "text_embedding", None), "segments", None)
        if not seg or getattr(seg[0], "float_", None) is None:
            raise HTTPException(
                status_code=502, detail="Marengo returned no text embedding"
            )
        vectors.append([float(x) for x in seg[0].float_])

    logger.info(
        "[marengo] embedded %s input(s), dim=%s",
        len(vectors),
        len(vectors[0]) if vectors else 0,
    )
    return vectors
