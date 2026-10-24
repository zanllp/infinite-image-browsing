"""Portable export / import of IIB user-generated data.

Why this module exists
----------------------
`iib.db` mixes three very different kinds of data:

1. derived index   — image rows, parsed exif cache, prompt tags, embeddings;
2. caches          — cluster/title caches, cover cache, trend stats;
3. user data       — custom tags (favorites), tag colors, scan roots + aliases,
                     app settings / workspace snapshots, manually edited prompts.

Only (3) is irreplaceable.  Deleting `iib.db` and rebuilding the index restores
(1) from the image files themselves (embeddings can be recomputed too).  A full
DB copy is gigabytes because (1) dominates, so this module exports only (3) into
a small, path-keyed JSON document that can be imported into a fresh database.

Design notes
------------
* Everything is keyed by absolute image path + (tag name, tag type).  Row ids
  are meaningless across databases and are intentionally not exported.
* Tag assignments whose file is not indexed yet (the typical state right after
  deleting `iib.db`) are queued into `user_tag_pending` and applied
  automatically as soon as the file appears in the `image` table.
* Import is idempotent: running it twice does not duplicate anything.
* Import runs in a single transaction; any failure rolls the whole batch back.

See issue #982.
"""

import json
import os
from contextlib import closing
from datetime import datetime
from sqlite3 import Connection
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from scripts.iib.db.datamodel import UserTagPending, commit_with_retry
from scripts.iib.logger import logger

# Bump when the document shape changes in an incompatible way.
USER_DATA_FORMAT = "iib-user-data"
USER_DATA_FORMAT_VERSION = 1

# Tag types produced by (re)indexing files.  They are regenerated from the
# image files, so exporting them would only bloat the backup.
DERIVED_TAG_TYPES = {
    "pos",
    "size",
    "Size",
    "Model",
    "Sampler",
    "lora",
    "lyco",
    "Media Type",
    "Source Identifier",
    "Postprocess upscale by",
    "Postprocess upscaler",
    "Refiner",
    "Hires upscaler",
}

# global_setting rows that are derived caches rather than user settings.
DERIVED_SETTING_PREFIXES = ("trend_stats_", "tag_abstraction_")

ALLOWED_EXTRA_PATH_TYPES = {"walk", "scanned", "scanned-fixed", "cli_access_only"}


