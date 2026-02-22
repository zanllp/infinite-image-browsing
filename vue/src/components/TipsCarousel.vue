<template>
  <div v-if="availableTips.length > 0" class="tips-carousel">
    <transition name="tip-fade" mode="out-in">
      <div :key="currentIndex" class="tip-content">
        <a-tag :color="getTipColor(currentTip.type)" class="tip-tag">
          {{ currentTip.title }}
        </a-tag>
        <span class="tip-text">{{ currentTip.content }}</span>
        <div v-if="currentTip.type === 'warning'" class="tip-actions">
          <a-button size="small" type="link" @click="dismissCurrentTip">
            {{ t('dontShowAgain') }}
          </a-button>
        </div>
      </div>
    </transition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useLocalStorage } from '@vueuse/core'
import { t } from '@/i18n'

interface LoadingTip {
  title: string
  content: string
  type: 'warning' | 'info' | 'tip'
}

const props = defineProps({
  interval: {
    type: Number,
    default: 10000 // 10秒切换一次
  }
})

// 存储用户已关闭的提示
const dismissedTips = useLocalStorage<Record<string, boolean>>('iib-dismissed-tips-v2', {})

// 当前提示索引
const currentIndex = ref(0)
let tipIntervalId: ReturnType<typeof setInterval> | null = null

// 获取所有可用的提示
const availableTips = computed<LoadingTip[]>(() => {
  const tips: LoadingTip[] = []

  // 遍历 loadingTip1 到 loadingTip10
  for (let i = 1; i <= 10; i++) {
    const key = `loadingTip${i}` as const
    const rawTip = t(key) as string

    if (!rawTip || typeof rawTip !== 'string') continue

    // 解析格式: "标题\n\n内容|类型"
    const parts = rawTip.split('|')
    const contentPart = parts[0]
    const typePart = parts[1]?.trim() || 'info'

    // 分离标题和内容
    const titleContentSeparator = '\n\n'
    const separatorIndex = contentPart.indexOf(titleContentSeparator)

    if (separatorIndex === -1) continue

    const title = contentPart.substring(0, separatorIndex).trim()
    const content = contentPart.substring(separatorIndex + titleContentSeparator.length).trim()

    const tip: LoadingTip = {
      title,
      content,
      type: typePart as 'warning' | 'info' | 'tip'
    }

    // 如果是警告类型且用户已关闭，则跳过
    if (tip.type === 'warning' && dismissedTips.value[title]) {
      continue
    }

    tips.push(tip)
  }

  // Fisher-Yates 洗牌算法随机打乱顺序
  for (let i = tips.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [tips[i], tips[j]] = [tips[j], tips[i]]
  }

  return tips
})

// 当前显示的提示
const currentTip = computed<LoadingTip>(() => {
  const tips = availableTips.value
  if (tips.length === 0) {
    return {
      title: '',
      content: '',
      type: 'info'
    }
  }
  return tips[currentIndex.value % tips.length]
})

// 获取提示颜色
const getTipColor = (type: string) => {
  switch (type) {
    case 'warning':
      return 'warning'
    case 'info':
      return 'blue'
    case 'tip':
      return 'green'
    default:
      return 'default'
  }
}

// 关闭当前提示
const dismissCurrentTip = () => {
  if (currentTip.value.type === 'warning') {
    dismissedTips.value = {
      ...dismissedTips.value,
      [currentTip.value.title]: true
    }
  }
}

// 启动轮播
const startCarousel = () => {
  if (tipIntervalId) {
    clearInterval(tipIntervalId)
  }
  tipIntervalId = setInterval(() => {
    if (availableTips.value.length > 1) {
      currentIndex.value = (currentIndex.value + 1) % availableTips.value.length
    }
  }, props.interval)
}

// 停止轮播
const stopCarousel = () => {
  if (tipIntervalId) {
    clearInterval(tipIntervalId)
    tipIntervalId = null
  }
}

onMounted(() => {
  startCarousel()
})

onUnmounted(() => {
  stopCarousel()
})
</script>

<style scoped>
.tips-carousel {
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(0, 0, 0, 0.02);
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.tip-content {
  display: flex;
  flex-direction: row;
  gap: 6px;
  align-items: flex-start;
  text-align: left;
}

.tip-tag {
  font-weight: 500;
  flex-shrink: 0;
  font-size: 11px;
  padding: 0 4px;
  line-height: 18px;
}

.tip-text {
  margin: 0;
  font-size: 12px;
  line-height: 18px;
  color: rgba(0, 0, 0, 0.65);
  flex: 1;
}

.tip-actions {
  align-self: flex-start;
  margin-top: 0;
  margin-left: auto;
}

.tip-actions :deep(.ant-btn) {
  padding: 0 4px;
  font-size: 11px;
  height: 20px;
}

/* 提示切换动画 */
.tip-fade-enter-active,
.tip-fade-leave-active {
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

.tip-fade-enter-from {
  opacity: 0;
  transform: translateY(5px);
}

.tip-fade-leave-to {
  opacity: 0;
  transform: translateY(-5px);
}

/* 暗色主题适配 */
html.dark .tips-carousel {
  background: rgba(255, 255, 255, 0.05);
  border-color: rgba(255, 255, 255, 0.1);
}

html.dark .tip-text {
  color: rgba(255, 255, 255, 0.65);
}
</style>
