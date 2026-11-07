<script setup lang="ts">
import { useImgSliStore } from '@/store/useImgSli'
import { toImageThumbnailUrl, toRawFileUrl, getFileTransferDataFromDragEvent } from '@/util/file'
import { CloseCircleOutlined } from '@/icon'
import { isImageFile } from '@/util'
import { storeToRefs } from 'pinia'
import { useGlobalStore, ImgSliTabPane } from '@/store/useGlobalStore'
import { ok } from 'vue3-ts-util'
import { t } from '@/i18n'
import { uniqueId } from 'lodash-es'

const sliStore = useImgSliStore()
const g = useGlobalStore()
const { left, right } = storeToRefs(sliStore)

const onImageDrop = async (e: DragEvent, side: 'left' | 'right') => {
  const data = getFileTransferDataFromDragEvent(e)
  if (data) {
    const img = data.nodes[0]
    if (!isImageFile(img.name)) {
      return
    }
    sliStore[side] = img
  }
}


const onCancel = () => {
  sliStore.fileDragging = false
  sliStore.left = undefined
  sliStore.right = undefined
  sliStore.opened = false
}

const openInNewTab = () => {
  ok(left.value && right.value)
  const pane: ImgSliTabPane = {
    type: 'img-sli',
    left: left.value,
    right: right.value,
    name: `${t('imgCompare')} ( ${left.value.name} vs ${right.value.name})`,
    key: uniqueId()
  }
  g.tabList[0].panes.push(pane)
  g.tabList[0].key = pane.key
}
</script>
<template>
  <Transition>
    <div class="dragging-port-wrap" v-if="(sliStore.fileDragging || left || right || sliStore.opened) && !sliStore.imgSliActived">
      <h2>{{ $t('imgCompare') }}</h2>
      <div class="content">
        <div class="left port" @dragover.prevent @drop.prevent="onImageDrop($event, 'left')">
          <div v-if="left" class="img-wrap">
            <AImage :src="toImageThumbnailUrl(left)" :preview="{ src: toRawFileUrl(left) }" />
            <CloseCircleOutlined class="close" @click="left = undefined" />
          </div>
          <div v-else>
            {{ $t('dragImageHere') }}
          </div>
        </div>
        <div style="padding: 16px" />
        <div class="right port" @dragover.prevent @drop.prevent="onImageDrop($event, 'right')">
          <div v-if="right" class="img-wrap">
            <AImage :src="toImageThumbnailUrl(right)" :preview="{ src: toRawFileUrl(right) }" />
            <CloseCircleOutlined class="close" @click="right = undefined" />
          </div>
          <div v-else>
            {{ $t('dragImageHere') }}
          </div>
        </div>
      </div>
      <p class="tips" v-if="sliStore.opened" style="max-width: 30vw;">
        Tips: {{ $t('imageCompareTips') }}
      </p>
      <div class="actions" >
        <AButton v-if="left && right" type="primary" @click="sliStore.drawerVisible = true">{{ $t('confirm') }}</AButton>
        <AButton v-if="left && right" type="primary" @click="openInNewTab">{{ $t('confirm') }}({{$t('openInNewTab')}})</AButton>
        <AButton style="margin-left: 16px;" @click="onCancel">{{ $t('close') }}</AButton>
      </div>

    </div>
  </Transition>
</template>
<style scoped lang="scss">
.dragging-port-wrap {
  position: fixed;
  bottom: 15%;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  background: var(--zp-primary-background);
  box-shadow: 0 0 4px var(--zp-secondary);
  border-radius: 8px;
  padding: 16px 32px;

  .content {

    display: flex;
    align-items: center;
    margin: 16px 0;

    .port {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 128px;
      height: 128px;
      border-radius: 8px;
      border: 1px solid var(--zp-tertiary);

      .img-wrap {
        position: relative;

        :deep() img {
          max-width: 128px;
          max-height: 128px;
          object-fit: contain;
        }

        .close {
          position: absolute;
          top: 0;
          right: 0;
          transform: translate(50%, -50%);
          font-size: 1.5em;
          background: white;
          border-radius: 100%;
          color: black;
          z-index: 999;
          cursor: pointer;

        }
      }
    }
  }
}

.v-enter-active,
.v-leave-active {
  transition: opacity 0.5s ease;
}

.v-enter-from,
.v-leave-to {
  opacity: 0;
}
</style>
