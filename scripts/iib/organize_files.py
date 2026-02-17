"""
Smart file organization based on topic clustering.
Organizes scattered image files into folders based on semantic clustering.
"""

import os
import uuid
import asyncio
import threading
import time
import shutil
from typing import Dict, List, Optional, Callable, Any
from collections import defaultdict
from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException

from scripts.iib.logger import logger
from scripts.iib.db.datamodel import DataBase, Image as DbImg
from scripts.iib.tool import get_img_geninfo_txt_path, is_media_file


# ========== Job Storage ==========

_ORGANIZE_JOBS: Dict[str, Dict] = {}
_ORGANIZE_JOBS_LOCK = threading.Lock()
_ORGANIZE_JOBS_MAX = 16


def _job_now() -> float:
    return time.time()


def _organize_job_get(job_id: str) -> Optional[Dict]:
    with _ORGANIZE_JOBS_LOCK:
        j = _ORGANIZE_JOBS.get(job_id)
        return dict(j) if isinstance(j, dict) else None


def _organize_job_upsert(job_id: str, patch: Dict) -> None:
    with _ORGANIZE_JOBS_LOCK:
        cur = _ORGANIZE_JOBS.get(job_id)
        if not isinstance(cur, dict):
            cur = {"job_id": job_id}
        cur.update(patch or {})
        cur["updated_at"] = _job_now()
        _ORGANIZE_JOBS[job_id] = cur
    _organize_job_trim()


def _organize_job_trim() -> None:
    with _ORGANIZE_JOBS_LOCK:
        if len(_ORGANIZE_JOBS) <= _ORGANIZE_JOBS_MAX:
            return
        items = sorted(
            _ORGANIZE_JOBS.items(),
            key=lambda kv: kv[1].get("updated_at", 0),
            reverse=True
        )
        keep = dict(items[:_ORGANIZE_JOBS_MAX])
        _ORGANIZE_JOBS.clear()
        _ORGANIZE_JOBS.update(keep)


# ========== Request/Response Models ==========

class OrganizeFilesReq(BaseModel):
    folder_paths: List[str]
    dest_folder: Optional[str] = None
    threshold: float = 0.90
    min_cluster_size: int = 2
    lang: str = "en"
    recursive: bool = False  # If True, treat all files in subfolders as files to organize

    # Folder naming options
    folder_naming: str = "title"  # "title" | "keywords" | "id"
    max_folder_name_length: int = 50

    # Operation options
    action: str = "move"  # "move" | "copy"
    handle_noise: str = "unsorted"  # "skip" | "unsorted" | "leave"
    noise_folder_name: str = "未分类"


class OrganizeFolderEdit(BaseModel):
    cluster_id: str
    new_folder_name: str


class OrganizeFilesConfirmReq(BaseModel):
    job_id: str
    folder_edits: Optional[List[OrganizeFolderEdit]] = None
    skip_cluster_ids: Optional[List[str]] = None


# ========== Utility Functions ==========

def _sanitize_folder_name(name: str, max_len: int = 50) -> str:
    """Sanitize folder name, remove illegal characters but keep spaces for readability."""
    if not name:
        return "cluster"

    import re

    # Remove illegal characters for Windows/Unix
    illegal = r'<>:"/\|?*'
    for c in illegal:
        name = name.replace(c, ' ')

    # Remove leading/trailing spaces and dots
    name = name.strip(' .')

    # Replace multiple consecutive spaces with single space (keep spaces, don't convert to underscore)
    name = re.sub(r'\s+', ' ', name)

    # Truncate
    if len(name) > max_len:
        name = name[:max_len].rstrip(' ._')

    return name or "cluster"


