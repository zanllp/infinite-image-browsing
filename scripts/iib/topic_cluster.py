import hashlib
import asyncio
import json
import math
import os
import re
import time
import uuid
import threading
from array import array
from contextlib import closing
from typing import Callable, Dict, List, Optional, Tuple
from sqlite3 import Connection, connect
import requests
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from scripts.iib.db.datamodel import DataBase, ImageEmbedding, ImageEmbeddingFail, TopicClusterCache, TopicTitleCache
from scripts.iib.tool import cwd, accumulate_streaming_response
from scripts.iib.logger import logger

# Perf deps (required for this feature)
_np = None
_hnswlib = None
_PERF_DEPS_READY = False


def _ensure_perf_deps() -> None:
    """
    We do NOT allow users to use TopicSearch/TopicClustering feature without these deps.
    But we also do NOT want the whole server to crash on import, so we lazy-load and fail at API layer.
    """
    global _np, _hnswlib, _PERF_DEPS_READY
    if _PERF_DEPS_READY:
        return
    try:
        import numpy as np  # type: ignore
        import hnswlib as hnswlib  # type: ignore
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Topic clustering requires numpy+hnswlib. Please install them. import_error={type(e).__name__}: {e}",
        )
    _np = np
    _hnswlib = hnswlib
    _PERF_DEPS_READY = True


_TOPIC_CLUSTER_JOBS: Dict[str, Dict] = {}
_TOPIC_CLUSTER_JOBS_LOCK = threading.Lock()
_TOPIC_CLUSTER_JOBS_MAX = 16


_EMBEDDING_MAX_TOKENS_SOFT = 7800
# Some providers enforce a max token budget per *request* (sum of all inputs), not only per input.
# Keep a conservative per-request budget to avoid "context length exceeded" across batched inputs.
_EMBEDDING_REQUEST_MAX_TOKENS_SOFT = 7600
_EMBEDDING_DEBUG = os.getenv("IIB_EMBEDDING_DEBUG", "0").strip().lower() in ["1", "true", "yes", "on"]


def _estimate_tokens_soft(s: str) -> int:
    """
    Lightweight, dependency-free token estimator.
    - ASCII-ish chars: ~4 chars per token (roughly OpenAI/BPE average for English)
    - Non-ASCII chars (CJK etc): ~1 char per token (conservative)
    This is intentionally conservative to avoid provider-side "max tokens exceeded" errors.
    """
    if not s:
        return 0
    ascii_chars = 0
    non_ascii = 0
    for ch in s:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            non_ascii += 1
    return (ascii_chars + 3) // 4 + non_ascii


def _truncate_for_embedding_tokens(s: str, max_tokens: int = _EMBEDDING_MAX_TOKENS_SOFT) -> str:
    """
    Truncate text to a conservative token budget.
    We do not rely on provider tokenizers to keep the system lightweight.
    """
    s = (s or "").strip()
    if not s:
        return ""
    if _estimate_tokens_soft(s) <= max_tokens:
        return s

    # Walk from start, stop when budget is reached.
    out = []
    tokens = 0
    ascii_bucket = 0  # count ascii chars; every 4 ascii chars ~= +1 token
    for ch in s:
        if ord(ch) < 128:
            ascii_bucket += 1
            if ascii_bucket >= 4:
                tokens += 1
                ascii_bucket = 0
        else:
            tokens += 1
        if tokens > max_tokens:
            break
        out.append(ch)
    return ("".join(out)).strip()


def _batched_by_token_budget(
    items: List[Dict],
    *,
    max_items: int,
    max_tokens_sum: int,
) -> List[List[Dict]]:
    """
    Split a list of items (each contains 'text') into batches constrained by:
    - max_items
    - sum(estimated_tokens(text)) <= max_tokens_sum
    """
    batches: List[List[Dict]] = []
    cur: List[Dict] = []
    cur_tokens = 0
    for it in items:
        txt = str(it.get("text") or "")
        t = _estimate_tokens_soft(txt)
        # ensure progress even if one item is huge (should already be truncated per-input)
        if cur and (len(cur) >= max_items or (cur_tokens + t) > max_tokens_sum):
            batches.append(cur)
            cur = []
            cur_tokens = 0
        cur.append(it)
        cur_tokens += t
    if cur:
        batches.append(cur)
    return batches


def _job_now() -> float:
    return time.time()


def _job_trim() -> None:
    # Keep only the most recent jobs in memory.
    with _TOPIC_CLUSTER_JOBS_LOCK:
        if len(_TOPIC_CLUSTER_JOBS) <= _TOPIC_CLUSTER_JOBS_MAX:
            return
        # sort by updated_at desc
        items = sorted(_TOPIC_CLUSTER_JOBS.items(), key=lambda kv: (kv[1].get("updated_at") or 0), reverse=True)
        keep = dict(items[: _TOPIC_CLUSTER_JOBS_MAX])
        _TOPIC_CLUSTER_JOBS.clear()
        _TOPIC_CLUSTER_JOBS.update(keep)


def _job_get(job_id: str) -> Optional[Dict]:
    with _TOPIC_CLUSTER_JOBS_LOCK:
        j = _TOPIC_CLUSTER_JOBS.get(job_id)
        return dict(j) if isinstance(j, dict) else None


# Export for use by other modules (e.g., organize_files)
def get_cluster_job_status(job_id: str) -> Optional[Dict]:
    """Public function to get cluster job status."""
    return _job_get(job_id)


def _job_upsert(job_id: str, patch: Dict) -> None:
    with _TOPIC_CLUSTER_JOBS_LOCK:
        cur = _TOPIC_CLUSTER_JOBS.get(job_id)
        if not isinstance(cur, dict):
            cur = {"job_id": job_id}
        cur.update(patch or {})
        cur["updated_at"] = _job_now()
        _TOPIC_CLUSTER_JOBS[job_id] = cur
    _job_trim()


def _normalize_base_url(base_url: str) -> str:
    return base_url[:-1] if base_url.endswith("/") else base_url


_PROMPT_NORMALIZE_ENABLED = os.getenv("IIB_PROMPT_NORMALIZE", "1").strip().lower() not in ["0", "false", "no", "off"]
# balanced: keep some discriminative style words (e.g. "科学插图/纪实/胶片") while dropping boilerplate quality/camera terms
# theme_only: more aggressive removal, closer to "only subject/theme nouns"
_PROMPT_NORMALIZE_MODE = (os.getenv("IIB_PROMPT_NORMALIZE_MODE", "balanced") or "balanced").strip().lower()

# Remove common SD boilerplate / quality descriptors.
_DROP_PATTERNS_COMMON = [
    # SD / A1111 tags
    r"<lora:[^>]+>",
    r"<lyco:[^>]+>",
    # quality / resolution / generic
    r"\b(masterpiece|best\s*quality|high\s*quality|best\s*rating|(?:highly|ultra|hyper)[-\s\u2010\u2011\u2012\u2013\u2212]*detailed|absurdres|absurd\s*res|hires|hdr|uhd|8k|4k|2k|raw\s*photo|photorealistic|realistic|cinematic)\b",
    # photography / camera / lens
    r"\b(film\s+photography|photography|dslr|camera|canon|nikon|sony|sigma|leica|lens|bokeh|depth\s+of\s+field|dof|sharp\s+focus|wide\s+angle|fisheye)\b",
    r"\b(iso\s*\d{2,5}|f\/\d+(?:\.\d+)?|\d{2,4}mm)\b",
]
# chinese quality descriptors (common)
_DROP_PATTERNS_ZH_COMMON = [
    r"(超高分辨率|高分辨率|高清|超清|8K|4K|2K|照片级|高质量|最佳质量|大师作品|杰作|超细节|细节丰富|极致细节|极致|完美)",
]
# chinese "style/photography" words: keep in balanced mode (discriminative), drop in theme_only mode
_DROP_PATTERNS_ZH_STYLE = [
    r"(电影质感|写真|写实|真实感|摄影|摄影作品|摄影图像|摄影图|镜头|景深|胶片|光圈|光影|构图|色彩|渲染|纪实|插图|科学插图)",
]

def _build_drop_re() -> re.Pattern:
    pats = list(_DROP_PATTERNS_COMMON) + list(_DROP_PATTERNS_ZH_COMMON)
    if _PROMPT_NORMALIZE_MODE in ["theme", "theme_only", "strict"]:
        pats += list(_DROP_PATTERNS_ZH_STYLE)
    return re.compile("|".join(f"(?:{p})" for p in pats), flags=re.IGNORECASE)

_DROP_RE = _build_drop_re()


def _compute_prompt_normalize_version() -> str:
    """
    IMPORTANT:
    - Do NOT allow users to override normalize-version via environment variables.
    - Version should be deterministic from the normalization rules themselves, so cache invalidation
      happens automatically when we change rules in code (or switch mode).
    """
    payload = {
        "enabled": bool(_PROMPT_NORMALIZE_ENABLED),
        "mode": str(_PROMPT_NORMALIZE_MODE),
        "drop_common": list(_DROP_PATTERNS_COMMON),
        "drop_zh_common": list(_DROP_PATTERNS_ZH_COMMON),
        "drop_zh_style": list(_DROP_PATTERNS_ZH_STYLE),
    }
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "nv_" + hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# Derived normalize version fingerprint (for embedding/title cache invalidation)
_PROMPT_NORMALIZE_VERSION = _compute_prompt_normalize_version()


