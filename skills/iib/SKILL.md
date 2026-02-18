---
name: iib
description: Interact with IIB (Infinite Image Browsing) service for searching, browsing, tagging, and organizing AI-generated images. Use when the user needs to search images by prompt/keyword, manage image tags, organize files into folders, get image generation parameters, or work with an image library.
---

# IIB (Infinite Image Browsing)

IIB is an image/video browsing and management tool that parses metadata from AI generation tools (Stable Diffusion, ComfyUI, etc.).

## Before You Start

**IMPORTANT:** Always do these two things first:

1. **Ask the user for the port** if they started the service themselves (common ports: `7866` standalone, `7860` SD WebUI extension)
2. **Test connectivity** with a hello request before any other operation

```bash
curl --noproxy "*" -s http://127.0.0.1:<PORT>/infinite_image_browsing/hello
# Returns: "hello" if service is running
```

Note: Use `--noproxy "*"` to bypass proxy for localhost connections.

**If service is not running**, start it:

```bash
cd /path/to/sd-webui-infinite-image-browsing
python app.py --port 7866
```

To run as a background daemon:

```bash
nohup python app.py --port 7866 > iib.log 2>&1 &
```

## Quick Reference

| Task | Method | Endpoint |
|------|--------|----------|
| Search images | POST | `/db/search_by_substr` |
| Search by tags | POST | `/db/match_images_by_tags` |
| Random images | GET | `/db/random_images` |
| List folder | GET | `/files?folder_path=...` |
| Move files | POST | `/move_files` |
| Copy files | POST | `/copy_files` |
| Tag images | POST | `/db/batch_update_image_tag` |
| Get image metadata | GET | `/image_geninfo?path=...` |
| Add library path | POST | `/db/extra_paths` |
| Remove library path | DELETE | `/db/extra_paths` |

Base URL: `http://127.0.0.1:7866/infinite_image_browsing`

## Core Operations

### Search Images

Search by keyword in file path or generation parameters:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "landscape", "cursor": "", "size": 50}'
```

Search with regex pattern:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "", "regexp": "masterpiece.*1girl", "cursor": "", "size": 50}'
```

Limit to specific folders:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "portrait", "folder_paths": ["/path/to/folder"], "cursor": "", "size": 50}'
```

Response:
```json
{
  "files": [{"fullpath": "/path/to/image.png", "name": "image.png", "size": "1.2 MB", ...}],
  "cursor": {"has_next": true, "next": "cursor_string"}
}
```

### Tag Management

Get all tags:

```bash
curl http://127.0.0.1:7866/infinite_image_browsing/db/basic_info
# Response includes: {"tags": [{"id": 1, "name": "favorites", "type": "custom"}, ...]}
```

Create a tag:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/add_custom_tag \
  -H "Content-Type: application/json" \
  -d '{"tag_name": "favorites"}'
```

Tag multiple images:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/batch_update_image_tag \
  -H "Content-Type: application/json" \
  -d '{"img_paths": ["/path/to/img1.png", "/path/to/img2.png"], "action": "add", "tag_id": 1}'
```

Search by tags (AND/OR/NOT logic):

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/match_images_by_tags \
  -H "Content-Type: application/json" \
  -d '{"and_tags": [1], "or_tags": [], "not_tags": [2], "cursor": "", "size": 50}'
```

### File Operations

List folder contents:

```bash
curl "http://127.0.0.1:7866/infinite_image_browsing/files?folder_path=/path/to/images"
```

Move files:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/move_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/img1.png"], "dest": "/new/folder", "create_dest_folder": true}'
```

Copy files:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/copy_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/img1.png"], "dest": "/backup/folder", "create_dest_folder": true}'
```

Delete files:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/delete_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/unwanted.png"]}'
```

### Image Metadata

Get generation parameters (prompt, seed, model, etc.):

```bash
curl "http://127.0.0.1:7866/infinite_image_browsing/image_geninfo?path=/path/to/image.png"
```

Batch get metadata:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/image_geninfo_batch \
  -H "Content-Type: application/json" \
  -d '{"paths": ["/path/to/img1.png", "/path/to/img2.png"]}'
```

### Library Management

List registered paths:

```bash
curl http://127.0.0.1:7866/infinite_image_browsing/db/extra_paths
```

Add a folder to library:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/extra_paths \
  -H "Content-Type: application/json" \
  -d '{"path": "/new/image/folder", "types": ["scanned"]}'
```

Remove a folder (also cleans up orphaned image records):

```bash
curl -X DELETE http://127.0.0.1:7866/infinite_image_browsing/db/extra_paths \
  -H "Content-Type: application/json" \
  -d '{"path": "/old/folder", "types": ["scanned"]}'
```

Rebuild index after adding paths:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/rebuild_index
```

Path types:
- `scanned`: Indexed, appears in search results
- `scanned-fixed`: Like scanned, but pinned in UI
- `walk`: Browse only, not indexed

## AI Features

### Smart File Organization

Automatically organize images into themed folders:

```bash
# Start organization job
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_start \
  -H "Content-Type: application/json" \
  -d '{
    "folder_paths": ["/messy/folder"],
    "dest_folder": "/organized/folder",
    "threshold": 0.85,
    "lang": "en",
    "action": "move"
  }'
# Returns: {"job_id": "uuid"}

# Check status
curl "http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_status?job_id=<job_id>"

# Confirm and execute
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_confirm \
  -H "Content-Type: application/json" \
  -d '{"job_id": "<job_id>"}'
```

### Image Clustering

Analyze images and group by semantic similarity:

```bash
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/cluster_iib_output_job_start \
  -H "Content-Type: application/json" \
  -d '{
    "folder_paths": ["/path/to/images"],
    "threshold": 0.85,
    "min_cluster_size": 3,
    "lang": "en",
    "recursive": true
  }'
```

## Reference

See [references/api-reference.md](references/api-reference.md) for complete API documentation.