def _generate_folder_name(
    cluster: Dict,
    naming: str,
    existing_disk_names: set,
    max_len: int
) -> str:
    """
    Generate folder name for a cluster (without uniqueness check).

    Args:
        cluster: cluster info dict
        naming: naming strategy ("title" | "keywords" | "id")
        existing_disk_names: set of folder names already existing on disk (case-insensitive, for merging)
        max_len: max folder name length

    Returns:
        folder name string

    Note: This function may return the same name for different clusters.
          The caller (_build_file_mappings) is responsible for merging clusters with the same name.
    """
    if naming == "title":
        base = cluster.get("title", "") or f"cluster_{cluster.get('id', '')}"
    elif naming == "keywords":
        kws = cluster.get("keywords", [])[:3]
        base = "_".join(kws) if kws else f"cluster_{cluster.get('id', '')}"
    else:  # id
        base = f"cluster_{cluster.get('id', '')}"

    base = _sanitize_folder_name(base, max_len)

    # Check if this name matches an existing folder on disk (case-insensitive)
    # If so, use the disk name (preserving case) for merging
    existing_disk_lower_map = {n.lower(): n for n in existing_disk_names}
    if base.lower() in existing_disk_lower_map:
        return existing_disk_lower_map[base.lower()]

    return base


def _build_file_mappings(
    cluster_result: Dict,
    dest_folder: str,
    folder_naming: str,
    max_len: int,
    handle_noise: str,
    noise_folder_name: str
) -> Dict:
    """
    Build file mappings from cluster result.
    Returns: {clusters, noise, all_mappings}

    Note: Clusters with the same generated folder name will be merged into one.
    """
    # Get existing folder names on disk (for merge detection)
    existing_disk_names = set()
    if os.path.isdir(dest_folder):
        for item in os.listdir(dest_folder):
            item_path = os.path.join(dest_folder, item)
            if os.path.isdir(item_path):
                existing_disk_names.add(item)

    all_mappings = []

    # First pass: generate folder names and group clusters by folder name
    # This merges clusters that would end up with the same folder name
    folder_to_clusters: Dict[str, Dict] = {}  # folder_name -> merged cluster info

    for cluster in cluster_result.get("clusters", []):
        folder_name = _generate_folder_name(cluster, folder_naming, existing_disk_names, max_len)

        if folder_name in folder_to_clusters:
            # Merge into existing
            existing = folder_to_clusters[folder_name]
            existing["paths"].extend(cluster.get("paths", []))
            existing["cluster_ids"].append(cluster["id"])
            # Merge keywords (dedupe)
            for kw in cluster.get("keywords", []):
                if kw not in existing["keywords"]:
                    existing["keywords"].append(kw)
        else:
            # New folder
            folder_to_clusters[folder_name] = {
                "folder_name": folder_name,
                "title": cluster.get("title", ""),
                "keywords": list(cluster.get("keywords", [])),
                "paths": list(cluster.get("paths", [])),
                "cluster_ids": [cluster["id"]],
            }

    # Second pass: build file mappings from merged clusters
    clusters_preview = []
    for folder_name, merged in folder_to_clusters.items():
        file_mappings = []
        # Use first cluster_id as the main id, or combine them
        cluster_id = merged["cluster_ids"][0] if len(merged["cluster_ids"]) == 1 else "_".join(merged["cluster_ids"])

        for path in merged["paths"]:
            filename = os.path.basename(path)
            dest_path = os.path.join(dest_folder, folder_name, filename)
            mapping = {
                "src_path": path,
                "dest_folder_name": folder_name,
                "dest_path": dest_path,
                "cluster_id": cluster_id,
                "is_noise": False
            }
            file_mappings.append(mapping)
            all_mappings.append(mapping)

        clusters_preview.append({
            "cluster_id": cluster_id,
            "suggested_folder_name": folder_name,
            "title": merged["title"],
            "keywords": merged["keywords"],
            "size": len(file_mappings),
            "file_mappings": file_mappings
        })

    # Process noise files
    noise_mappings = []
    noise_paths = cluster_result.get("noise", [])
    noise_folder = ""

    if handle_noise == "unsorted" and noise_paths:
        # Put into "unsorted" folder
        noise_folder_base = _sanitize_folder_name(noise_folder_name, max_len)

        # Check if noise folder already exists on disk (merge into it)
        existing_disk_lower_map = {n.lower(): n for n in existing_disk_names}
        if noise_folder_base.lower() in existing_disk_lower_map:
            # Use existing folder name (case-preserved)
            noise_folder = existing_disk_lower_map[noise_folder_base.lower()]
        else:
            # Check if it conflicts with a cluster folder name
            used_folder_names = set(folder_to_clusters.keys())
            used_lower_map = {n.lower(): n for n in used_folder_names}
            if noise_folder_base.lower() in used_lower_map:
                # Merge into the cluster folder (or add suffix to avoid confusion)
                # For noise, we add suffix to avoid mixing with clustered files
                noise_folder = noise_folder_base
                counter = 1
                while noise_folder.lower() in used_lower_map or noise_folder.lower() in existing_disk_lower_map:
                    noise_folder = f"{noise_folder_base}_{counter}"
                    counter += 1
            else:
                noise_folder = noise_folder_base

        for path in noise_paths:
            filename = os.path.basename(path)
            dest_path = os.path.join(dest_folder, noise_folder, filename)
            mapping = {
                "src_path": path,
                "dest_folder_name": noise_folder,
                "dest_path": dest_path,
                "cluster_id": "__noise__",
                "is_noise": True
            }
            noise_mappings.append(mapping)
            all_mappings.append(mapping)

    elif handle_noise == "leave":
        # Keep in original location, don't move
        for path in noise_paths:
            mapping = {
                "src_path": path,
                "dest_folder_name": "",
                "dest_path": path,  # Keep original
                "cluster_id": "__noise__",
                "is_noise": True
            }
            noise_mappings.append(mapping)
        # Don't add to all_mappings since no move needed

    # "skip": completely ignore noise files

    noise_cluster = {
        "cluster_id": "__noise__",
        "suggested_folder_name": noise_folder,
        "title": "未分类" if noise_folder_name == "未分类" else "Unsorted",
        "keywords": [],
        "size": len(noise_paths),
        "file_mappings": noise_mappings
    }

    return {
        "clusters": clusters_preview,
        "noise": noise_cluster,
        "all_mappings": all_mappings
    }


