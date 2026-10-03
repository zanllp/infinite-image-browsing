import { reactive } from 'vue'
import type { FileNodeInfo } from '@/api/files'
import { toStreamVideoUrl } from './file'

/**
 * 视频时长的懒加载探测。
 * 用浏览器原生 <video> 读 metadata（只取文件头部），结果按 fullpath + 修改时间缓存，
 * 并做并发限流，避免滚动浏览大目录时打出一堆请求。
 * 浏览器读不出时长（mkv/avi 之类）时缓存 null，不重复探测。
 */
const durationCache = reactive(new Map<string, number | null>())
const pending = new Map<string, Promise<number | null>>()

const MAX_CONCURRENT = 3
let running = 0
const waiters: Array<() => void> = []

const acquire = () =>
  new Promise<void>((resolve) => {
    if (running < MAX_CONCURRENT) {
      running++
      resolve()
      return
    }
    waiters.push(() => {
      running++
      resolve()
    })
  })

const release = () => {
  running--
  waiters.shift()?.()
}

const cacheKey = (file: FileNodeInfo) => `${file.fullpath}|${file.date ?? ''}`

/** 已有结果时同步返回；undefined 表示还没探测过 */
export const getCachedVideoDuration = (file: FileNodeInfo): number | null | undefined => {
  const key = cacheKey(file)
  return durationCache.has(key) ? durationCache.get(key) ?? null : undefined
}

const probe = (file: FileNodeInfo) =>
  new Promise<number | null>((resolve) => {
    const video = document.createElement('video')
    video.preload = 'metadata'
    video.muted = true
    let settled = false
    const finish = (value: number | null) => {
      if (settled) {
        return
      }
      settled = true
      video.removeEventListener('loadedmetadata', onLoaded)
      video.removeEventListener('error', onError)
      // 释放连接，避免探测完还继续缓冲
      video.removeAttribute('src')
      video.load()
      resolve(value)
    }
    const onLoaded = () => {
      const duration = video.duration
      finish(Number.isFinite(duration) && duration > 0 ? duration : null)
    }
    const onError = () => finish(null)
    video.addEventListener('loadedmetadata', onLoaded)
    video.addEventListener('error', onError)
    video.src = toStreamVideoUrl(file)
  })

/** 探测视频时长（秒）；结果会缓存，不支持或失败时返回 null */
export const loadVideoDuration = async (file: FileNodeInfo): Promise<number | null> => {
  const key = cacheKey(file)
  if (durationCache.has(key)) {
    return durationCache.get(key) ?? null
  }
  const inflight = pending.get(key)
  if (inflight) {
    return inflight
  }
  const task = (async () => {
    await acquire()
    try {
      const duration = await probe(file)
      durationCache.set(key, duration)
      return duration
    } finally {
      release()
    }
  })()
  pending.set(key, task)
  try {
    return await task
  } finally {
    pending.delete(key)
  }
}
