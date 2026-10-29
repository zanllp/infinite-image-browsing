import { useGlobalStore, type FileTransferTabPane  } from '@/store/useGlobalStore'
import { useImgSliStore } from '@/store/useImgSli'
import { onLongPress } from '@vueuse/core'
import { ref, computed, watch, reactive } from 'vue'
import {
  createTypedShareStateHook,
  delay,
  typedEventEmitter
} from 'vue3-ts-util'
import { type FileNodeInfo } from '@/api/files'
import { sortFiles } from '../fileSort'
import { last, range } from 'lodash-es'
import * as Path from '@/util/path'
import { isImageFile, isVideoFile, isAudioFile } from '@/util/file'
import { useTagStore } from '@/store/useTagStore'
import { useBatchDownloadStore } from '@/store/useBatchDownloadStore'
import { Walker } from '../walker'
import { getVisibleRange } from '@/util/scroller'

export const stackCache = new Map<string, Page[]>()

export const global = useGlobalStore()
export const batchDownload = useBatchDownloadStore()
export const tagStore = useTagStore()
export const sli = useImgSliStore()
export const imgTransferBus = new BroadcastChannel('iib-image-transfer-bus')
export const { eventEmitter: events, useEventListen } = typedEventEmitter<{
  removeFiles (_: { paths: string[]; loc: string }): void
  addFiles (_: { files: FileNodeInfo[]; loc: string }): void
}>()



export * from './useLocation'
export * from './usePreview'
export * from './useFilesDisplay'
export * from './useFileTransfer'
export * from './useFileItemActions'
export * from './useGenInfoDiff'



export interface Scroller {
  /**
   * vue-virtual-scroller v3 暴露的「已分配 view」列表（按 nr.index 排序）。
   * 用于推算当前渲染窗口范围，替代 v2 时代的内部字段 $_startIndex / $_endIndex。
   */
  visiblePool?: Array<{ nr: { index: number, used: boolean } }>
  /** v3 expose 出来的根元素（用于实测内容盒宽度算列数） */
  el?: HTMLElement
  scrollToItem (idx: number): void
}

