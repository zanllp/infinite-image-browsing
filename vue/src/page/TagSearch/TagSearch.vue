<script lang="ts" setup>
import { onMounted, ref, computed } from 'vue'
import {
  getDbBasicInfo,
  updateImageData,
  type DataBaseBasicInfo,
  type Tag,
  addCustomTag,
  removeCustomTag,
  getExpiredDirs,
  type MatchImageByTagsReq,
  type TagId,
  updateTag
} from '@/api/db'
import { SearchSelect, delay } from 'vue3-ts-util'
import { PlusOutlined, ArrowRightOutlined } from '@/icon'
import { useGlobalStore } from '@/store/useGlobalStore'
import { groupBy, uniqueId, debounce, cloneDeep } from 'lodash-es'
import { createReactiveQueue, type Dict, useGlobalEventListen, globalEvents } from '@/util'
import { Modal, message } from 'ant-design-vue'
import { t } from '@/i18n'
import { makeAsyncFunctionSingle } from '@/util'
import TagSearchItem from './TagSearchItem.vue'
import { reactive, nextTick } from 'vue'
import { watch } from 'vue'
import { tagSearchHistory } from '@/store/searchHistory'
import { useTagStore } from '@/store/useTagStore'
import { useLocalStorage } from '@vueuse/core'
import TipsCarousel from '@/components/TipsCarousel.vue'
import { useBackgroundMaintenance } from '@/composables/useBackgroundMaintenance'

const props = defineProps<{ tabIdx: number; paneIdx: number, searchScope?: string }>()
const global = useGlobalStore()
const showAutoUpdateFeatureTip = useLocalStorage('iib_auto_update_feature_tip_shown', false)
const tagStore = useTagStore()

/**
 * 索引更新后标签缓存可能已过期：清空缓存并通知各网格立即补拉当前可视区标签。
 * 不能只 clear 就完事，否则要等用户滚动触发 viewableAreaFilesChange 才会补拉，
 * 表现为「切回来标签没了，滚一下又好了」。
 */
const invalidateTagCache = () => {
  tagStore.tagMap.clear()
  globalEvents.emit('tagsInvalidated')
}
const queue = createReactiveQueue()
const loading = computed(() => !queue.isIdle)
/** 手动触发索引更新中的状态（与 queue 解耦，避免锁住搜索按钮） */
const indexUpdating = ref(false)
const info = ref<DataBaseBasicInfo>()
const bg = useBackgroundMaintenance()
/** 实验功能开启且后台未报错时，过期交给后台静默处理，不再显示黄色提示/手动更新按钮 */
const hideExpiredAlert = computed(() => bg.enabled.value && !bg.backgroundFailed.value)
/** 实验功能开启但索引还没追平：给一行轻提示，避免把"搜不到新图"当成 bug */
const showIndexStaleHint = computed(() => bg.enabled.value && !!info.value?.expired)
const showHistoryRecord = ref(false)
const matchIds = ref<MatchImageByTagsReq>({ and_tags: [], or_tags: [], not_tags: [], folder_paths_str: props.searchScope })
const tags = computed(() =>
  info.value ? info.value.tags.slice().sort((a, b) => b.count - a.count) : []
)
const classSort = [
  'custom',
  'Source Identifier',
  'Model',
  'Media Type',
  'lora',
  'lyco',
  'pos',
  'size',
  'Sampler',
  'Postprocess upscaler',
  'Postprocess upscale by',
].reduce((p, c, i) => {
  p[c] = i
  return p
}, {} as Dict<number>)

const classifyTags = computed(() => {
  return Object.entries(groupBy(tags.value, (v) => v.type)).sort(
    (a, b) => {
      const aClass = classSort[a[0]] !== undefined ? classSort[a[0]] : Number.MAX_SAFE_INTEGER;
      const bClass = classSort[b[0]] !== undefined ? classSort[b[0]] : Number.MAX_SAFE_INTEGER;
      return aClass - bClass;
    }
  )
})
const tagMaxNum = reactive(new Map<string, number>())
const getTagMaxNum = (name: string) => {
  return tagMaxNum.get(name) ?? 512
}

const tagClassSearch = ref({} as Dict<string>)
const tagClassSearchDebounceSync = ref({} as Dict<string>)

watch(tagClassSearch, debounce((val) => {
  tagClassSearchDebounceSync.value = cloneDeep(val)
}, 300), { deep: true })

