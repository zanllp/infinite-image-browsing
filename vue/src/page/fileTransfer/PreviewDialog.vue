<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, shallowRef, watch, nextTick } from 'vue'
import Dialog from 'ant-design-vue/es/vc-dialog'
import 'ant-design-vue/es/image/style'
import {
  CloseOutlined,
  ExpandOutlined,
  OneToOneOutlined,
  RotateLeftOutlined,
  RotateRightOutlined,
  ZoomInOutlined,
  ZoomOutOutlined
} from '@/icon'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import {
  SCALE_EPSILON,
  ZOOM_STEP,
  centerPosition,
  clampPosition,
  fitScale,
  oneToOneScale,
  scaleLimits,
  wheelFactor,
  zoomAt,
  zoomKeyAction,
  type PreviewDims
} from '@/util/previewZoom'

/**
 * 全屏预览弹层，是 ant-design-vue 3.2.20 `vc-image/src/Preview.js` 的本地版本：
 * 同样的 Dialog 和 DOM（`ant-image-preview-*` 类名，样式和 IIB 里依赖这些类名的地方不变），
 * 但工具栏多了「原始大小 / 适应窗口」，缩放改为连续比例、以鼠标/视图中心为锚点，并限制拖动范围。
 * antd 的预览工具栏固定且拿不到 scale，只能替换整个组件。
 */
const props = defineProps<{
  src: string
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
  afterClose: []
}>()

const prefixCls = 'ant-image-preview'
const globalStore = useGlobalStore()

const scale = ref(1)
const rotate = ref(0)
const position = reactive({ x: 0, y: 0 })
const isMoving = ref(false)
const imgRef = ref<HTMLImageElement>()
const wrapperRef = ref<HTMLElement>()
let dragStart = { x: 0, y: 0 }

// 布局尺寸（不含 transform），用于计算缩放上下限；加载、窗口/面板尺寸变化时更新
const baseDims = shallowRef<Omit<PreviewDims, 'rotate'> | null>(null)

const measure = (): PreviewDims | null => {
  const img = imgRef.value
  const area = wrapperRef.value
  if (!img || !area || !img.offsetWidth || !img.naturalWidth || !area.clientWidth || !area.clientHeight) {
    return null
  }
  baseDims.value = {
    baseW: img.offsetWidth,
    baseH: img.offsetHeight,
    naturalW: img.naturalWidth,
    areaW: area.clientWidth,
    areaH: area.clientHeight,
    offsetX: img.offsetLeft + img.offsetWidth / 2 - area.clientWidth / 2,
    offsetY: img.offsetTop + img.offsetHeight / 2 - area.clientHeight / 2
  }
  return { ...baseDims.value, rotate: rotate.value }
}

const limits = computed(() =>
  baseDims.value ? scaleLimits({ ...baseDims.value, rotate: rotate.value }) : null
)
const canZoomOut = computed(() => !limits.value || scale.value > limits.value.min + SCALE_EPSILON)
const canZoomIn = computed(() => !limits.value || scale.value < limits.value.max - SCALE_EPSILON)

const setPosition = (p: { x: number; y: number }) => {
  position.x = p.x
  position.y = p.y
}

const reclamp = () => {
  // the configured opening zoom follows layout changes (e.g. the info panel appearing) until the user takes over
  if (followInitialZoom && globalStore.previewInitialZoom !== 'shrinkToFit') return applyInitialZoom()
  const d = measure()
  // the untouched opening state stays as antd lays it out (no shift by a few px after loading)
  const pristine = scale.value === 1 && rotate.value === 0 && position.x === 0 && position.y === 0
  if (d && !isMoving.value && !pristine) setPosition(clampPosition(position, scale.value, d))
}

const zoomBy = (factor: number, point = { x: 0, y: 0 }) => {
  const d = measure()
  if (!d) return
  followInitialZoom = false
  const next = zoomAt({ scale: scale.value, position: { ...position } }, factor, point, d)
  scale.value = next.scale
  setPosition(next.position)
}

