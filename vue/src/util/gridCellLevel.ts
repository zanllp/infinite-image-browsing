/**
 * 缩略图档位。
 *
 * 用户只选「档位」，不直接填像素：档位只是一个「目标宽度」，
 * 实际单格宽度由窗口可用宽度 auto-fit 得出（见 useFilesDisplay 的 gridItems / gridSize）。
 *
 * 8 档按几何级数排布（每档 ≈ ×1.49，覆盖原 64~1024 范围），
 * 换算到常见窗口宽度后列数步进约等于 16 → 12 → 8 → 6 → 4 → 3 → 2 → 1，
 * 每滑一格密度变化幅度接近，手感均匀。
 */
export const gridCellWidthLevels = [64, 96, 144, 208, 312, 464, 688, 1024] as const

/**
 * 每个档位对应的目标宽度区间。
 * 边界取相邻档位的几何平均，和 nearestGridCellLevelIndex 的对数吸附保持一致
 * （即任意 px 值都能落到唯一一档，且边界与滑块视觉中点吻合）。
 */
export const gridCellLevelRanges: ReadonlyArray<readonly [number, number]> = (() => {
  const bounds: number[] = [gridCellWidthLevels[0]]
  for (let index = 1; index < gridCellWidthLevels.length; index++) {
    bounds.push(Math.round(Math.sqrt(gridCellWidthLevels[index - 1] * gridCellWidthLevels[index])))
  }
  bounds.push(gridCellWidthLevels[gridCellWidthLevels.length - 1])
  return gridCellWidthLevels.map((_, index) => [bounds[index], bounds[index + 1]] as const)
})()

/**
 * 找最接近的档位索引。
 * 档位是几何级数，所以按对数距离取最近；用线性距离会让大档位区间过宽、把低档位全吸过去。
 */
export const nearestGridCellLevelIndex = (width: number) => {
  const w = Math.max(1, Number(width) || 0)
  let bestIndex = 0
  let bestDistance = Number.POSITIVE_INFINITY
  gridCellWidthLevels.forEach((levelWidth, index) => {
    const distance = Math.abs(Math.log(levelWidth) - Math.log(w))
    if (distance < bestDistance) {
      bestDistance = distance
      bestIndex = index
    }
  })
  return bestIndex
}

/** 把任意历史像素值吸附到最近的档位像素值（兼容老配置里存的精确宽度） */
export const nearestGridCellLevel = (width: number) =>
  gridCellWidthLevels[nearestGridCellLevelIndex(width)]
