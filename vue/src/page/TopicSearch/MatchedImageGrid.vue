<script lang="ts" setup>
import fileItemCell from '@/components/FileItem.vue'
import '@zanllp/vue-virtual-scroller/dist/vue-virtual-scroller.css'
// @ts-ignore
import { RecycleScroller } from '@zanllp/vue-virtual-scroller'
import { toImageUrl } from '@/util/file'
import { nextTick, watch, reactive } from 'vue'
import { copy2clipboardI18n } from '@/util'
import fullScreenContextMenu from '@/page/fileTransfer/fullScreenContextMenu.vue'
import { LeftCircleOutlined, RightCircleOutlined } from '@/icon'
import { useImageSearch } from '@/page/TagSearch/hook'
import { useGlobalStore } from '@/store/useGlobalStore'
import { useKeepMultiSelect } from '@/page/fileTransfer/hook'
import { openTiktokViewWithFiles } from '@/util/tiktokHelper'
import { batchGetFilesInfo } from '@/api/files'

const props = defineProps<{
  tabIdx: number
  paneIdx: number
  id: string
  title: string
  paths: string[]
}>()

type Iter = {
  res: any[]
  load: boolean
  loading: boolean
  next: () => Promise<void>
  reset: (opts?: { refetch?: boolean }) => Promise<void>
}

const iter = reactive<Iter>({
  res: [],
  load: false,
  loading: false,
  async next() {
    if (iter.loading || iter.load) return
    iter.loading = true
    try {
      const pageSize = 200
      const offset = iter.res.length
      const slice = (props.paths ?? []).slice(offset, offset + pageSize)
      if (!slice.length) {
        iter.load = true
        return
      }
      const infoMap = await batchGetFilesInfo(slice)
      const files = slice.map((p) => infoMap[p]).filter(Boolean)
      iter.res.push(...files)
      if (offset + pageSize >= (props.paths?.length ?? 0)) {
        iter.load = true
      }
    } finally {
      iter.loading = false
    }
  },
  async reset() {
    iter.res = []
    iter.load = false
    await iter.next()
  }
})

const {
  queue,
  images,
  onContextMenuClickU,
  stackViewEl,
  previewIdx,
  previewing,
  onPreviewVisibleChange,
  previewImgMove,
  canPreview,
  itemSize,
  gridItems,
  showGenInfo,
  imageGenInfo,
  q: genInfoQueue,
  multiSelectedIdxs,
  onFileItemClick,
  scroller,
  showMenuIdx,
  onFileDragStart,
  onFileDragEnd,
  cellWidth,
  onScroll,
  saveAllFileAsJson,
  saveLoadedFileAsJson,
  changeIndchecked,
  seedChangeChecked,
  getGenDiff,
  getGenDiffWatchDep
} = useImageSearch(iter as any)

watch(
  () => props.paths,
  async () => {
    await iter.reset({ refetch: true } as any)
    await nextTick()
    scroller.value?.scrollToItem(0)
    onScroll()
  },
  { immediate: true }
)

const g = useGlobalStore()
const { onClearAllSelected, onSelectAll, onReverseSelect } = useKeepMultiSelect()

const onTiktokViewClick = () => {
  if (images.value.length === 0) return
  openTiktokViewWithFiles(images.value, 0)
}
</script>

