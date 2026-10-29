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
import { getVisibleRange } from '@/util/scroller'
import { nearestGridCellLevel } from '@/util/gridCellLevel'

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
  /** 目标格宽：来自用户选的档位（历史配置里存的精确宽会被吸附到最近档位） */
  const cellTarget = ref(nearestGridCellLevel(global.defaultGridCellWidth))
  /** 卡片左右 margin 各 8px */
  const CELL_GAP = 16
  const profileHeight = 44
  const { width } = useElementSize(stackViewEl)
  /**
   * 宽度必须按 scroller 自己的「内容盒宽度」算。
   *
   * 外层 .container 和被测量的 .file-list 之间差着两截：padding(8px×2) 和
   * 竖向滚动条(~15px)。用外层宽度算会稳定多算一列，而 v3 会给 item-wrapper
   * 设 minWidth = 列数 × 每格宽，于是最后一列从 v2 时代的「被 overflow 裁掉」
   * 变成「撑出包装层 → 横向可滚 + 副轴虚拟化 + 缺口自愈被关闭」。
   * ResizeObserver 的 contentRect 已经把 padding 和滚动条扣掉了。
   */
  const scrollerEl = computed<HTMLElement | undefined>(() => {
    const s = scroller.value as any
    const raw = s?.el ?? s?.$el
    // 兼容「expose 出来的是 ref 还是已解包元素」两种情况
    const el = raw && typeof raw === 'object' && 'value' in raw ? raw.value : raw
    return el?.nodeType === 1 ? el as HTMLElement : undefined
  })
  const { width: scrollerWidth } = useElementSize(scrollerEl)

  /** 整行可用宽度：优先用 scroller 内容盒（已扣 padding 与滚动条），挂载前退化成外层宽 - padding */
  const availableWidth = computed(() => scrollerWidth.value || Math.max(width.value - 16, 0))

  /**
   * 列数 auto-fit：取「实际格宽最接近目标档位」的列数，保证整行占满。
   * 例：可用宽 1334、目标 256 → round(1334 / 272) = 5 列（旧行为是 4 列 + 右侧 246px 空白）
   */
  const gridItems = computed(() => {
    const w = availableWidth.value
    if (!w) return 1
    return Math.max(1, Math.round(w / (cellTarget.value + CELL_GAP)))
  })

  /**
   * 列步长：整除铺满整行，右侧余量 < 列数 px（5 列时 <5px，肉眼不可见）。
   * 用整数而不是浮点，避免 transform 亚像素导致缩略图被重采样发虚。
   */
  const gridSize = computed(() => {
    const w = availableWidth.value
    if (!w) return cellTarget.value + CELL_GAP
    return Math.max(CELL_GAP + 1, Math.floor(w / gridItems.value))
  })

  /** 实际格宽：由窗口宽度算出来，不再由用户直接给值 */
  const cellWidth = computed(() => gridSize.value - CELL_GAP)
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
    const currIdx = () => (isFullScreenPreview ? previewIdx.value : (s ? getVisibleRange(s).end : 0))
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
      currPage.value.scrollIndex = getVisibleRange(s).start
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
    cellTarget,
    dirCoverCache
  }
}