const onZoomIn = () => {
  if (canZoomIn.value) zoomBy(ZOOM_STEP)
}
const onZoomOut = () => {
  if (canZoomOut.value) zoomBy(1 / ZOOM_STEP)
}
const showOneToOne = (d: PreviewDims) => {
  scale.value = oneToOneScale(d)
  setPosition(centerPosition(d))
}
const showFit = (d: PreviewDims) => {
  scale.value = fitScale(d)
  setPosition(centerPosition(d))
}
const onOneToOne = () => {
  const d = measure()
  if (!d) return
  followInitialZoom = false
  showOneToOne(d)
}
const onFit = () => {
  const d = measure()
  if (!d) return
  followInitialZoom = false
  showFit(d)
}
const rotateBy = (deg: number) => {
  followInitialZoom = false
  rotate.value += deg
  reclamp()
}
const onClose = () => emit('close')

// the view is in the configured opening zoom (global setting) and nobody has zoomed, dragged or rotated since: it is
// applied again whenever the image loads or the layout changes. `shrinkToFit` needs nothing, antd's max-size CSS
// already follows the layout.
let followInitialZoom = true

// back to the opening state: by default fitted by antd's max-size CSS, upright, centred
const resetView = () => {
  scale.value = 1
  rotate.value = 0
  isMoving.value = false
  setPosition({ x: 0, y: 0 })
  followInitialZoom = true
}

// the other modes are the state of the fit / actual size buttons; without a measurable image it waits for the load
const applyInitialZoom = () => {
  const mode = globalStore.previewInitialZoom
  // while a switched image is loading, the old one may still be measured
  if (mode === 'shrinkToFit' || !imgRef.value?.complete) return
  const d = measure()
  if (!d) return
  if (mode === 'fit') showFit(d)
  else showOneToOne(d)
}

const onAfterClose = () => {
  resetView()
  emit('afterClose')
}

// 切换图片时回到初始状态；新图异步加载，加载前旧的测量值不再适用，load 时重新测量
watch(
  () => props.src,
  () => {
    resetView()
    baseDims.value = null
  }
)

const onMouseDown = (e: MouseEvent) => {
  // only the main button
  if (e.button !== 0) return
  e.preventDefault()
  // without this the mask would close
  e.stopPropagation()
  dragStart = { x: e.pageX - position.x, y: e.pageY - position.y }
  isMoving.value = true
}

const onMouseMove = (e: MouseEvent) => {
  if (props.visible && isMoving.value) {
    followInitialZoom = false
    setPosition({ x: e.pageX - dragStart.x, y: e.pageY - dragStart.y })
  }
}

const onMouseUp = () => {
  if (props.visible && isMoving.value) {
    isMoving.value = false
    reclamp()
  }
}

const onWheel = (e: WheelEvent) => {
  if (!props.visible) return
  e.preventDefault()
  const d = measure()
  const origin = wrapperRef.value?.parentElement?.getBoundingClientRect()
  if (!d || !origin) return
  zoomBy(wheelFactor(e, d.areaH), {
    x: e.clientX - origin.left - d.areaW / 2,
    y: e.clientY - origin.top - d.areaH / 2
  })
}

const onKeyDown = (e: KeyboardEvent) => {
  if (!props.visible) return
  const action = zoomKeyAction(e)
  if (!action) return
  e.preventDefault()
  if (action === 'in') onZoomIn()
  else if (action === 'out') onZoomOut()
  else if (action === 'oneToOne') onOneToOne()
  else onFit()
}

const onDoubleClick = () => {
  if (!props.visible) return
  // back to the opening zoom (rotation stays); fit / actual size follow layout changes again
  followInitialZoom = true
  scale.value = 1
  setPosition({ x: 0, y: 0 })
  applyInitialZoom()
}