const pairid = uniqueId()
const openedKeys = ref((classifyTags.value.map(v => v[0])))
onMounted(async () => {
  // 先确保实验功能配置已加载，避免 mount 时读到默认值导致误走阻塞式更新
  await bg.loadSettings().catch(() => undefined)
  console.log(new Date().toLocaleString())
  info.value = await getDbBasicInfo()
  await delay(20)
  console.log(new Date().toLocaleString())
  openedKeys.value = (classifyTags.value.map(v => v[0]))
  nextTick(() => {
    console.log(new Date().toLocaleString())
  })
  if (info.value.img_count && info.value.expired) {
    // 实验功能开启时交给后台 worker，mount 不再阻塞等待索引更新
    if (global.autoUpdateIndex && !bg.enabled.value) {
      await onUpdateBtnClick()
    }
  }
  if (props.searchScope) {
    query()
  }
})

useGlobalEventListen('searchIndexExpired', () => info.value && (info.value.expired = true))

const onUpdateBtnClick = makeAsyncFunctionSingle(async () => {
  indexUpdating.value = true
  try {
    await updateImageData()
    info.value = await getDbBasicInfo()
    openedKeys.value = (classifyTags.value.map(v => v[0]))

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

const query = () => {
  tagSearchHistory.value.add(matchIds.value)
  global.openTagSearchMatchedImageGridInRight(props.tabIdx, pairid, matchIds.value)
}

const reuse = (record: MatchImageByTagsReq) => {
  matchIds.value = record
  showHistoryRecord.value = false
  query()
}

useGlobalEventListen('returnToIIB', async () => {
  const res = await queue.pushAction(getExpiredDirs).res
  info.value!.expired = res.expired
})
const toTagDisplayName = (v: Tag, withType = false) =>
  (withType ? `[${v.type}] ` : '') + (v.display_name ? `${v.display_name} : ${v.name}` : v.name)

const addInputing = ref(false)
const addTagName = ref('')
const onAddTagBtnSubmit = async () => {
  if (!addTagName.value) {
    addInputing.value = false
    return
  }
  const tag = await queue.pushAction(() => addCustomTag({ tag_name: addTagName.value })).res
  if (tag.type !== 'custom') {
    message.error(t('existInOtherType'))
  }
  if (info.value?.tags.find((v) => v.id === tag.id)) {
    message.error(t('alreadyExists'))
  } else {
    info.value?.tags.push(tag)
    global.conf?.all_custom_tags.push(tag)
  }
  addTagName.value = ''
  addInputing.value = false
}
const onTagRemoveClick = (tagId: TagId) => {
  Modal.confirm({
    title: t('confirmDelete'),
    async onOk () {
      await removeCustomTag({ tag_id: tagId })
      const idx = info.value?.tags.findIndex((v) => v.id === tagId) ?? -1
      info.value?.tags.splice(idx, 1)
      global.conf?.all_custom_tags.splice(
        global.conf?.all_custom_tags.findIndex((v) => v.id === tagId),
        1
      )
    }
  })
}
const selectedTagIds = computed(
  () => new Set([matchIds.value.and_tags, matchIds.value.or_tags, matchIds.value.not_tags].flat())
)
const onTagClick = (tag: Tag) => {
  if (selectedTagIds.value.has(tag.id)) {
    matchIds.value.and_tags = matchIds.value.and_tags.filter((v) => v !== tag.id)
    matchIds.value.or_tags = matchIds.value.or_tags.filter((v) => v !== tag.id)
    matchIds.value.not_tags = matchIds.value.not_tags.filter((v) => v !== tag.id)
  } else {
    matchIds.value.and_tags.push(tag.id)
  }
}

const conv = {
  value: (v: Tag) => v.id,
  text: toTagDisplayName,
  optionText: (v: Tag) => toTagDisplayName(v, true)
}

const toggleTag = (tag_id: TagId, taglist: TagId[]) => {
  const idx = taglist.indexOf(tag_id)
  if (idx === -1) {
    taglist.push(tag_id)
  } else {
    taglist.splice(idx, 1)
  }
  
}

const onTagColorChange = async (tag: Tag, color: string) => {
  tag.color = color
  tagStore.notifyCacheUpdate(tag)
  await updateTag(tag)
}

const tagListFilter = (list: Tag[], name: string) => {
  const max = getTagMaxNum(name)
  let kw = tagClassSearchDebounceSync.value[name]
  if (kw) {
    kw = kw.trim()
    list = list.filter(tag => toTagDisplayName(tag).toLowerCase().includes(kw.toLowerCase()))
  }
  return list.slice(0, max)
}

const tagIdsToString = (tagIds: TagId[]) => {
  return tagIds.map((id) => tags.value.find((v) => v.id === id)?.name).join(', ')
}

</script>
<template>
  <div class="container">
    
  <a-modal v-model:visible="showHistoryRecord" width="70vw" mask-closable @ok="showHistoryRecord = false">
    <HistoryRecord :records="tagSearchHistory" @reuse-record="reuse">
      <template #default="{ record }">
        <div style="padding-right: 16px;">
          <a-row v-if="record.and_tags.length">
            <a-col :span="4">{{ $t('exactMatch') }}:</a-col>
            <a-col :span="20">{{ tagIdsToString(record.and_tags) }}</a-col>
          </a-row>
          <a-row v-if="record.or_tags.length">
            <a-col :span="4">{{ $t('anyMatch') }}:</a-col>
            <a-col :span="20">{{ tagIdsToString(record.or_tags) }}</a-col>
          </a-row>
          <a-row v-if="record.not_tags.length">
            <a-col :span="4">{{ $t('exclude') }}:</a-col>
            <a-col :span="20">{{ tagIdsToString(record.not_tags) }}</a-col>
          </a-row>
          <a-row v-if="record.folder_paths_str">
            <a-col :span="4">{{ $t('searchScope') }}:</a-col>
            <a-col :span="20">{{ record.folder_paths_str }}</a-col>
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
    <ASelect v-if="false" />
    <template v-if="info">
      <a-alert
        v-if="!showAutoUpdateFeatureTip && !hideExpiredAlert"
        type="info"
        show-icon
        :message="$t('autoUpdateFeatureTip')"
        style="margin-bottom: 8px;"
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
        v-if="info.expired && !global.autoUpdateIndex && !bg.enabled.value"
        type="warning"
        show-icon
        :message="$t('indexExpiredManualUpdate')"
        style="margin-bottom: 8px;"
        closable
      />
      <div v-if="showIndexStaleHint" class="index-stale-hint">{{ $t('indexStaleHint') }}</div>
      <div>
        <div class="search-bar">
          <div class="form-name">{{ $t('exactMatch') }}</div>
          <SearchSelect :conv="conv" mode="multiple" style="width: 100%" :options="tags"
            v-model:value="matchIds.and_tags" :disabled="!tags.length" :placeholder="$t('selectExactMatchTag')" />
          <AButton @click="onUpdateBtnClick" :loading="indexUpdating" type="primary"
            v-if="!info.img_count">
            {{ $t('generateIndexHint') }}</AButton>
          <template v-else>
            <AButton type="primary" @click="query">{{
        $t('search') }}
            </AButton>
            <AButton @click="onUpdateBtnClick" :loading="indexUpdating"
              v-if="info.expired && !global.autoUpdateIndex && !bg.enabled.value"
              style="margin-left: 8px;">
              {{ $t('UpdateIndex') }}
            </AButton>
          </template>
        </div>
        <div class="search-bar">
          <div class="form-name">{{ $t('anyMatch') }}</div>
          <SearchSelect :conv="conv" mode="multiple" style="width: 100%" :options="tags"
            v-model:value="matchIds.or_tags" :disabled="!tags.length" :placeholder="$t('selectAnyMatchTag')" />
            <div style="padding-left: 4px"></div>
            <AButton @click="showHistoryRecord = true">{{ $t('history') }}</AButton>
        </div>
        <div class="search-bar">
          <div class="form-name">{{ $t('exclude') }}</div>
          <SearchSelect :conv="conv" mode="multiple" style="width: 100%" :options="tags"
            v-model:value="matchIds.not_tags" :disabled="!tags.length" :placeholder="$t('selectExcludeTag')" />
        </div>
        <div class="search-bar">
          <div class="form-name">{{ $t('searchScope') }}</div>
          <ATextarea :auto-size="{ maxRows: 8 }" v-model:value="matchIds.folder_paths_str"
            :placeholder="$t('specifiedSearchFolder')" />
        </div>
      </div>

      <p class="generate-idx-hint" v-if="!tags.filter((v) => v.type !== 'custom').length">
        {{ $t('needGenerateIdx') }}
      </p>
      <div class="list-container">
      <div class="pinned-search">
        <TipsCarousel :interval="10000" />
      </div>
      <template :key="name" v-for="[name, list] in classifyTags">
         <ul class="tag-list" v-if="name !== 'Media Type' || list.length > 1">
          <h3 class="cat-name"
            @click="!openedKeys.includes(name) ? openedKeys.push(name) : openedKeys.splice(openedKeys.indexOf(name), 1)">
            <ArrowRightOutlined class="arrow" :class="{ down: openedKeys.includes(name) }" />
            {{ $t(name) }}
            <div @click.stop.prevent class="filter-input">
              <a-input v-model:value="tagClassSearch[name]" size="small" allowClear
                :placeholder="$t('filterByKeyword')" />
            </div>
          </h3>
          <a-collapse ghost v-model:activeKey="openedKeys">
            <template #expandIcon></template>
            <a-collapse-panel :key="name">
              <tag-search-item @click="onTagClick(tag)" @remove="onTagRemoveClick(tag.id)"
                @TagColorChange="onTagColorChange(tag, $event)"
                @toggle-and="toggleTag(tag.id, matchIds.and_tags)" @toggle-or="toggleTag(tag.id, matchIds.or_tags)"
                @toggle-not="toggleTag(tag.id, matchIds.not_tags)" v-for="(tag, idx) in tagListFilter(list, name)"
                :key="tag.id" :idx="idx" :name="name" :tag="tag" :selected="selectedTagIds.has(tag.id)" />
              <li v-if="name === 'custom'" class="tag" @click="addInputing = true">
                <template v-if="addInputing">
                  <a-input-group compact>
                    <a-input v-model:value="addTagName" style="width: 128px" :loading="loading" allow-clear
                      size="small" />
                    <a-button size="small" type="primary" @click.capture.stop="onAddTagBtnSubmit" :loading="loading">{{
      addTagName ? $t('submit') : $t('cancel') }}</a-button>
                  </a-input-group>
                </template>
                <template v-else>
                  <PlusOutlined /> {{ $t('add') }}
                </template>
              </li>
              <div v-if="getTagMaxNum(name) < list.length">
                <a-button block @click="tagMaxNum.set(name, getTagMaxNum(name) + 512)">{{ $t('loadmore') }}</a-button>
              </div>
            </a-collapse-panel>
          </a-collapse>
        </ul>
      </template>


      </div>
    </template>
    <div class="spin-container" v-else>
      <a-spin size="large" />
    </div>
  </div>
</template>
<style scoped lang="scss">
.index-stale-hint {
  margin: 0 4px 8px;
  font-size: 12px;
  line-height: 1.4;
  color: #8c8c8c;
}

.spin-container {
  text-align: center;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 4px;
  padding: 256px;
}

:deep() {
  .ant-collapse>.ant-collapse-item>.ant-collapse-header {
    padding: 0;
  }
}

.container {
  height: var(--pane-max-height);
  overflow: auto;
  display: flex;
  flex-direction: column;
  align-items: stretch;

  .generate-idx-hint {
    margin: 64px;
    padding: 64px;
    font-size: 2em;
    text-align: center;
    background-color: var(--zp-secondary-background);
    white-space: pre-line;
    line-height: 2.5em;
    border-radius: 16px;
  }

  .remove {
    padding: 4px;
    position: cursor;
    border-radius: 2px;

    &:hover {
      background-color: var(--zp-secondary-background);
    }
  }

  .select {
    padding: 8px;
  }

  .search-bar {
    padding: 8px;
    display: flex;

    .form-name {
      flex-shrink: 0;
      padding: 4px 8px;
      width: 128px;
    }
  }

  .list-container {
    background-color: var(--zp-secondary-background);
    overflow: scroll;
  }

  .cat-name {
    user-select: none;
    position: sticky;
    top: 0;
    padding: 4px 16px;
    background: var(--zp-primary-background);
    margin: 4px;
    transition: all .3s ease;
    border-left: 4px solid var(--primary-color);
    cursor: pointer;
    z-index: 1;
    display: flex;
    align-items: center;
    flex-direction: row;

    .filter-input {
      margin-left: 32px;
      width: 256px;
       & > span {
        border-radius: 6px;
       }
    }


    &:hover {
      border-radius: 4px;
      background-color: var(--zp-secondary-background);
    }

    .arrow {
      color: var(--primary-color);
      transition: all .3s ease;
      margin-right: 16px;

      &.down {
        transform: rotate(90deg);
      }
    }
  }

  .pinned-search {
    padding: 0;
    margin: 8px 16px;
    border-radius: 16px;
    background: var(--zp-primary-background);
  }

  .tag-list {
    list-style: none;
    padding: 0;
    margin: 16px;
    border-radius: 16px;
    background: var(--zp-primary-background);
    padding: 8px;


    .tag {
      border: 2px solid var(--zp-secondary);
      color: var(--zp-primary);
      border-radius: 999px;
      padding: 4px 16px;
      margin: 4px;
      display: inline-block;
      cursor: pointer;
      position: relative;

    }
  }
}
</style>
