# Agent Guide

Task-oriented patterns for common IIB operations.

## Decision Tree

```
User wants to find images
├── By keyword/prompt text → POST /db/search_by_substr (surstr)
├── By regex pattern → POST /db/search_by_substr (regexp)
├── By tag → POST /db/match_images_by_tags
└── Random selection → GET /db/random_images

User wants to organize images
├── Move specific files → POST /move_files
├── Copy specific files → POST /copy_files
├── Auto-organize by theme → POST /db/organize_files_start
└── Delete files → POST /delete_files

User wants image info
├── Generation params → GET /image_geninfo
├── Batch metadata → POST /image_geninfo_batch
└── File listing → GET /files

User wants to manage tags
├── List all tags → GET /db/basic_info
├── Create tag → POST /db/add_custom_tag
├── Apply tag to images → POST /db/batch_update_image_tag
└── Search by tag → POST /db/match_images_by_tags

User wants to manage library
├── List paths → GET /db/extra_paths
├── Add folder → POST /db/extra_paths
├── Remove folder → DELETE /db/extra_paths
└── Rebuild index → POST /db/rebuild_index
```

## Common Workflows

### Find and tag images

```bash
# 1. Search for images
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "sunset", "cursor": "", "size": 100}'
# Note the fullpath values from response

# 2. Check existing tags
curl http://127.0.0.1:7866/infinite_image_browsing/db/basic_info
# Note tag IDs, or create new tag:

# 3. Create tag if needed
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/add_custom_tag \
  -H "Content-Type: application/json" \
  -d '{"tag_name": "sunset-photos"}'
# Returns: {"id": 5, "name": "sunset-photos", ...}

# 4. Apply tag to found images
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/batch_update_image_tag \
  -H "Content-Type: application/json" \
  -d '{"img_paths": ["/path/to/img1.png", "/path/to/img2.png"], "action": "add", "tag_id": 5}'
```

### Organize messy folder

```bash
# 1. Start AI organization
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_start \
  -H "Content-Type: application/json" \
  -d '{
    "folder_paths": ["/Downloads/ai-images"],
    "dest_folder": "/Pictures/organized",
    "threshold": 0.85,
    "lang": "en",
    "action": "move"
  }'
# Returns: {"job_id": "abc123"}

# 2. Poll for completion
curl "http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_status?job_id=abc123"
# Wait until status is "completed"

# 3. Confirm and execute
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/organize_files_confirm \
  -H "Content-Type: application/json" \
  -d '{"job_id": "abc123"}'
```

### Add new folder to library

```bash
# 1. Add path
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/extra_paths \
  -H "Content-Type: application/json" \
  -d '{"path": "/new/image/folder", "types": ["scanned"]}'

# 2. Trigger index update
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/update_image_data \
  -H "Content-Type: application/json" \
  -d '{"path": "/new/image/folder"}'

# 3. Verify images are indexed
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "", "folder_paths": ["/new/image/folder"], "cursor": "", "size": 10}'
```

### Paginate large results

```bash
# First page
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "landscape", "cursor": "", "size": 100}'
# Response: {"files": [...], "cursor": {"has_next": true, "next": "cursor_abc"}}

# Next page
curl -X POST http://127.0.0.1:7866/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "landscape", "cursor": "cursor_abc", "size": 100}'
# Continue until has_next is false
```

## Error Handling

| Status | Meaning | Action |
|--------|---------|--------|
| 200 | Success | Parse response |
| 400 | Bad request | Check JSON syntax and parameters |
| 404 | Not found | Verify file path exists |
| 500 | Server error | Check IIB logs |
| Connection refused | Service not running | Start IIB service |

## Performance Tips

1. Use `folder_paths` to limit search scope when possible
2. Use batch endpoints (`image_geninfo_batch`, `batch_update_image_tag`) for multiple items
3. Use pagination for large result sets
4. Call `update_image_data` for specific folders instead of full `rebuild_index`
