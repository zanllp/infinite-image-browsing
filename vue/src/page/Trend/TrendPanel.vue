<template>
  <div class="trend-panel">
    <div v-if="loading" class="loading-container">
      <a-spin size="large" />
    </div>

    <a-alert v-else-if="error" type="error" :message="error" show-icon />

    <template v-else-if="stats">
      <div class="summary-bar">
        <a-statistic :title="t('trendTotalImages')" :value="stats.total_images" />
        <a-statistic :title="t('trendTotalDiskUsage')" :value="formatSize(stats.total_disk_usage)" />
      </div>

      <div class="chart-section">
        <h3>{{ t('trendContributionHeatmap') }}</h3>
        <div class="heatmap-wrap" v-if="heatmap">
          <div class="heatmap-row">
            <div class="heatmap-day-col" :style="{ marginTop: heatmap.labelsH + 'px' }">
              <span v-for="d in dayLabels" :key="d.label" :style="{ height: CELL + 'px', marginBottom: GAP + 'px' }">{{ d.label }}</span>
            </div>
            <div class="heatmap-scroll" ref="heatmapScrollRef" @wheel="onHeatmapWheel">
              <div class="heatmap-body" :style="{ width: heatmap.bodyW + 'px', height: heatmap.bodyH + 'px' }">
                <!-- year labels -->
                <span
                  v-for="(yl, i) in heatmap.yearLabels" :key="'y'+i"
                  class="heatmap-year-label"
                  :style="{ left: yl.col * STEP + 'px', width: yl.cols * STEP - GAP + 'px' }"
                >{{ yl.label }}</span>
                <!-- month labels -->
                <span
                  v-for="(ml, i) in heatmap.monthLabels" :key="'m'+i"
                  class="heatmap-month-label"
                  :style="{ left: ml.col * STEP + 'px', width: ml.cols * STEP - GAP + 'px' }"
                >{{ ml.label }}</span>
                <!-- cells -->
                <div
                  v-for="(cell, ci) in heatmap.cells" :key="ci"
                  class="heatmap-cell"
                  :style="{
                    left: cell.w * STEP + 'px',
                    top: heatmap.labelsH + cell.d * STEP + 'px',
                    background: cellColor(cell.count),
                  }"
                  :title="cell.date + ': ' + cell.count + ' images'"
                ></div>
              </div>
            </div>
          </div>
          <div class="heatmap-legend">
            <span>{{ t('trendHeatmapLess') }}</span>
            <span class="legend-swatch" v-for="(lv, i) in legendLevels" :key="lv" :style="{ background: lv }" :title="legendTitles[i]"></span>
            <span>{{ t('trendHeatmapMore') }}</span>
          </div>
        </div>
      </div>

      <div class="chart-section">
        <h3>{{ t('trendMonthlyTrend') }}</h3>
        <div ref="monthlyRef" class="chart-container chart-bar"></div>
      </div>

      <div class="charts-row">
        <div class="chart-section chart-half">
          <h3>{{ t('trendTopModels') }}</h3>
          <div ref="modelRef" class="chart-container chart-bar"></div>
        </div>
        <div class="chart-section chart-half">
          <h3>{{ t('trendTopSource') }}</h3>
          <div ref="sourceRef" class="chart-container chart-bar"></div>
        </div>
      </div>
    </template>

    <a-empty v-else :description="t('trendNoData')" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, nextTick, onUnmounted, watch } from 'vue'
import * as echarts from 'echarts'
import { getTrendStats, type TrendStats } from '@/api/db'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'

defineProps<{
  tabIdx: number
  paneIdx: number
  paneKey: string
}>()

const global = useGlobalStore()
const loading = ref(false)
const error = ref('')
const stats = ref<TrendStats | null>(null)

const monthlyRef = ref<HTMLDivElement>()
const modelRef = ref<HTMLDivElement>()
const sourceRef = ref<HTMLDivElement>()
const heatmapScrollRef = ref<HTMLDivElement>()

let monthlyChart: echarts.ECharts | null = null
let modelChart: echarts.ECharts | null = null
let sourceChart: echarts.ECharts | null = null

const HEATMAP_YEARS = 3
const CELL = 14
const GAP = 3
const STEP = CELL + GAP // 17
const YEAR_ROW_H = 16
const MONTH_ROW_H = 14
const ROW_GAP = 2

const monthNames = computed(() => {
  const fmt = new Intl.DateTimeFormat(global.lang, { month: 'short' })
  return Array.from({ length: 12 }, (_, i) => fmt.format(new Date(2024, i, 1)))
})

const dayLabels = computed(() => {
  const fmt = new Intl.DateTimeFormat(global.lang, { weekday: 'short' })
  return Array.from({ length: 7 }, (_, i) =>
    ({ label: fmt.format(new Date(2024, 0, i + 8)) })
  )
})

const legendLevels = computed(() => [
  global.computedTheme === 'dark' ? '#1a1a2e' : '#ebedf0',
  '#9be9a8',
  '#40c463',
  '#30a14e',
  '#216e39',
])

const formatSize = (bytes: number): string => {
  if (!bytes) return '0 B'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), u.length - 1)
  return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + ' ' + u[i]
}