// 与 antd 一样，监听器绑在 window 上并随 visible 开关
const listeners: [keyof WindowEventMap, (e: any) => void, AddEventListenerOptions?][] = [
  ['mouseup', onMouseUp],
  ['mousemove', onMouseMove],
  ['wheel', onWheel, { passive: false }],
  ['keydown', onKeyDown]
]
let removeListeners = () => {}
watch(
  () => props.visible,
  (visible) => {
    removeListeners()
    removeListeners = () => {}
    if (!visible) return
    listeners.forEach(([name, fn, opt]) => window.addEventListener(name, fn, opt))
    removeListeners = () => listeners.forEach(([name, fn, opt]) => window.removeEventListener(name, fn, opt))
    // reopened with the same, already loaded image: no load event will come, reclamp applies the opening zoom
    nextTick(reclamp)
  },
  { immediate: true }
)

// 面板宽度（LR 布局）或窗口变化时重新测量并修正位置
let observer: ResizeObserver | undefined
watch([imgRef, wrapperRef], ([img, wrapper]) => {
  observer?.disconnect()
  if (!img || !wrapper || typeof ResizeObserver === 'undefined') return
  observer = new ResizeObserver(reclamp)
  observer.observe(img)
  observer.observe(wrapper)
})

onBeforeUnmount(() => {
  removeListeners()
  observer?.disconnect()
})

const wrapClassName = computed(() => (isMoving.value ? `${prefixCls}-moving` : ''))

// 工具栏由 CSS 的 row-reverse 从右向左排列：关闭、放大、适应、1:1、缩小、右转、左转
const tools = computed(() => [
  { type: 'close', icon: CloseOutlined, title: t('close'), onClick: onClose, disabled: false },
  { type: 'zoomIn', icon: ZoomInOutlined, title: t('previewZoomIn'), onClick: onZoomIn, disabled: !canZoomIn.value },
  { type: 'fit', icon: ExpandOutlined, title: t('previewFitToWindow'), onClick: onFit, disabled: false },
  { type: 'oneToOne', icon: OneToOneOutlined, title: t('previewActualSize'), onClick: onOneToOne, disabled: false },
  { type: 'zoomOut', icon: ZoomOutOutlined, title: t('previewZoomOut'), onClick: onZoomOut, disabled: !canZoomOut.value },
  { type: 'rotateRight', icon: RotateRightOutlined, title: t('previewRotateRight'), onClick: () => rotateBy(90), disabled: false },
  { type: 'rotateLeft', icon: RotateLeftOutlined, title: t('previewRotateLeft'), onClick: () => rotateBy(-90), disabled: false }
])
</script>

<template>
  <Dialog
    :visible="visible"
    :prefix-cls="prefixCls"
    transition-name="ant-zoom"
    mask-transition-name="ant-fade"
    :closable="false"
    :keyboard="true"
    :wrap-class-name="wrapClassName"
    :after-close="onAfterClose"
    @close="onClose"
  >
    <ul :class="`${prefixCls}-operations`">
      <li
        v-for="tool in tools"
        :key="tool.type"
        :class="[
          `${prefixCls}-operations-operation`,
          { [`${prefixCls}-operations-operation-disabled`]: tool.disabled }
        ]"
        :title="tool.title"
        @click="tool.onClick"
      >
        <component :is="tool.icon" :class="`${prefixCls}-operations-icon`" />
      </li>
    </ul>
    <div
      ref="wrapperRef"
      :class="`${prefixCls}-img-wrapper`"
      :style="{ transform: `translate3d(${position.x}px, ${position.y}px, 0)` }"
    >
      <!-- fullScreenContextMenu.vue 按 className === 'ant-image-preview-img' 识别这张图，类名不能多 -->
      <img
        ref="imgRef"
        class="ant-image-preview-img"
        :src="src"
        :style="{ transform: `scale3d(${scale}, ${scale}, 1) rotate(${rotate}deg)` }"
        @mousedown="onMouseDown"
        @dblclick="onDoubleClick"
        @load="reclamp"
      />
    </div>
  </Dialog>
</template>
