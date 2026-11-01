type CloseHandler = () => void

/**
 * 全屏预览现在是应用级组件（`FullscreenPreview.vue`），不再依赖 DOM 里的
 * `.ant-image-preview-wrap` / `.ant-image-img` 去模拟点击。
 *
 * 这里只保留一个「关闭所有全屏预览」的注册表：每个持有预览状态的 pane 挂载时注册自己的
 * 关闭函数，关闭时对所有 pane 生效（只有一个 pane 的预览是开着的，其余是空操作）。
 */
const closers = new Set<CloseHandler>()

export const registerFullscreenPreviewCloser = (closer: CloseHandler) => {
  closers.add(closer)
  return () => {
    closers.delete(closer)
  }
}

export const closeImageFullscreenPreview = () => {
  closers.forEach((close) => close())
}
