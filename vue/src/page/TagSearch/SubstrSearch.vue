<script lang="ts" setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import fileItemCell from '@/components/FileItem.vue'
import { RecycleScroller } from '@/components/recycleScroller'
import { toImageUrl } from '@/util/file'
import { getDbBasicInfo, getExpiredDirs, getImagesBySubstr, updateImageData, type DataBaseBasicInfo, type Tag, type TagId, SearchBySubstrReq } from '@/api/db'
import { copy2clipboardI18n,  makeAsyncFunctionSingle, useGlobalEventListen, globalEvents } from '@/util'
import fullScreenContextMenu from '@/page/fileTransfer/fullScreenContextMenu.vue'
import { LeftCircleOutlined, RightCircleOutlined, regex, AimOutlined } from '@/icon'
import { message } from 'ant-design-vue'
import { t } from '@/i18n'
import { createImageSearchIter, useImageSearch } from './hook'
import { useKeepMultiSelect } from '../fileTransfer/hook'
import MultiSelectKeep from '@/components/MultiSelectKeep.vue'
import { useGlobalStore } from '@/store/useGlobalStore'
import HistoryRecord from '@/components/HistoryRecord.vue'
import TipsCarousel from '@/components/TipsCarousel.vue'
import { fuzzySearchHistory, FuzzySearchHistoryRecord } from '@/store/searchHistory'
import { openTiktokViewWithFiles } from '@/util/tiktokHelper'
import { useTagStore } from '@/store/useTagStore'
import { useLocalStorage } from '@vueuse/core'
import { SearchSelect, type SearchSelectConv } from 'vue3-ts-util'
import { useBackgroundMaintenance } from '@/composables/useBackgroundMaintenance'
const tagStore = useTagStore()
const bg = useBackgroundMaintenance()

/**
 * 索引更新后标签缓存可能已过期：清空缓存并通知各网格立即补拉当前可视区标签。
 * 不能只 clear 就完事，否则要等用户滚动触发 viewableAreaFilesChange 才会补拉，
 * 表现为「切回来标签没了，滚一下又好了」。
 */
