import { setAppFeSetting } from '@/api'
import type { MatchImageByTagsReq, TagId } from '@/api/db'
import { isSync } from '@/util'
import { prefix } from '@/util/const'
import { cloneDeep, debounce, uniqueId } from 'lodash-es'
import { defineStore } from 'pinia'
import { ref, toRaw, watch } from 'vue'
import { useGlobalStore, type TabPane } from './useGlobalStore'

export type ImageViewMediaType = 'all' | 'image' | 'video'

export interface TagImageViewQuery {
  type: 'tags'
  condition: MatchImageByTagsReq
}

export interface FuzzyImageViewQuery {
  type: 'fuzzy'
  condition: {
    substr: string
    folder_paths_str: string
    isRegex: boolean
    pathOnly?: boolean
    mediaType?: ImageViewMediaType
    and_tags?: TagId[]
  }
}

export type ImageViewQuery = TagImageViewQuery | FuzzyImageViewQuery

export interface ImageView {
  id: string
  name: string
  query: ImageViewQuery
}

/** Turn a saved query into an existing search pane without duplicating result rendering. */
export const createImageViewPane = (view: ImageView): TabPane => {
  if (view.query.type === 'tags') {
    return {
      type: 'tag-search-matched-image-grid',
      id: `image-view-${view.id}`,
      selectedTagIds: cloneDeep(view.query.condition),
      key: uniqueId(),
      name: view.name
    }
  }

  const condition = view.query.condition
  return {
    type: 'fuzzy-search',
    searchScope: condition.folder_paths_str,
    initialSubstr: condition.substr,
    initialIsRegex: condition.isRegex,
    initialPathOnly: condition.pathOnly,
    initialMediaType: condition.mediaType ?? 'all',
    initialAndTags: cloneDeep(condition.and_tags ?? []),
    autoSearch: true,
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
