/**
 * RecycleScroller 渲染窗口的索引范围。
 *
 * 背景：vue-virtual-scroller 从 2.0.0-beta.9 起把 RecycleScroller 重写成了
 * 组合式实现（useRecycleScroller），内部字段 `$_startIndex` / `$_endIndex`
 * 不再挂在组件实例上，只通过 defineExpose 暴露 `visiblePool`
 * （已按 nr.index 排序的「已分配 view」列表）。
 *
 * 这里统一收口，避免各处直接摸组件内部结构；拿不到时返回 { start: 0, end: 0 }。
 */
export interface VisibleRange {
  /** 渲染窗口的起始索引（含） */
  start: number
  /** 渲染窗口的结束索引（不含） */
  end: number
}

export const getVisibleRange = (scroller: any): VisibleRange => {
  const raw = scroller?.visiblePool
  // 兼容两种情况：模板 ref 上被 proxyRefs 解包成数组，或者仍是 computed ref
  const pool: any[] = Array.isArray(raw) ? raw : (raw?.value ?? [])
  if (!pool.length) {
    return { start: 0, end: 0 }
  }

  // visiblePool 本身已排序，这里仍取 min/max 兜底，防止上游实现调整顺序
  let start = Number.POSITIVE_INFINITY
  let end = -1
  for (const view of pool) {
    const idx = view?.nr?.index
    if (typeof idx !== 'number' || !Number.isFinite(idx)) continue
    if (idx < start) start = idx
    if (idx > end) end = idx
  }
  if (end < 0) {
    return { start: 0, end: 0 }
  }
  return { start, end: end + 1 }
}

/**
 * 某个索引是否落在当前渲染窗口内（含预渲染 buffer，`end` 为开区间）。
 */
export const isIndexInVisibleRange = (scroller: any, idx: number) => {
  const { start, end } = getVisibleRange(scroller)
  return idx >= start && idx < end
}
