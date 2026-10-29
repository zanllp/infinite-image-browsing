import { t } from '@/i18n'
import { isImageFile } from '@/util'
import { message } from 'ant-design-vue'
import { useWatchDocument } from 'vue3-ts-util'
import { useHookShareState, useEventListen } from '.'
import { closeImageFullscreenPreview } from '@/util/imagePreviewOperation'
import { isIndexInVisibleRange } from '@/util/scroller'

/**
 * 全屏查看
 * @param props
 * @returns
 */
export function usePreview (spec?: { loadNext?: () => void }) {
  const {
    previewIdx,
    eventEmitter,
    canLoadNext,
    previewing,
    sortedFiles: files,
    scroller,
    props
  } = useHookShareState().toRefs()
  const { state } = useHookShareState()
  let waitScrollTo = null as number | null
  const onPreviewVisibleChange = (v: boolean, lv: boolean) => {
    previewing.value = v
    if (waitScrollTo != null && !v && lv) {
      // 关闭预览时滚动过去
      scroller.value?.scrollToItem(waitScrollTo)
      waitScrollTo = null
    }
  }

  const scrollToIndex = (idx: number) => {
    const s = scroller.value
    if (!s || idx < 0) return
    if (!isIndexInVisibleRange(s, idx)) {
      s.scrollToItem(idx)
    } else {
      console.log('scrollToIndex already in view', idx, 's', s)
    }
  }

  const scrollToFileId = (fullpath: string) => {
    if (!fullpath) return
    const idx = files.value.findIndex(v => v.fullpath === fullpath)
    console.log('idx', {idx, files})
    if (idx >= 0) {
      scrollToIndex(idx)
    }
  }

  const loadNextIfNeeded = () => {
    if (canPreview('next')) {
      return
    }
    if (spec?.loadNext) {
      return spec.loadNext()
    }
    if (props.value.mode === 'walk') {
      if (canLoadNext.value) {
        message.info(t('loadingNextFolder'))
        eventEmitter.value.emit('loadNextDir', true) // 如果在全屏查看时外面scroller可能还停留在很久之前，使用全屏查看的索引
      }
    }
  }

  useWatchDocument('keydown', (e) => {
    if (previewing.value) {
      let next = previewIdx.value
      if (['ArrowDown', 'ArrowRight'].includes(e.key)) {
        next++
        while (files.value[next] && !isImageFile(files.value[next].name)) {
          next++
        }
      } else if (['ArrowUp', 'ArrowLeft'].includes(e.key)) {
        next--
        while (files.value[next] && !isImageFile(files.value[next].name)) {
          next--
        }
      }
      if (isImageFile(files.value[next]?.name) ?? '') {
        previewIdx.value = next
        const s = scroller.value
        if (s && !isIndexInVisibleRange(s, next)) {
          waitScrollTo = next // 关闭预览时滚动过去
        }
      }
      loadNextIfNeeded()
    }
  })
  const previewImgMove = (type: 'next' | 'prev') => {
    let next = previewIdx.value
    if (type === 'next') {
      next++
      while (files.value[next] && !isImageFile(files.value[next].name)) {
        next++
      }
    } else if (type === 'prev') {
      next--
      while (files.value[next] && !isImageFile(files.value[next].name)) {
        next--
      }
    }
    if (isImageFile(files.value[next]?.name) ?? '') {
      previewIdx.value = next
      const s = scroller.value
      if (s && !isIndexInVisibleRange(s, next)) {
        waitScrollTo = next // 关闭预览时滚动过去
      }
    }
    loadNextIfNeeded()
  }
  const canPreview = (type: 'next' | 'prev') => {
    let next = previewIdx.value
    if (type === 'next') {
      next++
      while (files.value[next] && !isImageFile(files.value[next].name)) {
        next++
      }
    } else if (type === 'prev') {
      next--
      while (files.value[next] && !isImageFile(files.value[next].name)) {
        next--
      }
    }
    return isImageFile(files.value[next]?.name)
  }

  useEventListen('removeFiles', async () => {
    if (previewing.value && !state.sortedFiles[previewIdx.value]) {
      closeImageFullscreenPreview()
    }
  })

  return {
    previewIdx,
    onPreviewVisibleChange,
    previewing,
    previewImgMove,
    canPreview,
    scrollToIndex,
    scrollToFileId
  }
}
