<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { gridCellLevelRanges, gridCellWidthLevels, nearestGridCellLevelIndex } from '@/util/gridCellLevel'

/**
 * 缩略图档位滑块。
 *
 * 对用户只暴露「第 N 档」，像素值只是档位的内部映射；
 * v-model 绑定的仍是像素，设置项 / 面板本地状态都不用改存储结构。
 *
 * 拖动过程中只更新本地显示，松手（after-change）才真正应用：
 * 每换一次列数，虚拟列表都要把可见 view 全量重挂，拖动时逐档触发会叠成明显卡顿。
 */
const { t } = useI18n()

const props = defineProps<{ value: number }>()
const emit = defineEmits<{ 'update:value': [value: number] }>()

/** 拖动中的临时档位；null 表示未在拖动 */
const draggingLevel = ref<number | null>(null)

const displayLevel = computed(() => draggingLevel.value ?? nearestGridCellLevelIndex(props.value))

const onDrag = (value?: number) => {
  draggingLevel.value = Number(value ?? 0)
}

const onRelease = (value?: number) => {
  draggingLevel.value = null
  emit('update:value', gridCellWidthLevels[Number(value ?? 0)])
}

/** 每档一个刻度，只标序号 */
const marks = computed(() => Object.fromEntries(
  gridCellWidthLevels.map((_, index) => [index, String(index + 1)]),
))

/** tooltip：档位 + 该档覆盖的 px 区间（文案走 i18n） */
const tipFormatter = (value?: number) => {
  const index = Math.max(0, Math.min(Number(value ?? 0), gridCellLevelRanges.length - 1))
  const [min, max] = gridCellLevelRanges[index]
  return t('gridCellLevelTooltip', { level: index + 1, min, max })
}
</script>

<template>
  <div class="grid-cell-level-slider">
    <a-slider
      :value="displayLevel"
      :min="0"
      :max="gridCellWidthLevels.length - 1"
      :step="1"
      :marks="marks"
      :tip-formatter="tipFormatter"
      @change="onDrag"
      @after-change="onRelease"
    />
  </div>
</template>

<style scoped>
.grid-cell-level-slider {
  /* 设置页里 form-item 是整行宽，不限宽会把滑块拉到上千像素；弹层里仍然自适应 */
  max-width: 340px;
  padding: 0 10px 12px;
}
</style>