const LEVEL_STEP = 30

const legendTitles = computed(() => [
  '0',
  '1-' + LEVEL_STEP,
  (LEVEL_STEP + 1) + '-' + LEVEL_STEP * 2,
  (LEVEL_STEP * 2 + 1) + '-' + LEVEL_STEP * 3,
  '>' + LEVEL_STEP * 3,
])

const cellColor = (count: number): string => {
  const levels = legendLevels.value
  if (count === 0) return levels[0]
  if (count <= LEVEL_STEP) return levels[1]
  if (count <= LEVEL_STEP * 2) return levels[2]
  if (count <= LEVEL_STEP * 3) return levels[3]
  return levels[4]
}

interface Cell {
  date: string
  count: number
  w: number // week index
  d: number // day index 0=Mon..6=Sun
}

interface LabelSpan { label: string; col: number; cols: number }

interface HeatmapData {
  cells: Cell[]
  yearLabels: LabelSpan[]
  monthLabels: LabelSpan[]
  bodyW: number
  bodyH: number
  labelsH: number
}

const mdow = (d: Date): number => (d.getDay() + 6) % 7 // 0=Mon..6=Sun

const fmtLocal = (d: Date): string => {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

const heatmap = computed<HeatmapData | null>(() => {
  if (!stats.value) return null

  const cutoff = new Date(Date.now() - HEATMAP_YEARS * 365 * 24 * 60 * 60 * 1000)
  const cutoffStr = fmtLocal(cutoff)
  const contributions = stats.value.daily_contributions.filter(c => c.date >= cutoffStr)
  if (contributions.length === 0) return null

  const countMap = new Map<string, number>()
  for (const c of contributions) countMap.set(c.date, c.count)

  const dates = contributions.map(c => c.date).sort()
  const first = new Date(dates[0] + 'T00:00:00')
  const last = new Date(dates[dates.length - 1] + 'T00:00:00')

  // Round start to previous Monday, end to next Sunday
  const start = new Date(first)
  start.setDate(start.getDate() - mdow(start))
  const end = new Date(last)
  end.setDate(end.getDate() + (6 - mdow(end)))

  // Iterate day by day, tracking year/month/week transitions
  // Month/year labels start at the column of their FIRST day (not next Monday)
  const cells: Cell[] = []
  const yearLabels: LabelSpan[] = []
  const monthLabels: LabelSpan[] = []

  let weekIdx = 0
  let prevMonth = -1
  let prevYear = -1
  let monthStartCol = 0
  let yearStartCol = 0

  const cur = new Date(start)
  while (cur <= end) {
    const ds = fmtLocal(cur)
    const dayIdx = mdow(cur)
    const m = cur.getMonth()
    const y = cur.getFullYear()

    cells.push({ date: ds, count: countMap.get(ds) || 0, w: weekIdx, d: dayIdx })

    // Month changed? Push previous month label, record new start
    if (prevMonth >= 0 && m !== prevMonth) {
      monthLabels.push({ label: monthNames.value[prevMonth], col: monthStartCol, cols: weekIdx - monthStartCol })
      monthStartCol = weekIdx
    }
    if (prevYear >= 0 && y !== prevYear) {
      yearLabels.push({ label: String(prevYear), col: yearStartCol, cols: weekIdx - yearStartCol })
      yearStartCol = weekIdx
    }
    prevMonth = m
    prevYear = y

    cur.setDate(cur.getDate() + 1)
    if (mdow(cur) === 0) weekIdx++
  }
  // Close final labels
  monthLabels.push({ label: monthNames.value[prevMonth], col: monthStartCol, cols: weekIdx - monthStartCol })
  yearLabels.push({ label: String(prevYear), col: yearStartCol, cols: weekIdx - yearStartCol })

  const numCols = weekIdx

  // debug
  console.log('heatmap', JSON.parse(JSON.stringify({
    yearLabels, monthLabels,
    first: cells[0], last: cells[cells.length - 1],
    w0: cells.filter(c => c.w === 0).map(c => c.date + ' d' + c.d),
    wLast: cells.filter(c => c.w === numCols - 1).map(c => c.date + ' d' + c.d),
    numCols,
  })))
  const labelsH = YEAR_ROW_H + ROW_GAP + MONTH_ROW_H + ROW_GAP

  return {
    cells,
    yearLabels,
    monthLabels,
    bodyW: numCols * STEP,
    bodyH: labelsH + 7 * STEP,
    labelsH,
  }
})

const mkTheme = (): string => {
  return global.computedTheme === 'dark' ? 'dark' : undefined as unknown as string
}

const disposeEcharts = () => {
  monthlyChart?.dispose(); monthlyChart = null
  modelChart?.dispose(); modelChart = null
  sourceChart?.dispose(); sourceChart = null
}

const handleResize = () => {
  monthlyChart?.resize()
  modelChart?.resize()
  sourceChart?.resize()
}

const truncName = (name: string, max = 28): string =>
  name.length > max ? name.substring(0, max - 2) + '..' : name

const renderMonthlyChart = () => {
  if (!monthlyRef.value || !stats.value) return
  if (monthlyChart) monthlyChart.dispose()
  monthlyChart = echarts.init(monthlyRef.value, mkTheme())
  monthlyChart.setOption({
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: stats.value.monthly_trends.map(m => m.month), axisLabel: { rotate: 45 } },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: stats.value.monthly_trends.map(m => m.count), itemStyle: { color: '#40c463' }, barMaxWidth: 40 }],
    grid: { left: '8%', right: '4%', bottom: '15%', top: '8%' },
  })
}

