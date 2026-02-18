# IIB API Reference

Complete API endpoint reference documentation.

Base path: `/infinite_image_browsing`

---

## 1. File System Operations

### GET /files
List directory files.

**Parameters:**
- `folder_path` (string): Directory path

**Response:**
```json
{
  "files": [{
    "type": "file|dir",
    "date": 1234567890.0,
    "created_time": 1234567890.0,
    "size": "1.2 MB",
    "bytes": 1258291,
    "name": "image.png",
    "fullpath": "/path/to/image.png",
    "is_under_scanned_path": true
  }]
}
```

### POST /batch_get_files_info
Batch get file information.

**Request body:**
```json
{ "paths": ["/path/to/file1", "/path/to/file2"] }
```

### POST /delete_files
Delete files or empty folders.

**Request body:**
```json
{ "file_paths": ["/path/to/file1", "/path/to/file2"] }
```

### POST /mkdirs
Create directory.

**Request body:**
```json
{ "dest_folder": "/path/to/new/folder" }
```

### POST /copy_files
Copy files.

**Request body:**
```json
{
  "file_paths": ["/path/to/file1"],
  "dest": "/destination/folder",
  "create_dest_folder": false,
  "continue_on_error": false
}
```

### POST /move_files
Move files.

**Request body:**
```json
{
  "file_paths": ["/path/to/file1"],
  "dest": "/destination/folder",
  "create_dest_folder": false,
  "continue_on_error": false
}
```

### POST /db/rename
Rename file.

**Request body:**
```json
{ "path": "/path/to/file", "name": "new_name.png" }
```

### POST /flatten_folder
Flatten folder (move files from subdirectories to root).

**Request body:**
```json
{
  "folder_path": "/path/to/folder",
  "dry_run": true
}
```

### POST /zip
Create ZIP archive.

**Request body:**
```json
{
  "paths": ["/path/to/file1", "/path/to/file2"],
  "compress": true,
  "pack_only": false
}
```

### POST /check_path_exists
Check if paths exist.

**Request body:**
```json
{ "paths": ["/path1", "/path2"] }
```

---

## 2. Media File Access

### GET /image-thumbnail
Get image thumbnail.

**Parameters:**
- `path` (string): Image path
- `t` (string): Timestamp (for caching)
- `size` (string, default "256x256"): Thumbnail size

**Response:** WebP image

### GET /file
Get original file.

**Parameters:**
- `path` (string): File path
- `t` (string): Timestamp
- `disposition` (string, optional): Download filename

### GET /stream_video
Stream video with HTTP Range support.

**Parameters:**
- `path` (string): Video path

### GET /video_cover
Get video cover thumbnail.

**Parameters:**
- `path` (string): Video path
- `mt` (string): Modified time

---

## 3. Image Metadata

### GET /image_geninfo
Get image generation info (SD prompt, etc.).

**Parameters:**
- `path` (string): Image path

**Response:** Generation parameter text

### POST /image_geninfo_batch
Batch get generation info.

**Request body:**
```json
{ "paths": ["/path/to/img1.png", "/path/to/img2.png"] }
```

### GET /image_exif
Get image EXIF data.

**Parameters:**
- `path` (string): Image path

---

## 4. Database & Search

### GET /db/basic_info
Get database basic info.

**Response:**
```json
{
  "img_count": 10000,
  "tags": [{"id": 1, "name": "tag1", "type": "custom", "color": "#ff0000"}],
  "expired": false,
  "expired_dirs": []
}
```

### GET /db/random_images
Get random images (128 images).

### POST /db/update_image_data
Refresh image index (incremental update).

### POST /db/rebuild_index
Full rebuild of image index.

### POST /db/search_by_substr
Substring search.

**Request body:**
```json
{
  "surstr": "search term",
  "cursor": "",
  "regexp": "",
  "folder_paths": [],
  "size": 200,
  "path_only": false,
  "media_type": "all"
}
```

**Response:**
```json
{
  "files": [{...FileInfo...}],
  "cursor": { "has_next": true, "next": "cursor_string" }
}
```

### POST /db/match_images_by_tags
Tag-based search.

**Request body:**
```json
{
  "and_tags": [1, 2],
  "or_tags": [3],
  "not_tags": [4],
  "cursor": "",
  "folder_paths": [],
  "size": 200,
  "random_sort": false
}
```

---

## 5. Tag Management

### GET /db/img_selected_custom_tag
Get image's custom tags.

**Parameters:**
- `path` (string): Image path

### POST /db/get_image_tags
Batch get image tags.

**Request body:**
```json
{ "paths": ["/path/to/img1.png"] }
```

### POST /db/add_custom_tag
Add custom tag.

**Request body:**
```json
{ "tag_name": "my_tag" }
```

**Response:**
```json
{ "id": 1, "name": "my_tag", "type": "custom", "color": "" }
```

### POST /db/toggle_custom_tag_to_img
Toggle image tag (add if missing, remove if present).

**Request body:**
```json
{ "img_path": "/path/to/image.png", "tag_id": 1 }
```