export const { useHookShareState } = createTypedShareStateHook(
  (_inst, { images }) => {
    const props = ref<Props>({ tabIdx: -1, paneIdx: -1 })
    const currPage = computed(() => last(stack.value))
    const stack = ref<Page[]>([])
    const basePath = computed(() =>
      stack.value.map((v) => v.curr).slice(global.conf?.is_win && props.value.mode !== 'scanned-fixed' ? 1 : 0)
    )
    const currLocationScannedOnlyAvailable = computed(() => Path.join(...basePath.value))
    const currLocation = computed(() => {
      if(props.value.mode === 'scanned-fixed') return stack.value?.[0]?.curr ?? ''
      if(props.value.mode === 'walk') return props.value.path ?? ''
      return stack.value.length === 1  ? '/' : currLocationScannedOnlyAvailable.value
    })
    const sortMethod = ref(global.defaultSortingMethod)
    const walker = ref(props.value.mode == 'walk' ? new Walker(props.value.path!, sortMethod.value) : undefined)
    watch([() => props.value.mode, () => props.value.path, sortMethod], async ([mode, path, method]) => {
      if (mode === 'walk') {
        walker.value = new Walker(path!, method)
        stack.value = [{ files: [], curr: path!  }]
        await delay()
        await walker.value?.reset()
        events.eventEmitter.emit('loadNextDir')
      } else {
        walker.value = undefined
      }
    })

    const deletedFiles = reactive(new Set<string>())
    watch(currPage, () => deletedFiles.clear())
    const sortedFiles = computed(() => {
      if (images.value) {
        return images.value
      }

      if (walker.value) {
        return walker.value.images.filter(v => !deletedFiles.has(v.fullpath))
      }
      if (!currPage.value) {
        return []
      }
      const files = currPage.value?.files ?? []
      const method = sortMethod.value
      const filter = (files: FileNodeInfo[]) => {
        const filterTypes = global.fileTypeFilter
        // 如果选择了 'all'，显示所有文件
        if (filterTypes.includes('all')) {
          return files
        }
        // 如果没有选择任何类型，显示所有文件
        if (filterTypes.length === 0) {
          return files
        }
        // 根据选择的类型过滤
        return files.filter((file) => {
          if (file.type === 'dir') return true // 始终显示文件夹
          if (filterTypes.includes('image') && isImageFile(file.name)) return true
          if (filterTypes.includes('video') && isVideoFile(file.name)) return true
          if (filterTypes.includes('audio') && isAudioFile(file.name)) return true
          return false
        })
      }
      return sortFiles(filter(files), method).filter(v => !deletedFiles.has(v.fullpath))
    })
    const multiSelectedIdxs = ref([] as number[])
    const previewIdx = ref(-1)

    const canLoadNext = computed(() => walker.value ? !walker.value.isCompleted : false)

    const spinning = ref(false)

    const previewing = ref(false)

    const scroller = ref<Scroller>()

    const getPane = () => {
      return global.tabList?.[props.value.tabIdx]?.panes?.[props.value.paneIdx] as FileTransferTabPane
    }

    const events = typedEventEmitter<{
      loadNextDir (isFullscreenPreview?: boolean): Promise<void>
      refresh (): Promise<void>
      selectAll (): void
      viewableAreaFilesChange(): void
      navigateUp (): void
    }>()
    events.useEventListen('selectAll', () => {
      console.log(`select all 0 -> ${sortedFiles.value.length}`)
      multiSelectedIdxs.value = range(0, sortedFiles.value.length)
    })

    const getViewableAreaFiles = () => {
      const s = scroller.value
      if (s) {
        const { start, end } = getVisibleRange(s)
        const startIdx = Math.max(start - 10, 0)
        return sortedFiles.value.slice(startIdx, end + 10)
      }
      return []
    }
    
    return {
      previewing,
      spinning,
      canLoadNext,
      multiSelectedIdxs,
      previewIdx,
      basePath,
      currLocation,
      currPage,
      stack,
      sortMethod,
      sortedFiles,
      scroller,
      stackViewEl: ref<HTMLDivElement>(),
      props,
      getPane,
      walker,
      deletedFiles,
      getViewableAreaFiles,
      ...events
    }
  },
  () => ({ images: ref<FileNodeInfo[]>() })
)

export interface Props {
  tabIdx: number
  paneIdx: number
  path?: string
  mode?: 'walk' | 'scanned' | 'scanned-fixed'
  fixed?: boolean
}

export interface Page {
  files: FileNodeInfo[]
  curr: string
  scrollIndex?: number
}


export function useKeepMultiSelect () {
  const { eventEmitter, multiSelectedIdxs, sortedFiles } = useHookShareState().toRefs()
  const onSelectAll = () => eventEmitter.value.emit('selectAll')
  const onReverseSelect = () => {
    multiSelectedIdxs.value = sortedFiles.value.map((_, idx) => idx)
      .filter(v => !multiSelectedIdxs.value.includes(v))
  }
  const onClearAllSelected = () => {
    multiSelectedIdxs.value = []
  }
  return {
    onSelectAll,
    onReverseSelect,
    onClearAllSelected
  }
}






/**
 * 针对移动端端操作优化，使用长按提到右键
 */
export const useMobileOptimization = () => {
  const { stackViewEl } = useHookShareState().toRefs()
  const showMenuIdx = ref(-1)
  onLongPress(stackViewEl, (e) => {
    let fileEl = e.target as HTMLDivElement
    while (fileEl.parentElement) {
      fileEl = fileEl.parentElement as any
      if (fileEl.tagName.toLowerCase() === 'li' && fileEl.classList.contains('file-item-trigger')) {
        const idx = fileEl.dataset?.idx
        if (idx && Number.isSafeInteger(+idx)) {
          showMenuIdx.value = +idx
        }
        return
      }
    }
  })
  return {
    showMenuIdx
  }
}
