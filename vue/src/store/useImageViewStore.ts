import { setAppFeSetting } from '@/api'
import { getDbBasicInfo, type Tag, type TagId } from '@/api/db'
import { isSync } from '@/util'
import { prefix } from '@/util/const'
import { cloneDeep, debounce, uniqueId } from 'lodash-es'
import { defineStore } from 'pinia'
import { ref, toRaw, watch } from 'vue'
import { useGlobalStore, type TabPane } from './useGlobalStore'

export type ImageViewMediaType = 'all' | 'image' | 'video'

export interface ImageViewTagRef {
  name: string
  type: string
}

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

export class MissingImageViewTagError extends Error {
  constructor(public readonly tag: Partial<ImageViewTagRef>) {
    super(`Image view tag is missing: ${tag.type ?? '?'} / ${tag.name ?? '?'}`)
    this.name = 'MissingImageViewTagError'
  }
}

const tagRefKey = (tag: Pick<ImageViewTagRef, 'name' | 'type'>) => `${tag.type}\u0000${tag.name}`

/** Tag IDs are not stable across index rebuilds; saved views must use name + type. */
export const toImageViewTagRefs = (tags: Tag[], ids: TagId[]): ImageViewTagRef[] => {
  const tagMap = new Map(tags.map((tag) => [tag.id, tag]))
  return ids.map((id) => {
    const tag = tagMap.get(id)
    if (!tag) {
      throw new Error(`Cannot resolve selected tag: ${String(id)}`)
    }
    return { name: tag.name, type: tag.type }
  })
}

const resolveTagIdMap = async (refs: ImageViewTagRef[]) => {
  const uniqueRefs = new Map<string, ImageViewTagRef>()
  refs.forEach((ref) => {
    if (typeof ref?.name !== 'string' || typeof ref?.type !== 'string') {
      throw new MissingImageViewTagError(ref)
    }
    uniqueRefs.set(tagRefKey(ref), ref)
  })
  if (!uniqueRefs.size) {
    return new Map<string, TagId>()
  }

  const tagMap = new Map((await getDbBasicInfo()).tags.map((tag) => [tagRefKey(tag), tag.id]))
  uniqueRefs.forEach((ref) => {
    if (!tagMap.has(tagRefKey(ref))) {
      throw new MissingImageViewTagError(ref)
    }
  })
  return tagMap
}

const resolveTagIds = (refs: ImageViewTagRef[], idMap: Map<string, TagId>) =>
  refs.map((ref) => idMap.get(tagRefKey(ref))!)

/** Turn a saved query into an existing search pane without duplicating result rendering. */
export const createImageViewPane = async (view: ImageView): Promise<TabPane> => {
  if (view.query.type === 'tags') {
    const condition = view.query.condition
    const refs = [...condition.and_tags, ...condition.or_tags, ...condition.not_tags]
    const idMap = await resolveTagIdMap(refs)
    const andTags = resolveTagIds(condition.and_tags, idMap)
    const orTags = resolveTagIds(condition.or_tags, idMap)
    const notTags = resolveTagIds(condition.not_tags, idMap)
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
  const idMap = await resolveTagIdMap(andTags)
  return {
    type: 'fuzzy-search',
    searchScope: condition.folder_paths_str,
    initialSubstr: condition.substr,
    initialIsRegex: condition.isRegex,
    initialPathOnly: condition.pathOnly,
    initialMediaType: condition.mediaType ?? 'all',
    initialAndTags: resolveTagIds(andTags, idMap),
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