### POST /db/batch_update_image_tag
Batch update tags.

**Request body:**
```json
{
  "img_paths": ["/path/to/img1.png", "/path/to/img2.png"],
  "action": "add",
  "tag_id": 1
}
```

### POST /db/remove_custom_tag
Delete custom tag.

**Request body:**
```json
{ "tag_id": 1 }
```

### POST /db/update_tag
Update tag properties.

**Request body:**
```json
{ "id": 1, "color": "#ff0000" }
```

---

## 6. AI Features

### POST /ai-chat
General AI chat interface (OpenAI compatible).

**Request body:**
```json
{
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Hello!" }
  ],
  "temperature": 0.7,
  "max_tokens": null,
  "stream": false
}
```

### POST /db/build_iib_output_embeddings
Build image embeddings.

**Request body:**
```json
{
  "folder": "/path/to/folder",
  "model": "text-embedding-3-small",
  "force": false,
  "batch_size": 100,
  "max_chars": 2000,
  "recursive": false
}
```

### POST /db/cluster_iib_output_job_start
Start clustering background job.

**Request body:**
```json
{
  "folder": "/path/to/folder",
  "folder_paths": [],
  "model": "text-embedding-3-small",
  "threshold": 0.85,
  "min_cluster_size": 3,
  "title_model": "gpt-4o-mini",
  "lang": "en",
  "recursive": false
}
```

**Response:**
```json
{ "job_id": "uuid-string" }
```

### GET /db/cluster_iib_output_job_status
Query clustering job status.

**Parameters:**
- `job_id` (string): Job ID

**Response:**
```json
{
  "job_id": "uuid",
  "status": "running|completed|failed",
  "progress": 0.5,
  "result": {
    "clusters": [{
      "id": "c1",
      "title": "Landscape Photos",
      "size": 50,
      "keywords": ["landscape", "nature"],
      "paths": ["/path/to/img1.png", ...]
    }]
  }
}
```

---

## 7. Smart File Organization

### POST /db/organize_files_start
Start file organization job.

**Request body:**
```json
{
  "folder_paths": ["/path/to/source"],
  "dest_folder": "/path/to/destination",
  "threshold": 0.90,
  "min_cluster_size": 2,
  "lang": "en",
  "recursive": false,
  "folder_naming": "title",
  "action": "move",
  "handle_noise": "unsorted",
  "noise_folder_name": "Unsorted"
}
```

**Parameter details:**
- `folder_naming`: "title" | "keywords" | "id"
- `action`: "move" | "copy"
- `handle_noise`: "skip" | "unsorted" | "leave"

### GET /db/organize_files_status
Query organization job status.

**Parameters:**
- `job_id` (string): Job ID

### POST /db/organize_files_confirm
Confirm and execute organization.

**Request body:**
```json
{
  "job_id": "uuid",
  "folder_edits": [
    { "cluster_id": "c1", "new_folder_name": "Custom Name" }
  ],
  "skip_cluster_ids": ["c2", "c3"]
}
```

---

## 8. Tag Graph

### POST /db/cluster_tag_graph
Build tag relationship graph.

**Request body:**
```json
{
  "folder_paths": ["/path/to/folder"],
  "lang": "en"
}
```

---

## 9. Extra Paths Management

### GET /db/extra_paths
Get extra paths list.

### POST /db/extra_paths
Add extra path.

**Request body:**
```json
{ "path": "/new/scan/path", "types": ["scan"] }
```

### DELETE /db/extra_paths
Remove extra path.

**Request body:**
```json
{ "path": "/path/to/remove", "types": ["scan"] }
```

### POST /db/alias_extra_path
Set path alias.

**Request body:**
```json
{ "path": "/path", "alias": "My Alias" }
```

---

## 10. System APIs

### GET /hello
Health check. Returns `"hello"`

### GET /version
Get version info.

### GET /global_setting
Get global settings.

### POST /app_fe_setting
Save frontend setting.

### DELETE /app_fe_setting
Delete frontend setting.

### POST /open_folder
Open file browser.

### POST /open_with_default_app
Open file with default application.

### POST /shutdown
Shutdown application (requires `--enable_shutdown`).

---

## Data Models

### FileInfoDict
```typescript
{
  type: "file" | "dir"
  date: number           // Modified timestamp
  created_time: number   // Created timestamp
  size: string           // Human readable size "1.2 MB"
  bytes: number          // Raw byte count
  name: string           // Filename
  fullpath: string       // Full path
  is_under_scanned_path: boolean
}
```

### Cursor
```typescript
{
  has_next: boolean
  next: string           // Next page cursor
}
```

### Tag
```typescript
{
  id: number
  name: string
  type: "custom" | "auto"
  color: string
}
```

---

## Error Handling

| Status Code | Meaning |
|-------------|---------|
| 200 | Success |
| 400 | Bad request / Invalid parameters |
| 401 | Authentication failed |
| 403 | Permission denied |
| 404 | Resource not found |
| 500 | Server error |

Error response format:
```json
{ "detail": "error message" }
```
