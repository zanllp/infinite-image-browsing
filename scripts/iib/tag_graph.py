"""
Hierarchical tag graph generation for topic clusters.
Builds a multi-layer neural-network-style visualization with LLM-driven abstraction.
"""

import hashlib
import json
import os
import re
import time
from typing import Dict, List, Optional
from pydantic import BaseModel
from fastapi import Depends, FastAPI, HTTPException

from scripts.iib.db.datamodel import DataBase, GlobalSetting
from scripts.iib.tool import normalize_output_lang, accumulate_streaming_response
from scripts.iib.logger import logger

# Cache version for tag abstraction - increment to invalidate all caches
TAG_ABSTRACTION_CACHE_VERSION = 2.1
TAG_GRAPH_CACHE_VERSION = 1
_MAX_TAGS_FOR_LLM = int(os.getenv("IIB_TAG_GRAPH_MAX_TAGS_FOR_LLM", "500") or "500")
_TOPK_TAGS_FOR_LLM = int(os.getenv("IIB_TAG_GRAPH_TOPK_TAGS_FOR_LLM", "500") or "500")
_LLM_REQUEST_TIMEOUT_SEC = int(os.getenv("IIB_TAG_GRAPH_LLM_TIMEOUT_SEC", "180") or "180")
_LLM_MAX_ATTEMPTS = int(os.getenv("IIB_TAG_GRAPH_LLM_MAX_ATTEMPTS", "5") or "5")

# JSON Schema for llama.cpp grammar-constrained decoding.
# Enforces valid JSON structure so weak models can't produce broken output.
# llama.cpp converts this to a GBNF grammar at inference time.
TAG_GRAPH_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "layers": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2
                    },
                    "groups": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "minLength": 1
                                },
                                "label": {
                                    "type": "string",
                                    "minLength": 1
                                },
                                "tags": {
                                    "type": "array",
                                    "items": {
                                        "type": "string"
                                    }
                                },
                                "categories": {
                                    "type": "array",
                                    "items": {
                                        "type": "string"
                                    }
                                }
                            },
                            "required": ["id", "label"],
                            "additionalProperties": False
                        }
                    }
                },
                "required": ["level", "groups"],
                "additionalProperties": False
            }
        }
    },
    "required": ["layers"],
    "additionalProperties": False
}


class TagGraphReq(BaseModel):
    folder_paths: List[str]
    lang: Optional[str] = "en"  # Language for LLM output


class LayerNode(BaseModel):
    id: str
    label: str
    size: float  # Weight/importance of this node
    metadata: Optional[dict] = None


class GraphLayer(BaseModel):
    level: int
    name: str  # Layer name: "Clusters", "Tags", "Abstract-1", "Abstract-2"
    nodes: List[LayerNode]


class GraphLink(BaseModel):
    source: str
    target: str
    weight: float


class TagGraphResp(BaseModel):
    layers: List[GraphLayer]
    links: List[GraphLink]
    stats: dict


