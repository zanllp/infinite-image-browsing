<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'
import type { FileNodeInfo } from '@/api/files'
import { toImageUrl } from '@/util/file'
import { registerFullscreenPreviewCloser } from '@/util/imagePreviewOperation'
import PreviewDialog from './PreviewDialog.vue'

/**
 * 应用级全屏预览宿主。
 *
 * 以前全屏预览挂在每个列表项自己的 `a-image` 上：列表一更新（自动刷新/删除），列表项被
 * 复用重绑，持有预览的 `a-image` 就被卸载，预览随之销毁。现在列表外只有这一个预览实例，
 * `src` 由 `files[previewIdx]` 驱动，列表怎么更新都不会碰到它。
 *
 * 注意：共享状态（useHookShareState）是按组件实例存的，子组件里取不到父组件的状态，
 * 所以这里必须通过 props 传进来。
 */
const props = defineProps<{
  files: FileNodeInfo[]
  previewIdx: number
  previewing: boolean
}>()

const emit = defineEmits<{
  visibleChange: [value: boolean, last: boolean]
}>()

const currentFile = computed(() => props.files[props.previewIdx])
const previewSrc = computed(() => (currentFile.value ? toImageUrl(currentFile.value) : ''))

// 与 antd 的 a-image 一致：visible 每次变化（包括由 prop 驱动的）都通知外部，
// usePreview 靠它在关闭时滚动列表
watch(
  () => props.previewing,
  (visible, lastVisible) => emit('visibleChange', visible, lastVisible)
)

// 全屏查看时其它入口（右键菜单、删除、快捷键）通过 util 里的注册表关闭预览
const unregister = registerFullscreenPreviewCloser(() => {
  emit('visibleChange', false, true)
})
onBeforeUnmount(unregister)
</script>

<template>
  <PreviewDialog
    v-if="currentFile"
    :src="previewSrc"
    :visible="previewing"
    @close="emit('visibleChange', false, true)"
  />
</template>
