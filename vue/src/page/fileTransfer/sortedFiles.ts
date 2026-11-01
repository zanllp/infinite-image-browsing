import type { FileNodeInfo } from '@/api/files'
import { isAudioFile, isImageFile, isVideoFile } from '@/util/file'
import { sortFiles, type SortMethod } from './fileSort'

/**
 * 计算文件列表在界面上真正的展示顺序：类型过滤 + 排序 + 本地已删除过滤。
 *
 * `index.tsx` 里的 `sortedFiles` 和 `useLocation` 里「更新后预览组件是否还能存活」
 * 的判断共用这一份逻辑，避免两边算法漂移导致误判。
 */
export const computeSortedFiles = (
  files: FileNodeInfo[],
  method: SortMethod,
  filterTypes: string[],
  deleted: Set<string>
) => {
  let filtered = files
  // 如果选择了 'all' 或没有选择任何类型，显示所有文件
  if (!filterTypes.includes('all') && filterTypes.length > 0) {
    filtered = files.filter((file) => {
      if (file.type === 'dir') return true
      if (filterTypes.includes('image') && isImageFile(file.name)) return true
      if (filterTypes.includes('video') && isVideoFile(file.name)) return true
      if (filterTypes.includes('audio') && isAudioFile(file.name)) return true
      return false
    })
  }
  return sortFiles(filtered, method).filter((v) => !deleted.has(v.fullpath))
}
