import { typedEventEmitter } from 'vue3-ts-util'

// 创建视频原地播放的事件发射器（模块级别，所有组件共享）
export const { eventEmitter, useEventListen } = typedEventEmitter<{
  stopInlinePlay: () => void
}>()
