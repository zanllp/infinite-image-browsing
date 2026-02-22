# 与 AI 助手一起使用 IIB（Claude Code、Cursor、OpenClaw 等）

IIB 可以作为 [Agent Skill](https://agentskills.io) 使用，让 AI 助手通过自然语言来搜索、浏览、标签化和整理你的图片。

## 安装

为你的 AI 助手安装 IIB 技能：

```bash
npx skills add https://github.com/zanllp/infinite-image-browsing --skill iib
```

## 使用方法

### 第一步：启动 IIB 服务

首先，启动 IIB 服务：

```bash
python app.py --port <port>
```

### 第二步：与 AI 助手交互

IIB 运行后，你可以让 AI 助手帮你完成各种图片任务：

**搜索与筛选：**
- "查找所有提示词中包含 'sunset' 的图片"
- "显示使用模型 X 生成的图片"
- "查找标记为 '收藏' 的图片"
- "查找包含人物但不是草稿的图片"
- "搜索质量高且包含风景的图片"

**标签管理：**
- "将这些图片标记为 '高质量'"
- "从所有图片中移除 'test' 标签"
- "为有人物的图片添加 'portrait' 标签"

**文件整理：**
- "按主题整理我的下载文件夹"
- "将所有风景图片移到单独的文件夹"
- "为不同艺术风格创建文件夹"

**信息获取：**
- "显示这张图片的生成参数"
- "这些图片使用了什么提示词？"
- "比较这两张图片的设置"




### 可用的视图类型

| 视图类型 | 描述 | URL 示例 |
|----------|------|----------|
| **快速查看单张图片** | 全屏预览指定图片 | `?action=view&path=/path/to/img.png` |
| **关键词搜索页** | 搜索页面入口 | `?action=pane&type=fuzzy-search` |
| **关键词搜索（预填充）** | 自动填充关键词并搜索 | `?action=pane&type=fuzzy-search&props={"substr":"sunset"}` |
| **标签搜索页** | 按标签筛选 | `?action=pane&type=tag-search` |
| **标签搜索结果** | 预设标签的搜索结果 | `?action=pane&type=tag-search-matched-image-grid&props=...` |
| **图片对比** | 并排对比两张图 | `?action=pane&type=img-sli&props=...` |
| **文件夹浏览** | 浏览指定文件夹 | `?action=pane&type=local&props=...` |
| **随机图片** | 随机展示图片 | `?action=pane&type=random-image` |

**示例对话：**
- "搜索包含 'sunset' 的图片"
- "显示我标记为收藏的所有图片"
- "在 IIB 中对比这两张图片"
## 支持的 AI 助手

IIB 的技能兼容以下平台：
- **Claude Code** - Anthropic 的官方 Claude CLI
- **Cursor** - AI 代码编辑器
- **OpenClaw** - AI 编程助手
- 任何其他支持 [Agent Skills](https://agentskills.io) 格式的助手

## 系统要求

- Python 3.7+
- IIB 服务运行在可访问的端口上
- 支持 Agent Skills 的 AI 助手

## 故障排查

如果 AI 助手无法连接到 IIB：
1. 确保 IIB 服务正在运行（`python app.py --port <port>`）
2. 检查端口是否与助手配置的端口匹配
3. 验证网络/防火墙设置允许本地连接
