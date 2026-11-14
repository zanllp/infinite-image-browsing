/**
 * 跨浏览器标签页同步通道。
 *
 * 每个浏览器 tab 都是独立的 Vue app / pinia 实例，内存状态天然不共享，所以
 * A tab 打了标签、B tab 的 useTagStore.tagMap 会一直命中旧数据。
 *
 * 这里只做一件事：把「哪些数据失效了」广播出去，绝不搬运状态本身。
 * 状态永远以 sqlite 为准，接收方收到后自己删缓存 + 重拉。
 *
 * 限制：同源（协议 + 域名 + 端口一致）的 tab 才能互通；跨浏览器 / 跨设备
 * 需要后端版本号兜底，不在本模块范围内。
 */

/** 当前 tab 的身份，用于过滤自己发出去的消息 */
export const tabId = `iib-tab-${Math.random().toString(36).slice(2, 10)}`

export interface TagsChangedPayload {
  type: 'tagsChanged'
  /**
   * 发生变化的图片路径。
   * 为空 / 不传 表示标签本身变了（新增、删除、改名、改色），需要整体失效。
   */
  paths?: string[]
  /** 标签库级别的变化，整体失效 */
  all?: boolean
}

export type CrossTabPayload = TagsChangedPayload
export type CrossTabMsg = CrossTabPayload & { from: string; ts: number }
export type CrossTabHandler = (msg: CrossTabMsg) => void

const CHANNEL_NAME = 'iib-cross-tab-sync'

let bus: BroadcastChannel | null = null
const handlers = new Set<CrossTabHandler>()

/** 懒创建：不在模块加载阶段就占一个 BroadcastChannel（Tauri / 旧环境可能没有） */
const ensureBus = (): BroadcastChannel | null => {
  if (bus) {
    return bus
  }
  if (typeof BroadcastChannel === 'undefined') {
    return null
  }
  try {
    bus = new BroadcastChannel(CHANNEL_NAME)
  } catch (error) {
    console.warn('[crossTab] BroadcastChannel unavailable', error)
    return null
  }
  bus.onmessage = (e: MessageEvent) => {
    const msg = e.data as CrossTabMsg | undefined
    // 正常情况 BroadcastChannel 不会把消息发回给发送方，但保留 from 判断更稳
    if (!msg || !msg.type || msg.from === tabId) {
      return
    }
    handlers.forEach((fn) => {
      try {
        fn(msg)
      } catch (error) {
        console.error('[crossTab] handler error', error)
      }
    })
  }
  return bus
}

/**
 * 注册跨 tab 消息处理器，返回取消注册的函数。
 * 同一个处理器重复注册不会报错（Set 去重）。
 */
export const onCrossTab = (handler: CrossTabHandler) => {
  ensureBus()
  handlers.add(handler)
  return () => {
    handlers.delete(handler)
  }
}

/** 广播一条失效通知。失败静默：同步只是增强能力，不能影响主流程 */
export const postCrossTab = (payload: CrossTabPayload) => {
  try {
    const msg = { ...payload, from: tabId, ts: Date.now() } as CrossTabMsg
    ensureBus()?.postMessage(msg)
  } catch (error) {
    console.warn('[crossTab] postMessage failed', error)
  }
}
