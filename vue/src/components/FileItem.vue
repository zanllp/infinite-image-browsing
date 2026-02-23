<script setup lang="ts">
import { FileOutlined, FolderOpenOutlined, EllipsisOutlined, HeartOutlined, HeartFilled } from '@/icon'
import { useGlobalStore } from '@/store/useGlobalStore'
import { fallbackImage, ok } from 'vue3-ts-util'
import type { FileNodeInfo } from '@/api/files'
import { isImageFile, isVideoFile, isAudioFile } from '@/util'
import { toImageThumbnailUrl, toVideoCoverUrl, toRawFileUrl } from '@/util/file'
import type { MenuInfo } from 'ant-design-vue/lib/menu/src/interface'
import { computed, ref, nextTick, watch } from 'vue'
import ContextMenu from './ContextMenu.vue'
import ChangeIndicator from './ChangeIndicator.vue'
import { useTagStore } from '@/store/useTagStore'
import { CloseCircleOutlined, StarFilled, StarOutlined } from '@/icon'
import { Tag } from '@/api/db'
import { openVideoModal, openAudioModal } from './functionalCallableComp'
import type { GenDiffInfo } from '@/api/files'
import { play } from '@/icon'
import { Top4MediaInfo } from '@/api'
import { debounce } from 'lodash-es'
import { closeImageFullscreenPreview } from '@/util/imagePreviewOperation'
import { eventEmitter as videoEventEmitter, useEventListen } from './videoEventEmitter'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const global = useGlobalStore()
const tagStore = useTagStore()

const props = withDefaults(
  defineProps<{
    file: FileNodeInfo,
    idx: number
    selected?: boolean
    showMenuIdx?: number
    cellWidth: number
    fullScreenPreviewImageUrl?: string
    enableRightClickMenu?: boolean,
    enableCloseIcon?: boolean,
    isSelectedMutilFiles?: boolean
    genInfo?: string
    enableChangeIndicator?: boolean
    extraTags?: Tag[]
    coverFiles?: Top4MediaInfo[]
    getGenDiff?: (ownGenInfo: any, idx: any, increment: any, ownFile: FileNodeInfo) => GenDiffInfo,
    getGenDiffWatchDep?: (idx: number) => any
  }>(),
  {
    selected: false, enableRightClickMenu: true, enableCloseIcon: false
  }
)


const genDiffToPrevious = ref<GenDiffInfo>()
const genDiffToNext = ref<GenDiffInfo>()
const calcGenInfoDiff = debounce(() => {
  const { getGenDiff, file, idx } = props
  if (!getGenDiff) return 
  genDiffToNext.value = getGenDiff(file.gen_info_obj, idx, 1, file)
  genDiffToPrevious.value = getGenDiff(file.gen_info_obj, idx, -1, file)
}, 200 + 100 * Math.random())

watch(() => props.getGenDiffWatchDep?.(props.idx), () => {
  calcGenInfoDiff()
}, { immediate: true, deep: true })

const emit = defineEmits<{
  'update:showMenuIdx': [v: number],
  'fileItemClick': [event: MouseEvent, file: FileNodeInfo, idx: number],
  'dragstart': [event: DragEvent, idx: number],
  'dragend': [event: DragEvent, idx: number],
  'dropToFolder': [event: DragEvent, file: FileNodeInfo, idx: number],
  'previewVisibleChange': [value: boolean, last: boolean],
  'contextMenuClick': [e: MenuInfo, file: FileNodeInfo, idx: number],
  'close-icon-click': [],
  'tiktokView': [file: FileNodeInfo, idx: number]
}>()

const customTags = computed(() => {
  return tagStore.tagMap.get(props.file.fullpath) ?? []
})

const imageSrc = computed(() => {
  const r = global.gridThumbnailResolution
  return global.enableThumbnail ? toImageThumbnailUrl(props.file, [r, r].join('x')) : toRawFileUrl(props.file)
})

const tags = computed(() => {
  return (global.conf?.all_custom_tags ?? []).reduce((p, c) => {
    return [...p, { ...c, selected: !!customTags.value.find((v) => v.id === c.id) }]
  }, [] as (Tag & { selected: boolean })[])
})

