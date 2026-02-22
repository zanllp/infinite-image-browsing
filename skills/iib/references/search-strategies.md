# Search Strategies

For complex search requests, use these strategies to find the images users need.

## Multi-Word Searches

**Important:** When users search with multiple words (e.g., "太空电梯 三体", "sunset beach"), these words are likely **NOT concatenated together** in the generation parameters.

### 1. Regex OR Pattern (Recommended)

Use regex OR pattern to search for images containing either word:

```bash
# Chinese: 太空电梯 OR 三体
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{"regexp": "(太空电梯|三体)", "size": 50}'

# English: sunset OR beach
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"regexp": "(sunset|beach)", "size": 50}'

# Generate URL with regex mode
?action=pane&type=fuzzy-search&props=%7B%22substr%22%3A%22(%E5%A4%AA%E7%A9%BA%E7%94%B5%E6%A2%AF%7C%E4%B8%89%E4%BD%93)%22%2C%22isRegex%22%3Atrue%7D
```

### 2. Multiple Sequential Searches

Search for each condition separately, then cross-reference results:

```bash
# First search (太空电梯)
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"surstr": "太空电梯", "size": 200}'

# Second search (三体)
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"surstr": "三体", "size": 200}'

# Compare and find overlapping results
```

### 3. Tag Combination Search (Most Flexible)

Combine multiple tags with AND/OR/NOT logic:

```bash
# AND: Images with ALL these tags
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/match_images_by_tags \
  -d '{"and_tags": [1, 2, 3], "size": 50}'

# OR: Images with ANY of these tags
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/match_images_by_tags \
  -d '{"and_tags": [], "or_tags": [1, 2, 3], "size": 50}'

# NOT: Images WITHOUT these tags
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/match_images_by_tags \
  -d '{"and_tags": [1], "not_tags": [5, 6], "size": 50}'

# Combined: (tag1 AND tag2) OR (tag3) NOT (tag4)
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/match_images_by_tags \
  -d '{"and_tags": [1, 2], "or_tags": [3], "not_tags": [4], "size": 50}'
```

## Complex Pattern Matching

Use regex for advanced patterns:

```bash
# Sequential patterns (AND logic)
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"regexp": "masterpiece.*best.*quality", "size": 50}'

# Multiple patterns (OR logic)
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"regexp": "(sunset|sunrise|dawn)", "size": 50}'

# Negative patterns (NOT logic) - exclude monochrome and sketch
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -d '{"regexp": "landscape", "size": 50}'
```

## Combining Filters

You can combine multiple filters in a single search:

```bash
# Regex OR + Folder restriction + Media type filter
curl -X POST http://127.0.0.1:<port>/infinite_image_browsing/db/search_by_substr \
  -H "Content-Type: application/json" \
  -d '{
    "regexp": "(sunset|beach)",
    "folder_paths": ["/path/to/folder"],
    "media_type": "image",
    "size": 50
  }'
```

## URL Encoding Tips

When generating URLs for users to open in their browser:

1. **JSON Props** must be URL-encoded
2. **Non-ASCII characters** (Chinese, Japanese, etc.) need UTF-8 percent encoding

| JSON Props | URL Encoded |
|------------|-------------|
| `{"substr":"sunset"}` | `%7B%22substr%22%3A%22sunset%22%7D` |
| `{"substr":"(词1|词2)","isRegex":true}` | `%7B%22substr%22%3A%22(%E8%AF%8D1%7C%E8%AF%8D2)%22%2C%22isRegex%22%3Atrue%7D` |
| `{"substr":"a|b","isRegex":true}` | `%7B%22substr%22%3A%22(a%7Cb)%22%2C%22isRegex%22%3Atrue%7D` |