def mount_tag_graph_routes(
    app: FastAPI,
    db_api_base: str,
    verify_secret,
    embedding_model: str,
    ai_model: str,
    openai_base_url: str,
    openai_api_key: str,
):
    """Mount hierarchical tag graph endpoints"""

    async def _call_llm_for_abstraction(
        tags: List[str],
        lang: str,
        model: str,
        base_url: str,
        api_key: str
    ) -> dict:
        """
        Call LLM to create hierarchical abstraction of tags.
        Returns a dict with layers and groupings.
        """
        import asyncio
        import requests
        import re

        def _normalize_base_url(url: str) -> str:
            url = url.strip()
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            return url.rstrip("/")

        def _call_sync():
            if not api_key:
                raise HTTPException(500, "OpenAI API Key not configured")

            url = f"{_normalize_base_url(base_url)}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            # Normalize language for consistent LLM output
            normalized_lang = normalize_output_lang(lang)
            print(f"tags length: {len(tags)}")
            # Determine Level 2 requirement based on tag count
            level2_requirement = ""
            if len(tags) <= 8:
                level2_requirement = "Level 2 is optional; only create if categories naturally form clear super-groups."
            elif len(tags) <= 64:
                level2_requirement = "Level 2 is recommended; create 2-5 super-categories to group Level 1 categories when meaningful."
            else:
                level2_requirement = "Level 2 is strongly recommended; create 2-5 super-categories to group the many Level 1 categories for better organization."

            sys_prompt = f"""You are a tag categorization assistant. Organize tags into hierarchical categories.

GUIDELINES:
1. Create 5-15 Level 1 categories (broad groupings)
2. {level2_requirement}
3. Every tag must belong to exactly ONE Level 1 category
4. Use {normalized_lang} for all category labels
5. Category IDs must be simple lowercase (e.g., "style", "char", "scene1")
6. Level 1 groups use "tags" field (list of tag strings)
7. Level 2 groups use "categories" field (list of Level 1 group IDs)
8. Output must be valid JSON matching the provided schema"""

            user_prompt = f"Tags to categorize:\n{', '.join(tags)}"

            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.0,
                "max_tokens": 32768,
                "stream": True,
                "response_format": {
                    "type": "json_object",
                    "schema": TAG_GRAPH_JSON_SCHEMA,
                },
            }

            request_id = f"tg_{int(time.time() * 1000)}_{os.getpid()}"
            logger.info(
                "[tag_graph][%s] LLM request: model=%s tags_count=%s max_attempts=%s timeout=%s",
                request_id, model, len(tags), _LLM_MAX_ATTEMPTS, _LLM_REQUEST_TIMEOUT_SEC
            )
            logger.debug("[tag_graph][%s] LLM request payload: %s", request_id, json.dumps(payload, ensure_ascii=False, indent=2))

            # Retry a few times then fallback quickly (to avoid frontend timeout on large datasets).
            # Use streaming requests to avoid blocking too long on a single non-stream response.
            last_error = ""
            full_response_content = ""
            for attempt in range(1, _LLM_MAX_ATTEMPTS + 1):
                try:
                    resp = requests.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=_LLM_REQUEST_TIMEOUT_SEC,
                        stream=True,
                    )
                    # Check status early
                    if resp.status_code != 200:
                        body = resp.text or ""
                        if resp.status_code == 429 or resp.status_code >= 500:
                            last_error = f"api_error_retriable: status={resp.status_code} body={body}"
                            logger.warning(
                                "[tag_graph][%s] llm_http_error attempt=%s status=%s body=%s",
                                request_id, attempt, resp.status_code, body
                            )
                            continue
                        logger.error(
                            "[tag_graph][%s] llm_http_client_error attempt=%s status=%s body=%s",
                            request_id, attempt, resp.status_code, body
                        )
                        raise Exception(f"API client error: {resp.status_code} {body}")

                    # Accumulate streamed content chunks
                    content = accumulate_streaming_response(resp)
                    full_response_content = content
                    logger.info(
                        "[tag_graph][%s] llm_response attempt=%s content_length=%s",
                        request_id, attempt, len(content)
                    )
                    logger.debug("[tag_graph][%s] llm_response content: %s", request_id, content)

                    # Strategy 1: Direct parse (if response is pure JSON)
                    try:
                        result = json.loads(content)
                        if isinstance(result, dict) and 'layers' in result:
                            logger.info("[tag_graph][%s] llm_success attempt=%s layers_count=%s", request_id, attempt, len(result.get('layers', [])))
                            return result
                    except Exception as parse_err:
                        logger.debug("[tag_graph][%s] direct_parse_failed attempt=%s err=%s", request_id, attempt, str(parse_err))

                    # Strategy 2: Extract JSON from markdown code blocks
                    json_str = None
                    code_block = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content)
                    if code_block:
                        json_str = code_block.group(1)
                    else:
                        m = re.search(r"\{[\s\S]*\}", content)
                        if m:
                            json_str = m.group(0)

                    if not json_str:
                        last_error = f"no_json_found: {content}"
                        logger.warning("[tag_graph][%s] llm_no_json attempt=%s content=%s", request_id, attempt, content)
                        continue

                    # Clean up common JSON issues
                    json_str = json_str.strip()
                    json_str = re.sub(r',(\s*[}\]])', r'\1', json_str)

                    try:
                        result = json.loads(json_str)
                    except json.JSONDecodeError as e:
                        last_error = f"json_parse_error: {e} json_str={json_str}"
                        logger.warning(
                            "[tag_graph][%s] llm_json_parse_error attempt=%s err=%s json_str=%s",
                            request_id, attempt, str(e), json_str
                        )
                        continue

                    if not isinstance(result, dict) or 'layers' not in result:
                        last_error = f"invalid_structure: result={str(result)}"
                        logger.warning(
                            "[tag_graph][%s] llm_invalid_structure attempt=%s result=%s",
                            request_id, attempt, str(result)
                        )
                        continue

                    logger.info("[tag_graph][%s] llm_success attempt=%s layers_count=%s", request_id, attempt, len(result.get('layers', [])))
                    return result
                except requests.RequestException as e:
                    last_error = f"network_error: {type(e).__name__}: {e}"
                    logger.warning(
                        "[tag_graph][%s] llm_request_error attempt=%s err=%s",
                        request_id, attempt, str(e)
                    )
                    continue

            # No fallback: expose error to frontend, but log enough info for debugging.
            logger.error(
                "[tag_graph][%s] llm_failed all_attempts=%s timeout_sec=%s last_error=%s full_response=%s",
                request_id, _LLM_MAX_ATTEMPTS, _LLM_REQUEST_TIMEOUT_SEC, last_error, full_response_content
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "type": "tag_graph_llm_failed",
                    "attempts": _LLM_MAX_ATTEMPTS,
                    "timeout_sec": _LLM_REQUEST_TIMEOUT_SEC,
                    "last_error": last_error,
                },
            )

        return await asyncio.to_thread(_call_sync)

    @app.post(
        f"{db_api_base}/cluster_tag_graph",
        dependencies=[Depends(verify_secret)],
    )
    async def cluster_tag_graph(req: TagGraphReq):
        """
        Build hierarchical tag graph from clustering results.
        Returns multi-layer structure similar to neural network visualization.

        Layer structure (bottom to top):
        - Layer 0: Cluster nodes
        - Layer 1: Tag nodes (deduplicated cluster keywords)
        - Layer 2+: Abstract groupings (LLM-generated, max 2 layers)
        """
        t0 = time.time()
        # Validate
        if not req.folder_paths:
            raise HTTPException(400, "folder_paths is required")

        folders = sorted(req.folder_paths)

        # Get the latest cluster result for these folders
        conn = DataBase.get_conn()

        from contextlib import closing
        with closing(conn.cursor()) as cur:
            # Avoid full table scan on large DBs; we only need recent caches for matching.
            cur.execute(
                """SELECT cache_key, folders, result FROM topic_cluster_cache
                   ORDER BY updated_at DESC
                   LIMIT 200"""
            )
            rows = cur.fetchall()

        # Find a cache that matches the folders (order-independent)
        folders_set = set(folders)
        row = None
        topic_cluster_cache_key: Optional[str] = None

        for cache_row in rows:
            try:
                cached_folders = json.loads(cache_row[1]) if isinstance(cache_row[1], str) else cache_row[1]
                if isinstance(cached_folders, list) and set(cached_folders) == folders_set:
                    topic_cluster_cache_key = str(cache_row[0] or "")
                    row = (topic_cluster_cache_key, cache_row[2])
                    break
            except Exception:
                continue

        if not row:
            raise HTTPException(
                400,
                f"No clustering result found for these {len(folders)} folders. Please run clustering first."
            )

        cached_result = json.loads(row[1]) if isinstance(row[1], str) else row[1]

        if not cached_result or not isinstance(cached_result, dict):
            raise HTTPException(400, "Invalid clustering result format.")

        result = cached_result
        clusters = result.get("clusters", [])

        if not clusters:
            raise HTTPException(400, "No clusters found in result")

        logger.info(
            "[tag_graph] start folders=%s clusters=%s lang=%s",
            len(folders),
            len(clusters) if isinstance(clusters, list) else "n/a",
            str(req.lang or ""),
        )



        # Graph cache (best-effort): cache by topic_cluster_cache_key + lang + version.
        graph_cache_key = None
        if topic_cluster_cache_key:
            try:
                graph_cache_key_hash = hashlib.md5(
                    json.dumps(
                        {
                            "v": TAG_GRAPH_CACHE_VERSION,
                            "topic_cluster_cache_key": topic_cluster_cache_key,
                            "lang": str(req.lang or ""),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                graph_cache_key = f"tag_graph_v{TAG_GRAPH_CACHE_VERSION}_{graph_cache_key_hash}"
                cached_graph = GlobalSetting.get_setting(conn, graph_cache_key)
                if cached_graph:
                    cached_graph_obj = json.loads(cached_graph) if isinstance(cached_graph, str) else cached_graph
                    if isinstance(cached_graph_obj, dict) and "layers" in cached_graph_obj and "links" in cached_graph_obj:
                        cached_graph_obj.setdefault("stats", {})
                        if isinstance(cached_graph_obj["stats"], dict):
                            cached_graph_obj["stats"].setdefault("topic_cluster_cache_key", topic_cluster_cache_key)
                        logger.info(
                            "[tag_graph] cache_hit topic_cluster_cache_key=%s cost_ms=%s",
                            topic_cluster_cache_key,
                            int((time.time() - t0) * 1000),
                        )
                        return cached_graph_obj
            except Exception:
                pass

        # === Layer 0: Cluster Nodes ===
        top_clusters = sorted(clusters, key=lambda c: c.get("size", 0), reverse=True)

        cluster_nodes = []
        cluster_to_tags_links = []

        for cluster in top_clusters:
            cluster_id = cluster.get("id", "")
            cluster_title = cluster.get("title", "Untitled")
            cluster_size = cluster.get("size", 0)
            keywords = cluster.get("keywords", []) or []
            keywords = list(dict.fromkeys(keywords))  # Deduplicate
            node_id = f"cluster_{cluster_id}"
            cluster_nodes.append(LayerNode(
                id=node_id,
                label=cluster_title,
                size=float(cluster_size),
                metadata={
                    "type": "cluster",
                    "image_count": cluster_size,
                    # Do NOT include full paths list here (can be huge); fetch on demand.
                    "cluster_id": cluster_id,
                }
            ))

            # Store links from clusters to their tags (will be created later)
            for keyword in keywords:
                if keyword:
                    cluster_to_tags_links.append({
                        "cluster_id": node_id,
                        "tag": str(keyword).strip(),
                        "weight": float(cluster_size)
                    })

        # === Layer 1: Tag Nodes (deduplicated) ===
        tag_stats: Dict[str, Dict] = {}

        for cluster in clusters:
            keywords = cluster.get("keywords", []) or []
            cluster_size = cluster.get("size", 0)
            cluster_id = cluster.get("id", "")

            for keyword in keywords:
                keyword = str(keyword).strip()
                if not keyword:
                    continue

                if keyword not in tag_stats:
                    tag_stats[keyword] = {
                        "cluster_ids": [],
                        "total_images": 0,
                    }

                tag_stats[keyword]["cluster_ids"].append(cluster_id)
                tag_stats[keyword]["total_images"] += cluster_size

        # Filter and sort tags
        sorted_tags = sorted(
            tag_stats.items(),
            key=lambda x: x[1]["total_images"],
            reverse=True
        )

        tag_nodes = []
        selected_tags = set()

        for tag, stats in sorted_tags:
            tag_id = f"tag_{tag}"
            selected_tags.add(tag)
            tag_nodes.append(LayerNode(
                id=tag_id,
                label=tag,
                size=float(stats["total_images"]),
                metadata={
                    "type": "tag",
                    "cluster_count": len(stats["cluster_ids"]),
                    "image_count": stats["total_images"]
                }
            ))

        # Filter cluster->tag links to only include selected tags
        layer0_to_1_links = []
        for link in cluster_to_tags_links:
            if link["tag"] in selected_tags:
                layer0_to_1_links.append(GraphLink(
                    source=link["cluster_id"],
                    target=f"tag_{link['tag']}",
                    weight=link["weight"]
                ))

        # === Layer 2+: LLM-driven abstraction ===
        abstract_layers = []
        layer1_to_2_links = []

        # LLM abstraction: for large datasets, do TopK by total_images (frequency/weight) instead of skipping.
        # This keeps response useful while controlling LLM latency and prompt size.
        llm_tags = [t for (t, _stats) in sorted_tags][: max(0, int(_TOPK_TAGS_FOR_LLM))]
        llm_tags_set = set(llm_tags)
        # IMPORTANT: Do not gate LLM by total tag count; only use TopK tags for LLM input.
        # Otherwise, large datasets would "skip" abstraction even though we already limited llm_tags.
        should_use_llm = (
            len(llm_tags) > 3
            and len(llm_tags) <= _MAX_TAGS_FOR_LLM
            and bool(openai_api_key)
            and bool(openai_base_url)
        )
        if not should_use_llm:
            logger.info(
                "[tag_graph] llm_disabled reasons={topk_tags:%s,max:%s,has_key:%s,has_base:%s}",
                len(llm_tags),
                _MAX_TAGS_FOR_LLM,
                bool(openai_api_key),
                bool(openai_base_url),
            )
        logger.info(
            "[tag_graph] tags_total=%s tags_for_llm=%s llm_enabled=%s",
            len(selected_tags),
            len(llm_tags),
            bool(should_use_llm),
        )

        if should_use_llm:
            # Use language from request
            lang = req.lang or "en"

            # Generate cache key for this set of tags (with version)
            import hashlib
            tags_sorted = sorted(llm_tags_set)
            cache_input = f"v{TAG_ABSTRACTION_CACHE_VERSION}|{ai_model}|{lang}|topk={len(tags_sorted)}|{','.join(tags_sorted)}"
            cache_key_hash = hashlib.md5(cache_input.encode()).hexdigest()
            cache_key = f"tag_abstraction_v{TAG_ABSTRACTION_CACHE_VERSION}_{cache_key_hash}"

            # Try to get from cache
            abstraction = None
            try:
                cached_data = GlobalSetting.get_setting(conn, cache_key)
                if cached_data and isinstance(cached_data, dict):
                    abstraction = cached_data
            except:
                pass

            # Call LLM if not cached
            if not abstraction:
                t_llm = time.time()
                try:
                    abstraction = await _call_llm_for_abstraction(
                        list(llm_tags_set),
                        lang,
                        ai_model,
                        openai_base_url,
                        openai_api_key
                    )
                except Exception as e:
                    logger.exception(
                        "[tag_graph] llm_call_failed cost_ms=%s err=%s",
                        int((time.time() - t_llm) * 1000),
                        f"{type(e).__name__}: {e}",
                    )
                    raise
                logger.info("[tag_graph] llm_done cost_ms=%s cached=%s", int((time.time() - t_llm) * 1000), False)

                # Save to cache if successful
                if abstraction and isinstance(abstraction, dict) and abstraction.get("layers"):
                    try:
                        GlobalSetting.save_setting(conn, cache_key, json.dumps(abstraction, ensure_ascii=False))
                    except Exception:
                        pass
            else:
                logger.info("[tag_graph] llm_done cost_ms=%s cached=%s", 0, True)

            # Build abstract layers from LLM response
            llm_layers = abstraction.get("layers", [])

            for layer_info in llm_layers:
                level = layer_info.get("level", 1)
                groups = layer_info.get("groups", [])

                if not groups:
                    continue

                abstract_nodes = []

                # Process each group in this layer
                for group in groups:
                    group_id = f"abstract_l{level}_{group.get('id', 'unknown')}"
                    group_label = group.get("label", "Unnamed")

                    # Calculate size based on contained tags/categories
                    if "tags" in group:
                        # Level 1: references tags directly
                        contained_tags = group.get("tags", [])
                        total_size = sum(
                            tag_stats.get(tag, {}).get("total_images", 0)
                            for tag in contained_tags
                            if tag in llm_tags_set
                        )

                        abstract_nodes.append(LayerNode(
                            id=group_id,
                            label=group_label,
                            size=float(total_size),
                            metadata={"type": "abstract", "level": level}
                        ))

                        # Create links from tags to this abstract node
                        for tag in contained_tags:
                            if tag in llm_tags_set:
                                tag_id = f"tag_{tag}"
                                layer1_to_2_links.append(GraphLink(
                                    source=tag_id,
                                    target=group_id,
                                    weight=float(tag_stats.get(tag, {}).get("total_images", 1))
                                ))

                    elif "categories" in group and level == 2:
                        # Level 2: references Level 1 categories
                        # Calculate size from contained categories
                        contained_cats = group.get("categories", [])
                        total_size = 0.0

                        # Find size from level 1 nodes
                        level1_nodes = abstract_layers[0].nodes if abstract_layers else []
                        for cat_id in contained_cats:
                            full_cat_id = f"abstract_l1_{cat_id}"
                            for node in level1_nodes:
                                if node.id == full_cat_id:
                                    total_size += node.size
                                    break

                        abstract_nodes.append(LayerNode(
                            id=group_id,
                            label=group_label,
                            size=float(total_size),
                            metadata={"type": "abstract", "level": level}
                        ))

                        # Create links from Level 1 categories to this Level 2 node
                        for cat_id in contained_cats:
                            full_cat_id = f"abstract_l1_{cat_id}"
                            # Verify the category exists
                            if any(n.id == full_cat_id for n in level1_nodes):
                                layer1_to_2_links.append(GraphLink(
                                    source=full_cat_id,
                                    target=group_id,
                                    weight=total_size  # Use aggregated weight
                                ))

                if abstract_nodes:
                    abstract_layers.append(GraphLayer(
                        level=level + 1,  # +1 because Layer 0=clusters, Layer 1=tags
                        name=f"Abstract-{level}",
                        nodes=abstract_nodes
                    ))

        # === Build final response ===
        layers = [
            GraphLayer(level=0, name="Clusters", nodes=cluster_nodes),
            GraphLayer(level=1, name="Tags", nodes=tag_nodes),
        ] + abstract_layers

        all_links = [link.dict() for link in (layer0_to_1_links + layer1_to_2_links)]

        stats = {
            "total_clusters": len(clusters),
            "selected_clusters": len(cluster_nodes),
            "total_tags": len(tag_stats),
            "selected_tags": len(tag_nodes),
            "abstraction_layers": len(abstract_layers),
            "total_links": len(all_links),
        }
        if topic_cluster_cache_key:
            stats["topic_cluster_cache_key"] = topic_cluster_cache_key

        resp_obj = TagGraphResp(
            layers=layers,
            links=all_links,
            stats=stats,
        ).dict()

        # Save graph cache (best-effort)
        if graph_cache_key:
            try:
                GlobalSetting.save_setting(conn, graph_cache_key, json.dumps(resp_obj, ensure_ascii=False))
                conn.commit()
            except Exception:
                pass

        logger.info(
            "[tag_graph] done nodes=%s links=%s abstraction_layers=%s cost_ms=%s",
            sum(len(l.get("nodes") or []) for l in (resp_obj.get("layers") or []) if isinstance(l, dict)),
            len(resp_obj.get("links") or []),
            int((resp_obj.get("stats") or {}).get("abstraction_layers") or 0),
            int((time.time() - t0) * 1000),
        )
        return resp_obj

    class ClusterPathsReq(BaseModel):
        topic_cluster_cache_key: str
        cluster_id: str

    @app.post(
        f"{db_api_base}/cluster_tag_graph_cluster_paths",
        dependencies=[Depends(verify_secret)],
    )
    async def cluster_tag_graph_cluster_paths(req: ClusterPathsReq):
        """
        Fetch full paths for a specific cluster from cached clustering result.
        This avoids returning huge "paths" arrays inside cluster_tag_graph response.
        """
        ck = str(req.topic_cluster_cache_key or "").strip()
        cid = str(req.cluster_id or "").strip()
        if not ck or not cid:
            raise HTTPException(400, "topic_cluster_cache_key and cluster_id are required")
        conn = DataBase.get_conn()
        from contextlib import closing
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT result FROM topic_cluster_cache WHERE cache_key = ?", (ck,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, "topic_cluster_cache not found")
        try:
            result_obj = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        except Exception:
            result_obj = None
        if not isinstance(result_obj, dict):
            raise HTTPException(400, "invalid cached result")
        clusters = result_obj.get("clusters", [])
        if not isinstance(clusters, list):
            clusters = []
        for c in clusters:
            if not isinstance(c, dict):
                continue
            if str(c.get("id", "")) == cid:
                paths = c.get("paths", [])
                if not isinstance(paths, list):
                    paths = []
                return {"paths": [str(p) for p in paths if p]}
        raise HTTPException(404, "cluster not found")
