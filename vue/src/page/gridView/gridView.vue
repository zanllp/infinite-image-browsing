<script lang="ts" setup>
// @ts-ignore
import { RecycleScroller } from '@zanllp/vue-virtual-scroller'
import '@zanllp/vue-virtual-scroller/dist/vue-virtual-scroller.css'
import FileItem from '@/components/FileItem.vue'
import { useFilesDisplay, useHookShareState } from '@/page/fileTransfer/hook'
import { getFileTransferDataFromDragEvent, toImageUrl, uniqueFile } from '@/util/file'
import { ref, watchEffect, toRaw } from 'vue'
import { GridViewFile, useGlobalStore } from '@/store/useGlobalStore'
import { useTagStore } from '@/store/useTagStore'

const g = useGlobalStore()
const { stackViewEl } = useHookShareState().toRefs()
const { itemSize, gridItems, cellWidth } = useFilesDisplay()
const tag = useTagStore()

const props = defineProps<{
  tabIdx: number
  paneIdx: number
  id: string,
  removable?: boolean
  allowDragAndDrop?: boolean,
  files: GridViewFile[]
  paneKey: string
}>()


const files = ref(props.files ?? [])
const onDrop = async (e: DragEvent) => {
  const data = getFileTransferDataFromDragEvent(e)
  if (props.allowDragAndDrop && data) {
    files.value = uniqueFile([...files.value, ...data.nodes])
  }
}

const onDeleteClick = (idx: number) => {
  files.value.splice(idx, 1)
}


watchEffect(() => {
  g.pageFuncExportMap.set(props.paneKey, {
    getFiles: () => toRaw(files.value),
    setFiles: (_files: GridViewFile[]) => files.value = _files
  })
})

</script>
<template>
  <div class="container" ref="stackViewEl" @drop="onDrop">
    <RecycleScroller ref="scroller" class="file-list" :items="files.slice()" :item-size="itemSize.first"
      key-field="fullpath" :item-secondary-size="itemSize.second" :gridItems="gridItems">
      <template v-slot="{ item: file, index: idx }">
        <file-item :idx="idx" :file="file" :cell-width="cellWidth" :enable-close-icon="props.removable"
          @close-icon-click="onDeleteClick(idx)" :full-screen-preview-image-url="toImageUrl(file)"
          :extra-tags="file?.tags?.map(tag.tagConvert)" :enable-right-click-menu="false" />
      </template>
    </RecycleScroller>
  </div>
</template>
<style scoped lang="scss">
.container {
  background: var(--zp-secondary-background);

  height: 100%;
  overflow: auto;
  display: flex;
  flex-direction: column;

  .actions-panel {
    padding: 8px;
    background-color: var(--zp-primary-background);
  }

  .file-list {
    flex: 1;
    list-style: none;
    padding: 8px;
    height: var(--pane-max-height);
    width: 100%;

    .hint {
      text-align: center;
      font-size: 2em;
      padding: 30vh 128px 0;
    }
  }
}
</style>