const invalidateTagCache = () => {
  tagStore.tagMap.clear()
  globalEvents.emit('tagsInvalidated')
}
/** 实验功能开启且后台未报错时，过期交给后台静默处理，不再显示黄色提示/手动更新按钮 */
const hideExpiredAlert = computed(() => bg.enabled.value && !bg.backgroundFailed.value)
const showAutoUpdateFeatureTip = useLocalStorage('iib_auto_update_feature_tip_shown', false)
const props = defineProps<{
  tabIdx: number
  paneIdx: number
  searchScope?: string
  /** Initial search keyword value */
  initialSubstr?: string
  /** Initial regex mode value */
  initialIsRegex?: boolean
  /** Initial path-only mode value */
  initialPathOnly?: boolean
  /** Initial media type filter value */
  initialMediaType?: 'all' | 'image' | 'video'
  /** Whether to auto-search on mount */
  autoSearch?: boolean
}>()
const isRegex = ref(false)
const substr = ref('')
const pathOnly = ref(false)
const folder_paths_str = ref(props.searchScope ?? '')
const showHistoryRecord = ref(false)
const searchCount = ref(0)
const mediaType = ref('all')
const andTags = ref<TagId[]>([])
const iter = createImageSearchIter(cursor => {
  const req: SearchBySubstrReq = {
    cursor,
    regexp: isRegex.value ? substr.value : '',
    surstr: !isRegex.value ? substr.value : '',
    path_only: pathOnly.value,
    folder_paths: (folder_paths_str.value ?? '').split(/,|\n/).map(v => v.trim()).filter(v => v),
    media_type: mediaType.value,
    and_tags: andTags.value
  }
  return getImagesBySubstr(req)
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
  props: propsUpstream,
  changeIndchecked,
  seedChangeChecked,
  getGenDiff,
  getGenDiffWatchDep
} = useImageSearch(iter)

/** 只有搜索请求自身在跑时才锁搜索区 / 转圈，后台更新索引不再影响它 */
const searching = computed(() => !!iter.loading)
/** 手动触发索引更新中的状态（与搜索结果 loading 解耦） */
const indexUpdating = ref(false)


const info = ref<DataBaseBasicInfo>()
/** 实验功能开启但索引还没追平：给一行轻提示，避免把"搜不到新图"当成 bug */
const showIndexStaleHint = computed(() => bg.enabled.value && !!info.value?.expired)

// 标签多选，选中多个时是 AND（同时含有）
// label 跟标签搜索页保持一致：[类型] 显示名 : 原始名
// pos/size 动辄几十万条，直接在前端过滤掉，选择器里只留常用类型
const EXCLUDED_TAG_TYPES = ['pos', 'size']

// 跟标签搜索页的 toTagDisplayName 保持一致
const toTagDisplayName = (tag: Tag, withType = false) =>
  (withType ? `[${tag.type}] ` : '') + (tag.display_name ? `${tag.display_name} : ${tag.name}` : tag.name)

const tagMap = computed(() => new Map((info.value?.tags ?? []).map(tag => [tag.id, tag])))

const tags = computed(() =>
  (info.value?.tags ?? [])
    .filter(tag => !EXCLUDED_TAG_TYPES.includes(tag.type))
    .sort((a, b) => {
      // custom 的排最前，其余按 count 降序
      const customDiff = (b.type === 'custom' ? 1 : 0) - (a.type === 'custom' ? 1 : 0)
      return customDiff || (b.count ?? 0) - (a.count ?? 0)
    })
)

const tagConv: SearchSelectConv<Tag> = {
  value: (v: Tag) => v.id,
  text: (v: Tag) => toTagDisplayName(v),
  optionText: (v: Tag) => toTagDisplayName(v, true)
}

const tagNameOf = (id: TagId) => {
  const tag = tagMap.value.get(id)
  return tag ? toTagDisplayName(tag, true) : String(id)
}
const tagIdsToString = (ids?: TagId[]) => (ids ?? []).map(tagNameOf).join(', ')

onMounted(async () => {
  // 先确保实验功能配置已加载，避免 mount 时读到默认值导致误走阻塞式更新
  await bg.loadSettings().catch(() => undefined)
  info.value = await getDbBasicInfo()
  if (info.value.img_count && info.value.expired) {
    // 实验功能开启时交给后台 worker，mount 不再阻塞等待索引更新
    if (g.autoUpdateIndex && !bg.enabled.value) {
      await onUpdateBtnClick()
    }
  }
  // Apply pre-filled values from props
  if (props.initialSubstr !== undefined) {
    substr.value = props.initialSubstr
  }
  if (props.initialIsRegex !== undefined) {
    isRegex.value = props.initialIsRegex
  }
  if (props.initialPathOnly !== undefined) {
    pathOnly.value = props.initialPathOnly
  }
  if (props.initialMediaType !== undefined) {
    mediaType.value = props.initialMediaType
  }
  // Auto-search if substr is provided and autoSearch is not false
  if (props.initialSubstr && props.autoSearch !== false) {
    await query()
  } else if (props.searchScope && !props.initialSubstr) {
    // Legacy behavior: only search if searchScope but no search term
    await query()
  }
})



watch(
  () => props,
  async (v) => {
    propsUpstream.value = v
  },
  { deep: true, immediate: true}
)


const onUpdateBtnClick = makeAsyncFunctionSingle(async () => {
  indexUpdating.value = true
  try {
    await updateImageData()
    info.value = await getDbBasicInfo()
    invalidateTagCache()
    return info.value
  } finally {
    indexUpdating.value = false
  }
})

// 后台静默维护完成一轮索引更新后，重新拉取标签列表（不刷新已打开的匹配结果）
useGlobalEventListen('backgroundMaintenanceUpdated', async () => {
  try {
    info.value = await getDbBasicInfo()
    invalidateTagCache()
  } catch (error) {
    // 后台刷新标签失败不打扰用户，下次轮询/进入页面会再拉
  }
})


const reuse = (rec: FuzzySearchHistoryRecord & { id: string; time: string }) => {
  substr.value = rec.substr
  folder_paths_str.value = rec.folder_paths_str
  isRegex.value = rec.isRegex
  mediaType.value = rec.mediaType || 'all'
  andTags.value = rec.and_tags ?? []
  showHistoryRecord.value = false
  query()
}

const query = async () => {
  searchCount.value++
  fuzzySearchHistory.value.add({
    substr: substr.value,
    folder_paths_str: folder_paths_str.value,
    isRegex: isRegex.value,
    mediaType: mediaType.value,
    and_tags: andTags.value
  })
  await iter.reset({ refetch: true })
  await nextTick()
  onScroll()
  scroller.value!.scrollToItem(0)
  if (!images.value.length) {
    message.info(t('fuzzy-search-noResults'))
  }
}

useGlobalEventListen('returnToIIB', async () => {
  const res = await queue.pushAction(getExpiredDirs).res
  info.value!.expired = res.expired
})

useGlobalEventListen('searchIndexExpired', () => info.value && (info.value.expired = true))

const onRegexpClick = () => {
  isRegex.value = !isRegex.value
}
const g = useGlobalStore()

const { onClearAllSelected, onSelectAll, onReverseSelect } = useKeepMultiSelect()
</script>
<template>
  <a-modal v-model:visible="showHistoryRecord" width="70vw" mask-closable @ok="showHistoryRecord = false">
    <HistoryRecord :records="fuzzySearchHistory" @reuse-record="reuse">
      <template #default="{ record }">
        <div style="padding-right: 16px;">
          <a-row>
            <a-col :span="4">{{ $t('historyRecordsSubstr') }}:</a-col>
            <a-col :span="20">{{ record.substr }}</a-col>
          </a-row>
          <a-row v-if="record.folder_paths_str">
            <a-col :span="4">{{ $t('searchScope') }}:</a-col>
            <a-col :span="20">{{ record.folder_paths_str }}</a-col>
          </a-row>
          <a-row>
            <a-col :span="4">{{ $t('historyRecordsisRegex') }}:</a-col>
            <a-col :span="20">{{ record.isRegex }}</a-col>
          </a-row>
          <a-row v-if="record.mediaType">
            <a-col :span="4">{{ $t('mediaType') }}:</a-col>
            <a-col :span="20">{{ record.mediaType }}</a-col>
          </a-row>
          <a-row v-if="record.and_tags?.length">
            <a-col :span="4">{{ $t('tags') }}:</a-col>
            <a-col :span="20">{{ tagIdsToString(record.and_tags) }} (AND)</a-col>
          </a-row>
          <a-row>
            <a-col :span="4">{{ $t('time') }}:</a-col>
            <a-col :span="20">{{ record.time }}</a-col>
          </a-row>
          <div>
          </div>
        </div>
      </template>
    </HistoryRecord>
  </a-modal>
  <div class="container" ref="stackViewEl">
    <a-alert
      v-if="!showAutoUpdateFeatureTip && !hideExpiredAlert"
      type="info"
      show-icon
      :message="$t('autoUpdateFeatureTip')"
      style="margin: 8px;"
      closable
      @close="showAutoUpdateFeatureTip = true"
    >
      <template #action>
        <a-button size="small" type="link" @click="showAutoUpdateFeatureTip = true">
          {{ $t('gotIt') }}
        </a-button>
      </template>
    </a-alert>
    <a-alert
      v-if="info && info.expired && !g.autoUpdateIndex && !bg.enabled.value"
      type="warning"
      show-icon
      :message="$t('indexExpiredManualUpdate')"
      style="margin: 8px;"
      closable
    />
    <div v-if="showIndexStaleHint" class="index-stale-hint">{{ $t('indexStaleHint') }}</div>
    <MultiSelectKeep :show="!!multiSelectedIdxs.length || g.keepMultiSelect" @clear-all-selected="onClearAllSelected"
      @select-all="onSelectAll" @reverse-select="onReverseSelect" />
    <div class="search-bar"  @keydown.stop>
      <a-input v-model:value="substr" :placeholder="$t('fuzzy-search-placeholder') + ' ' + $t('regexSearchEnabledHint')"
        :disabled="searching" @keydown.enter="query" allow-clear />
      <ASelect v-model:value="mediaType" style="width: 100px; margin: 0 4px;" :disabled="searching">
        <ASelectOption value="all">{{ $t('all') }}</ASelectOption>
        <ASelectOption value="image">{{ $t('image') }}</ASelectOption>
        <ASelectOption value="video">{{ $t('video') }}</ASelectOption>
      </ASelect>
        <div class="regex-icon" :class="{ selected: pathOnly }" @keydown.stop @click="pathOnly = !pathOnly"
        :title="$t('pathOnly')"><AimOutlined /></div>
      <div class="regex-icon" :class="{ selected: isRegex }" @keydown.stop @click="onRegexpClick"
        title="Use Regular Expression"> <img :src="regex"></div>
      <AButton @click="onUpdateBtnClick" :loading="indexUpdating" type="primary" v-if="info && !info.img_count">
        {{ $t('generateIndexHint') }}</AButton>
      <template v-else>
        <AButton type="primary" @click="query" :loading="searching"
           >{{ $t('search') }}
        </AButton>
        <AButton @click="onUpdateBtnClick" :loading="indexUpdating"
          v-if="info && info.expired && !g.autoUpdateIndex && !bg.enabled.value"
          style="margin-left: 8px;">
          {{ $t('UpdateIndex') }}
        </AButton>
      </template>
    </div>
    <div class="search-bar">
      <SearchSelect
        :conv="tagConv"
        mode="multiple"
        :options="tags"
        v-model:value="andTags"
        :placeholder="$t('tagFilterAnd')"
        :disabled="searching || !tags.length"
        :max-tag-count="2"
        allow-clear
        style="width: 300px; margin: 0 4px 0 0; flex-shrink: 0;"
      />
      <div class="form-name">{{ $t('searchScope') }}</div>
      <ATextarea :auto-size="{ maxRows: 8 }" v-model:value="folder_paths_str"
        :placeholder="$t('specifiedSearchFolder')" />
    </div>
    <div class="search-bar last actions">
      <a-button @click="saveLoadedFileAsJson" v-if="images.length">{{ $t('saveLoadedImageAsJson') }}</a-button>
      <a-button @click="saveAllFileAsJson" v-if="images.length">{{ $t('saveAllAsJson') }}</a-button>
      <a-button @click="showHistoryRecord = true">{{ $t('history') }}</a-button>
    <div class="tips-wrapper">
      <TipsCarousel :interval="10000" />
    </div>
    </div>
    <ASpin size="large" :spinning="searching">
      <AModal v-model:visible="showGenInfo" width="70vw" mask-closable @ok="showGenInfo = false">
        <template #cancelText />
        <ASkeleton active :loading="!genInfoQueue.isIdle">
          <div style="
                            width: 100%;
                              word-break: break-all;
                              white-space: pre-line;
                              max-height: 70vh;
                              overflow: auto;
                            " @dblclick="copy2clipboardI18n(imageGenInfo)">
            <div class="hint">{{ $t('doubleClickToCopy') }}</div>
            {{ imageGenInfo }}
          </div>
        </ASkeleton>
      </AModal>
      <div v-if="searchCount === 0 && !images.length && fuzzySearchHistory.getRecords().length"
        style="margin: 64px 16px 32px; padding: 8px; background: var(--zp-secondary-variant-background);border-radius: 16px">
        <h2 style="margin: 16px 32px 16px;">
          {{ $t('restoreFromHistory') }}
        </h2>
        <HistoryRecord :records="fuzzySearchHistory" @reuse-record="reuse">
          <template #default="{ record }">
            <div style="padding-right: 16px;;">
              <a-row>
                <a-col :span="4">{{ $t('historyRecordsSubstr') }}:</a-col>
                <a-col :span="20">{{ record.substr }}</a-col>
              </a-row>
              <a-row  v-if="record.folder_paths_str">
                <a-col :span="4">{{ $t('searchScope') }}:</a-col>
                <a-col :span="20">{{ record.folder_paths_str }}</a-col>
              </a-row>
              <a-row>
                <a-col :span="4">{{ $t('historyRecordsisRegex') }}:</a-col>
                <a-col :span="20">{{ record.isRegex }}</a-col>
              </a-row>
              <a-row v-if="record.mediaType">
                <a-col :span="4">{{ $t('mediaType') }}:</a-col>
                <a-col :span="20">{{ record.mediaType }}</a-col>
              </a-row>
              <a-row v-if="record.and_tags?.length">
                <a-col :span="4">{{ $t('tags') }}:</a-col>
                <a-col :span="20">{{ tagIdsToString(record.and_tags) }} (AND)</a-col>
              </a-row>
              <a-row>
                <a-col :span="4">{{ $t('time') }}:</a-col>
                <a-col :span="20">{{ record.time }}</a-col>
              </a-row>
              <div>
              </div>
            </div>
          </template>
        </HistoryRecord>
      </div>
      <RecycleScroller ref="scroller" class="file-list" v-if="images" :items="images" :item-size="itemSize.first"
        key-field="fullpath" :item-secondary-size="itemSize.second" :gridItems="gridItems" @scroll="onScroll">
        <template #after>
          <div style="padding: 16px 0 512px;" />
        </template>
        <template v-slot="{ item: file, index: idx }">
          <!-- idx 和file有可能丢失 -->
          <file-item-cell :idx="idx" :file="file" v-model:show-menu-idx="showMenuIdx" @file-item-click="onFileItemClick"
            :full-screen-preview-image-url="images[previewIdx] ? toImageUrl(images[previewIdx]) : ''"
            :cell-width="cellWidth" :selected="multiSelectedIdxs.includes(idx)"
            @context-menu-click="onContextMenuClickU" @dragstart="onFileDragStart" @dragend="onFileDragEnd"
            @tiktok-view="(_file, idx) => openTiktokViewWithFiles(images, idx)"
            :enable-change-indicator="changeIndchecked"
            :seed-change-checked="seedChangeChecked"
            :get-gen-diff="getGenDiff"
            :get-gen-diff-watch-dep="getGenDiffWatchDep"
            :is-selected-mutil-files="multiSelectedIdxs.length > 1" @preview-visible-change="onPreviewVisibleChange" />
        </template>
      </RecycleScroller>
      <div v-if="previewing" class="preview-switch">
        <LeftCircleOutlined @click="previewImgMove('prev')" :class="{ disable: !canPreview('prev') }" />
        <RightCircleOutlined @click="previewImgMove('next')" :class="{ disable: !canPreview('next') }" />
      </div>
    </ASpin>
    <fullScreenContextMenu v-if="previewing && images && images[previewIdx]" :file="images[previewIdx]"
      :idx="previewIdx" @context-menu-click="onContextMenuClickU" />
  </div>
</template>
<style scoped lang="scss">
.index-stale-hint {
  margin: 0 8px 8px;
  font-size: 12px;
  line-height: 1.4;
  color: #8c8c8c;
}

::v-deep {
  .float-panel {
    position: fixed;
  }
}

.regex-icon {
  img {
    height: 1.5em;
  }

  user-select: none;
  padding: 4px;
  margin: 0 4px;
  cursor: pointer;
  border: 1px solid var(--zp-border);
  border-radius: 4px;

  &:hover {
    background: var(--zp-border);
  }

  &.selected {
    background: var(--primary-color-1);
    border: 1px solid var(--primary-color);
  }
}

.search-bar {
  padding: 8px 8px 0 8px;

  &.last {
    padding-bottom: 8px;
  }

  display: flex;

  .form-name {
    flex-shrink: 0;
    padding: 4px 8px;
  }

  .actions>* {
    margin-right: 4px;
  }
}

.tips-wrapper {
  padding: 0 8px;
}


.container {
  background: var(--zp-secondary-background);

  position: relative;

  .file-list {
    list-style: none;
    padding: 8px;
    height: 100%;
    overflow: auto;
    height: var(--pane-max-height);
    width: 100%;
  }
}
</style>
