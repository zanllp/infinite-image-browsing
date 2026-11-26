import { Tag, batchGetTagsByPath } from '@/api/db'
import { defineStore } from 'pinia'
import sjcl from 'sjcl'
import { reactive, ref } from 'vue'
import { globalEvents } from '@/util'
import { onCrossTab } from '@/util/crossTab'
import type { GridViewFileTag } from './useGlobalStore'
function generateColors() {
  const colors = []
  const saturation = 90
  const lightness = 35
  const interval = 360 / 50

  for (let i = 0; i < 72; i++) {
    const hue = i * interval
    const hslColor = `hsl(${hue}, ${saturation}%, ${lightness}%)`
    colors.push(hslColor)
  }

  return colors
}
const tagColors = generateColors()

export const useTagStore = defineStore('useTagStore', () => {
  // tagMap only holds data that came from the server (or from `set`), never a placeholder
  const tagMap = reactive(new Map<string, Tag[]>())
  // requests in flight per path, kept out of tagMap so a loading image does not look untagged
  const inflight = new Map<string, { done: Promise<void> }>()
  // paths whose cached value is outdated: it stays visible until the refetch replaces it
  const stale = new Set<string>()

  // the running request for the path is dropped, so its response is discarded
  const markStale = (path: string) => {
    if (tagMap.has(path)) {
      stale.add(path)
    }
    inflight.delete(path)
  }

  // a response only writes the paths it is still the current request for
  const requestTags = async (paths: string[], req: { done: Promise<void> }) => {
    try {
      const res = await batchGetTagsByPath(paths)
      for (const path of paths) {
        if (inflight.get(path) === req) {
          inflight.delete(path)
          // the server omits images without tags
          tagMap.set(path, res[path] ?? [])
        }
      }
    } catch (e) {
      for (const path of paths) {
        if (inflight.get(path) === req) {
          inflight.delete(path)
          if (tagMap.has(path)) {
            stale.add(path)
          }
        }
      }
      throw e
    }
  }

  // resolves when every path is cached and fresh, rejects if a request failed
  const load = async (paths: string[]) => {
    const waits: Promise<void>[] = []
    const todo: string[] = []
    for (const path of new Set(paths)) {
      const running = inflight.get(path)
      if (running) {
        waits.push(running.done)
      } else if (!tagMap.has(path) || stale.has(path)) {
        todo.push(path)
      }
    }
    if (todo.length) {
      const req = { done: Promise.resolve() }
      todo.forEach((v) => {
        inflight.set(v, req)
        stale.delete(v)
      })
      req.done = requestTags(todo, req)
      waits.push(req.done)
    }
    await Promise.all(waits)
  }

  const fetchImageTags = async (paths: string[]) => {
    try {
      await load(paths)
    } catch {
      // the old value (if any) stays and the next call retries
    }
  }

  /** Tags of the image as the server has them now. Rejects instead of returning [] when they are unknown. */
  const getTags = async (path: string) => {
    // an invalidation or a newer request can land while waiting, so check again
    for (let i = 0; i < 3; i++) {
      await load([path])
      const tags = tagMap.get(path)
      if (tags && !stale.has(path) && !inflight.has(path)) {
        return tags
      }
    }
    throw new Error(`Failed to load tags: ${path}`)
  }
  const colorCache = ref(new Map<string, string>())
  const notifyCacheUpdate = (updateTag?: Tag) => {
    // colorCache.value = cloneDeep(colorCache.value)
    if (updateTag) {
      colorCache.value.set(updateTag.id.toString(), updateTag.color)
    }
  }
  const getColor = (tag: Tag) => {
    const idStr = tag.id.toString()
    let color = colorCache.value.get(idStr)
    if (color) {
      return color
    }
    if (!color && tag.color) {
      colorCache.value.set(idStr, tag.color)
      return tag.color
    }
    if (!color) {
      const hash = sjcl.hash.sha256.hash(idStr)
      const num = parseInt(sjcl.codec.hex.fromBits(hash), 16) % tagColors.length
      color = tagColors[num]
      colorCache.value.set(idStr, color)
    }
    return color
  }
  const refreshTags = async (paths: string[]) => {
    paths.forEach(markStale)
    await fetchImageTags(paths)
  }
  const tagConvert = (tag: GridViewFileTag): Tag => ({
    id: tag.name,
    count: 0,
    display_name: null,
    type: 'temp',
    color: '',
    ...tag
  })
  const set = (path: string, tags: (string|Tag| GridViewFileTag)[]) => {
    const normalizedTags = tags.map(v => tagConvert(typeof v === 'string' ? { name: v } : v))
    tagMap.set(path, normalizedTags)
    inflight.delete(path)
    stale.delete(path)
  }

  const invalidateTagCache = () => {
    // keep the values visible, the listeners of tagsInvalidated refetch what is on screen
    for (const path of tagMap.keys()) {
      stale.add(path)
    }
    inflight.clear()
    globalEvents.emit('tagsInvalidated')
  }

  /**
   * 其他浏览器标签页改了标签：把对应路径的缓存标为过期，再交给已有的补拉链路。
   *
   * 只处理「变化的那几个路径」而不是整个失效：那会让每个 tab 都把整屏可视区
   * 重新拉一遍（几十个请求），而 fetchImageTags 本身只拉缓存里没有或已过期的路径。
   */
  onCrossTab((msg) => {
    if (msg.type !== 'tagsChanged') {
      return
    }
    const bulk = !!msg.all || !msg.paths?.length
    if (bulk) {
      // 标签库本身变了（新增 / 删除 / 改名 / 改色）
      invalidateTagCache()
      // TagSearch / SubstrSearch 靠这个事件重拉 getDbBasicInfo，标签计数才能跟上
      globalEvents.emit('backgroundMaintenanceUpdated')
      return
    }
    msg.paths!.forEach(markStale)
    globalEvents.emit('tagsInvalidated')
  })

  return {
    set,
    colorCache,
    tagMap,
    getColor,
    fetchImageTags,
    getTags,
    invalidateTagCache,
    refreshTags,
    tagConvert,
    notifyCacheUpdate
  }
})