const renderModelChart = () => {
  if (!modelRef.value || !stats.value) return
  if (modelChart) modelChart.dispose()
  modelChart = echarts.init(modelRef.value, mkTheme())
  const top10 = stats.value.top_models.slice(0, 10)
  modelChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: '30%', right: '4%', bottom: '4%', top: '4%' },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: top10.map(m => truncName(m.name)), inverse: true, axisLabel: { fontSize: 10 } },
    series: [{ type: 'bar', data: top10.map(m => m.count), itemStyle: { color: '#4A90E2' } }],
  })
}

const renderSourceChart = () => {
  if (!sourceRef.value || !stats.value) return
  if (sourceChart) sourceChart.dispose()
  sourceChart = echarts.init(sourceRef.value, mkTheme())
  const top10 = stats.value.top_source.slice(0, 10)
  sourceChart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: '35%', right: '4%', bottom: '4%', top: '4%' },
    xAxis: { type: 'value' },
    yAxis: { type: 'category', data: top10.map(m => truncName(m.name, 40)), inverse: true, axisLabel: { fontSize: 10 } },
    series: [{ type: 'bar', data: top10.map(m => m.count), itemStyle: { color: '#E8913A' } }],
  })
}

const renderCharts = () => { renderMonthlyChart(); renderModelChart(); renderSourceChart() }

const onHeatmapWheel = (e: WheelEvent) => {
  if (!heatmapScrollRef.value) return
  e.preventDefault()
  heatmapScrollRef.value.scrollLeft += e.deltaY
}

const fetchData = async () => {
  loading.value = true
  error.value = ''
  try {
    stats.value = await getTrendStats()
  } catch (err: any) {
    error.value = err?.message || 'Failed to load trend stats'
  } finally {
    loading.value = false
    await nextTick()
    if (stats.value) {
      renderCharts()
      if (heatmapScrollRef.value) {
        heatmapScrollRef.value.scrollLeft = heatmapScrollRef.value.scrollWidth
      }
    }
  }
}

watch(() => global.computedTheme, () => {
  if (stats.value) { disposeEcharts(); renderCharts() }
})

onMounted(() => {
  fetchData()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  disposeEcharts()
})
</script>

<style scoped lang="scss">
.trend-panel {
  padding: 20px;
  height: 100%;
  overflow-y: auto;
  background: var(--zp-secondary-background);
}

.loading-container {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 200px;
}

.summary-bar {
  margin-bottom: 24px;
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}

.chart-section {
  margin-bottom: 24px;
  h3 {
    font-size: 16px;
    font-weight: 600;
    margin-bottom: 12px;
    color: var(--zp-primary);
  }
}

// ===== Heatmap =====
.heatmap-wrap {
  background: var(--zp-primary-background);
  border-radius: 8px;
  padding: 16px;
}

.heatmap-row {
  display: flex;
}

.heatmap-scroll {
  overflow-x: auto;
  overflow-y: hidden;
  flex: 1;
  &::-webkit-scrollbar { height: 6px; }
  &::-webkit-scrollbar-track { background: transparent; }
  &::-webkit-scrollbar-thumb {
    background: var(--zp-border);
    border-radius: 3px;
    &:hover { background: var(--zp-secondary); }
  }
  scrollbar-width: thin;
  scrollbar-color: var(--zp-border) transparent;
}

.heatmap-body {
  position: relative;
}

.heatmap-day-col {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  margin-right: 6px;
  span {
    font-size: 10px;
    line-height: 14px;
    color: var(--zp-secondary);
    text-align: right;
    width: 26px;
  }
}

.heatmap-year-label {
  position: absolute;
  top: 0;
  height: 16px;
  font-size: 11px;
  font-weight: 600;
  line-height: 16px;
  color: var(--zp-primary);
  overflow: hidden;
}

.heatmap-month-label {
  position: absolute;
  top: calc(16px + 2px);
  height: 14px;
  font-size: 10px;
  line-height: 14px;
  color: var(--zp-secondary);
  overflow: hidden;
}

.heatmap-cell {
  position: absolute;
  width: 14px;
  height: 14px;
  border-radius: 2px;
  cursor: default;
}

.heatmap-legend {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--zp-secondary);
  margin-top: 12px;
  .legend-swatch {
    width: 14px;
    height: 14px;
    border-radius: 2px;
  }
}

// ===== ECharts =====
.charts-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  @media (max-width: 900px) { grid-template-columns: 1fr; }
}

.chart-container {
  width: 100%;
  background: var(--zp-primary-background);
  border-radius: 8px;
  padding: 12px;
}

.chart-bar { height: 320px; }
</style>
