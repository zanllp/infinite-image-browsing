import { setAppFeSetting } from '@/api'
import { getDbBasicInfo } from '@/api/db'
import { isSync } from '@/util'
import { prefix } from '@/util/const'
import { cloneDeep, debounce, uniqueId } from 'lodash-es'
import { defineStore } from 'pinia'
import { ref, toRaw, watch } from 'vue'
import { resolveImageViewTagIds, type ImageViewTagRef } from '@/util/imageViewTagRefs'
import { useGlobalStore, type TabPane } from './useGlobalStore'

export { MissingImageViewTagError, toImageViewTagRefs, type ImageViewTagRef } from '@/util/imageViewTagRefs'

export type ImageViewMediaType = 'all' | 'image' | 'video'

export interface TagImageViewCondition {
  folder_paths_str?: string
  and_tags: ImageViewTagRef[]
  or_tags: ImageViewTagRef[]
  not_tags: ImageViewTagRef[]
}

export interface TagImageViewQuery {
  type: 'tags'
  condition: TagImageViewCondition
}

export interface FuzzyImageViewQuery {
  type: 'fuzzy'
  condition: {
    substr: string
    folder_paths_str: string
    isRegex: boolean
    pathOnly?: boolean
    mediaType?: ImageViewMediaType
    and_tags?: ImageViewTagRef[]
  }
}

export type ImageViewQuery = TagImageViewQuery | FuzzyImageViewQuery

export interface ImageView {
  id: string
  name: string
  query: ImageViewQuery
}

/** Turn a saved query into an existing search pane without duplicating result rendering. */
export const createImageViewPane = async (view: ImageView): Promise<TabPane> => {
  if (view.query.type === 'tags') {
    const condition = view.query.condition
    const refs = [...condition.and_tags, ...condition.or_tags, ...condition.not_tags]
    const tags = refs.length ? (await getDbBasicInfo()).tags : []
    const andTags = resolveImageViewTagIds(condition.and_tags, tags)
    const orTags = resolveImageViewTagIds(condition.or_tags, tags)
    const notTags = resolveImageViewTagIds(condition.not_tags, tags)
    return {
      type: 'tag-search-matched-image-grid',
      id: `image-view-${view.id}`,
      selectedTagIds: {
        folder_paths_str: condition.folder_paths_str,
        and_tags: andTags,
        or_tags: orTags,
        not_tags: notTags
      },
      key: uniqueId(),
      name: view.name
    }
  }

  const condition = view.query.condition
  const andTags = condition.and_tags ?? []
  const tags = andTags.length ? (await getDbBasicInfo()).tags : []
  return {
    type: 'fuzzy-search',
    searchScope: condition.folder_paths_str,
    initialSubstr: condition.substr,
    initialIsRegex: condition.isRegex,
    initialPathOnly: condition.pathOnly,
    initialMediaType: condition.mediaType ?? 'all',
    initialAndTags: resolveImageViewTagIds(andTags, tags),
    autoSearch: true,
    fromView: true,
    key: uniqueId(),
    name: view.name
  }
}

export const useImageViewStore = defineStore(
  prefix + 'useImageViewStore',
  () => {
    const global = useGlobalStore()
    const views = ref<ImageView[]>([])

    watch(
      () => global.conf?.app_fe_setting?.image_views,
      (saved) => {
        if (isSync() && Array.isArray(saved?.views)) {
          views.value = cloneDeep(saved.views)
        }
      },
      { immediate: true }
    )

    const persist = debounce(() => {
      if (!isSync() || global.conf?.is_readonly) {
        return
      }
      setAppFeSetting('image_views', { views: cloneDeep(toRaw(views.value)) }).catch((error) => {
        console.error('failed to save image views', error)
      })
    }, 300)

    const addView = (name: string, query: ImageViewQuery) => {
      const view: ImageView = {
        id: String(Date.now()) + uniqueId(),
        name: name.trim(),
        query: cloneDeep(query)
      }
      views.value.push(view)
      persist()
      return view
    }

    const renameView = (id: string, name: string) => {
      const view = views.value.find((item) => item.id === id)
      if (!view) {
        return
      }
      view.name = name.trim()
      persist()
    }

    const removeView = (id: string) => {
      views.value = views.value.filter((item) => item.id !== id)
      persist()
    }

    return {
      views,
      addView,
      renameView,
      removeView
    }
  },
  {
    persist: {
      paths: ['views']
    }
  }
)