const likeTag = computed(() => tags.value.find(v => v.type === 'custom' && v.name === 'like'))

const taggleLikeTag = () => {
  ok(likeTag.value)
  emit('contextMenuClick', { key: `toggle-tag-${likeTag.value.id}` } as MenuInfo, props.file, props.idx)
}

const minShowDetailWidth = 160

// 视频原地播放相关
const isPlayingInline = ref(false)
const videoElementRef = ref<HTMLVideoElement | null>(null)

// 切换原地播放
const toggleInlinePlay = (event: MouseEvent) => {
  console.log('toggleInlinePlay', { event, isPlayingInline: isPlayingInline.value, videoRef: videoElementRef.value })
  event.stopPropagation()

  // 如果要开始播放，先通知其他视频停止
  if (!isPlayingInline.value) {
    videoEventEmitter.emit('stopInlinePlay')
  }

  // 先切换状态，让video元素渲染出来
  isPlayingInline.value = !isPlayingInline.value

  // 使用 nextTick 确保 video 元素已经渲染
  if (!isPlayingInline.value) {
    // 如果是暂停，直接暂停
    if (videoElementRef.value) {
      videoElementRef.value.pause()
    }
  } else {
    // 如果是播放，等待DOM更新后再播放
    nextTick(() => {
      if (videoElementRef.value) {
        console.log('Playing video', videoElementRef.value)
        videoElementRef.value.play().catch(err => {
          console.error('Play failed:', err)
          isPlayingInline.value = false
        })
      } else {
        console.error('Video ref is null after nextTick')
        isPlayingInline.value = false
      }
    })
  }
}

// 处理其他视频播放的通知
const handleStopInlinePlay = () => {
  if (isPlayingInline.value && videoElementRef.value) {
    videoElementRef.value.pause()
    isPlayingInline.value = false
  }
}

// 监听停止事件
useEventListen('stopInlinePlay', handleStopInlinePlay)

// 视频播放结束处理
const handleVideoEnded = () => {
  isPlayingInline.value = false
}

// 判断是否显示原地播放按钮（宽度大于400且未在播放）
const shouldShowInlinePlayBtn = computed(() => {
  return props.cellWidth > 400 && !isPlayingInline.value
})

// 监听 idx 变化，如果正在播放则停止
watch(() => props.idx, () => {
  if (isPlayingInline.value && videoElementRef.value) {
    videoElementRef.value.pause()
    isPlayingInline.value = false
  }
})

const handleDragOver = (event: DragEvent) => {
  if (props.file.type !== 'dir') {
    return
  }
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = 'move'
  }
}

const handleDrop = (event: DragEvent) => {
  if (props.file.type !== 'dir') {
    return
  }
  event.preventDefault()
  event.stopPropagation()
  emit('dropToFolder', event, props.file, props.idx)
}

// 处理文件点击事件
const handleFileClick = (event: MouseEvent) => {
  // 检查magic switch是否开启且是图片文件（视频有自己的处理逻辑）
  if (global.magicSwitchTiktokView && props.file.type === 'file' && isImageFile(props.file.name)) {
    // 阻止事件传播，防止 a-image 组件也触发预览
    event.stopPropagation()
    event.preventDefault()
    // 直接触发TikTok视图
    emit('tiktokView', props.file, props.idx)
    setTimeout(() => {
      closeImageFullscreenPreview()
    }, 500);
  } else {
    // 正常触发文件点击事件
    emit('fileItemClick', event, props.file, props.idx)
  }
}

// 处理视频点击事件
const handleVideoClick = () => {
  // 如果正在原地播放，先停止播放
  if (isPlayingInline.value) {
    isPlayingInline.value = false
    if (videoElementRef.value) {
      videoElementRef.value.pause()
    }
    return
  }

  if (global.magicSwitchTiktokView) {
    // 直接触发TikTok视图
    emit('tiktokView', props.file, props.idx)
  } else {
    // 正常打开视频模态框
    openVideoModal(
      props.file,
      (id) => emit('contextMenuClick', { key: `toggle-tag-${id}` } as any, props.file, props.idx),
      () => emit('tiktokView', props.file, props.idx)
    )
  }
}

