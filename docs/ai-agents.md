# Using IIB with AI Agents (Claude Code, Cursor, OpenClaw, etc.)

IIB can be used as an [Agent Skill](https://agentskills.io), allowing AI agents to search, browse, tag, and organize your images through natural language.

## Installation

Install the IIB skill for your AI agent:

```bash
npx skills add https://github.com/zanllp/infinite-image-browsing --skill iib
```

## Usage

### Step 1: Start IIB Service

First, start the IIB service:

```bash
python app.py --port <port>
```

### Step 2: Interact with Your AI Agent

Once IIB is running, you can ask your AI agent to help with various image tasks:

**Search & Filter:**
- "Find all images with 'sunset' in the prompt"
- "Show me images generated with model X"
- "Find images tagged as 'favorites'"
- "Find images with people but exclude drafts"
- "Search for high quality landscape images"

**Tag Management:**
- "Tag these images as 'best quality'"
- "Remove the 'test' tag from all images"
- "Add the 'portrait' tag to images with people"

**File Organization:**
- "Organize my Downloads folder by theme"
- "Move all landscape images to a separate folder"
- "Create folders for different art styles"

**Information Retrieval:**
- "Show me the generation parameters of this image"
- "What prompts were used for these images?"
- "Compare the settings between these two images"


### Available View Types

| View Type | Description | URL Example |
|-----------|-------------|-------------|
| **Quick View** | Fullscreen preview of single image | `?action=view&path=/path/to/img.png` |
| **Keyword Search** | Search page for keyword filtering | `?action=pane&type=fuzzy-search` |
| **Keyword Search (Pre-filled)** | Search with auto-filled keyword | `?action=pane&type=fuzzy-search&props={"substr":"sunset"}` |
| **Tag Search** | Filter by tags | `?action=pane&type=tag-search` |
| **Tag Results** | Pre-filtered tag search results | `?action=pane&type=tag-search-matched-image-grid&props=...` |
| **Image Compare** | Side-by-side comparison | `?action=pane&type=img-sli&props=...` |
| **Folder Browse** | Browse specific folder | `?action=pane&type=local&props=...` |
| **Random Images** | Display random images | `?action=pane&type=random-image` |

**Example prompts:**
- "Search for images with 'sunset' in the prompt"
- "Show all images tagged as favorites"
- "Compare these two images in IIB"

## Supported AI Agents

IIB's skill is compatible with:
- **Claude Code** - Anthropic's official CLI for Claude
- **Cursor** - AI code editor
- **OpenClaw** - AI coding assistant
- Any other agent that supports the [Agent Skills](https://agentskills.io) format

## Requirements

- Python 3.7+
- IIB service running on an accessible port
- AI agent with Agent Skills support

## Troubleshooting

If the agent cannot connect to IIB:
1. Ensure IIB service is running (`python app.py --port <port>`)
2. Check that the port matches the one configured in your agent
3. Verify network/firewall settings allow local connections
