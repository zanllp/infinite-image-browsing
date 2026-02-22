---
name: iib
description: Interact with IIB (Infinite Image Browsing) service for searching, browsing, tagging, and organizing AI-generated images. Use when the user needs to search images by prompt/keyword, manage image tags, organize files into folders, get image generation parameters, or work with an image library.
---

# IIB (Infinite Image Browsing)

IIB is an image/video browsing and management tool that parses metadata from AI generation tools (Stable Diffusion, ComfyUI, etc.).

---

## Quick Links to Detailed Guides

| Topic | Description |
|-------|-------------|
| **[API Reference](references/api-reference.md)** | Complete API endpoint documentation |
| **[Search Strategies](references/search-strategies.md)** | Multi-word, regex, tag combination searches |
| **[Agent Patterns](references/agent-patterns.md)** | Common workflows and decision trees |

---

## Before You Start

**IMPORTANT:** Always do these two things first:

1. **Ask the user for the port** if they started the service themselves (common ports: `<port>` standalone, `7860` SD WebUI extension)
2. **Test connectivity** with a hello request before any other operation

```bash
curl --noproxy "*" -s http://127.0.0.1:<PORT>/infinite_image_browsing/hello
# Returns: "hello" if service is running
```

Note: Use `--noproxy "*"` to bypass proxy for localhost connections.

**If service is not running**, start it:

```bash
cd /path/to/sd-webui-infinite-image-browsing
python app.py --port <port>
```

To run as a background daemon:

```bash
nohup python app.py --port <port> > iib.log 2>&1 &
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

Base URL: `http://127.0.0.1:<port>/infinite_image_browsing`

## Core Operations

### Search Images

#### Basic Keyword Search

Search by keyword in file path or generation parameters:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "landscape", "size": 50}'
```

#### Advanced Search

For complex searches (multi-word, regex patterns, tag combinations), see **[Search Strategies](references/search-strategies.md)**.

**Quick tips:**
- Multi-word searches? Use regex OR: `(word1|word2)`
- Need tag combinations? Use `/db/match_images_by_tags` with AND/OR/NOT logic
- See **[Search Strategies](references/search-strategies.md)** for detailed strategies

#### Limit to Specific Folders

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"surstr": "portrait", "folder_paths": ["/path/to/folder"], "size": 50}'
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
curl http://127.0.0.1:<port>/infinite_image_browsing/db/basic_info
# Response includes: {"tags": [{"id": 1, "name": "favorites", "type": "custom"}, ...]}
```

Create a tag:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/add_custom_tag \
  -H "Content-Type: application/json" \
  -d '{"tag_name": "favorites"}'
```

Tag multiple images:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/batch_update_image_tag \
  -H "Content-Type: application/json" \
  -d '{"img_paths": ["/path/to/img1.png", "/path/to/img2.png"], "action": "add", "tag_id": 1}'
```

Search by tags (AND/OR/NOT logic):

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/match_images_by_tags \
  -H "Content-Type: application/json" \
  -d '{"and_tags": [1], "or_tags": [], "not_tags": [2], "cursor": "", "size": 50}'
```

### File Operations

List folder contents:

```bash
curl "http://127.0.0.1:<port>/infinite_image_browsing/files?folder_path=/path/to/images"
```

Move files:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/move_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/img1.png"], "dest": "/new/folder", "create_dest_folder": true}'
```

Copy files:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/copy_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/img1.png"], "dest": "/backup/folder", "create_dest_folder": true}'
```

Delete files:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/delete_files \
  -H "Content-Type: application/json" \
  -d '{"file_paths": ["/path/to/unwanted.png"]}'
```

### Image Metadata

Get generation parameters (prompt, seed, model, etc.):

```bash
curl "http://127.0.0.1:<port>/infinite_image_browsing/image_geninfo?path=/path/to/image.png"
```

Batch get metadata:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/image_geninfo_batch \
  -H "Content-Type: application/json" \
  -d '{"paths": ["/path/to/img1.png", "/path/to/img2.png"]}'
```

### Library Management

List registered paths:

```bash
curl http://127.0.0.1:<port>/infinite_image_browsing/db/extra_paths
```

Add a folder to library:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/extra_paths \
  -H "Content-Type: application/json" \
  -d '{"path": "/new/image/folder", "types": ["scanned"]}'
```

Remove a folder (also cleans up orphaned image records):

```bash
curl -X DELETE http://127.0.0.1:<port>/infinite_image_browsing/db/extra_paths \
  -H "Content-Type: application/json" \
  -d '{"path": "/old/folder", "types": ["scanned"]}'
```

Rebuild index after adding paths:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/rebuild_index
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
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/organize_files_start \
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
curl "http://127.0.0.1:<port>/infinite_image_browsing/db/organize_files_status?job_id=<job_id>"

# Confirm and execute
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/organize_files_confirm \
  -H "Content-Type: application/json" \
  -d '{"job_id": "<job_id>"}'
```

### Image Clustering

Analyze images and group by semantic similarity:

```bash
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/cluster_iib_output_job_start \
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

## Viewing Images

When helping users view images, always provide BOTH:
1. A link to view the first/selected image
2. A link to the search results page showing all matching images

### Image Search Results

When a user searches for images (by keyword, tag, etc.):

