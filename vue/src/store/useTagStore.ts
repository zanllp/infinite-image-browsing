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
  const tagMap = reactive(new Map<string, Tag[]>())
  const fetchImageTags = async (paths: string[]) => {
    paths = paths.filter((v) => !tagMap.has(v))
    if (!paths.length) {
      return
    }
    try {
      paths.forEach((v) => tagMap.set(v, []))
      const res = await batchGetTagsByPath(paths)
      for (const path in res) {
        tagMap.set(path, res[path])
      }
    } catch {
      paths.forEach((v) => tagMap.delete(v))
    }
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
    paths.forEach((v) => tagMap.delete(v))
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
  }

  const invalidateTagCache = () => {
    tagMap.clear()
    globalEvents.emit('tagsInvalidated')
  }

  /**
   * 其他浏览器标签页改了标签：删掉对应路径的缓存，再交给已有的补拉链路。
   *
   * 只删「变化的那几个路径」而不是整个 clear：clear 会让每个 tab 都把整屏可视区
   * 重新拉一遍（几十个请求），而 fetchImageTags 本身只拉缓存里没有的路径。
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
    msg.paths!.forEach((path) => tagMap.delete(path))
    globalEvents.emit('tagsInvalidated')
  })

  return {
    set,
    colorCache,
    tagMap,
    getColor,
    fetchImageTags,
    invalidateTagCache,
    refreshTags,
    tagConvert,
    notifyCacheUpdate
  }
})