# ========== Route Mounting ==========

def mount_organize_routes(
    app: FastAPI,
    db_api_base: str,
    verify_secret,
    write_permission_required,
    # Cluster job functions from topic_cluster
    start_cluster_job_func: Callable,
    get_cluster_job_status_func: Callable,
):
    """Mount file organization routes."""

    async def _run_organize_job(job_id: str, req: OrganizeFilesReq):
        """
        Background task for file organization.
        Uses topic_cluster job API internally.
        """
        try:
            logger.info(f"[organize_files][{job_id}] Starting organize job")

            _organize_job_upsert(job_id, {
                "status": "running",
                "progress": {
                    "stage": "clustering",
                    "embedded_done": 0,
                    "to_embed": 0,
                    "clusters_done": 0,
                    "clusters_total": 0,
                    "moved_done": 0,
                    "moved_total": 0,
                    "current_file": "",
                    "created_folders": [],
                    "errors": []
                }
            })

            # 1. Start cluster job using topic_cluster API
            logger.info(f"[organize_files][{job_id}] Starting cluster job (recursive={req.recursive})")
            cluster_job_id = await start_cluster_job_func(
                folder_paths=req.folder_paths,
                threshold=req.threshold,
                min_cluster_size=req.min_cluster_size,
                lang=req.lang,
                recursive=req.recursive,
            )

            # 2. Poll cluster job status until done
            logger.info(f"[organize_files][{job_id}] Polling cluster job {cluster_job_id}")
            max_wait = 3600  # 1 hour max
            start_time = time.time()
            poll_interval = 0.5

            while True:
                if time.time() - start_time > max_wait:
                    raise Exception("Cluster job timeout")

                cluster_status = await get_cluster_job_status_func(cluster_job_id)
                status = cluster_status.get("status", "")
                stage = cluster_status.get("stage", "")
                progress = cluster_status.get("progress", {})

                # Update organize job progress based on cluster job
                _organize_job_upsert(job_id, {
                    "progress": {
                        "stage": stage,
                        "embedded_done": progress.get("embedded_done", 0),
                        "to_embed": progress.get("to_embed", 0),
                        "clusters_done": progress.get("clusters_done", 0),
                        "clusters_total": progress.get("clusters_total", 0),
                        "moved_done": 0,
                        "moved_total": 0,
                        "current_file": "",
                        "created_folders": [],
                        "errors": []
                    }
                })

                if status == "done":
                    cluster_result = cluster_status.get("result", {})
                    break
                elif status == "error":
                    raise Exception(f"Cluster job failed: {cluster_status.get('error', 'Unknown error')}")

                await asyncio.sleep(poll_interval)
                # Increase poll interval gradually
                poll_interval = min(poll_interval * 1.2, 2.0)

            # 3. Build file mappings for preview
            logger.info(f"[organize_files][{job_id}] Building file mappings")
            dest_folder = req.dest_folder or req.folder_paths[0]
            dest_folder = os.path.abspath(dest_folder)

            preview_data = _build_file_mappings(
                cluster_result,
                dest_folder,
                req.folder_naming,
                req.max_folder_name_length,
                req.handle_noise,
                req.noise_folder_name
            )

            preview = {
                "job_id": job_id,
                "dest_folder": dest_folder,
                "total_files": len(preview_data["all_mappings"]),
                "clusters": preview_data["clusters"],
                "noise": preview_data["noise"],
                "all_mappings": preview_data["all_mappings"]
            }

            _organize_job_upsert(job_id, {
                "status": "preview_ready",
                "preview": preview,
                "progress": {
                    "stage": "preview_ready",
                    "embedded_done": 0,
                    "to_embed": 0,
                    "clusters_done": len(preview_data["clusters"]),
                    "clusters_total": len(preview_data["clusters"]),
                    "moved_done": 0,
                    "moved_total": 0,
                    "current_file": "",
                    "created_folders": [],
                    "errors": []
                }
            })

            logger.info(f"[organize_files][{job_id}] Preview ready: {len(preview_data['all_mappings'])} files")

        except Exception as e:
            import traceback
            logger.error(f"[organize_files][{job_id}] Error: {e}")
            logger.error(traceback.format_exc())
            _organize_job_upsert(job_id, {
                "status": "error",
                "error": str(e),
            })

    async def _execute_organize(job_id: str, req: OrganizeFilesConfirmReq):
        """
        Execute file organization (move/copy files).
        """
        try:
            job = _organize_job_get(job_id)
            if not job:
                return

            original_req = job.get("req", {})
            action = original_req.get("action", "move")
            preview = job.get("preview", {})
            all_mappings = list(preview.get("all_mappings", []))
            dest_folder = preview.get("dest_folder", "")

            logger.info(f"[organize_files][{job_id}] Executing organize: action={action}, mappings={len(all_mappings)}")

            # Apply user edits
            folder_edits = {}
            for edit in (req.folder_edits or []):
                folder_edits[edit.cluster_id] = edit.new_folder_name

            skip_ids = set(req.skip_cluster_ids or [])

            # Filter and update mappings
            final_mappings = []

            for m in all_mappings:
                # Skip if cluster is skipped
                if m["cluster_id"] in skip_ids:
                    continue

                # Apply folder name edits
                if m["cluster_id"] in folder_edits:
                    new_name = _sanitize_folder_name(folder_edits[m["cluster_id"]])
                    m["dest_folder_name"] = new_name
                    m["dest_path"] = os.path.join(dest_folder, new_name, os.path.basename(m["src_path"]))

                # Skip files that don't need moving (same src and dest)
                if m["dest_path"] != m["src_path"]:
                    final_mappings.append(m)

            total = len(final_mappings)

            _organize_job_upsert(job_id, {
                "status": "moving",
                "progress": {
                    "stage": "moving",
                    "moved_total": total,
                    "moved_done": 0,
                    "current_file": "",
                    "created_folders": [],
                    "errors": []
                }
            })

            # Group by destination folder
            by_dest: Dict[str, List[Dict]] = defaultdict(list)
            for m in final_mappings:
                dest_dir = os.path.dirname(m["dest_path"])
                by_dest[dest_dir].append(m)

            created_folders = []
            errors = []
            moved_done = 0

            conn = DataBase.get_conn()

            # Execute moves/copies
            for dest_dir, mappings in by_dest.items():
                try:
                    # Update progress
                    current_folder = os.path.basename(dest_dir)
                    _organize_job_upsert(job_id, {
                        "progress": {
                            "stage": "moving",
                            "moved_done": moved_done,
                            "moved_total": total,
                            "current_file": f"-> {current_folder}/",
                            "created_folders": created_folders,
                            "errors": errors
                        }
                    })

                    # Create destination folder
                    if not os.path.exists(dest_dir):
                        os.makedirs(dest_dir, exist_ok=True)
                        created_folders.append(dest_dir)

                    # Move or copy each file
                    for m in mappings:
                        src_path = m["src_path"]
                        dest_path = m["dest_path"]

                        try:
                            txt_path = get_img_geninfo_txt_path(src_path)
                            if action == "move":
                                if txt_path:
                                    shutil.move(txt_path, dest_dir)
                                img = DbImg.get(conn, src_path)
                                if img:
                                    img.update_path(conn, dest_path, force=True)
                                shutil.move(src_path, dest_path)
                            else:
                                shutil.copy2(src_path, dest_path)
                                if txt_path:
                                    shutil.copy2(txt_path, dest_dir)

                            moved_done += 1

                        except Exception as e:
                            errors.append({
                                "src": src_path,
                                "dest": dest_path,
                                "error": str(e)
                            })
                            logger.error(f"[organize_files][{job_id}] Error moving {src_path}: {e}")

                    conn.commit()
                    logger.info(f"[organize_files][{job_id}] Processed {len(mappings)} files to {dest_dir}")

                except Exception as e:
                    conn.rollback()
                    error_info = {"dest": dest_dir, "count": len(mappings), "error": str(e)}
                    errors.append(error_info)
                    logger.error(f"[organize_files][{job_id}] Error with folder {dest_dir}: {e}")

                # Yield control
                await asyncio.sleep(0)

            # Done
            _organize_job_upsert(job_id, {
                "status": "done",
                "progress": {
                    "stage": "done",
                    "moved_done": moved_done,
                    "moved_total": total,
                    "current_file": "",
                    "created_folders": created_folders,
                    "errors": errors
                },
                "result": {
                    "moved_count": moved_done,
                    "created_folders": created_folders,
                    "errors": errors
                }
            })

            logger.info(f"[organize_files][{job_id}] Completed: moved {moved_done} files to {len(created_folders)} folders")

        except Exception as e:
            import traceback
            logger.error(f"[organize_files][{job_id}] Execute error: {e}")
            logger.error(traceback.format_exc())
            _organize_job_upsert(job_id, {
                "status": "error",
                "error": str(e),
            })

    @app.post(
        f"{db_api_base}/organize_files_start",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def organize_files_start(req: OrganizeFilesReq):
        """
        Start file organization task (background).
        Returns job_id for progress polling.
        """
        # Validate folders
        folders = []
        for p in req.folder_paths:
            if isinstance(p, str) and p.strip():
                folders.append(os.path.normpath(p.strip()))

        if not folders:
            raise HTTPException(400, "folder_paths is required")

        for f in folders:
            if not os.path.exists(f) or not os.path.isdir(f):
                raise HTTPException(400, f"Folder not found: {f}")

        # Update req with normalized folders
        req.folder_paths = folders

        job_id = uuid.uuid4().hex
        _organize_job_upsert(job_id, {
            "status": "queued",
            "req": req.dict(),
            "created_at": _job_now()
        })

        asyncio.create_task(_run_organize_job(job_id, req))

        logger.info(f"[organize_files] Started job {job_id} for folders: {folders}")
        return {"job_id": job_id}

    @app.get(
        f"{db_api_base}/organize_files_status",
        dependencies=[Depends(verify_secret)],
    )
    async def organize_files_status(job_id: str):
        """
        Query task status and progress.
        Status flow: queued -> running -> preview_ready -> (confirm) -> moving -> done
        """
        job = _organize_job_get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @app.post(
        f"{db_api_base}/organize_files_confirm",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def organize_files_confirm(req: OrganizeFilesConfirmReq):
        """
        Confirm and execute file organization.
        - Can modify folder names via folder_edits
        - Can skip clusters via skip_cluster_ids
        """
        job = _organize_job_get(req.job_id)
        if not job:
            raise HTTPException(404, "Job not found")

        if job.get("status") != "preview_ready":
            raise HTTPException(400, f"Job not ready for confirmation, current status: {job.get('status')}")

        asyncio.create_task(_execute_organize(req.job_id, req))

        return {"ok": True, "job_id": req.job_id}