1. **Provide a summary**: Show how many images were found
2. **First image link**: Use the quick view URL for the first result
3. **Search results page link**: Use the fuzzy-search or tag-search pane URL

**Example response format:**

```markdown
Found 150+ images matching "sunset":

[View First Image](http://127.0.0.1:<port>/infinite_image_browsing?action=view&path=/first/image.png)

[View All Results in IIB](http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22sunset%22%7D)

| # | Name | Path | Size |
|---|------|------|------|
| 1 | image1.png | /folder | 1.2 MB |
| 2 | image2.png | /folder | 1.1 MB |
...
```

**Note:** The search results page link includes the search term in the URL, so it will automatically display the matching results when opened.

### Building Shareable URLs

When generating URLs for users to open in their browser, you need to properly encode the `props` parameter (JSON URL-encoded).

**URL Format:**
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=<pane_type>&props=<url_encoded_json>
```

**Encoding the props:**
The `props` parameter must be URL-encoded JSON. Use this encoding method:

| JSON Props | URL Encoded | Notes |
|------------|-------------|-------|
| `{"substr":"sunset"}` | `%7B%22substr%22%3A%22sunset%22%7D` | Standard encoding |
| `{"substr":"(词1\|词2)","isRegex":true}` | `%7B%22substr%22%3A%22(%E8%AF%8D1%7C%E8%AF%8D2)%22%2C%22isRegex%22%3Atrue%7D` | Chinese chars encoded |
| `{"substr":"a\|b","isRegex":true}` | `%7B%22substr%22%3A%22(a%7Cb)%22%2C%22isRegex%22%3Atrue%7D` | Pipe `\|` encoded as `%7C` |

**Common URL patterns:**

| Use Case | URL Pattern |
|----------|-------------|
| Simple keyword search | `?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22keyword%22%7D` |
| Regex OR search | `?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22(word1%7Cword2)%22%2C%22isRegex%22%3Atrue%7D` |
| Search in folder | `?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22keyword%22%2C%22searchScope%22%3A%22%2Fpath%22%7D` |
| View single image | `?action=view&path=/path/to/image.png` |

**Tip:** When generating URLs with non-ASCII characters (Chinese, Japanese, etc.), use proper URL encoding (UTF-8 percent encoding).

**Example: Multi-word search with regex (Chinese)**
```bash
# Search for "太空电梯" OR "三体"
# JSON: {"substr":"(太空电梯|三体)","isRegex":true}
# Encoded: %7B%22substr%22%3A%22(%E6%B1%89%E6%9C%8D%7C%E4%BA%BA%E5%A6%BB)%22%2C%22isRegex%22%3Atrue%7D
?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22(%E6%B1%89%E6%9C%8D%7C%E4%BA%BA%E5%A6%BB)%22%2C%22isRegex%22%3Atrue%7D
```

### Available View Options

#### 1. Quick View (Single Image)
```
http://127.0.0.1:<port>/infinite_image_browsing?action=view&path=/path/to/image.png
```
Opens IIB directly to the image in fullscreen preview.

#### 2. Keyword Search Page
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=fuzzy-search
```
Opens the keyword search page where users can search.

**With pre-filled search term:**
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22sunset%22%7D
```
Automatically searches for "sunset" when opened.

**Available props for fuzzy-search (URL → Component mapping):**

| URL Prop | Component Prop | Type | Description |
|----------|----------------|------|-------------|
| `substr` | `initialSubstr` | string | Search keyword or regex pattern |
| `isRegex` | `initialIsRegex` | boolean | Use regex mode (default: false) |
| `searchScope` | `searchScope` | string | Folder path to limit search |
| `pathOnly` | `initialPathOnly` | boolean | Search only file paths (default: false) |
| `mediaType` | `initialMediaType` | string | Filter by media type: "all", "image", "video" |
| `autoSearch` | `autoSearch` | boolean | Auto-trigger search (default: true when substr is set) |

**Note:** Use the URL prop names (left column) when constructing URLs. The component prop names (middle column) are for internal use.

**Examples:**
```bash
# Regex search for multiple keywords (OR logic)
?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22(sunset%7Csunrise%7Cdawn)%22%2C%22isRegex%22%3Atrue%7D

# Search in specific folder
?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22portrait%22%2C%22searchScope%22%3A%22%2Foutputs%22%7D

# Search images only
?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22landscape%22%2C%22mediaType%22%3A%22image%22%7D
```

#### 3. Tag Search Page
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=tag-search
```
Opens the tag search page for filtering by tags.

#### 4. Tag Search Results (Pre-filtered)
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=tag-search-matched-image-grid&props=%7B%22selectedTagIds%22%3A%7B%22and_tags%22%3A%5B1%2C2%5D%7D%7D%7D
```
Shows images matching specific tags (replace tag IDs with actual values).

#### 5. Image Comparison
```
http://127.0.0.1:<port>/infinite_image_browsing?action=pane&type=img-sli&props=%7B%22left%22%3A%7B%22fullpath%22%3A%22%2Fa.png%22%7D%2C%22right%22%3A%7B%22fullpath%22%3A%22%2Fb.png%22%7D%7D
```
Compare two images side by side.

**Note:** The `props` parameter must be URL-encoded JSON.