// 处理音频点击事件
const handleAudioClick = () => {
  if (global.magicSwitchTiktokView) {
    // 直接触发TikTok视图
    emit('tiktokView', props.file, props.idx)
  } else {
    // 正常打开音频模态框
    openAudioModal(
      props.file, 
      (id) => emit('contextMenuClick', { key: `toggle-tag-${id}` } as any, props.file, props.idx),
      () => emit('tiktokView', props.file, props.idx)
    )
  }
}
</script>
<template>
  <a-dropdown :trigger="['contextmenu']" :visible="!global.longPressOpenContextMenu ? undefined : typeof idx === 'number' && showMenuIdx === idx
    " @update:visible="(v: boolean) => typeof idx === 'number' && emit('update:showMenuIdx', v ? idx : -1)">
    <li class="file file-item-trigger grid" :class="{
    clickable: file.type === 'dir',
    selected
  }" :data-idx="idx" :key="file.name" draggable="true" @dragstart="emit('dragstart', $event, idx)"
      @dragend="emit('dragend', $event, idx)" @dragover="handleDragOver" @drop="handleDrop"
      @click.capture="handleFileClick($event)">

      <div>
        <div class="close-icon" v-if="enableCloseIcon" @click="emit('close-icon-click')">
          <close-circle-outlined />
        </div>
        <div class="more" v-if="enableRightClickMenu">
          <a-dropdown>
            <div class="float-btn-wrap">
              <ellipsis-outlined />
            </div>
            <template #overlay>
              <context-menu :file="file" :idx="idx" :selected-tag="customTags"
                @context-menu-click="(e, f, i) => emit('contextMenuClick', e, f, i)"
                :is-selected-mutil-files="isSelectedMutilFiles" />
            </template>
          </a-dropdown>
          <a-dropdown v-if="file.type === 'file'">
            <div class="float-btn-wrap" :class="{ 'like-selected': likeTag?.selected }" @click="taggleLikeTag">
              <HeartFilled v-if="likeTag?.selected" />
              <HeartOutlined v-else />
            </div>
            <template #overlay>
              <a-menu @click="emit('contextMenuClick', $event, file, idx)" v-if="tags.length > 1">
                <a-menu-item v-for="tag in tags" :key="`toggle-tag-${tag.id}`">{{ tag.name }}
                  <star-filled v-if="tag.selected" /><star-outlined v-else />
                </a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </div>
        <!-- :key="fullScreenPreviewImageUrl ? undefined : file.fullpath" 
          这么复杂是因为再全屏查看时可能因为直接删除导致fullpath变化，然后整个预览直接退出-->
        <div :key="file.fullpath" :class="`idx-${idx} item-content`" v-if="isImageFile(file.name)">

          <!-- change indicators -->
          <ChangeIndicator v-if="enableChangeIndicator && genDiffToNext && genDiffToPrevious"
            :gen-diff-to-next="genDiffToNext" :gen-diff-to-previous="genDiffToPrevious" />
          <!-- change indicators END -->

          <a-image :src="imageSrc" :fallback="fallbackImage" :preview="{
    src: fullScreenPreviewImageUrl,
    onVisibleChange: (v: boolean, lv: boolean) => emit('previewVisibleChange', v, lv)
  }" />
          <div class="tags-container" v-if="customTags && cellWidth > minShowDetailWidth">
            <a-tag v-for="tag in extraTags ?? customTags" :key="tag.id" :color="tagStore.getColor(tag)">
              {{ tag.name }}
            </a-tag>
          </div>
        </div>
        <div :class="[`idx-${idx} item-content video`, { 'playing-inline': isPlayingInline }]" :url="toVideoCoverUrl(file)"
          :style="{ 'background-image': isPlayingInline ? 'none' : `url('${file.cover_url ?? toVideoCoverUrl(file)}')` }" v-else-if="isVideoFile(file.name)"
          @click="handleVideoClick">

          <!-- 原地播放视频元素 -->
          <video
            v-if="cellWidth > 400 && isPlayingInline"
            :ref="(el) => videoElementRef = el as HTMLVideoElement"
            :src="toRawFileUrl(file)"
            class="inline-video-player"
            @ended="handleVideoEnded"
            @click.stop
            controls
          />

          <!-- 遮罩层和原地播放按钮 -->
          <div v-if="shouldShowInlinePlayBtn" class="inline-play-overlay" @click="toggleInlinePlay">
            <div class="inline-play-btn">
              <img :src="play" class="play-icon-img">
              <span class="play-text">{{ t('playInline') }}</span>
            </div>
          </div>

          <!-- 原有的中心播放图标（用于打开modal） -->
          <div class="play-icon" v-show="!isPlayingInline">
            <img :src="play" style="width: 40px;height: 40px;">
          </div>
          <div class="tags-container" v-if="customTags && cellWidth > minShowDetailWidth">
            <a-tag v-for="tag in customTags" :key="tag.id" :color="tagStore.getColor(tag)">
              {{ tag.name }}
            </a-tag>
          </div>
        </div>
        <div :class="`idx-${idx} item-content audio`" v-else-if="isAudioFile(file.name)"
          @click="handleAudioClick">
          <div class="audio-icon">🎵</div>
          <div class="tags-container" v-if="customTags && cellWidth > minShowDetailWidth">
            <a-tag v-for="tag in customTags" :key="tag.id" :color="tagStore.getColor(tag)">
              {{ tag.name }}
            </a-tag>
          </div>
        </div>
        <div v-else class="preview-icon-wrap">
          <file-outlined class="icon center" v-if="file.type === 'file'" />
          <div v-else-if="coverFiles?.length && cellWidth > 160" class="dir-cover-container">
            <img class="dir-cover-item"
              :src="item.media_type === 'image' ? toImageThumbnailUrl(item) : toVideoCoverUrl(item)"
              v-for="item in coverFiles" :key="item.fullpath">
          </div>

          <folder-open-outlined class="icon center" v-else />
        </div>
        <div class="profile" v-if="cellWidth > minShowDetailWidth">
          <div class="name line-clamp-1" :title="file.name">
            {{ file.name }}
          </div>
          <div class="basic-info">
            <div style="margin-right: 4px;">
              {{ file.type }} {{ file.size }}
            </div>
            <div>
              {{ file.date }}
            </div>
          </div>
        </div>
      </div>
    </li>
    <template #overlay>
      <context-menu :file="file" :idx="idx" :selected-tag="customTags" v-if="enableRightClickMenu"
        @context-menu-click="(e, f, i) => emit('contextMenuClick', e, f, i)"
        :is-selected-mutil-files="isSelectedMutilFiles" />
    </template>
  </a-dropdown>
