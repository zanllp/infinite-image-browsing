import { useElementSize } from '@vueuse/core'
import { ref, computed, watch, reactive } from 'vue'
import { Top4MediaInfo, batchGetDirTop4MediaInfo } from '@/api'
import {
  delay} from 'vue3-ts-util'
import { sortMethodConv } from '../fileSort'
import { debounce } from 'lodash-es'
import { isMediaFile } from '@/util/file'
import { useHookShareState, global, tagStore } from '.'
import { makeAsyncFunctionSingle, useGlobalEventListen } from '@/util'

export function useFilesDisplay ({ fetchNext }: {fetchNext?: () => Promise<any>} = {  }) {
  const {
    scroller,
    sortedFiles,
    sortMethod,
    currLocation,
    currPage,
    stackViewEl,
    canLoadNext,
    previewIdx,
    props,
    walker,
    getViewableAreaFiles
  } = useHookShareState().toRefs()
  const { state } = useHookShareState()
  const moreActionsDropdownShow = ref(false)
  const cellWidth = ref(global.defaultGridCellWidth)
  const gridSize = computed(() => cellWidth.value + 16) // margin 8
  const profileHeight = 44
  const { width } = useElementSize(stackViewEl)
  const gridItems = computed(() => ~~(width.value / gridSize.value))
  const dirCoverCache = reactive(new Map<string, Top4MediaInfo[]>())

  /**
   * 只补拉当前可视区的图片标签。
   * 标签是跟着可视区懒加载的，缓存被清空（后台静默维护完成一轮 / 手动更新索引）后
   * 如果只等滚动来触发补拉，用户看到的就是「标签丢了，滚一下才回来」。
   */
  const fetchViewableAreaTags = () => {
    const paths = getViewableAreaFiles.value()
      .filter((v) => v.is_under_scanned_path && isMediaFile(v.name))
      .map((v) => v.fullpath)
    if (paths.length) {
      tagStore.fetchImageTags(paths)
    }
  }

  const itemSize = computed(() => {
    const second = gridSize.value
    const first = second + (cellWidth.value <= 160 ? 0 : profileHeight)

    return {
      first,
      second
    }
  })

  const loadNextDirLoading = ref(false)

  const loadNextDir = async () => {
    if (loadNextDirLoading.value || props.value.mode !== 'walk' || !canLoadNext.value) {
      return
    }
    try {
      loadNextDirLoading.value = true
      await walker.value?.next()
    } finally {
      loadNextDirLoading.value = false
    }
  }

  // 填充够一页，直到不行为止
  const fetchDataUntilViewFilled = async (isFullScreenPreview = false) => {
    const s = scroller.value
    const currIdx = () => (isFullScreenPreview ? previewIdx.value : s?.$_endIndex ?? 0)
    const needLoad = () => {
      const len = sortedFiles.value.length
      const preload = 50
      if (!len) {
        return true
      }
      if (fetchNext) {
        return currIdx() > len - preload
      }
      return currIdx() > len - preload && canLoadNext.value // canLoadNext 是walker的，表示加载完成
    }
    while (needLoad()) {
      await delay(30)
      const ret = await (fetchNext ?? loadNextDir)()
      if (typeof ret === 'boolean' && !ret) {
        return // 返回false同样表示加载完成
      }
    }
  }

  state.useEventListen('loadNextDir', makeAsyncFunctionSingle(async (isFullScreenPreview = false) => {
    await fetchDataUntilViewFilled(isFullScreenPreview)
    if (props.value.mode === 'walk') {
      onViewableAreaChangeDebounced()
    }
  }))

  state.useEventListen('viewableAreaFilesChange', () => {
    fetchViewableAreaTags()
    const files = getViewableAreaFiles.value()
    const fetchDirTop4MediaPaths = files
      .filter(v => v.is_under_scanned_path && v.type === 'dir' && !dirCoverCache.has(v.fullpath))
      .map(v => v.fullpath)
    if (fetchDirTop4MediaPaths.length) {
      batchGetDirTop4MediaInfo(fetchDirTop4MediaPaths).then(v => {
        for (const key in v) {
          if (Object.prototype.hasOwnProperty.call(v, key)) {
            const element = v[key];
            dirCoverCache.set(key, element)
          }
        }
      })
    }
  })

  // 切回 IIB 页（标签缓存可能在离开期间被后台维护清空）或缓存刚被清空时，
  // 立即补拉一次可视区标签，避免要等用户滚动才恢复
  useGlobalEventListen('returnToIIB', fetchViewableAreaTags)
  useGlobalEventListen('tagsInvalidated', fetchViewableAreaTags)

  state.useEventListen('refresh', async () => {
    state.eventEmitter.emit('viewableAreaFilesChange')
  })

  const onViewableAreaChangeDebounced = debounce(() => state.eventEmitter.emit('viewableAreaFilesChange'), 300)
  watch(currLocation, onViewableAreaChangeDebounced)

  const onScroll = debounce(async () => {
    const s = scroller.value
    if (s && currPage.value) {
      currPage.value.scrollIndex = s.$_startIndex
    }
    await fetchDataUntilViewFilled()
    onViewableAreaChangeDebounced()
  }, 150)

  return {
    gridItems,
    sortedFiles,
    sortMethodConv,
    moreActionsDropdownShow,
    gridSize,
    sortMethod,
    onScroll,
    loadNextDir,
    loadNextDirLoading,
    canLoadNext,
    itemSize,
    cellWidth,
    dirCoverCache
  }
}