<template>
  <div class="container" ref="stackViewEl">
    <MultiSelectKeep
      :show="!!multiSelectedIdxs.length || g.keepMultiSelect"
      @clear-all-selected="onClearAllSelected"
      @select-all="onSelectAll"
      @reverse-select="onReverseSelect"
    />
    <ASpin size="large" :spinning="!queue.isIdle || iter.loading">
      <AModal v-model:visible="showGenInfo" width="70vw" mask-closable @ok="showGenInfo = false">
        <template #cancelText />
        <ASkeleton active :loading="!genInfoQueue.isIdle">
          <div
            style="
              width: 100%;
              word-break: break-all;
              white-space: pre-line;
              max-height: 70vh;
              overflow: auto;
            "
            @dblclick="copy2clipboardI18n(imageGenInfo)"
          >
            <div class="hint">{{ $t('doubleClickToCopy') }}</div>
            {{ imageGenInfo }}
          </div>
        </ASkeleton>
      </AModal>

      <div class="action-bar">
        <div class="title line-clamp-1">🧩 {{ props.title }}</div>
        <div flex-placeholder />
        <a-button @click="onTiktokViewClick" :disabled="!images?.length">{{ $t('tiktokView') }}</a-button>
        <a-button @click="saveLoadedFileAsJson" :disabled="!images?.length">{{ $t('saveLoadedImageAsJson') }}</a-button>
        <a-button @click="saveAllFileAsJson" :disabled="!images?.length">{{ $t('saveAllAsJson') }}</a-button>
      </div>

      <RecycleScroller
        ref="scroller"
        class="file-list"
        v-if="images?.length"
        :items="images"
        :item-size="itemSize.first"
        key-field="fullpath"
        :item-secondary-size="itemSize.second"
        :gridItems="gridItems"
        @scroll="onScroll"
      >
        <template #after>
          <div style="padding: 16px 0 512px;" />
        </template>
        <template v-slot="{ item: file, index: idx }">
          <file-item-cell
            :idx="idx"
            :file="file"
            :cell-width="cellWidth"
            v-model:show-menu-idx="showMenuIdx"
            @dragstart="onFileDragStart"
            @dragend="onFileDragEnd"
            @file-item-click="onFileItemClick"
            @tiktok-view="(_file, idx) => openTiktokViewWithFiles(images, idx)"
            :full-screen-preview-image-url="images[previewIdx] ? toImageUrl(images[previewIdx]) : ''"
            :selected="multiSelectedIdxs.includes(idx)"
            @context-menu-click="onContextMenuClickU"
            @preview-visible-change="onPreviewVisibleChange"
            :is-selected-mutil-files="multiSelectedIdxs.length > 1"
            :enable-change-indicator="changeIndchecked"
            :seed-change-checked="seedChangeChecked"
            :get-gen-diff="getGenDiff"
            :get-gen-diff-watch-dep="getGenDiffWatchDep"
          />
        </template>
      </RecycleScroller>

      <div v-else class="no-res-hint">
        <p class="hint">暂无结果</p>
      </div>

      <div v-if="previewing" class="preview-switch">
        <LeftCircleOutlined @click="previewImgMove('prev')" :class="{ disable: !canPreview('prev') }" />
        <RightCircleOutlined @click="previewImgMove('next')" :class="{ disable: !canPreview('next') }" />
      </div>
    </ASpin>

    <fullScreenContextMenu
      v-if="previewing && images && images[previewIdx]"
      :file="images[previewIdx]"
      :idx="previewIdx"
      @context-menu-click="onContextMenuClickU"
    />
  </div>
</template>

<style scoped lang="scss">
.container {
  background: var(--zp-secondary-background);
  position: relative;
  height: var(--pane-max-height);
}

.action-bar {
  display: flex;
  align-items: center;
  user-select: none;
  gap: 6px;
  padding: 6px 8px;
}

.title {
  font-weight: 700;
  max-width: 40vw;
}

.file-list {
  list-style: none;
  padding: 8px;
  overflow: auto;
  height: calc(var(--pane-max-height) - 44px);
  width: 100%;
}

.no-res-hint {
  height: calc(var(--pane-max-height) - 44px);
  display: flex;
  align-items: center;
  flex-direction: column;
  justify-content: center;
  .hint {
    font-size: 1.2em;
    opacity: 0.7;
  }
}

.preview-switch {
  position: fixed;
  bottom: 24px;
  right: 24px;
  display: flex;
  gap: 8px;
  font-size: 36px;
  user-select: none;
}

.disable {
  opacity: 0.3;
  pointer-events: none;
}
</style>


