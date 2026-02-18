---
name: IIB API
description: Access IIB (Infinite Image Browsing) APIs for image searching, browsing, tagging, and AI-powered organization.
---

# IIB (Infinite Image Browsing) API Skill

IIB is a powerful image/video browsing, searching, and management tool with support for parsing metadata from multiple AI generation tools.

## Starting the Service

### Method 1: Standalone Mode (Recommended)

```bash
# Basic startup
python app.py --port 8000 --host 127.0.0.1

# With extra scan paths
python app.py --port 8000 --extra_paths /path/to/images /another/path

# Update index on startup
python app.py --port 8000 --extra_paths /path/to/images --update_image_index

# Enable CORS for external access
python app.py --port 8000 --allow_cors

# Full example
python app.py --port 8000 --host 0.0.0.0 --allow_cors --extra_paths /my/images --update_image_index
```

### Method 2: As SD WebUI Extension

Place the project in `extensions/sd-webui-infinite-image-browsing` directory and start with SD WebUI.

API Base URL: `http://localhost:7860/infinite_image_browsing`

### Method 3: Python Code Integration

```python
from app import launch_app, AppUtils
from fastapi import FastAPI

# Option A: Direct launch
launch_app(port=8000, extra_paths=["/my/images"], allow_cors=True)

# Option B: Mount to existing FastAPI app
app = FastAPI()
app_utils = AppUtils(extra_paths=["/my/images"], allow_cors=True)
app_utils.wrap_app(app)

# Option C: Async launch for Jupyter Notebook
import asyncio
await async_launch_app(port=8000, extra_paths=["/my/images"])
```

### Environment Variables

```bash
# Authentication key (optional, enables API authentication)
export IIB_SECRET_KEY="your_secret_key"

# AI features configuration (required for clustering, smart organization)
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://api.openai.com/v1"  # or compatible endpoint
export AI_MODEL="gpt-4o-mini"
export EMBEDDING_MODEL="text-embedding-3-small"

# Access control
export IIB_ACCESS_CONTROL_ALLOWED_PATHS="/path1,/path2"
export IIB_ACCESS_CONTROL_PERMISSION="read-write"  # read-only | read-write | write-only
```

---

## Core Feature: Image Search

IIB provides multiple image search methods - this is its core capability.

> **Note:** The examples below use Python for illustration, but you can use any language (Node.js, Go, Rust, etc.) that supports HTTP requests. The API is language-agnostic REST.

### 1. Substring Search (Fuzzy Search)

Search images by text in file path or generation parameters.

```python
import requests

BASE_URL = "http://localhost:8000/infinite_image_browsing"

# Search images containing "landscape"
resp = requests.post(f"{BASE_URL}/db/search_by_substr", json={
    "surstr": "landscape",      # Search keyword
    "cursor": "",               # Pagination cursor, empty for first page
    "regexp": "",               # Regular expression (optional)
    "size": 100,                # Results per page
    "folder_paths": [],         # Limit to specific directories (optional)
    "media_type": "image"       # "all" | "image" | "video"
})

result = resp.json()
for file in result["files"]:
    print(file["fullpath"], file["size"])

# Pagination
if result["cursor"]["has_next"]:
    next_resp = requests.post(f"{BASE_URL}/db/search_by_substr", json={
        "surstr": "landscape",
        "cursor": result["cursor"]["next"],
        "regexp": "",
        "size": 100
    })
```

### 2. Regular Expression Search

Use regex for precise pattern matching.

```python
# Search images with filenames starting with numbers
resp = requests.post(f"{BASE_URL}/db/search_by_substr", json={
    "surstr": "",
    "cursor": "",
    "regexp": r"^\d+.*\.png$",  # Regex pattern
    "size": 100
})

# Search images with specific prompt format
resp = requests.post(f"{BASE_URL}/db/search_by_substr", json={
    "surstr": "",
    "cursor": "",
    "regexp": r"masterpiece.*1girl.*blue eyes",
    "size": 100
})
```

### 3. Tag-based Search

Search by custom tags with AND/OR/NOT logic.

```python
# First get all tags
tags_resp = requests.get(f"{BASE_URL}/db/basic_info")
all_tags = tags_resp.json()["tags"]
# tags format: [{"id": 1, "name": "favorites", "type": "custom"}, ...]

# Search: (tag_id=1 AND tag_id=2) OR tag_id=3, excluding tag_id=4
resp = requests.post(f"{BASE_URL}/db/match_images_by_tags", json={
    "and_tags": [1, 2],         # Must have all these tags
    "or_tags": [3],             # Have any of these
    "not_tags": [4],            # Exclude these tags
    "cursor": "",
    "size": 100,
    "folder_paths": [],         # Limit to directories (optional)
    "random_sort": False        # Random order
})
```

### 4. Directory Browsing

List files and subdirectories in a folder.

```python
# List directory contents
resp = requests.get(f"{BASE_URL}/files", params={
    "folder_path": "/path/to/images"
})

files = resp.json()["files"]
for f in files:
    if f["type"] == "dir":
        print(f"[DIR] {f['name']}")
    else:
        print(f"[FILE] {f['name']} - {f['size']}")
```

### 5. Random Images

Get random images from the database.

```python
resp = requests.get(f"{BASE_URL}/db/random_images")
random_images = resp.json()  # Returns 128 random images
```

### 6. AI Semantic Clustering

Cluster images by semantic similarity of generation parameters.

```python
# Start clustering job
start_resp = requests.post(f"{BASE_URL}/db/cluster_iib_output_job_start", json={
    "folder_paths": ["/path/to/images"],
    "threshold": 0.85,          # Similarity threshold
    "min_cluster_size": 3,      # Minimum cluster size
    "lang": "en",               # Title language
    "recursive": True           # Include subdirectories
})
job_id = start_resp.json()["job_id"]

# Poll for completion
import time
while True:
    status = requests.get(f"{BASE_URL}/db/cluster_iib_output_job_status",
                          params={"job_id": job_id}).json()
    if status.get("status") == "completed":
        clusters = status["result"]["clusters"]
        for c in clusters:
            print(f"Topic: {c['title']}, Count: {c['size']}")
            print(f"  Keywords: {c['keywords']}")
            print(f"  Files: {c['paths'][:3]}...")
        break
    time.sleep(2)
```

---

## Common Operations

### Batch Tagging

```python
# Create a tag
tag = requests.post(f"{BASE_URL}/db/add_custom_tag",
                    json={"tag_name": "favorites"}).json()

# Batch add tag to images
requests.post(f"{BASE_URL}/db/batch_update_image_tag", json={
    "img_paths": ["/path/to/img1.png", "/path/to/img2.png"],
    "action": "add",
    "tag_id": tag["id"]
})
```

### Get Image Generation Parameters

```python
# Single image
geninfo = requests.get(f"{BASE_URL}/image_geninfo",
                       params={"path": "/path/to/image.png"}).text

# Batch get
batch_info = requests.post(f"{BASE_URL}/image_geninfo_batch", json={
    "paths": ["/path/to/img1.png", "/path/to/img2.png"]
}).json()
```

### Smart File Organization

```python
# Start organization job
job = requests.post(f"{BASE_URL}/db/organize_files_start", json={
    "folder_paths": ["/messy/folder"],
    "dest_folder": "/organized/folder",
    "threshold": 0.85,
    "lang": "en"
}).json()

# Wait for completion then confirm
requests.post(f"{BASE_URL}/db/organize_files_confirm", json={
    "job_id": job["job_id"]
})
```

---

## Reference Documentation

See detailed API documentation: [references/api-reference.md](references/api-reference.md)