</template>
<style lang="scss" scoped>
.center {
  display: flex;
  justify-content: center;
  align-items: center;
}

.item-content {
  position: relative;

  &.video {
    background-color: var(--zp-border);
    border-radius: 8px;
    overflow: hidden;
    width: v-bind('$props.cellWidth + "px"');
    height: v-bind('$props.cellWidth + "px"');
    background-size: cover;
    background-position: center;
    cursor: pointer;

    &.playing-inline {
      background-color: #000;
    }

    .inline-video-player {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }

    .inline-play-overlay {
      position: absolute;
      bottom: 8px;
      left: 8px;
      display: flex;
      align-items: flex-end;
      justify-content: flex-start;
      cursor: pointer;
      opacity: 0;
      transition: opacity 0.3s ease;
      z-index: 5;
    }

    .inline-play-btn {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 10px 16px;
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(0, 0, 0, 0.85) 0%, rgba(20, 20, 20, 0.9) 100%);
      backdrop-filter: blur(8px);
      cursor: pointer;
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      border: 1px solid rgba(255, 255, 255, 0.12);
      box-shadow:
        0 2px 8px rgba(0, 0, 0, 0.3),
        0 0 0 1px rgba(0, 0, 0, 0.1) inset,
        0 1px 0 rgba(255, 255, 255, 0.1) inset;

      &:hover {
        background: linear-gradient(135deg, rgba(0, 0, 0, 0.95) 0%, rgba(30, 30, 30, 0.95) 100%);
        border-color: rgba(255, 255, 255, 0.25);
        transform: translateY(-1px);
        box-shadow:
          0 4px 12px rgba(0, 0, 0, 0.4),
          0 0 0 1px rgba(0, 0, 0, 0.1) inset,
          0 1px 0 rgba(255, 255, 255, 0.15) inset;
      }

      &:active {
        transform: translateY(0);
        background: rgba(0, 0, 0, 0.95);
      }

      .play-icon-img {
        width: 24px;
        height: 24px;
        filter: brightness(0) invert(1);
        flex-shrink: 0;
      }

      .play-text {
        color: #fff;
        font-size: 13px;
        font-weight: 600;
        letter-spacing: 0.2px;
        text-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
        white-space: nowrap;
      }
    }

    &:hover .inline-play-overlay {
      opacity: 1;
    }
  }

  &.audio {
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    border-radius: 8px;
    overflow: hidden;
    width: v-bind('$props.cellWidth + "px"');
    height: v-bind('$props.cellWidth + "px"');
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    
    .audio-icon {
      font-size: 48px;
    }
  }

  .play-icon {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    border-radius: 100%;
    display: flex;
  }

  .tags-container {
    position: absolute;
    right: 8px;
    bottom: 8px;
    display: flex;
    width: calc(100% - 16px);
    flex-wrap: wrap-reverse;
    flex-direction: row-reverse;

    &>* {
      margin: 0 0 4px 4px;
      font-size: 14px;
      line-height: 1.6;
    }
  }
}