def _extract_prompt_text(raw_exif: str, max_chars: int = 4000) -> str:
    """
    Extract the natural-language prompt part from stored exif text.
    Keep text before 'Negative prompt:' to preserve semantics.
    """
    if not isinstance(raw_exif, str):
        return ""
    s = raw_exif.strip()
    if not s:
        return ""
    idx = s.lower().find("negative prompt:")
    if idx != -1:
        s = s[:idx].strip()
    if len(s) > max_chars:
        s = s[:max_chars]
    return s.strip()


def _clean_prompt_for_semantic(text: str) -> str:
    """
    Light, dependency-free prompt normalization:
    - remove lora tags / SD boilerplate / quality & photography descriptors
    - keep remaining text as 'theme' semantic signal for embeddings/clustering
    """
    if not isinstance(text, str):
        return ""
    s = text
    # remove negative prompt tail early (safety if caller passes raw exif)
    s = re.sub(r"(negative prompt:).*", " ", s, flags=re.IGNORECASE | re.DOTALL)
    # remove weights like (foo:1.2)
    s = re.sub(r"\(([^()]{1,80}):\s*\d+(?:\.\d+)?\)", r"\1", s)
    # drop boilerplate patterns
    s = _DROP_RE.sub(" ", s)
    # normalize separators
    s = s.replace("**", " ")
    s = re.sub(r"[\[\]{}()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # If it's a comma-tag style prompt, remove empty / tiny segments.
    parts = re.split(r"[,\n，;；]+", s)
    kept: List[str] = []
    for p in parts:
        t = p.strip()
        if not t:
            continue
        # drop segments that are basically leftover boilerplate (too short or all punctuation)
        if len(t) <= 2:
            continue
        kept.append(t)
    s2 = "，".join(kept) if kept else s
    return s2.strip()


def _clean_for_title(text: str) -> str:
    if not isinstance(text, str):
        return ""
    s = text
    s = s.replace("**", " ")
    s = re.sub(r"<lora:[^>]+>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<lyco:[^>]+>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"(negative prompt:).*", " ", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(r"(prompt:|提示词[:：]|提示[:：]|输出[:：])", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _title_from_representative_prompt(text: str, max_len: int = 18) -> str:
    """
    Local fallback title: take the first sentence/clause and truncate.
    This is much more readable than token n-grams without Chinese word segmentation.
    """
    # Use the same semantic cleaner as embeddings to avoid boilerplate titles like
    # "masterpiece, best quality" / "A highly detailed ..."
    base = _clean_for_title(text)
    s = _clean_prompt_for_semantic(base) if _PROMPT_NORMALIZE_ENABLED else base
    if not s:
        s = base
    if not s:
        return "主题"
    # Split by common sentence punctuations, keep the first segment.
    seg = re.split(r"[。！？!?\n\r;；]+", s)[0].strip()
    # Remove leading punctuation / separators
    seg = re.sub(r"^[,，;；:：\s-]+", "", seg).strip()
    # Remove leading english articles for nicer titles
    seg = re.sub(r"^(a|an|the)\s+", "", seg, flags=re.IGNORECASE).strip()
    # Strip common boilerplate templates in titles while keeping discriminative words.
    # English: "highly detailed scientific rendering/illustration of ..."
    seg = re.sub(
        r"^(?:(?:highly|ultra|hyper)[-\s\u2010\u2011\u2012\u2013\u2212]*detailed\s+)?(?:scientific\s+)?(?:rendering|illustration|image|depiction|scene)\s+of\s+",
        "",
        seg,
        flags=re.IGNORECASE,
    ).strip()
    # Chinese: "一张...图像/插图/照片..." template
    seg = re.sub(r"^一[张幅]\s*[^，,。]{0,20}(?:图像|插图|照片|摄影图像|摄影作品)\s*[，, ]*", "", seg).strip()
    # Remove trailing commas/colons
    seg = re.sub(r"[,:，：]\s*$", "", seg).strip()
    # If still too long, hard truncate.
    if len(seg) > max_len:
        seg = seg[:max_len].rstrip()
    return seg or "主题"


def _vec_to_blob_f32(vec: List[float]) -> bytes:
    arr = array("f", vec)
    return arr.tobytes()


def _blob_to_vec_f32(blob: bytes) -> array:
    arr = array("f")
    arr.frombytes(blob)
    return arr


def _l2_norm_sq(vec: array) -> float:
    return sum((x * x for x in vec))


def _dot(a: array, b: array) -> float:
    return sum((x * y for x, y in zip(a, b)))


def _centroid_vec_np(sum_vec: array, norm_sq: float):
    """
    Convert centroid sum-vector to a normalized numpy float32 vector.
    Cosine between unit v and centroid is dot(v, sum)/sqrt(norm_sq).
    So centroid direction is sum / ||sum||.
    """
    _ensure_perf_deps()
    if norm_sq <= 0:
        return None
    inv = 1.0 / math.sqrt(norm_sq)
    # array('f') -> numpy without extra python loops
    v = _np.frombuffer(sum_vec.tobytes(), dtype=_np.float32).copy()
    v *= _np.float32(inv)
    return v


def _build_hnsw_index(centroids_np, *, ef: int = 64, M: int = 32):
    """
    Build a cosine HNSW index over centroid vectors.
    Returns index or None.
    """
    _ensure_perf_deps()
    if centroids_np is None:
        return None
    if len(centroids_np) == 0:
        return None
    dim = int(centroids_np.shape[1])
    idx = _hnswlib.Index(space="cosine", dim=dim)
    idx.init_index(max_elements=int(centroids_np.shape[0]) + 8, ef_construction=ef, M=M)
    labels = _np.arange(centroids_np.shape[0], dtype=_np.int32)
    idx.add_items(centroids_np, labels)
    idx.set_ef(max(ef, 32))
    return idx

def _cos_sum(a_sum: array, a_norm_sq: float, b_sum: array, b_norm_sq: float) -> float:
    if a_norm_sq <= 0 or b_norm_sq <= 0:
        return 0.0
    dotv = sum((x * y for x, y in zip(a_sum, b_sum)))
    return dotv / (math.sqrt(a_norm_sq) * math.sqrt(b_norm_sq))


def _call_embeddings_sync(
    *,
    inputs: List[str],
    model: str,
    base_url: str,
    api_key: str,
) -> List[List[float]]:
    logger.info("[embeddings] === _call_embeddings_sync START ===")
    logger.info("[embeddings] base_url=%s model=%s n_inputs=%s", base_url, model, len(inputs))

    if not api_key:
        logger.error("[embeddings] API Key not configured")
        raise HTTPException(status_code=500, detail="OpenAI API Key not configured")

    logger.info("[embeddings] API Key configured (length=%s)", len(api_key))

    url = f"{_normalize_base_url(base_url)}/embeddings"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "input": inputs}

    logger.info("[embeddings] Request URL: %s", url)
    logger.info("[embeddings] Request model: %s", model)
    logger.info("[embeddings] Request n_inputs: %s", len(inputs))

    if "localhost" in base_url or "127.0.0.1" in base_url:
        logger.info("[embeddings] Detected local API request (Ollama/LocalAI): %s", base_url)

    logger.debug(
        "[embeddings] Request payload: %s",
        json.dumps({"model": model, "input": ["(truncated)" if len(s) > 50 else s for s in inputs]}, ensure_ascii=False)
    )

    try:
        logger.info("[embeddings] Initiating HTTP request, timeout=120s")
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        logger.info("[embeddings] HTTP request completed")
    except requests.exceptions.Timeout as e:
        logger.error("[embeddings] Request timeout: url=%s error=%s", url, str(e))
        logger.error("[embeddings] If using Ollama, ensure service is running: ollama serve")
        raise HTTPException(status_code=504, detail=f"Embedding API request timeout: {e}")
    except requests.exceptions.ConnectionError as e:
        logger.error("[embeddings] Connection failed: url=%s error=%s", url, str(e))
        logger.error("[embeddings] Please check if API address is correct and Ollama is running")
        raise HTTPException(status_code=502, detail=f"Embedding API connection failed: {e}")
    except requests.RequestException as e:
        logger.error(
            "[embeddings] Request failed: url=%s error=%s type=%s",
            url, str(e), type(e).__name__
        )
        raise HTTPException(status_code=502, detail=f"Embedding API request failed: {e}")

    logger.info(
        "[embeddings] Response: status=%s content_length=%s",
        resp.status_code, len(resp.content)
    )

    if resp.status_code != 200:
        status = 400 if resp.status_code == 401 else resp.status_code
        body = (resp.text or "")[:600]
        logger.error(
            "[embeddings] Non-200 response: status=%s body=%s",
            status, body
        )
        if resp.status_code == 404:
            logger.error("[embeddings] 404 error, please check API address and model name")
        raise HTTPException(status_code=status, detail=body)

    try:
        data = resp.json()
    except Exception as e:
        logger.error(
            "[embeddings] Failed to parse JSON: error=%s text=%s",
            str(e), resp.text[:500]
        )
        raise HTTPException(status_code=500, detail="Invalid JSON response")

    items = data.get("data") or []
    items.sort(key=lambda x: x.get("index", 0))
    embeddings = [x.get("embedding") for x in items]

    if any((not isinstance(v, list) for v in embeddings)):
        logger.error(
            "[embeddings] Invalid embeddings format: n_items=%s valid_embeddings=%s",
            len(items), sum(1 for v in embeddings if isinstance(v, list))
        )
        raise HTTPException(status_code=500, detail="Invalid embeddings response format")

    logger.info(
        "[embeddings] Success: n_embeddings=%s dim=%s",
        len(embeddings), len(embeddings[0]) if embeddings else 0
    )

    return embeddings


async def _call_embeddings(
    *,
    inputs: List[str],
    model: str,
    base_url: str,
    api_key: str,
) -> List[List[float]]:
    """
    IMPORTANT:
    - We must NOT block FastAPI's event loop thread with a long-running synchronous HTTP request.
    - Use a thread offload for requests.post to keep the server responsive while waiting the embedding API.
    """
    return await asyncio.to_thread(
        _call_embeddings_sync,
        inputs=inputs,
        model=model,
        base_url=base_url,
        api_key=api_key,
    )


def _call_chat_title_sync(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt_samples: List[str],
    output_lang: str,
    existing_keywords: Optional[List[str]] = None,
) -> Optional[Dict]:
    """
    Ask LLM to generate a short topic title and a few keywords. Returns dict or None.
    """
    logger.info("[chat_title] === _call_chat_title_sync START ===")
    logger.info("[chat_title] base_url=%s model=%s lang=%s", base_url, model, output_lang)

    if not api_key:
        logger.error("[chat_title] API Key not configured")
        raise HTTPException(status_code=500, detail="OpenAI API Key not configured")

    logger.info("[chat_title] API Key configured (length=%s)", len(api_key))

    url = f"{_normalize_base_url(base_url)}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    logger.info("[chat_title] Request URL: %s", url)
    logger.info("[chat_title] Request model: %s", model)

    if "localhost" in base_url or "127.0.0.1" in base_url:
        logger.info("[chat_title] Detected local API request (Ollama/LocalAI): %s", base_url)

    samples = [(_clean_prompt_for_semantic(_clean_for_title(s) or s) or s).strip() for s in prompt_samples if (s or "").strip()]
    samples = [s[:400] for s in samples][:6]
    logger.info("[chat_title] Prompt samples count: %s", len(samples))

    if not samples:
        logger.error("[chat_title] No prompt samples for title generation")
        raise HTTPException(status_code=400, detail="No prompt samples for title generation")

    json_example = '{"title":"...","keywords":["...","..."]}'
    sys = (
        "You are a topic naming assistant for image-generation prompts.\n"
        "Given several prompt snippets that belong to the SAME theme, output:\n"
        "- a short topic title\n"
        "- 3–6 keywords.\n"
        "\n"
        "Rules:\n"
        f"- Output language MUST be: {output_lang}\n"
        "- Prefer 4–12 characters for Chinese (Simplified/Traditional), otherwise 2–6 English/German words.\n"
        "- Avoid generic boilerplate like: masterpiece, best quality, highly detailed, cinematic, etc.\n"
        "- Keep distinctive terms if they help differentiate themes (e.g., Warhammer 40K, Lolita, scientific illustration).\n"
        "- Do NOT output explanations. Do NOT output markdown/code fences.\n"
        "- The output MUST start with '{' and end with '}' (no leading/trailing characters).\n"
        "\n"
    )
    if existing_keywords:
        # Dynamic keyword selection based on total unique count
        unique_count = len(existing_keywords)
        top_keywords = existing_keywords[:500]  # Relaxed to 500
        
        # Tiered conditions based on keyword count
        if unique_count <= 200:
            # First 200: Not very strict, can create new keywords if not highly relevant
            strictness_msg = (
                "TIP: Prioritize selecting from the existing list if they fit reasonably well. "
                "Creating new keywords is ACCEPTABLE when existing ones don't capture the essence well.\n"
            )
        elif unique_count <= 500:
            # 200-500: Moderate strictness
            strictness_msg = (
                "IMPORTANT: Try to select from the existing list when reasonably applicable. "
                "Only create new keywords when existing ones clearly don't match.\n"
            )
        else:
            # 500+: Very strict, only create if completely unrelated
            strictness_msg = (
                "CRITICAL: You MUST select from the existing list unless the theme is COMPLETELY UNRELATED to all existing keywords. "
                "In almost all cases, use existing keywords.\n"
            )
        
        sys += strictness_msg
        sys += f"Existing keywords ({len(top_keywords)} of {unique_count} total): {', '.join(top_keywords)}\n\n"
    sys += "Output STRICT JSON only:\n" + json_example
    user = "Prompt snippets:\n" + "\n".join([f"- {s}" for s in samples])

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": sys}, {"role": "user", "content": user}],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 4096,
        "stream": True,  # Enable streaming
    }
    # Some OpenAI-compatible providers may use different token limit fields / casing.
    payload["max_output_tokens"] = payload["max_tokens"]
    payload["max_completion_tokens"] = payload["max_tokens"]
    payload["maxOutputTokens"] = payload["max_tokens"]
    payload["maxCompletionTokens"] = payload["max_tokens"]

    # Parse JSON from streaming response.
    # Retry up to 5 times for: network errors, API errors, parsing failures.
    attempt_debug: List[Dict] = []
    last_err = ""
    full_response_content = ""
    request_id = f"tc_{int(time.time() * 1000)}_{os.getpid()}"

    logger.info(
        "[topic_cluster][%s] LLM request: model=%s samples=%s existing_keywords=%s max_tokens=%s timeout=120",
        request_id, model, len(samples), len(existing_keywords) if existing_keywords else 0, payload.get("max_tokens", "N/A")
    )
    logger.debug("[topic_cluster][%s] LLM request payload: %s", request_id, json.dumps(payload, ensure_ascii=False, indent=2))

    for attempt in range(1, 6):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
        except requests.RequestException as e:
            last_err = f"network_error: {type(e).__name__}: {e}"
            attempt_debug.append({"attempt": attempt, "reason": "network_error", "error": str(e)})
            logger.warning(
                "[topic_cluster][%s] llm_request_error attempt=%s err=%s",
                request_id, attempt, str(e)
            )
            continue

        # Retry on server-side errors (5xx) and rate limits (429), but not client errors (4xx except 429).
        if resp.status_code != 200:
            body = resp.text or ""
            status = 400 if resp.status_code == 401 else resp.status_code
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"api_error_retriable: status={status} body={body}"
                attempt_debug.append({"attempt": attempt, "reason": "api_error_retriable", "status": status, "body": body})
                logger.warning(
                    "[topic_cluster][%s] llm_http_error attempt=%s status=%s body=%s",
                    request_id, attempt, status, body
                )
                continue
            # 4xx (except 429): fail immediately (client error, not retriable)
            logger.error(
                "[topic_cluster][%s] llm_http_client_error status=%s body=%s",
                request_id, status, body
            )
            raise HTTPException(status_code=status, detail=body)

        try:
            # Use streaming response accumulation
            content = accumulate_streaming_response(resp)
            full_response_content = content
        except Exception as e:
            last_err = f"streaming_error: {type(e).__name__}: {e}"
            attempt_debug.append({"attempt": attempt, "reason": "streaming_error", "error": str(e)})
            logger.warning(
                "[topic_cluster][%s] llm_streaming_error attempt=%s err=%s",
                request_id, attempt, str(e)
            )
            continue

        logger.info(
            "[topic_cluster][%s] llm_response attempt=%s content_length=%s",
            request_id, attempt, len(content)
        )
        logger.debug("[topic_cluster][%s] llm_response content: %s", request_id, content)

        # Extract JSON from content
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            last_err = f"no_json_object; content={content}"
            attempt_debug.append({"attempt": attempt, "reason": "no_json_object", "content": content})
            logger.warning(
                "[topic_cluster][%s] llm_no_json attempt=%s content=%s",
                request_id, attempt, content
            )
            continue

        json_str = m.group(0)
        try:
            obj = json.loads(json_str)
        except Exception as e:
            last_err = f"json_parse_failed: {type(e).__name__}: {e}; json_str={json_str}"
            attempt_debug.append({"attempt": attempt, "reason": "json_parse_failed", "json_str": json_str, "error": str(e)})
            logger.warning(
                "[topic_cluster][%s] llm_json_parse_error attempt=%s err=%s json_str=%s",
                request_id, attempt, str(e), json_str
            )
            continue

        if not isinstance(obj, dict):
            last_err = f"json_not_object; json_str={json_str}"
            attempt_debug.append({"attempt": attempt, "reason": "json_not_object", "json_str": json_str})
            logger.warning(
                "[topic_cluster][%s] llm_json_not_object attempt=%s json_str=%s",
                request_id, attempt, json_str
            )
            continue

        title = str(obj.get("title") or "").strip()
        keywords = obj.get("keywords") or []
        if not title:
            last_err = f"missing_title; json_str={json_str}"
            attempt_debug.append({"attempt": attempt, "reason": "missing_title", "json_str": json_str})
            logger.warning(
                "[topic_cluster][%s] llm_missing_title attempt=%s json_str=%s",
                request_id, attempt, json_str
            )
            continue
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(x).strip() for x in keywords if str(x).strip()][:6]
        logger.info(
            "[topic_cluster][%s] llm_success attempt=%s title=%s keywords=%s",
            request_id, attempt, title, keywords
        )
        return {"title": title[:24], "keywords": keywords}

    # Exhausted retries
    dbg = json.dumps(attempt_debug, ensure_ascii=False)
    logger.error(
        "[topic_cluster][%s] llm_failed all_attempts=5 last_error=%s attempts=%s full_response=%s",
        request_id, last_err, dbg, full_response_content
    )
    raise HTTPException(
        status_code=502,
        detail=f"Chat API JSON extraction failed after 5 attempts; request_id={request_id}; last_error={last_err}; attempts={dbg}",
    )