class UserDataError(ValueError):
    """Invalid or incompatible user-data document."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _is_derived_setting(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in DERIVED_SETTING_PREFIXES)


def _normcase_path(path: Any) -> str:
    if not isinstance(path, str):
        return ""
    return os.path.normcase(os.path.normpath(path))


def _path_variants(path: str) -> List[str]:
    """Path spellings that should be treated as the same file.

    Windows databases migrated between versions contain both `D:/x` and
    `D:\\x`; we normalize on both ends but keep the lookup tolerant.
    """
    candidates = [
        path,
        os.path.normpath(path),
        path.replace("/", os.sep).replace("\\", os.sep),
    ]
    candidates.append(os.path.normpath(candidates[2]))
    out: List[str] = []
    seen = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_tag_entry(raw: Any) -> Optional[Tuple[str, str, str]]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    tag_type = raw.get("type")
    color = raw.get("color") or ""
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(tag_type, str) or not tag_type:
        return None
    if not isinstance(color, str):
        color = ""
    return name, tag_type, color


def _normalize_tagged_images(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        tags = item.get("tags")
        if not isinstance(path, str) or not path or not isinstance(tags, list):
            continue
        clean_tags: List[Tuple[str, str]] = []
        for tag in tags:
            if (
                isinstance(tag, (list, tuple))
                and len(tag) >= 2
                and isinstance(tag[0], str)
                and tag[0]
                and isinstance(tag[1], str)
                and tag[1]
                # derived tags are rebuilt from the image files; never import them
                and tag[1] not in DERIVED_TAG_TYPES
            ):
                clean_tags.append((tag[0], tag[1]))
        if clean_tags:
            out.append({"path": path, "tags": clean_tags})
    return out


def _normalize_settings(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or _is_derived_setting(name):
            continue
        out[name] = value
    return out


def _normalize_extra_paths(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        types = item.get("type")
        alias = item.get("alias") or ""
        if not isinstance(path, str) or not path:
            continue
        if not isinstance(types, str):
            types = ""
        if not isinstance(alias, str):
            alias = ""
        out.append({"path": os.path.normpath(path), "type": types, "alias": alias})
    return out


def _normalize_prompt_edits(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        exif = item.get("exif")
        if not isinstance(path, str) or not path or not isinstance(exif, str):
            continue
        out.append({"path": path, "exif": exif})
    return out


def _normalize_pending_tags(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        name = item.get("name")
        tag_type = item.get("type")
        if (
            isinstance(path, str)
            and path
            and isinstance(name, str)
            and name
            and isinstance(tag_type, str)
            and tag_type
            and tag_type not in DERIVED_TAG_TYPES
        ):
            out.append({"path": path, "name": name, "type": tag_type})
    return out


def _exif_edited_map(conn: Connection, image_ids: List[int]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for chunk in _chunks(sorted(set(int(x) for x in image_ids)), 400):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT id, exif_edited FROM image WHERE id IN ({})".format(
                    ",".join("?" * len(chunk))
                ),
                chunk,
            )
            for image_id, edited in cur.fetchall():
                out[int(image_id)] = int(edited or 0)
    return out


def _existing_tags(conn: Connection) -> Dict[Tuple[str, str], Tuple[int, str]]:
    """(name, type) -> (id, color)."""
    with closing(conn.cursor()) as cur:
        cur.execute("SELECT id, name, type, color FROM tag")
        result: Dict[Tuple[str, str], Tuple[int, str]] = {}
        for tag_id, name, tag_type, color in cur.fetchall():
            if isinstance(name, str) and isinstance(tag_type, str):
                result[(name, tag_type)] = (int(tag_id), color or "")
        return result


def _existing_setting_names(conn: Connection) -> Set[str]:
    with closing(conn.cursor()) as cur:
        cur.execute("SELECT name FROM global_setting")
        return {str(row[0]) for row in cur.fetchall() if row[0] is not None}


def _existing_extra_paths(conn: Connection) -> Set[str]:
    with closing(conn.cursor()) as cur:
        cur.execute("SELECT path FROM extra_path")
        return {str(row[0]) for row in cur.fetchall() if row[0] is not None}


def _existing_pairs(
    conn: Connection, image_ids: List[int], tag_ids: List[int]
) -> Set[Tuple[int, int]]:
    """(image_id, tag_id) pairs already present, restricted to the given ids."""
    pairs: Set[Tuple[int, int]] = set()
    if not image_ids or not tag_ids:
        return pairs
    tag_set = set(int(x) for x in tag_ids)
    for image_chunk in _chunks(sorted(set(int(x) for x in image_ids)), 300):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT image_id, tag_id FROM image_tag WHERE image_id IN ({})".format(
                    ",".join("?" * len(image_chunk))
                ),
                image_chunk,
            )
            for image_id, tag_id in cur.fetchall():
                if int(tag_id) in tag_set:
                    pairs.add((int(image_id), int(tag_id)))
    return pairs


def _resolve_image_paths(
    conn: Connection, paths: List[str], *, fallback_scan: bool = True
) -> Dict[str, int]:
    """Map exported image paths to current image ids.

    Exact matches first; if some paths do not match (different slash style,
    drive-letter case, or a DB written by an older version) fall back to a
    single normalized scan of the image table.

    `fallback_scan=False` skips that full scan, which matters because this is
    called on every folder scan to drain `user_tag_pending` (see
    apply_pending_user_tags) and the table is usually empty.
    """
    if not paths:
        return {}

    variant_to_orig: Dict[str, str] = {}
    wanted: List[str] = []
    seen = set()
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        for variant in _path_variants(path):
            if variant in seen:
                continue
            seen.add(variant)
            variant_to_orig[variant] = path
            wanted.append(variant)

    resolved: Dict[str, int] = {}
    with closing(conn.cursor()) as cur:
        for chunk in _chunks(wanted, 400):
            cur.execute(
                "SELECT id, path FROM image WHERE path IN ({})".format(
                    ",".join("?" * len(chunk))
                ),
                chunk,
            )
            for image_id, db_path in cur.fetchall():
                orig = variant_to_orig.get(db_path)
                if orig is not None and orig not in resolved:
                    resolved[orig] = int(image_id)

    missing = [p for p in paths if p not in resolved]
    if not missing or not fallback_scan:
        return resolved

    # Fallback: normalize both sides (handles separator / case differences).
    missing_by_key: Dict[str, List[str]] = {}
    for p in missing:
        if isinstance(p, str) and p:
            missing_by_key.setdefault(_normcase_path(p), []).append(p)
    if not missing_by_key:
        return resolved
    with closing(conn.cursor()) as cur:
        cur.execute("SELECT id, path FROM image")
        for image_id, db_path in cur.fetchall():
            originals = missing_by_key.pop(_normcase_path(db_path), None)
            if originals:
                for p in originals:
                    resolved[p] = int(image_id)
            if not missing_by_key:
                break
    return resolved


def _get_or_create_tag(cur, name: str, tag_type: str, color: str = "") -> Optional[int]:
    """Raw upsert used by restore paths.

    `Tag.get_or_create` validates names against the current creation rules;
    data restored from a backup was valid when it was created, so validation
    must not silently drop it here.
    """
    cur.execute(
        """INSERT INTO tag (name, score, type, count, color) VALUES (?, 0, ?, 0, ?)
           ON CONFLICT(name, type) DO NOTHING""",
        (name, tag_type, color or ""),
    )
    cur.execute("SELECT id FROM tag WHERE name = ? AND type = ?", (name, tag_type))
    row = cur.fetchone()
    return int(row[0]) if row else None


def _recount_tags(cur, tag_ids: Iterable[int]) -> None:
    ids = [int(x) for x in set(tag_ids)]
    if not ids:
        return
    for chunk in _chunks(ids, 300):
        cur.execute(
            """UPDATE tag SET count = (
                   SELECT COUNT(*) FROM image_tag WHERE image_tag.tag_id = tag.id
               ) WHERE id IN ({})""".format(",".join("?" * len(chunk))),
            chunk,
        )


def _begin_immediate(conn: Connection) -> None:
    if conn.in_transaction:
        # Callers may hand us a connection with read state only; never nest
        # transactions on a shared thread-local connection.
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_user_data(conn: Connection) -> Dict[str, Any]:
    """Collect all irreplaceable user data into a JSON-serializable document."""
    types = sorted(DERIVED_TAG_TYPES)
    type_ph = ",".join("?" * len(types))

    with closing(conn.cursor()) as cur:
        cur.execute("SELECT COUNT(*) FROM image")
        image_count = int(cur.fetchone()[0] or 0)

        cur.execute(
            "SELECT name, type, color FROM tag WHERE type NOT IN ({})".format(type_ph),
            types,
        )
        tags: List[Dict[str, str]] = []
        for name, tag_type, color in cur.fetchall():
            if isinstance(name, str) and isinstance(tag_type, str):
                tags.append({"name": name, "type": tag_type, "color": color or ""})

        cur.execute(
            """SELECT image.path, tag.name, tag.type
               FROM image_tag
               INNER JOIN tag ON tag.id = image_tag.tag_id
               INNER JOIN image ON image.id = image_tag.image_id
               WHERE tag.type NOT IN ({})
               ORDER BY image.path""".format(type_ph),
            types,
        )
        tagged_images: List[Dict[str, Any]] = []
        current_path: Optional[str] = None
        current_tags: List[List[str]] = []
        assignment_count = 0
        for path, name, tag_type in cur.fetchall():
            if (
                not isinstance(path, str)
                or not isinstance(name, str)
                or not isinstance(tag_type, str)
            ):
                continue
            if path != current_path:
                current_path = path
                current_tags = []
                tagged_images.append({"path": path, "tags": current_tags})
            current_tags.append([name, tag_type])
            assignment_count += 1

        cur.execute("SELECT name, setting_json FROM global_setting")
        settings: Dict[str, Any] = {}
        for name, raw in cur.fetchall():
            if not isinstance(name, str) or _is_derived_setting(name):
                continue
            try:
                settings[name] = json.loads(raw)
            except (TypeError, ValueError):
                continue

        cur.execute("SELECT path, type, alias FROM extra_path")
        extra_paths: List[Dict[str, str]] = []
        for path, types_str, alias in cur.fetchall():
            if not isinstance(path, str) or not path:
                continue
            extra_paths.append(
                {"path": path, "type": types_str or "", "alias": alias or ""}
            )

        cur.execute("SELECT path, exif FROM image WHERE exif_edited = 1")
        prompt_edits: List[Dict[str, str]] = []
        for path, exif in cur.fetchall():
            if not isinstance(path, str) or not path:
                continue
            prompt_edits.append({"path": path, "exif": exif or ""})

        cur.execute("SELECT image_path, tag_name, tag_type FROM user_tag_pending")
        pending_tags: List[Dict[str, str]] = []
        for path, name, tag_type in cur.fetchall():
            if isinstance(path, str) and isinstance(name, str) and isinstance(tag_type, str):
                pending_tags.append({"path": path, "name": name, "type": tag_type})

    db_file = ""
    try:
        db_file = os.path.basename(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    except Exception:
        pass

    return {
        "format": USER_DATA_FORMAT,
        "format_version": USER_DATA_FORMAT_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": {"db_file": db_file, "image_count": image_count},
        "settings": settings,
        "extra_paths": extra_paths,
        "tags": tags,
        "tagged_images": tagged_images,
        "prompt_edits": prompt_edits,
        "pending_tags": pending_tags,
        "summary": {
            "tags": len(tags),
            "tagged_images": len(tagged_images),
            "tag_assignments": assignment_count,
            "settings": len(settings),
            "extra_paths": len(extra_paths),
            "prompt_edits": len(prompt_edits),
            "pending_tags": len(pending_tags),
        },
    }


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def import_user_data(
    conn: Connection,
    data: Any,
    *,
    mode: str = "merge",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Merge an exported document into the current database.

    Args:
        conn: caller-owned connection (one per thread).
        data: parsed export document.
        mode: ``merge`` only fills gaps, ``overwrite`` replaces existing values.
        dry_run: compute the stats without writing anything.

    Returns:
        A stats dict suitable for a preview / result summary.

    Raises:
        UserDataError: document is not a valid IIB user-data export.
    """
    if not isinstance(data, dict):
        raise UserDataError("Invalid document: expected a JSON object.")
    if data.get("format") != USER_DATA_FORMAT:
        raise UserDataError(
            "Invalid document: this file is not an IIB user-data export "
            f"(expected format '{USER_DATA_FORMAT}')."
        )
    try:
        version = int(data.get("format_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > USER_DATA_FORMAT_VERSION:
        raise UserDataError(
            f"Unsupported export version {version}; this build supports "
            f"version {USER_DATA_FORMAT_VERSION}."
        )
    if mode not in ("merge", "overwrite"):
        raise UserDataError("mode must be 'merge' or 'overwrite'")

    overwrite = mode == "overwrite"

    raw_tags = data.get("tags")
    raw_tags = raw_tags if isinstance(raw_tags, list) else []
    tags_in: List[Tuple[str, str, str]] = []
    invalid_tags = 0
    for raw in raw_tags:
        normalized = _normalize_tag_entry(raw)
        if normalized is None:
            invalid_tags += 1
        else:
            tags_in.append(normalized)
    tagged_in = _normalize_tagged_images(data.get("tagged_images"))
    settings_in = _normalize_settings(data.get("settings"))
    extra_paths_in = _normalize_extra_paths(data.get("extra_paths"))
    prompt_edits_in = _normalize_prompt_edits(data.get("prompt_edits"))
    pending_in = _normalize_pending_tags(data.get("pending_tags"))
    source = data.get("source")

    stats: Dict[str, Any] = {
        "mode": mode,
        "dry_run": bool(dry_run),
        "export_created_at": data.get("created_at"),
        "source_image_count": source.get("image_count") if isinstance(source, dict) else None,
        "tags": {"created": 0, "existing": 0, "color_updated": 0, "invalid": invalid_tags},
        "tag_assignments": {"added": 0, "existing": 0},
        "images": {"matched": 0, "unmatched": 0},
        "pending_tags": 0,
        "pending_queued": 0,
        "pending_applied": 0,
        "pending_total": 0,
        "settings": {"added": 0, "updated": 0, "skipped": 0},
        "extra_paths": {"added": 0, "updated": 0, "skipped": 0, "invalid": 0},
        "prompt_edits": {"applied": 0, "skipped": 0, "unmatched": 0},
        "unmatched_paths_sample": [],
    }

    existing_tags = _existing_tags(conn)
    existing_settings = _existing_setting_names(conn)
    existing_extra_paths = _existing_extra_paths(conn)

    # --- plan (read-only; also the dry-run result) ------------------------
    tag_create: List[Tuple[str, str, str]] = []
    seen_new_tags: Set[Tuple[str, str]] = set()
    color_updates: List[Tuple[int, str]] = []
    for name, tag_type, color in tags_in:
        key = (name, tag_type)
        existing = existing_tags.get(key)
        if existing is None:
            if key in seen_new_tags:
                continue
            seen_new_tags.add(key)
            tag_create.append((name, tag_type, color))
            stats["tags"]["created"] += 1
        else:
            stats["tags"]["existing"] += 1
            tag_id, current_color = existing
            if color and (overwrite or not current_color):
                color_updates.append((tag_id, color))
                stats["tags"]["color_updated"] += 1

    invalid_extra_paths = 0
    allowed_extra_paths: List[Dict[str, str]] = []
    for item in extra_paths_in:
        types = [t for t in str(item["type"]).split("+") if t]
        if types and not set(types).issubset(ALLOWED_EXTRA_PATH_TYPES):
            invalid_extra_paths += 1
            continue
        allowed_extra_paths.append(item)
        if item["path"] in existing_extra_paths:
            if overwrite:
                stats["extra_paths"]["updated"] += 1
            else:
                stats["extra_paths"]["skipped"] += 1
        else:
            stats["extra_paths"]["added"] += 1
    stats["extra_paths"]["invalid"] = invalid_extra_paths

    for name in settings_in:
        if name in existing_settings:
            if overwrite:
                stats["settings"]["updated"] += 1
            else:
                stats["settings"]["skipped"] += 1
        else:
            stats["settings"]["added"] += 1

    image_map = _resolve_image_paths(conn, [entry["path"] for entry in tagged_in])
    matched_image_ids = sorted(
        {image_map[entry["path"]] for entry in tagged_in if entry["path"] in image_map}
    )
    unmatched_paths: List[str] = []
    planned_pending = 0
    for entry in tagged_in:
        if entry["path"] in image_map:
            stats["images"]["matched"] += 1
        else:
            stats["images"]["unmatched"] += 1
            if len(unmatched_paths) < 20:
                unmatched_paths.append(entry["path"])
            planned_pending += len(entry["tags"])
    stats["unmatched_paths_sample"] = unmatched_paths
    stats["pending_tags"] = planned_pending

    if dry_run and tagged_in:
        existing_pair_set = _existing_pairs(
            conn,
            matched_image_ids,
            [tag_id for tag_id, _ in existing_tags.values()],
        )
        for entry in tagged_in:
            image_id = image_map.get(entry["path"])
            if image_id is None:
                continue
            for name, tag_type in entry["tags"]:
                existing = existing_tags.get((name, tag_type))
                if existing is not None and (int(image_id), existing[0]) in existing_pair_set:
                    stats["tag_assignments"]["existing"] += 1
                else:
                    stats["tag_assignments"]["added"] += 1

    matched_edits: List[Tuple[Dict[str, str], int]] = []
    for edit in prompt_edits_in:
        image_id = image_map.get(edit["path"])
        if image_id is not None:
            matched_edits.append((edit, int(image_id)))

    if dry_run:
        stats["prompt_edits"]["unmatched"] += len(prompt_edits_in) - len(matched_edits)
        edited_map = _exif_edited_map(conn, [image_id for _, image_id in matched_edits])
        for _edit, image_id in matched_edits:
            if overwrite or not edited_map.get(image_id):
                stats["prompt_edits"]["applied"] += 1
            else:
                stats["prompt_edits"]["skipped"] += 1

    if dry_run:
        stats["pending_total"] = (
            UserTagPending.count(conn) + planned_pending + len(pending_in)
        )
        return stats

    # --- write phase ------------------------------------------------------
    created_tag_map: Dict[Tuple[str, str], int] = {}
    for name, tag_type, color in tag_create:
        key = (name, tag_type)
        existing = existing_tags.get(key)
        if existing is not None:
            created_tag_map[key] = existing[0]
    affected_tag_ids: Set[int] = set()
    try:
        _begin_immediate(conn)
        with closing(conn.cursor()) as cur:
            for name, tag_type, color in tag_create:
                tag_id = _get_or_create_tag(cur, name, tag_type, color)
                if tag_id is not None:
                    created_tag_map[(name, tag_type)] = tag_id

            for tag_id, color in color_updates:
                cur.execute("UPDATE tag SET color = ? WHERE id = ?", (color, tag_id))

            # tag assignments (matched images) / pending queue (unmatched)
            for entry in tagged_in:
                image_id = image_map.get(entry["path"])
                if image_id is None:
                    for name, tag_type in entry["tags"]:
                        cur.execute(
                            """INSERT OR IGNORE INTO user_tag_pending
                               (image_path, tag_name, tag_type, created_at)
                               VALUES (?, ?, ?, ?)""",
                            (
                                entry["path"],
                                name,
                                tag_type,
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                    continue
                for name, tag_type in entry["tags"]:
                    key = (name, tag_type)
                    tag_id = created_tag_map.get(key)
                    if tag_id is None:
                        existing = existing_tags.get(key)
                        tag_id = (
                            existing[0]
                            if existing is not None
                            else _get_or_create_tag(cur, name, tag_type)
                        )
                    if tag_id is None:
                        continue
                    cur.execute(
                        """INSERT OR IGNORE INTO image_tag (image_id, tag_id, created_at)
                           VALUES (?, ?, CURRENT_TIMESTAMP)""",
                        (int(image_id), int(tag_id)),
                    )
                    if cur.rowcount > 0:
                        stats["tag_assignments"]["added"] += 1
                        affected_tag_ids.add(int(tag_id))
                    else:
                        stats["tag_assignments"]["existing"] += 1

            # pending rows carried over from the source database
            for item in pending_in:
                cur.execute(
                    """INSERT OR IGNORE INTO user_tag_pending
                       (image_path, tag_name, tag_type, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        item["path"],
                        item["name"],
                        item["type"],
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                if cur.rowcount > 0:
                    stats["pending_queued"] += 1

            # app settings
            for name, value in settings_in.items():
                try:
                    payload = json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError):
                    continue
                now = datetime.now().isoformat()
                if overwrite:
                    cur.execute(
                        """INSERT INTO global_setting
                             (setting_json, name, created_time, modified_time)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(name) DO UPDATE SET
                             setting_json = excluded.setting_json,
                             modified_time = excluded.modified_time""",
                        (payload, name, now, now),
                    )
                else:
                    cur.execute(
                        """INSERT OR IGNORE INTO global_setting
                             (setting_json, name, created_time, modified_time)
                           VALUES (?, ?, ?, ?)""",
                        (payload, name, now, now),
                    )

            # scan roots (aliases)
            for item in allowed_extra_paths:
                types = [t for t in str(item["type"]).split("+") if t]
                type_str = "+".join(types) if types else "scanned"
                path = item["path"]
                if overwrite:
                    cur.execute(
                        """INSERT INTO extra_path (path, type, alias) VALUES (?, ?, ?)
                           ON CONFLICT(path) DO UPDATE SET
                             type = excluded.type, alias = excluded.alias""",
                        (path, type_str, item["alias"]),
                    )
                else:
                    cur.execute(
                        """INSERT OR IGNORE INTO extra_path (path, type, alias)
                           VALUES (?, ?, ?)""",
                        (path, type_str, item["alias"]),
                    )

            # manually edited prompts
            for edit in prompt_edits_in:
                image_id = image_map.get(edit["path"])
                if image_id is None:
                    stats["prompt_edits"]["unmatched"] += 1
                    continue
                if overwrite:
                    cur.execute(
                        "UPDATE image SET exif = ?, exif_edited = 1 WHERE id = ?",
                        (edit["exif"], int(image_id)),
                    )
                    stats["prompt_edits"]["applied"] += 1
                else:
                    cur.execute(
                        """UPDATE image SET exif = ?, exif_edited = 1
                           WHERE id = ? AND exif_edited = 0""",
                        (edit["exif"], int(image_id)),
                    )
                    if cur.rowcount > 0:
                        stats["prompt_edits"]["applied"] += 1
                    else:
                        stats["prompt_edits"]["skipped"] += 1

            # keep counters consistent with what we just inserted
            _recount_tags(cur, affected_tag_ids)
        commit_with_retry(conn)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    stats["pending_total"] = UserTagPending.count(conn)
    applied = apply_pending_user_tags(conn, limit=5000)
    if applied:
        stats["pending_applied"] = applied
        stats["pending_total"] = UserTagPending.count(conn)
    return stats


# ---------------------------------------------------------------------------
# pending queue
# ---------------------------------------------------------------------------


def _apply_pending_user_tags_batch(conn: Connection, batch_limit: int) -> int:
    with closing(conn.cursor()) as cur:
        cur.execute(
            "SELECT image_path, tag_name, tag_type FROM user_tag_pending LIMIT ?",
            (max(1, int(batch_limit)),),
        )
        rows = cur.fetchall()
    if not rows:
        return 0

    image_map = _resolve_image_paths(
        conn, [str(row[0]) for row in rows], fallback_scan=False
    )
    applied = 0
    affected_tag_ids: Set[int] = set()
    _begin_immediate(conn)
    with closing(conn.cursor()) as cur:
        for path, name, tag_type in rows:
            image_id = image_map.get(str(path))
            if image_id is None:
                continue
            tag_id = _get_or_create_tag(cur, str(name), str(tag_type))
            if tag_id is None:
                continue
            cur.execute(
                """INSERT OR IGNORE INTO image_tag (image_id, tag_id, created_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (int(image_id), int(tag_id)),
            )
            if cur.rowcount > 0:
                affected_tag_ids.add(int(tag_id))
            cur.execute(
                """DELETE FROM user_tag_pending
                   WHERE image_path = ? AND tag_name = ? AND tag_type = ?""",
                (path, name, tag_type),
            )
            applied += 1
        _recount_tags(cur, affected_tag_ids)
    commit_with_retry(conn)
    return applied


def apply_pending_user_tags(conn: Connection, *, limit: int = 5000) -> int:
    """Apply queued tag assignments whose image is now indexed.

    Safe to call after every index pass: it is a no-op when `user_tag_pending`
    is empty.  Returns the number of applied pending rows (capped by `limit`);
    rows whose file is still missing stay queued for a later pass.

    Never raises: a failure here must not break indexing.
    """
    try:
        total = 0
        cap = max(1, int(limit))
        while total < cap:
            applied = _apply_pending_user_tags_batch(conn, min(1000, cap - total))
            if applied <= 0:
                break
            total += applied
        if total:
            logger.info("Applied %d pending user tag assignment(s)", total)
        return total
    except Exception as e:
        logger.warning("apply_pending_user_tags failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