.close-icon {
  position: absolute;
  top: 0;
  right: 0;
  transform: translate(50%, -50%) scale(1.5);
  cursor: pointer;
  z-index: 100;
  border-radius: 100%;
  overflow: hidden;
  line-height: 1;
  background-color: var(--zp-primary-background);
}

.file {
  padding: 8px 16px;
  margin: 8px;
  display: flex;
  align-items: center;
  background: var(--zp-primary-background);
  border-radius: 8px;
  box-shadow: 0 0 4px var(--zp-secondary-variant-background);
  position: relative;

  &:hover .more {
    opacity: 1;
  }

  .more {
    opacity: 0;
    transition: all 0.3s ease;
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    line-height: 1em;

    .float-btn-wrap {
      font-size: 1.5em;
      cursor: pointer;
      font-size: 500;
      padding: 4px;
      border-radius: 100vh;
      color: white;
      background: var(--zp-icon-bg);

      margin-bottom: 4px;

      &.like-selected {
        color: rgb(223, 5, 5);
      }
    }
  }

  &.grid {
    padding: 0;
    display: inline-block;
    box-sizing: content-box;
    box-shadow: unset;

    background-color: var(--zp-secondary-background);

    :deep() {
      .icon {
        font-size: 8em;
      }

      .profile {
        padding: 0 4px;

        .name {
          font-weight: 500;
          padding: 0;
        }

        .basic-info {
          display: flex;
          justify-content: space-between;
          flex-direction: row;
          margin: 0;
          font-size: 0.7em;
          * {
            white-space: nowrap;
            overflow: hidden;
          }
        }
      }

      .ant-image,
      .preview-icon-wrap {
        border: 1px solid var(--zp-secondary);
        background-color: var(--zp-secondary-variant-background);
        border-radius: 8px;
        overflow: hidden;
      }

      img:not(.dir-cover-item),
      .dir-cover-container,
      .preview-icon-wrap>[role='img'] {
        height: v-bind('$props.cellWidth + "px"');
        width: v-bind('$props.cellWidth + "px"');
        object-fit: contain;
      }
    }
  }


  &.clickable {
    cursor: pointer;
  }

  &.selected {
    outline: #0084ff solid 2px;
  }

  .name {
    flex: 1;
    padding: 8px;
    word-break: break-all;
  }

  .basic-info {
    overflow: hidden;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
  }

  .dir-cover-container {
    top: 0;
    display: flex;
    flex-wrap: wrap;
    padding: 4px;

    &>img {
      width: calc(50% - 8px);
      height: calc(50% - 8px);
      margin: 4px;
      object-fit: cover;
      border-radius: 8px;
      overflow: hidden
    }
  }
}
</style>