async def _call_chat_title(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt_samples: List[str],
    output_lang: str,
    existing_keywords: Optional[List[str]] = None,
) -> Dict:
    """
    Same rationale as embeddings:
    - requests.post() is synchronous and would block the event loop thread for tens of seconds.
    - Offload to a worker thread to keep other API requests responsive.
    """
    ret = await asyncio.to_thread(
        _call_chat_title_sync,
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt_samples=prompt_samples,
        output_lang=output_lang,
        existing_keywords=existing_keywords,
    )
    if not isinstance(ret, dict):
        raise HTTPException(status_code=502, detail="Chat API returned empty title payload")
    return ret


def mount_topic_cluster_routes(
    app: FastAPI,
    db_api_base: str,
    verify_secret,
    write_permission_required,
    *,
    openai_base_url: str,
    openai_api_key: str,
    embedding_model: str,
    ai_model: str,
):
    """
    Mount embedding + topic clustering endpoints (MVP: manual, iib_output only).
    """
    # IMPORTANT:
    # Do NOT call _ensure_perf_deps() here.
    # We only fail at specific API endpoints so users without deps can still use other features.

    async def _run_cluster_job(job_id: str, req) -> None:
        logger.info("[cluster_job] === _run_cluster_job START ===")
        logger.info("[cluster_job] job_id=%s", job_id)

        try:
            logger.info("[cluster_job] Initializing job")
            _job_upsert(
                job_id,
                {
                    "status": "running",
                    "stage": "init",
                    "created_at": _job_now(),
                    "req": req.model_dump() if hasattr(req, "model_dump") else req.dict(),
                },
            )
            logger.info("[cluster_job] Job initialized successfully")

            logger.info("[cluster_job] Extracting and validating folders")
            folders = _extract_and_validate_folders(req)
            logger.info("[cluster_job] Folders validated: %s", folders)
            _job_upsert(job_id, {"folders": folders, "stage": "embedding"})

            # Aggregate per-folder embedding progress into totals
            per_folder: Dict[str, Dict] = {}

            def _embed_cb(p: Dict) -> None:
                if not isinstance(p, dict):
                    return
                if p.get("stage") != "embedding":
                    return
                f = str(p.get("folder") or "")
                if f:
                    per_folder[f] = dict(p)
                scanned = sum(int(x.get("scanned") or 0) for x in per_folder.values())
                to_embed = sum(int(x.get("to_embed") or 0) for x in per_folder.values())
                embedded_done = sum(int(x.get("embedded_done") or 0) for x in per_folder.values())
                updated = sum(int(x.get("updated") or 0) for x in per_folder.values())
                skipped = sum(int(x.get("skipped") or 0) for x in per_folder.values())
                _job_upsert(
                    job_id,
                    {
                        "stage": "embedding",
                        "progress": {
                            "scanned": scanned,
                            "to_embed": to_embed,
                            "embedded_done": embedded_done,
                            "updated": updated,
                            "skipped": skipped,
                            "folder": f,
                            "batch_n": int((p or {}).get("batch_n") or 0),
                        },
                    },
                )

            # Ensure embeddings exist (incremental per folder)
            model = req.model or embedding_model
            batch_size = max(1, min(int(req.batch_size or 64), 256))
            max_chars = max(256, min(int(req.max_chars or 4000), 8000))
            force = bool(req.force_embed)
            recursive = bool(req.recursive) if req.recursive is not None else True
            logger.info(f"[_run_cluster_job] recursive={recursive}, req.recursive={req.recursive}")
            for f in folders:
                logger.info(f"[_run_cluster_job] Building embeddings for folder: {f}, recursive={recursive}")
                await _build_embeddings_one_folder(
                    folder=f,
                    model=model,
                    force=force,
                    batch_size=batch_size,
                    max_chars=max_chars,
                    recursive=recursive,
                    progress_cb=_embed_cb,
                )

            # If embeddings didn't change and we have a cached clustering result, return it directly.
            conn = DataBase.get_conn()
            like_prefixes = [os.path.join(f, "%") for f in folders]
            with closing(conn.cursor()) as cur:
                where = " OR ".join(["image.path LIKE ?"] * len(like_prefixes))
                cur.execute(
                    f"""SELECT COUNT(*), MAX(image_embedding.updated_at)
                        FROM image
                        INNER JOIN image_embedding ON image_embedding.image_id = image.id
                        WHERE ({where}) AND image_embedding.model = ?""",
                    (*like_prefixes, model),
                )
                row = cur.fetchone() or (0, "")
            embeddings_count = int(row[0] or 0)
            embeddings_max_updated_at = str(row[1] or "")

            cache_params = {
                "model": model,
                "threshold": float(req.threshold or 0.90),
                "min_cluster_size": int(req.min_cluster_size or 2),
                "assign_noise_threshold": req.assign_noise_threshold,
                "title_model": req.title_model,
                "lang": str(req.lang or ""),
                "nv": _PROMPT_NORMALIZE_VERSION,
                "nm": _PROMPT_NORMALIZE_MODE,
                "recursive": recursive,
            }
            h = hashlib.sha1()
            h.update(json.dumps({"folders": folders, "params": cache_params}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            cache_key = h.hexdigest()
            cached = TopicClusterCache.get(conn, cache_key)
            if (
                cached
                and int(cached.get("embeddings_count") or 0) == embeddings_count
                and str(cached.get("embeddings_max_updated_at") or "") == embeddings_max_updated_at
                and isinstance(cached.get("result"), dict)
            ):
                _job_upsert(job_id, {"status": "done", "stage": "done", "result": cached["result"], "cache_hit": True})
                return

            # Clustering + titling progress
            def _cluster_cb(p: Dict) -> None:
                if not isinstance(p, dict):
                    return
                st = str(p.get("stage") or "")
                if st:
                    patch = {"stage": st, "status": "running"}
                    # keep small progress fields only
                    prog = {}
                    for k in [
                        "items_total",
                        "items_done",
                        "clusters_total",
                        "clusters_done",
                        "folder",
                    ]:
                        if k in p:
                            prog[k] = p.get(k)
                    if prog:
                        patch["progress"] = {**(_job_get(job_id) or {}).get("progress", {}), **prog}
                    _job_upsert(job_id, patch)

            res = await _cluster_after_embeddings(req, folders, progress_cb=_cluster_cb)
            try:
                TopicClusterCache.upsert(
                    conn,
                    cache_key=cache_key,
                    folders=folders,
                    model=model,
                    params=cache_params,
                    embeddings_count=embeddings_count,
                    embeddings_max_updated_at=embeddings_max_updated_at,
                    result=res,
                )
                conn.commit()
            except Exception:
                pass
            _job_upsert(job_id, {"status": "done", "stage": "done", "result": res})
        except HTTPException as e:
            _job_upsert(job_id, {"status": "error", "stage": "error", "error": str(e.detail)})
        except Exception as e:
            _job_upsert(job_id, {"status": "error", "stage": "error", "error": f"{type(e).__name__}: {e}"})

    async def _build_embeddings_one_folder(
        *,
        folder: str,
        model: str,
        force: bool,
        batch_size: int,
        max_chars: int,
        recursive: bool = True,
        progress_cb: Optional[Callable[[Dict], None]] = None,
    ) -> Dict:
        """
        Build embeddings for a single folder with optional progress callback.
        Progress payload (best-effort):
        - stage: "embedding"
        - folder, scanned, to_embed, embedded_done, updated, skipped

        Args:
            recursive: If True, include files in subfolders. Default True for backward compatibility.
        """
        logger.info("[build_embeddings] === _build_embeddings_one_folder START ===")
        logger.info("[build_embeddings] folder=%s model=%s force=%s batch_size=%s max_chars=%s recursive=%s",
                    folder, model, force, batch_size, max_chars, recursive)

        if not openai_api_key:
            logger.error("[build_embeddings] OpenAI API Key not configured")
            raise HTTPException(status_code=500, detail="OpenAI API Key not configured")

        if not openai_base_url:
            logger.error("[build_embeddings] OpenAI Base URL not configured")
            raise HTTPException(status_code=500, detail="OpenAI Base URL not configured")

        logger.info("[build_embeddings] Configuration check passed")
        logger.info("[build_embeddings] API URL: %s", openai_base_url)

        folder = os.path.normpath(folder)
        logger.info("[build_embeddings] Normalized folder path: %s", folder)

        if not os.path.exists(folder) or not os.path.isdir(folder):
            logger.error("[build_embeddings] Folder not found: %s", folder)
            raise HTTPException(status_code=400, detail=f"Folder not found: {folder}")

        conn = DataBase.get_conn()
        like_prefix = os.path.join(folder, "%")
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT id, path, exif FROM image WHERE path LIKE ?", (like_prefix,))
            rows = cur.fetchall()

        # Filter to direct children only if not recursive
        if not recursive:
            def is_direct_child(path: str) -> bool:
                parent_dir = os.path.dirname(path)
                return os.path.normpath(parent_dir) == os.path.normpath(folder)
            rows = [r for r in rows if is_direct_child(r[1])]
            logger.info("[build_embeddings] Filtered to direct children: %d files", len(rows))

        images = []
        for image_id, path, exif in rows:
            if not isinstance(path, str) or not os.path.exists(path):
                continue
            text_raw = _extract_prompt_text(exif, max_chars=max_chars)
            if _PROMPT_NORMALIZE_ENABLED:
                text = _clean_prompt_for_semantic(text_raw)
                if not text:
                    text = text_raw
            else:
                text = text_raw
            # Some embedding models/providers have strict context limits (often 8192 tokens).
            # Apply a conservative truncation before sending to embedding API.
            text = _truncate_for_embedding_tokens(text, _EMBEDDING_MAX_TOKENS_SOFT)
            if not text:
                continue
            images.append({"id": int(image_id), "path": path, "text": text})

        if not images:
            if progress_cb:
                progress_cb(
                    {
                        "stage": "embedding",
                        "folder": folder,
                        "scanned": 0,
                        "to_embed": 0,
                        "embedded_done": 0,
                        "updated": 0,
                        "skipped": 0,
                    }
                )
            return {"folder": folder, "count": 0, "updated": 0, "skipped": 0, "model": model}

        id_list = [x["id"] for x in images]
        existing = ImageEmbedding.get_by_image_ids(conn, id_list)
        existing_fail = ImageEmbeddingFail.get_by_image_ids(conn, id_list, model)

        to_embed = []
        skipped = 0
        skipped_failed = 0
        for item in images:
            # include normalize version to force refresh when rules change
            text_hash = ImageEmbedding.compute_text_hash(f"{_PROMPT_NORMALIZE_VERSION}:{item['text']}")
            old = existing.get(item["id"])
            old_fail = existing_fail.get(item["id"])
            if (
                (not force)
                and old
                and old.get("model") == model
                and old.get("text_hash") == text_hash
                and old.get("vec")
            ):
                skipped += 1
                continue
            # Skip known failures for the same model+text_hash (unless force is enabled).
            if (not force) and old_fail and str(old_fail.get("text_hash") or "") == text_hash:
                skipped_failed += 1
                continue
            to_embed.append({**item, "text_hash": text_hash})

        if progress_cb:
            progress_cb(
                {
                    "stage": "embedding",
                    "folder": folder,
                    "scanned": len(images),
                    "to_embed": len(to_embed),
                    "embedded_done": 0,
                    "updated": 0,
                    "skipped": skipped,
                    "skipped_failed": skipped_failed,
                    "failed": 0,
                }
            )

        updated = 0
        embedded_done = 0
        failed = 0
        batches = _batched_by_token_budget(
            to_embed,
            max_items=batch_size,
            max_tokens_sum=_EMBEDDING_REQUEST_MAX_TOKENS_SOFT,
        )
        for bi, batch in enumerate(batches):
            inputs = [x["text"] for x in batch]
            if _EMBEDDING_DEBUG:
                token_sum = sum(_estimate_tokens_soft(s) for s in inputs)
                token_max = max((_estimate_tokens_soft(s) for s in inputs), default=0)
                print(
                    f"[iib][embed] folder={folder} batch={bi+1}/{len(batches)} n={len(inputs)} token_sum~={token_sum} token_max~={token_max}"
                )
            try:
                logger.info("[build_embeddings] Calling embedding API for batch %d/%d, size=%d",
                          bi+1, len(batches), len(inputs))
                vectors = await _call_embeddings(
                    inputs=inputs,
                    model=model,
                    base_url=openai_base_url,
                    api_key=openai_api_key,
                )
                logger.info("[build_embeddings] Embedding API success for batch %d/%d",
                          bi+1, len(batches))
            except HTTPException as e:
                logger.error("[build_embeddings] Embedding API failed for batch %d/%d: %s",
                           bi+1, len(batches), str(e.detail))
                if "localhost" in openai_base_url or "127.0.0.1" in openai_base_url:
                    logger.error("[build_embeddings] Local API request failed, please check if Ollama is running: ollama serve")
                # Cache failures for this batch and continue (skip these images for now).
                err = str(e.detail)
                for it in batch:
                    try:
                        ImageEmbeddingFail.upsert(
                            conn,
                            image_id=int(it["id"]),
                            model=str(model),
                            text_hash=str(it.get("text_hash") or ""),
                            error=err,
                        )
                    except Exception:
                        pass
                try:
                    conn.commit()
                except Exception:
                    pass
                failed += len(batch)
                if progress_cb:
                    progress_cb(
                        {
                            "stage": "embedding",
                            "folder": folder,
                            "scanned": len(images),
                            "to_embed": len(to_embed),
                            "embedded_done": embedded_done,
                            "updated": updated,
                            "skipped": skipped,
                            "skipped_failed": skipped_failed,
                            "failed": failed,
                            "batch_n": len(inputs),
                        }
                    )
                await asyncio.sleep(0)
                continue
            if len(vectors) != len(batch):
                raise HTTPException(status_code=500, detail="Embeddings count mismatch")
            for item, vec in zip(batch, vectors):
                ImageEmbedding.upsert(
                    conn=conn,
                    image_id=item["id"],
                    model=model,
                    dim=len(vec),
                    text_hash=item["text_hash"],
                    vec_blob=_vec_to_blob_f32(vec),
                )
                # Success -> clear fail cache for this image+model (any old failure becomes irrelevant).
                try:
                    ImageEmbeddingFail.delete(conn, image_id=int(item["id"]), model=str(model))
                except Exception:
                    pass
                updated += 1
                embedded_done += 1
            conn.commit()
            if progress_cb:
                progress_cb(
                    {
                        "stage": "embedding",
                        "folder": folder,
                        "scanned": len(images),
                        "to_embed": len(to_embed),
                        "embedded_done": embedded_done,
                        "updated": updated,
                        "skipped": skipped,
                        "skipped_failed": skipped_failed,
                        "failed": failed,
                        "batch_n": len(inputs),
                    }
                )
            # yield between batches
            await asyncio.sleep(0)

        return {
            "folder": folder,
            "count": len(images),
            "updated": updated,
            "skipped": skipped,
            "skipped_failed": skipped_failed,
            "failed": failed,
            "model": model,
        }

    class BuildIibOutputEmbeddingReq(BaseModel):
        folder: Optional[str] = None  # default: {cwd}/iib_output
        model: Optional[str] = None
        force: Optional[bool] = False
        batch_size: Optional[int] = 64
        max_chars: Optional[int] = 4000
        # If True, recursively scan subfolders; default True for backward compatibility
        recursive: Optional[bool] = True

    @app.post(
        f"{db_api_base}/build_iib_output_embeddings",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def build_iib_output_embeddings(req: BuildIibOutputEmbeddingReq):
        # TopicSearch feature requires perf deps; fail at API layer, not at server start.
        _ensure_perf_deps()
        if not openai_api_key:
            raise HTTPException(status_code=500, detail="OpenAI API Key not configured")
        if not openai_base_url:
            raise HTTPException(status_code=500, detail="OpenAI Base URL not configured")
        folder = req.folder or os.path.join(cwd, "iib_output")
        model = req.model or embedding_model
        batch_size = max(1, min(int(req.batch_size or 64), 256))
        max_chars = max(256, min(int(req.max_chars or 4000), 8000))
        force = bool(req.force)
        recursive = bool(req.recursive) if req.recursive is not None else True
        return await _build_embeddings_one_folder(
            folder=folder,
            model=model,
            force=force,
            batch_size=batch_size,
            max_chars=max_chars,
            recursive=recursive,
            progress_cb=None,
        )

    class ClusterIibOutputReq(BaseModel):
        folder: Optional[str] = None
        folder_paths: Optional[List[str]] = None
        model: Optional[str] = None
        force_embed: Optional[bool] = False
        # Default a bit stricter to avoid over-merged broad topics for natural-language prompts.
        threshold: Optional[float] = 0.90
        batch_size: Optional[int] = 64
        max_chars: Optional[int] = 4000
        min_cluster_size: Optional[int] = 2
        # B: LLM title generation
        title_model: Optional[str] = None
        # Reduce noise by reassigning small-cluster members to best large cluster if similarity is high enough
        assign_noise_threshold: Optional[float] = None
        # Cache titles in sqlite to avoid repeated LLM calls
        use_title_cache: Optional[bool] = True
        force_title: Optional[bool] = False
        # Output language for titles/keywords (from frontend globalStore.lang)
        lang: Optional[str] = None
        # If True, recursively scan subfolders; default True for Topic Search (backward compatible)
        recursive: Optional[bool] = True

    def _scope_cache_stale_by_folders(conn: Connection, folders: List[str]) -> Dict:
        """
        Determine whether selected folders (and their subfolders) appear to have changed
        since last /db/update_image_data run.

        We use the existing `folders` table (Folder) which stores last observed modified_date.
        If any tracked folder's current modified_date differs, we treat cache as stale.
        If a selected root folder is missing from the table, treat as stale (not indexed yet).
        """
        try:
            from scripts.iib.db.datamodel import Folder
            from scripts.iib.tool import get_modified_date
        except Exception:
            # If imports fail for any reason, be conservative.
            return {"folders_changed": True, "reason": "folder_state_check_import_failed"}

        changed_path = ""
        # root folder missing in table => stale
        with closing(conn.cursor()) as cur:
            for f in folders:
                cur.execute("SELECT id, path, modified_date FROM folders WHERE path = ?", (str(f),))
                row = cur.fetchone()
                if not row:
                    return {"folders_changed": True, "reason": "folder_not_indexed", "path": str(f)}
        # check all known subfolders under each selected folder
        for f in folders:
            like_prefix = os.path.join(str(f), "%")
            with closing(conn.cursor()) as cur:
                cur.execute(
                    "SELECT path, modified_date FROM folders WHERE path = ? OR path LIKE ?",
                    (str(f), like_prefix),
                )
                rows = cur.fetchall() or []
            for p, stored in rows:
                p = str(p or "")
                if not p:
                    continue
                if not os.path.exists(p) or not os.path.isdir(p):
                    # path removed: treat as changed
                    changed_path = p
                    return {"folders_changed": True, "reason": "folder_missing", "path": changed_path}
                cur_md = str(get_modified_date(p))
                if cur_md != str(stored or ""):
                    changed_path = p
                    return {
                        "folders_changed": True,
                        "reason": "folder_modified_date_changed",
                        "path": changed_path,
                        "stored": str(stored or ""),
                        "current": cur_md,
                    }
        return {"folders_changed": False}

    def _embeddings_state(conn: Connection, folders: List[str], model: str) -> Dict:
        like_prefixes = [os.path.join(f, "%") for f in folders]
        with closing(conn.cursor()) as cur:
            where = " OR ".join(["image.path LIKE ?"] * len(like_prefixes))
            cur.execute(
                f"""SELECT COUNT(*), MAX(image_embedding.updated_at)
                    FROM image
                    INNER JOIN image_embedding ON image_embedding.image_id = image.id
                    WHERE ({where}) AND image_embedding.model = ?""",
                (*like_prefixes, str(model)),
            )
            row = cur.fetchone() or (0, "")
        return {"embeddings_count": int(row[0] or 0), "embeddings_max_updated_at": str(row[1] or "")}

    @app.post(
        f"{db_api_base}/cluster_iib_output_cached",
        # Read-only: do NOT require write permission; only reads sqlite cache and folder mtimes.
        dependencies=[Depends(verify_secret)],
    )
    async def cluster_iib_output_cached(req: ClusterIibOutputReq):
        """
        Return cached clustering result if exists, WITHOUT triggering embedding/clustering.
        Also returns a lightweight "stale" signal based on:
        - folders table modified_date changes (requires user to run refresh/index update)
        - embedding state changes (count / max(updated_at)) compared to cache metadata
        """
        folders = _extract_and_validate_folders(req)
        model = req.model or embedding_model
        conn = DataBase.get_conn()

        cache_params = {
            "model": str(model),
            "threshold": float(req.threshold or 0.90),
            "min_cluster_size": int(req.min_cluster_size or 2),
            "assign_noise_threshold": req.assign_noise_threshold,
            "title_model": req.title_model,
            "lang": str(req.lang or ""),
            "nv": _PROMPT_NORMALIZE_VERSION,
            "nm": _PROMPT_NORMALIZE_MODE,
        }
        h = hashlib.sha1()
        h.update(json.dumps({"folders": folders, "params": cache_params}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        cache_key = h.hexdigest()

        cached = TopicClusterCache.get(conn, cache_key)
        folder_state = _scope_cache_stale_by_folders(conn, folders)
        emb_state = _embeddings_state(conn, folders, model)

        embeddings_changed = False
        if cached:
            embeddings_changed = (
                int(cached.get("embeddings_count") or 0) != int(emb_state["embeddings_count"])
                or str(cached.get("embeddings_max_updated_at") or "") != str(emb_state["embeddings_max_updated_at"])
            )

        stale = bool(folder_state.get("folders_changed")) or bool(embeddings_changed) or (not cached)
        return {
            "cache_key": cache_key,
            "cache_hit": bool(cached and isinstance(cached.get("result"), dict)),
            "cached_at": (cached or {}).get("updated_at") if cached else "",
            "result": (cached or {}).get("result") if cached else None,
            "stale": stale,
            "stale_reason": {
                **folder_state,
                "embeddings_changed": bool(embeddings_changed),
                "embeddings_count": int(emb_state["embeddings_count"]),
                "embeddings_max_updated_at": str(emb_state["embeddings_max_updated_at"]),
            },
        }

    @app.post(
        f"{db_api_base}/cluster_iib_output_job_start",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def cluster_iib_output_job_start(req: ClusterIibOutputReq):
        # TopicSearch feature requires perf deps; fail at API layer, not at server start.
        _ensure_perf_deps()
        """
        Start a background job for embedding + clustering + LLM titling.
        Returns job_id immediately; frontend should poll job_status to show progress.
        """
        job_id = uuid.uuid4().hex
        _job_upsert(job_id, {"status": "queued", "stage": "queued", "created_at": _job_now()})
        asyncio.create_task(_run_cluster_job(job_id, req))
        return {"job_id": job_id}

    @app.get(
        f"{db_api_base}/cluster_iib_output_job_status",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def cluster_iib_output_job_status(job_id: str):
        j = _job_get(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="job not found")
        return j

    def _extract_and_validate_folders(req: ClusterIibOutputReq) -> List[str]:
        folders: List[str] = []
        if req.folder_paths:
            for p in req.folder_paths:
                if isinstance(p, str) and p.strip():
                    folders.append(os.path.normpath(p.strip()))
        if req.folder and isinstance(req.folder, str) and req.folder.strip():
            folders.append(os.path.normpath(req.folder.strip()))
        # 用户不会用默认 iib_output：未指定范围则直接报错
        if not folders:
            raise HTTPException(status_code=400, detail="folder_paths is required (select folders to cluster)")
        folders = list(dict.fromkeys(folders))
        for f in folders:
            if not os.path.exists(f) or not os.path.isdir(f):
                raise HTTPException(status_code=400, detail=f"Folder not found: {f}")
        return folders

    async def _cluster_after_embeddings(
        req: ClusterIibOutputReq,
        folders: List[str],
        progress_cb: Optional[Callable[[Dict], None]] = None,
    ) -> Dict:
        logger.info("[cluster_after] === _cluster_after_embeddings START ===")
        logger.info("[cluster_after] folders=%s", folders)

        folder = folders[0]
        model = req.model or embedding_model
        threshold = float(req.threshold or 0.90)
        threshold = max(0.0, min(threshold, 0.999))
        min_cluster_size = max(1, int(req.min_cluster_size or 2))
        title_model = req.title_model or os.getenv("TOPIC_TITLE_MODEL") or ai_model
        output_lang = _normalize_output_lang(req.lang)
        assign_noise_threshold = req.assign_noise_threshold

        logger.info("[cluster_after] model=%s threshold=%s min_cluster_size=%s", model, threshold, min_cluster_size)
        logger.info("[cluster_after] title_model=%s output_lang=%s", title_model, output_lang)

        if assign_noise_threshold is None:
            assign_noise_threshold = max(0.88, min(threshold + 0.02, 0.97))
        else:
            assign_noise_threshold = max(0.0, min(float(assign_noise_threshold), 0.999))

        logger.info("[cluster_after] assign_noise_threshold=%s", assign_noise_threshold)

        use_title_cache = bool(True if req.use_title_cache is None else req.use_title_cache)
        force_title = bool(req.force_title)

        logger.info("[cluster_after] use_title_cache=%s force_title=%s", use_title_cache, force_title)

        recursive = bool(req.recursive) if req.recursive is not None else False
        logger.info("[cluster_after] recursive=%s", recursive)

        if progress_cb:
            logger.info("[cluster_after] Calling progress callback with clustering stage")
            progress_cb({"stage": "clustering", "folder": folder, "folders": folders})

        conn = DataBase.get_conn()
        like_prefixes = [os.path.join(f, "%") for f in folders]
        with closing(conn.cursor()) as cur:
            where = " OR ".join(["image.path LIKE ?"] * len(like_prefixes))
            cur.execute(
                f"""SELECT image.id, image.path, image.exif, image_embedding.vec
                    FROM image
                    INNER JOIN image_embedding ON image_embedding.image_id = image.id
                    WHERE ({where}) AND image_embedding.model = ?""",
                (*like_prefixes, model),
            )
            rows = cur.fetchall()

        # Filter to direct children only if not recursive
        if not recursive:
            def is_direct_child(path: str, folders: List[str]) -> bool:
                for f in folders:
                    parent_dir = os.path.dirname(path)
                    if os.path.normpath(parent_dir) == os.path.normpath(f):
                        return True
                return False
            rows = [r for r in rows if is_direct_child(r[1], folders)]
            logger.info("[cluster_after] Filtered to direct children: %d files", len(rows))

        items = []
        for n, (image_id, path, exif, vec_blob) in enumerate(rows):
            if not isinstance(path, str) or not os.path.exists(path):
                continue
            if not vec_blob:
                continue
            vec = _blob_to_vec_f32(vec_blob)
            n2 = _l2_norm_sq(vec)
            if n2 <= 0:
                continue
            inv = 1.0 / math.sqrt(n2)
            for i in range(len(vec)):
                vec[i] *= inv
            text_raw = _extract_prompt_text(exif, max_chars=int(req.max_chars or 4000))
            if _PROMPT_NORMALIZE_ENABLED:
                text = _clean_prompt_for_semantic(text_raw)
                if not text:
                    text = text_raw
            else:
                text = text_raw
            items.append({"id": int(image_id), "path": path, "text": text, "vec": vec})
            if (n + 1) % 800 == 0:
                await asyncio.sleep(0)

        if not items:
            return {"folder": folder, "folders": folders, "model": model, "threshold": threshold, "clusters": [], "noise": []}

        # Incremental clustering by centroid-direction (sum vector)
        clusters = []  # {sum, norm_sq, members:[idx], sample_text}
        ann_idx = None
        ann_centroids = None
        ann_rebuild_every = 256  # rebuild index periodically to reflect centroid updates
        ann_topk = 8
        for idx, it in enumerate(items):
            v = it["vec"]
            best_ci = -1
            best_sim = -1.0
            best_dot = 0.0
            # Build / rebuild ANN index when helpful (many clusters)
            if len(clusters) >= 64:
                if ann_idx is None or (idx % ann_rebuild_every == 0):
                    # rebuild from current centroids
                    cents = []
                    for c in clusters:
                        cv = _centroid_vec_np(c["sum"], c["norm_sq"])
                        if cv is None:
                            # fallback to zeros; will never be nearest
                            cv = _np.zeros((len(v),), dtype=_np.float32)  # type: ignore
                        cents.append(cv)
                    ann_centroids = _np.stack(cents, axis=0).astype(_np.float32)  # type: ignore
                    ann_idx = _build_hnsw_index(ann_centroids)
                # Query candidates
                if ann_idx is not None:
                    q = _np.frombuffer(v.tobytes(), dtype=_np.float32).reshape(1, -1).copy()  # type: ignore
                    labels, _dists = ann_idx.knn_query(q, k=min(ann_topk, len(clusters)))
                    cand = [int(x) for x in (labels[0].tolist() if labels is not None else [])]
                else:
                    cand = list(range(len(clusters)))
            else:
                cand = list(range(len(clusters)))

            for ci in cand:
                c = clusters[ci]
                dotv = _dot(v, c["sum"])
                denom = math.sqrt(c["norm_sq"]) if c["norm_sq"] > 0 else 1.0
                sim = dotv / denom
                if sim > best_sim:
                    best_sim = sim
                    best_ci = ci
                    best_dot = dotv
            if best_ci != -1 and best_sim >= threshold:
                c = clusters[best_ci]
                for i in range(len(v)):
                    c["sum"][i] += v[i]
                c["norm_sq"] = c["norm_sq"] + 2.0 * best_dot + 1.0
                c["members"].append(idx)
            else:
                clusters.append({"sum": array("f", v), "norm_sq": 1.0, "members": [idx], "sample_text": it.get("text") or ""})
                ann_idx = None  # force rebuild after new cluster
            if (idx + 1) % 800 == 0:
                if progress_cb:
                    progress_cb({"stage": "clustering", "items_total": len(items), "items_done": idx + 1})
                await asyncio.sleep(0)

        # Merge highly similar clusters (fix: same theme split into multiple clusters)
        # IMPORTANT: keep this VERY strict; otherwise unrelated clusters can be merged.
        merge_threshold = min(0.999, max(threshold + 0.08, 0.965))
        merged = True
        while merged and len(clusters) > 1:
            merged = False
            best_i = best_j = -1
            best_sim = merge_threshold
            for i in range(len(clusters)):
                ci = clusters[i]
                for j in range(i + 1, len(clusters)):
                    cj = clusters[j]
                    sim = _cos_sum(ci["sum"], ci["norm_sq"], cj["sum"], cj["norm_sq"])
                    if sim >= best_sim:
                        best_sim = sim
                        best_i, best_j = i, j
            if best_i != -1:
                a = clusters[best_i]
                b = clusters[best_j]
                for k in range(len(a["sum"])):
                    a["sum"][k] += b["sum"][k]
                a["norm_sq"] = _l2_norm_sq(a["sum"])
                a["members"].extend(b["members"])
                if not a.get("sample_text"):
                    a["sample_text"] = b.get("sample_text", "")
                clusters.pop(best_j)
                merged = True
            await asyncio.sleep(0)

        # Reassign members from small clusters into best large cluster to reduce noise
        if min_cluster_size > 1 and assign_noise_threshold > 0 and clusters:
            large = [c for c in clusters if len(c["members"]) >= min_cluster_size]
            if large:
                new_large = []
                # copy large clusters first
                for c in clusters:
                    if len(c["members"]) >= min_cluster_size:
                        new_large.append(c)
                # Build ANN over large centroids once (optional)
                ann_large = None
                if len(new_large) >= 64:
                    cents = []
                    for c in new_large:
                        cv = _centroid_vec_np(c["sum"], c["norm_sq"])
                        if cv is None:
                            cv = _np.zeros((len(items[0]["vec"]),), dtype=_np.float32)  # type: ignore
                        cents.append(cv)
                    cent_mat = _np.stack(cents, axis=0).astype(_np.float32)  # type: ignore
                    ann_large = _build_hnsw_index(cent_mat, ef=64, M=32)
                # reassign items from small clusters
                for c in clusters:
                    if len(c["members"]) >= min_cluster_size:
                        continue
                    for mi in c["members"]:
                        v = items[mi]["vec"]
                        best_ci = -1
                        best_sim = -1.0
                        best_dot = 0.0
                        if ann_large is not None:
                            q = _np.frombuffer(v.tobytes(), dtype=_np.float32).reshape(1, -1).copy()  # type: ignore
                            labels, _dists = ann_large.knn_query(q, k=min(8, len(new_large)))
                            cand = [int(x) for x in (labels[0].tolist() if labels is not None else [])]
                        else:
                            cand = range(len(new_large))
                        for ci in cand:
                            bigc = new_large[ci]
                            dotv = _dot(v, bigc["sum"])
                            denom = math.sqrt(bigc["norm_sq"]) if bigc["norm_sq"] > 0 else 1.0
                            sim = dotv / denom
                            if sim > best_sim:
                                best_sim = sim
                                best_ci = ci
                                best_dot = dotv
                        if best_ci != -1 and best_sim >= assign_noise_threshold:
                            bigc = new_large[best_ci]
                            for k in range(len(v)):
                                bigc["sum"][k] += v[k]
                            bigc["norm_sq"] = bigc["norm_sq"] + 2.0 * best_dot + 1.0
                            bigc["members"].append(mi)
                        # else: keep in small cluster -> will become noise below
                    await asyncio.sleep(0)
                clusters = new_large

        # Split small clusters to noise, generate titles
        out_clusters = []
        noise = []
        if progress_cb:
            progress_cb({"stage": "titling", "clusters_total": len(clusters)})

        existing_keywords: List[str] = []
        keyword_frequency: Dict[str, int] = TopicTitleCache.get_all_keywords_frequency(conn, model)

        def _get_top_keywords() -> List[str]:
            if not keyword_frequency:
                return []
            sorted_keywords = sorted(keyword_frequency.items(), key=lambda x: x[1], reverse=True)
            return [k for k, v in sorted_keywords[:100]]

        # Separate clusters that need LLM title from those that are too small (noise)
        clusters_to_title = []
        for cidx, c in enumerate(clusters):
            if len(c["members"]) < min_cluster_size:
                for mi in c["members"]:
                    noise.append(items[mi]["path"])
            else:
                member_items = [items[mi] for mi in c["members"]]
                paths = [x["path"] for x in member_items]
                texts = [x.get("text") or "" for x in member_items]
                member_ids = [x["id"] for x in member_items]
                rep = (c.get("sample_text") or (texts[0] if texts else "")).strip()
                cluster_hash = _cluster_sig(
                    member_ids=member_ids,
                    model=model,
                    threshold=threshold,
                    min_cluster_size=min_cluster_size,
                    title_model=title_model,
                    lang=output_lang,
                )
                clusters_to_title.append({
                    "cidx": cidx,
                    "paths": paths,
                    "texts": texts,
                    "rep": rep,
                    "cluster_hash": cluster_hash,
                })

        # Process LLM titles concurrently in batches
        LLM_CONCURRENCY = 5  # Number of concurrent LLM requests
        completed_count = [0]  # Use list for closure mutation

        async def process_cluster_title(cluster_info: dict) -> dict:
            cidx = cluster_info["cidx"]
            paths = cluster_info["paths"]
            texts = cluster_info["texts"]
            rep = cluster_info["rep"]
            cluster_hash = cluster_info["cluster_hash"]

            # Check cache first
            cached = None
            if use_title_cache and (not force_title):
                cached = TopicTitleCache.get(conn, cluster_hash)

            if cached and isinstance(cached, dict) and cached.get("title"):
                title = str(cached.get("title"))
                keywords = cached.get("keywords") or []
            else:
                top_keywords = _get_top_keywords()
                llm = await _call_chat_title(
                    base_url=openai_base_url,
                    api_key=openai_api_key,
                    model=title_model,
                    prompt_samples=[rep] + texts[:5],
                    output_lang=output_lang,
                    existing_keywords=top_keywords,
                )
                title = (llm or {}).get("title")
                keywords = (llm or {}).get("keywords", [])
                if not title:
                    raise HTTPException(status_code=502, detail="Chat API returned empty title")
                if use_title_cache and title:
                    try:
                        TopicTitleCache.upsert(conn, cluster_hash, str(title), list(keywords or []), str(title_model))
                        conn.commit()
                    except Exception:
                        pass

            # Update progress
            completed_count[0] += 1
            if progress_cb:
                progress_cb({"stage": "titling", "clusters_total": len(clusters_to_title), "clusters_done": completed_count[0]})

            return {
                "id": f"topic_{cidx}",
                "title": title,
                "keywords": keywords,
                "size": len(paths),
                "paths": paths,
                "sample_prompt": _clean_for_title(rep)[:200],
            }

        # Use semaphore to limit concurrency
        semaphore = asyncio.Semaphore(LLM_CONCURRENCY)

        async def process_with_semaphore(cluster_info: dict) -> dict:
            async with semaphore:
                return await process_cluster_title(cluster_info)

        # Run all title generations concurrently (limited by semaphore)
        if clusters_to_title:
            logger.info(f"[cluster_after] Processing {len(clusters_to_title)} clusters with concurrency={LLM_CONCURRENCY}")
            tasks = [process_with_semaphore(c) for c in clusters_to_title]
            out_clusters = await asyncio.gather(*tasks)

            # Update keyword frequency from results
            for cluster in out_clusters:
                for kw in cluster.get("keywords") or []:
                    keyword_frequency[kw] = keyword_frequency.get(kw, 0) + 1

        out_clusters.sort(key=lambda x: x["size"], reverse=True)
        return {
            "folder": folder,
            "folders": folders,
            "count": len(items),
            "threshold": threshold,
            "min_cluster_size": min_cluster_size,
            "model": model,
            "assign_noise_threshold": assign_noise_threshold,
            "clusters": out_clusters,
            "noise": noise,
        }

    class PromptSearchReq(BaseModel):
        query: str
        folder: Optional[str] = None
        folder_paths: Optional[List[str]] = None
        model: Optional[str] = None
        top_k: Optional[int] = 50
        min_score: Optional[float] = 0.0
        # Ensure embeddings exist/updated before searching
        ensure_embed: Optional[bool] = True
        # Use the same normalization as clustering
        max_chars: Optional[int] = 4000

    @app.post(
        f"{db_api_base}/search_iib_output_by_prompt",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def search_iib_output_by_prompt(req: PromptSearchReq):
        # TopicSearch feature requires perf deps; fail at API layer, not at server start.
        _ensure_perf_deps()
        if not openai_api_key:
            raise HTTPException(status_code=500, detail="OpenAI API Key not configured")
        if not openai_base_url:
            raise HTTPException(status_code=500, detail="OpenAI Base URL not configured")

        q = (req.query or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="query is required")

        folders: List[str] = []
        if req.folder_paths:
            for p in req.folder_paths:
                if isinstance(p, str) and p.strip():
                    folders.append(os.path.normpath(p.strip()))
        if req.folder and isinstance(req.folder, str) and req.folder.strip():
            folders.append(os.path.normpath(req.folder.strip()))
        # 用户不会用默认 iib_output：未指定范围则直接报错
        if not folders:
            raise HTTPException(status_code=400, detail="folder_paths is required (select folders to search)")

        # validate folders
        folders = list(dict.fromkeys(folders))  # de-dup keep order
        for f in folders:
            if not os.path.exists(f) or not os.path.isdir(f):
                raise HTTPException(status_code=400, detail=f"Folder not found: {f}")

        folder = folders[0]
        model = req.model or embedding_model
        top_k = max(1, min(int(req.top_k or 50), 500))
        min_score = float(req.min_score or 0.0)
        min_score = max(-1.0, min(min_score, 1.0))
        max_chars = max(256, min(int(req.max_chars or 4000), 8000))

        if bool(req.ensure_embed):
            for f in folders:
                await build_iib_output_embeddings(
                    BuildIibOutputEmbeddingReq(folder=f, model=model, force=False, batch_size=64, max_chars=max_chars)
                )

        # Build query embedding
        q_text = _extract_prompt_text(q, max_chars=max_chars)
        if _PROMPT_NORMALIZE_ENABLED:
            q_text2 = _clean_prompt_for_semantic(q_text)
            if q_text2:
                q_text = q_text2
        q_text = _truncate_for_embedding_tokens(q_text, _EMBEDDING_MAX_TOKENS_SOFT)
        vecs = await _call_embeddings(inputs=[q_text], model=model, base_url=openai_base_url, api_key=openai_api_key)
        if not vecs or not isinstance(vecs[0], list) or not vecs[0]:
            raise HTTPException(status_code=502, detail="Embedding API returned empty vector")
        qv = array("f", [float(x) for x in vecs[0]])
        qn2 = _l2_norm_sq(qv)
        if qn2 <= 0:
            raise HTTPException(status_code=502, detail="Query embedding has zero norm")
        qinv = 1.0 / math.sqrt(qn2)
        for i in range(len(qv)):
            qv[i] *= qinv

        conn = DataBase.get_conn()
        like_prefixes = [os.path.join(f, "%") for f in folders]
        with closing(conn.cursor()) as cur:
            where = " OR ".join(["image.path LIKE ?"] * len(like_prefixes))
            cur.execute(
                f"""SELECT image.id, image.path, image.exif, image_embedding.vec
                    FROM image
                    INNER JOIN image_embedding ON image_embedding.image_id = image.id
                    WHERE ({where}) AND image_embedding.model = ?""",
                (*like_prefixes, model),
            )
            rows = cur.fetchall()

        # TopK by cosine similarity (brute force; MVP only)
        import heapq

        heap: List[Tuple[float, Dict]] = []
        total = 0
        for image_id, path, exif, vec_blob in rows:
            if not isinstance(path, str) or not os.path.exists(path):
                continue
            if not vec_blob:
                continue
            v = _blob_to_vec_f32(vec_blob)
            n2 = _l2_norm_sq(v)
            if n2 <= 0:
                continue
            inv = 1.0 / math.sqrt(n2)
            for i in range(len(v)):
                v[i] *= inv
            score = _dot(qv, v)
            total += 1
            if score < min_score:
                continue
            item = {
                "id": int(image_id),
                "path": path,
                "score": float(score),
                "sample_prompt": _clean_for_title(_extract_prompt_text(exif, max_chars=max_chars))[:200],
            }
            if len(heap) < top_k:
                heapq.heappush(heap, (score, item))
            else:
                if score > heap[0][0]:
                    heapq.heapreplace(heap, (score, item))

        heap.sort(key=lambda x: x[0], reverse=True)
        results = [x[1] for x in heap]
        return {
            "query": q,
            "folder": folder,
            "folders": folders,
            "model": model,
            "count": total,
            "top_k": top_k,
            "results": results,
        }

    def _cluster_sig(
        *,
        member_ids: List[int],
        model: str,
        threshold: float,
        min_cluster_size: int,
        title_model: str,
        lang: str,
    ) -> str:
        h = hashlib.sha1()
        h.update(f"m={model}|t={threshold:.6f}|min={min_cluster_size}|tm={title_model}|lang={lang}|nv={_PROMPT_NORMALIZE_VERSION}|nm={_PROMPT_NORMALIZE_MODE}".encode("utf-8"))
        for iid in sorted(member_ids):
            h.update(b"|")
            h.update(str(int(iid)).encode("utf-8"))
        return h.hexdigest()

    def _normalize_output_lang(lang: Optional[str]) -> str:
        """
        Map frontend language keys to a human-readable instruction for LLM output language.
        Frontend uses: en / zhHans / zhHant / de
        """
        if not lang:
            return "English"
        l = str(lang).strip()
        ll = l.lower()
        if ll in ["zh", "zhhans", "zh-hans", "zh_cn", "zh-cn", "cn", "zh-hans-cns", "zhs"]:
            return "Chinese (Simplified)"
        if ll in ["zhhant", "zh-hant", "zh_tw", "zh-tw", "zh_hk", "zh-hk", "tw", "hk", "zht"]:
            return "Chinese (Traditional)"
        if ll.startswith("de"):
            return "German"
        if ll.startswith("en"):
            return "English"
        # fallback
        return "English"

    # NOTE:
    # We intentionally do NOT keep the legacy synchronous `/cluster_iib_output` endpoint.
    # The UI should use `/cluster_iib_output_job_start` + `/cluster_iib_output_job_status` only.

    # Return internal functions for use by other modules (e.g., organize_files)
    async def start_cluster_job_for_organize(
        folder_paths: List[str],
        threshold: float = 0.90,
        min_cluster_size: int = 2,
        lang: str = "en",
        recursive: bool = False,
    ) -> str:
        """
        Start a cluster job and return job_id.
        This is a wrapper for organize_files to use.
        """
        _ensure_perf_deps()
        req = ClusterIibOutputReq(
            folder_paths=folder_paths,
            threshold=threshold,
            min_cluster_size=min_cluster_size,
            lang=lang,
            recursive=recursive,
        )
        job_id = uuid.uuid4().hex
        _job_upsert(job_id, {"status": "queued", "stage": "queued", "created_at": _job_now()})
        asyncio.create_task(_run_cluster_job(job_id, req))
        return job_id

    async def get_cluster_job_status_for_organize(job_id: str) -> Dict:
        """
        Get cluster job status.
        This is a wrapper for organize_files to use.
        """
        j = _job_get(job_id)
        if not j:
            return {"status": "not_found", "error": "job not found"}
        return j

    return {
        "start_cluster_job": start_cluster_job_for_organize,
        "get_cluster_job_status": get_cluster_job_status_for_organize,
    }


