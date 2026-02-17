<script setup lang="ts">
import type { Tag } from '@/api/db'
import type { FileNodeInfo } from '@/api/files'
import type { MenuInfo } from 'ant-design-vue/lib/menu/src/interface'
import { isMediaFile } from '@/util'
import { StarFilled, StarOutlined } from '@/icon'
import { useGlobalStore } from '@/store/useGlobalStore'
import { computed } from 'vue'
const global = useGlobalStore()
const props = defineProps<{
  file: FileNodeInfo
  idx: number,
  selectedTag: Tag[],
  isSelectedMutilFiles?: boolean
}>()
const emit = defineEmits<{
  (type: 'contextMenuClick', e: MenuInfo, file: FileNodeInfo, idx: number): void
}>()

const tags = computed(() => {
  return (global.conf?.all_custom_tags ?? []).reduce((p, c) => {
    return [...p, { ...c, selected: !!props.selectedTag.find((v) => v.id === c.id) }]
  }, [] as (Tag & { selected: boolean })[])
})
</script>
<template>
  <a-menu @click="emit('contextMenuClick', $event, file, idx)">
    <a-menu-item key="deleteFiles">{{ $t('deleteSelected') }}</a-menu-item>
    <a-menu-item key="openWithDefaultApp">{{ $t('openWithDefaultApp') }}</a-menu-item>
    <a-menu-item key="saveSelectedAsJson">{{ $t('saveSelectedAsJson') }}</a-menu-item>
    <template v-if="file.type === 'dir'">
      <a-menu-item key="openInNewTab">{{ $t('openInNewTab') }}</a-menu-item>
      <a-menu-item key="openOnTheRight">{{ $t('openOnTheRight') }}</a-menu-item>
      <a-menu-item key="openWithWalkMode">{{ $t('openWithWalkMode') }}</a-menu-item>
    </template>
    <template v-if="file.type === 'file'">
      <template v-if="isMediaFile(file.name)">
        <a-menu-item key="viewGenInfo">{{ $t('viewGenerationInfo') }}</a-menu-item>
        <a-menu-item key="tiktokView">{{ $t('tiktokView') }}</a-menu-item>
        <a-menu-divider />
        <template v-if="global.conf?.launch_mode !== 'server'">
          <a-menu-item key="send2txt2img">{{ $t('sendToTxt2img') }}</a-menu-item>
          <a-menu-item key="send2img2img">{{ $t('sendToImg2img') }}</a-menu-item>
          <a-menu-item key="send2inpaint">{{ $t('sendToInpaint') }}</a-menu-item>
          <a-menu-item key="send2extras">{{ $t('sendToExtraFeatures') }}</a-menu-item>
          <a-sub-menu key="sendToThirdPartyExtension" :title="$t('sendToThirdPartyExtension')">
            <a-menu-item key="send2controlnet-txt2img">ControlNet - {{ $t('t2i') }}</a-menu-item>
            <a-menu-item key="send2controlnet-img2img">ControlNet - {{ $t('i2i') }}</a-menu-item>
            <a-menu-item key="send2outpaint">openOutpaint</a-menu-item>
          </a-sub-menu>
        </template>

        <a-menu-item key="send2BatchDownload">{{ $t('sendToBatchDownload') }}</a-menu-item>

        <a-sub-menu key="copy2target" :title="$t('copyTo')">
          <a-menu-item v-for="path in global.quickMovePaths" :key="`copy-to-${path.dir}`">{{ path.zh }}
          </a-menu-item>

        </a-sub-menu>
        <a-sub-menu key="move2target" :title="$t('moveTo')">
          <a-menu-item v-for="path in global.quickMovePaths" :key="`move-to-${path.dir}`">{{ path.zh }}
          </a-menu-item>
        </a-sub-menu>

        <a-menu-divider />

        <template v-if="isSelectedMutilFiles">
          <a-sub-menu key="batch-add-tag" :title="$t('batchAddTag')">
            <a-menu-item key="add-custom-tag" >+ {{ $t('addNewCustomTag') }}</a-menu-item>
            <a-menu-item v-for="tag in tags" :key="`batch-add-tag-${tag.id}`">{{ tag.name }}
            </a-menu-item>
          </a-sub-menu>
          <a-sub-menu key="batch-remove-tag" :title="$t('batchRemoveTag')">
            <a-menu-item v-for="tag in tags" :key="`batch-remove-tag-${tag.id}`">{{ tag.name }}
            </a-menu-item>
          </a-sub-menu>
        </template>
        <a-sub-menu v-else key="toggle-tag" :title="$t('toggleTag')">
          <a-menu-item key="add-custom-tag" >+ {{ $t('addNewCustomTag') }}</a-menu-item>
          <a-menu-item v-for="tag in tags" :key="`toggle-tag-${tag.id}`">{{ tag.name }} <star-filled
              v-if="tag.selected" /><star-outlined v-else />
          </a-menu-item>
        </a-sub-menu>
        <a-menu-divider />
        <a-menu-item key="openFileLocationInNewTab">{{ $t('openFileLocationInNewTab') }}</a-menu-item>
        <a-menu-item key="openWithLocalFileBrowser">{{ $t('openWithLocalFileBrowser') }}</a-menu-item>
      </template>
      
      <a-menu-divider />
      <a-menu-item key="rename" >{{ $t('rename') }}</a-menu-item>
      <a-menu-item key="previewInNewWindow">{{ $t('previewInNewWindow') }}</a-menu-item>
      <a-menu-item key="download">{{ $t('download') }}</a-menu-item>
      <a-menu-item key="copyPreviewUrl">{{ $t('copySourceFilePreviewLink') }}</a-menu-item>
      <a-menu-item key="copyFilePath">{{ $t('copyFilePath') }}</a-menu-item>

    </template>
  </a-menu>
</template>