/**
 * Pure helpers for the zoom of the fullscreen preview (no Vue, no DOM).
 *
 * `scale` is relative to the base layout of the preview image: the image is laid out with
 * `max-width/max-height: 100%` of the area (big images are fitted, small ones keep their size),
 * and the transform scales that box. Positions are the translation of the image wrapper, in CSS px.
 */

export interface Point {
  x: number
  y: number
}

export interface PreviewDims {
  /** layout size of the image without transform (`offsetWidth/offsetHeight`) */
  baseW: number
  baseH: number
  /** `naturalWidth` of the image */
  naturalW: number
  /** size of the area the image is shown in */
  areaW: number
  areaH: number
  /**
   * where the center of the image box lies relative to the center of the area (px, no transform).
   * Not always 0: the preview css aligns the image on the text baseline, which shifts it by a few px.
   */
  offsetX: number
  offsetY: number
  /** rotation in degrees (multiples of 90) */
  rotate: number
}

export interface ZoomState {
  scale: number
  position: Point
}

export type ZoomKeyAction = 'in' | 'out' | 'oneToOne' | 'fit'

/** zoom factor of the buttons and the + / - keys (two steps double or halve the size) */
export const ZOOM_STEP = Math.SQRT2
/** the upper limit is this many times the actual size */
export const MAX_ACTUAL_SIZE_FACTOR = 16
/** tolerance for comparing scales (floating point, rounded layout sizes) */
export const SCALE_EPSILON = 0.002

const WHEEL_SENSITIVITY = 0.003
/** trackpad pinch arrives as ctrl+wheel with small deltas (a few px per event) */
const PINCH_SENSITIVITY = 0.01
/** at most this much log-zoom per event (ctrl + a mouse wheel notch would jump otherwise) */
const MAX_WHEEL_STEP = 0.5
const WHEEL_LINE_PX = 16

const isQuarterTurn = (rotate: number) => Math.abs(rotate) % 180 === 90

/** size of the image box as it is seen on screen at the given scale (swapped at 90/270 degrees) */
export const visualSize = (d: PreviewDims, scale: number) => {
  const w = isQuarterTurn(d.rotate) ? d.baseH : d.baseW
  const h = isQuarterTurn(d.rotate) ? d.baseW : d.baseH
  return { w: w * scale, h: h * scale }
}

/** scale at which one image pixel is one CSS pixel */
export const oneToOneScale = (d: PreviewDims) => d.naturalW / d.baseW

/** scale at which the whole image fills the area on one axis (also enlarges small images) */
export const fitScale = (d: PreviewDims) => {
  const { w, h } = visualSize(d, 1)
  return Math.min(d.areaW / w, d.areaH / h)
}

export const scaleLimits = (d: PreviewDims) => {
  const fit = fitScale(d)
  const one = oneToOneScale(d)
  return {
    min: Math.min(1, fit, one),
    max: Math.max(one * MAX_ACTUAL_SIZE_FACTOR, fit)
  }
}

const clamp = (v: number, min: number, max: number) => Math.min(max, Math.max(min, v))

/** position that puts the center of the image in the center of the area */
export const centerPosition = (d: PreviewDims): Point => ({ x: 0 - d.offsetX, y: 0 - d.offsetY })

/** keep the image from leaving gaps at the edges; an image smaller than the area is centered */
export const clampPosition = (position: Point, scale: number, d: PreviewDims): Point => {
  const { w, h } = visualSize(d, scale)
  const c = centerPosition(d)
  const maxX = Math.max(0, (w - d.areaW) / 2)
  const maxY = Math.max(0, (h - d.areaH) / 2)
  return {
    x: clamp(position.x, c.x - maxX, c.x + maxX),
    y: clamp(position.y, c.y - maxY, c.y + maxY)
  }
}

/**
 * Zoom by `factor` and keep the image point under `point` fixed.
 * `point` is relative to the center of the area.
 */
export const zoomAt = (state: ZoomState, factor: number, point: Point, d: PreviewDims): ZoomState => {
  const { min, max } = scaleLimits(d)
  const scale = clamp(state.scale * factor, min, max)
  const k = scale / state.scale
  // the image scales around its own center, which is offset from the center of the area
  const q = { x: point.x - d.offsetX, y: point.y - d.offsetY }
  const position = clampPosition(
    {
      x: q.x - (q.x - state.position.x) * k,
      y: q.y - (q.y - state.position.y) * k
    },
    scale,
    d
  )
  return { scale, position }
}

/** zoom factor for a wheel event (smooth, also for trackpad pinch which arrives as ctrl+wheel) */
export const wheelFactor = (
  e: { deltaY: number; deltaMode: number; ctrlKey?: boolean },
  areaH: number
) => {
  const px = e.deltaMode === 1 ? e.deltaY * WHEEL_LINE_PX : e.deltaMode === 2 ? e.deltaY * areaH : e.deltaY
  const step = px * (e.ctrlKey ? PINCH_SENSITIVITY : WHEEL_SENSITIVITY)
  return Math.exp(-clamp(step, -MAX_WHEEL_STEP, MAX_WHEEL_STEP))
}

export interface ZoomKeyEvent {
  key: string
  ctrlKey?: boolean
  metaKey?: boolean
  altKey?: boolean
  defaultPrevented?: boolean
  target?: object | null
}

/** which zoom action a key press means, `null` if none (or if the key belongs to something else) */
export const zoomKeyAction = (e: ZoomKeyEvent): ZoomKeyAction | null => {
  // a configured shortcut has already handled it / the browser zoom stays free
  if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return null
  const target = e.target as { tagName?: string; isContentEditable?: boolean } | null | undefined
  const tag = target?.tagName?.toLowerCase()
  if (tag === 'input' || tag === 'textarea' || target?.isContentEditable) return null
  switch (e.key) {
    case '+':
    case '=':
      return 'in'
    case '-':
      return 'out'
    case '1':
      return 'oneToOne'
    case '0':
      return 'fit'
    default:
      return null
  }
}
