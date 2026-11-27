<script setup lang="ts">
import {
  cancelImageGroupsJob,
  getImageGroups,
  getImageGroupsJobStatus,
  getImageGroupsState,
  startImageGroupsJob,
  type ImageGroup,
  type ImageGroupKind,
  type ImageGroupsJobStatusResp,
  type ImageGroupsStateResp
} from '@/api/db'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { toImageThumbnailUrl } from '@/util/file'
import { debounce, uniqueId } from 'lodash-es'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  DEFAULT_THRESHOLD,
  MAX_THRESHOLD,
  clampThreshold,
  createGeneration,
  decideJobAction,
  emptyPager,
  isJobActive,
  jobPercent,
  loadPager,
  shouldPartialRefresh,
  statusErrorPlan,
  unreadableCounts,
  type LoadMode,
  type Pager
} from './imageGroupsLogic'

const props = defineProps<{ tabIdx: number; paneIdx: number; paneKey?: string }>()
const g = useGlobalStore()

// View state lives in this component; the pane stays mounted while the tab is open.
const kind = ref<ImageGroupKind>('pixel')
const threshold = ref(DEFAULT_THRESHOLD)

const state = ref<ImageGroupsStateResp | null>(null)
const pager = ref<Pager>(emptyPager())
const groups = computed(() => pager.value.groups)
const loading = ref(false)
const error = ref('') // load errors: cleared by the next successful request
const jobError = ref('') // job errors: stay until the refresh button or a new job
const readonly = computed(() => !!g.conf?.is_readonly)

const job = ref<ImageGroupsJobStatusResp | null>(null)
const jobRunning = computed(() => isJobActive(job.value?.status))
const cancelling = ref(false)
const jobCounts = computed(() => ({ done: job.value?.done ?? 0, total: job.value?.total ?? 0 }))
const percent = computed(() => jobPercent(jobCounts.value.done, jobCounts.value.total))

const shownPending = computed(() => pager.value.pending ?? state.value?.pending ?? 0)
const unreadable = computed(() => unreadableCounts(state.value, job.value))
const summary = computed(() =>
  t('imageGroupsSummary', {
    groups: t('imageGroupsNGroups', pager.value.groupCount),
    images: t('imageGroupsSummaryImages', pager.value.imageCount),
    hashed: pager.value.hashed ?? state.value?.hashed ?? 0,
    total: state.value?.total ?? 0
  })
)

const errText = (e: any) => {
  const detail = e?.response?.data?.detail
  return String(typeof detail === 'string' ? detail : e?.message ?? '')
}

// ---- scroll position (the pane keeps its DOM, but a hidden pane loses its scroll offset) ----
const rootEl = ref<HTMLElement | null>(null)
let savedScroll = 0
const onScroll = () => {
  const el = rootEl.value
  if (el && el.offsetParent !== null) savedScroll = el.scrollTop
}
const restoreScroll = async () => {
  await nextTick()
  if (rootEl.value) rootEl.value.scrollTop = savedScroll
}

// ---- groups ----
let loadSeq = 0
const loadGroups = async (mode: LoadMode = 'first') => {
  const seq = ++loadSeq
  loading.value = true
  try {
    const next = await loadPager(
      (offset, limit) =>
        getImageGroups({ kind: kind.value, threshold: threshold.value, min_size: 2, offset, limit }),
      mode,
      pager.value,
      () => seq !== loadSeq // a newer request owns the list now
    )
    if (!next || seq !== loadSeq) return
    pager.value = next
    error.value = ''
  } catch (e) {
    if (seq !== loadSeq) return
    error.value = `${t('imageGroupsLoadFailed')}: ${errText(e)}`
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

/** Reload the state; false if the request failed. */
const refreshState = async () => {
  try {
    state.value = await getImageGroupsState()
    return true
  } catch (e) {
    error.value = `${t('imageGroupsLoadFailed')}: ${errText(e)}`
    return false
  }
}

// ---- job ----
let alive = true
let pollTimer: ReturnType<typeof setTimeout> | null = null
let activeJobId = ''
let starting = false
let autoStarted = false // the view starts a job by itself at most once; later starts are the user's
let lastPartial = 0
let statusFailures = 0 // failed status requests in a row
let notFoundCount = 0 // "job unknown" answers in a row

const isActive = computed(() => g.tabList[props.tabIdx]?.key === props.paneKey)

// One poll chain at a time: every chain carries the generation it was started in.
const pollGen = createGeneration()

const stopPoll = () => {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
  pollGen.invalidate()
}

const startPoll = () => {
  stopPoll()
  void pollJob(pollGen.current())
}

const scheduleNext = (gen: number, ms: number) => {
  pollTimer = setTimeout(() => void pollJob(gen), ms)
}

const pollJob = async (gen: number) => {
  pollTimer = null
  const id = activeJobId
  if (!id || !alive || !isActive.value || !pollGen.isCurrent(gen)) return
  let st: ImageGroupsJobStatusResp
  try {
    st = await getImageGroupsJobStatus(id)
  } catch (e: any) {
    if (pollGen.isCurrent(gen)) await onStatusError(gen, e)
    return
  }
  if (!pollGen.isCurrent(gen)) return
  statusFailures = 0
  notFoundCount = 0
  job.value = st
  if (isJobActive(st.status)) {
    if (!loading.value && shouldPartialRefresh(Date.now(), lastPartial, kind.value)) {
      lastPartial = Date.now()
      void loadGroups('keep')
    }
    scheduleNext(gen, 1000)
    return
  }
  // done / error / cancelled: no new job from here, whatever is still pending
  activeJobId = ''
  cancelling.value = false
  if (st.status === 'error') jobError.value = `${t('imageGroupsJobFailed')}: ${st.error ?? ''}`
  await refreshState()
  await loadGroups('keep')
}

/** A failed status request: wait and ask again (network trouble, restart), look at the state on 404, or give up. */
const onStatusError = async (gen: number, e: any) => {
  const plan = statusErrorPlan(e?.response?.status, statusFailures, notFoundCount)
  if (plan.action === 'retry') {
    statusFailures++
    scheduleNext(gen, plan.delayMs)
  } else if (plan.action === 'recover') {
    notFoundCount++
    statusFailures = 0
    await recoverJob(gen)
  } else {
    activeJobId = ''
    job.value = null
    jobError.value = `${t('imageGroupsJobFailed')}: ${errText(e)}`
  }
}

/** The server does not know the job (restart): look at the state, re-attach or restart if allowed. */
const recoverJob = async (gen: number) => {
  const stateOk = await refreshState()
  if (!alive || !pollGen.isCurrent(gen)) return
  if (!stateOk) {
    // server still not reachable: not a verdict on the job, keep waiting with the retry plan
    notFoundCount--
    await onStatusError(gen, { message: 'state unavailable' })
    return
  }
  activeJobId = ''
  job.value = null
  await ensureJob({ recover: true })
  if (!activeJobId) await loadGroups('keep')
}

const attachJob = (id: string) => {
  activeJobId = id
  lastPartial = Date.now()
  statusFailures = 0
  jobError.value = ''
  job.value = null
  cancelling.value = false
  startPoll()
}

/** Attach to a running job or start one if allowed; never twice in parallel. */
const ensureJob = async (opts: { manual?: boolean; recover?: boolean; attachOnly?: boolean } = {}) => {
  if (starting || activeJobId) return
  const d = decideJobAction(state.value, {
    readonly: readonly.value,
    manual: !!opts.manual,
    autoAllowed: !opts.attachOnly && (!autoStarted || !!opts.recover)
  })
  if (d.action === 'attach') return attachJob(d.jobId)
  if (d.action !== 'start') return
  starting = true
  if (!opts.manual && !opts.recover) autoStarted = true
  try {
    const r = await startImageGroupsJob()
    if (alive) attachJob(r.job_id)
  } catch (e) {
    jobError.value = `${t('imageGroupsJobFailed')}: ${errText(e)}`
  } finally {
    starting = false
  }
}

const computeMissing = async () => {
  statusFailures = 0
  notFoundCount = 0
  await refreshState()
  await ensureJob({ manual: true })
}

const cancelJob = async () => {
  if (!activeJobId || cancelling.value) return
  cancelling.value = true
  try {
    await cancelImageGroupsJob(activeJobId)
  } catch (e) {
    cancelling.value = false
    jobError.value = `${t('imageGroupsCancelFailed')}: ${errText(e)}`
  }
}

const activate = async () => {
  await refreshState()
  if (!alive || !isActive.value) return // left again while the state request was running
  if (activeJobId) {
    // job known from before the pane was hidden: poll it again instead of starting anything
    lastPartial = Date.now()
    startPoll()
  } else {
    void ensureJob()
  }
  // as many groups as before (first open: one page), at the old scroll position
  await loadGroups('keep')
  await restoreScroll()
}

/** Refresh: reload state and the shown groups, attach to a running job; never starts one. */
const refresh = async () => {
  jobError.value = ''
  await refreshState()
  void ensureJob({ attachOnly: true })
  await loadGroups('keep')
}

const deactivate = () => {
  stopPoll()
  loadSeq++ // drop answers of requests still in flight
  loading.value = false
}

watch(isActive, (v) => {
  // back in view: pick up a running job again, reload (images may have been deleted or tagged meanwhile)
  if (v) void activate()
  else deactivate()
})

// Kind / threshold changed: reload from the top (debounced while dragging the slider)
const reload = debounce(() => {
  if (!alive || !isActive.value) return
  savedScroll = 0
  if (rootEl.value) rootEl.value.scrollTop = 0
  void loadGroups('first')
}, 350)
watch(kind, () => reload())
watch(threshold, (v) => {
  const c = clampThreshold(v)
  if (c !== v) threshold.value = c
  else if (kind.value === 'near') reload()
})

onMounted(() => {
  if (isActive.value) void activate()
})
onBeforeUnmount(() => {
  alive = false
  reload.cancel()
  deactivate()
})

// ---- tiles; a click opens the group in the shared grid (same pane type as the topic clusters) ----
const kindText = (grp: ImageGroup) =>
  t(grp.kind === 'near' ? 'imageGroupsKindNear' : 'imageGroupsKindPixel')
const countText = (grp: ImageGroup) => t('imageGroupsCardCount', grp.size)
const distanceText = (grp: ImageGroup) =>
  grp.kind === 'near' ? t('imageGroupsCardDistance', { distance: grp.max_distance }) : ''
const groupTitle = (grp: ImageGroup) =>
  t(grp.kind === 'near' ? 'imageGroupsCardNear' : 'imageGroupsCardPixel', grp.size)
// the full texts of a tile, then the first file name (which the tile does not show)
const tileTooltip = (grp: ImageGroup) =>
  [kindText(grp), [countText(grp), distanceText(grp)].filter(Boolean).join(' · '), grp.images[0]?.name]
    .filter(Boolean)
    .join('\n')

const openGroup = (grp: ImageGroup) => {
  const title = groupTitle(grp)
  const pane = {
    type: 'topic-search-matched-image-grid' as const,
    name: title,
    key: Date.now() + uniqueId(),
    id: uniqueId(),
    title,
    subtitle: distanceText(grp) || undefined,
    paths: grp.paths // all members in backend order (medoid first), not only the preview
  }
  const tab = g.tabList[props.tabIdx]
  tab.panes.push(pane)
  tab.key = pane.key
}

// Tile size follows IIB's grid cell setting (the one folder tiles use)
const cellWidth = computed(() => Math.max(120, Math.min(480, Number(g.defaultGridCellWidth) || 256)))
</script>

<template>
  <div ref="rootEl" class="ig-root" data-testid="image-groups" @scroll.passive="onScroll">
    <div class="ig-toolbar">
      <div class="ig-row">
        <div class="ig-title-bar">
          <span class="icon">🧬</span>
          <span>{{ $t('imageGroups') }}</span>
        </div>
        <a-radio-group v-model:value="kind" button-style="solid" data-testid="kind">
          <a-radio-button value="pixel">{{ $t('imageGroupsKindPixel') }}</a-radio-button>
          <a-radio-button value="near">{{ $t('imageGroupsKindNear') }}</a-radio-button>
        </a-radio-group>
        <div v-if="kind === 'near'" class="ig-threshold">
          <span class="ig-label">{{ $t('imageGroupsThreshold') }}</span>
          <a-slider
            v-model:value="threshold"
            :min="0"
            :max="MAX_THRESHOLD"
            :step="1"
            class="ig-slider"
            data-testid="threshold-slider"
          />
          <a-input-number
            v-model:value="threshold"
            :min="0"
            :max="MAX_THRESHOLD"
            :step="1"
            style="width: 70px"
            data-testid="threshold-input"
          />
        </div>
        <div class="ig-spacer" />
        <a-button type="primary" ghost :loading="loading" @click="refresh" data-testid="refresh">
          {{ $t('imageGroupsRefresh') }}
        </a-button>
      </div>
      <!-- own class: a global ".hint" elsewhere in IIB positions itself absolutely-ish and covered the controls -->
      <div v-if="kind === 'near'" class="ig-help" data-testid="threshold-help">
        {{ $t('imageGroupsThresholdHint') }}
      </div>
      <div class="ig-summary" data-testid="summary" v-if="state || pager.groupCount">
        {{ summary }}
        <template v-if="shownPending > 0 && !jobRunning">
          <span class="ig-note" data-testid="pending">· {{ $t('imageGroupsPendingNote', shownPending) }}</span>
          <a-button size="small" class="ig-compute" :disabled="readonly" @click="computeMissing" data-testid="compute">
            {{ $t('imageGroupsComputeMissing') }}
          </a-button>
          <span v-if="readonly" class="ig-note" data-testid="readonly-hint">{{ $t('imageGroupsReadonly') }}</span>
        </template>
        <span
          v-if="unreadable.total > 0 && !jobRunning"
          class="ig-note"
          :title="$t('imageGroupsUnreadableDetail', { skipped: unreadable.skipped, failed: unreadable.failed })"
          data-testid="unreadable"
        >· {{ $t('imageGroupsUnreadable', unreadable.total) }}</span>
      </div>
    </div>

    <div v-if="jobRunning" class="ig-job" data-testid="job">
      <div class="ig-job-text">{{ $t('imageGroupsJobProgress', jobCounts) }}</div>
      <a-progress :percent="percent" size="small" />
      <a-button size="small" :loading="cancelling" :disabled="readonly" @click="cancelJob" data-testid="cancel">
        {{ $t('imageGroupsCancel') }}
      </a-button>
    </div>

    <a-alert
      v-if="job?.status === 'cancelled'"
      type="info"
      show-icon
      :message="$t('imageGroupsCancelled', jobCounts)"
      class="ig-alert"
      data-testid="cancelled"
    />
    <a-alert v-if="jobError" type="error" show-icon :message="jobError" class="ig-alert" data-testid="job-error" />
    <a-alert v-if="error" type="error" show-icon :message="error" class="ig-alert" data-testid="error" />

    <a-spin :spinning="loading && !groups.length">
      <div class="ig-grid" v-if="groups.length" :style="{ '--ig-cell': cellWidth + 'px' }" data-testid="grid">
        <div
          class="ig-tile"
          v-for="grp in groups"
          :key="grp.id"
          role="button"
          tabindex="0"
          :aria-label="groupTitle(grp)"
          :title="tileTooltip(grp)"
          @click="openGroup(grp)"
          @keydown.enter.self.prevent="openGroup(grp)"
          @keydown.space.self.prevent="openGroup(grp)"
          data-testid="card"
        >
          <div class="ig-cover" :class="`n${Math.min(4, grp.images.length)}`">
            <img
              v-for="img in grp.images.slice(0, 4)"
              :key="img.fullpath"
              class="ig-cover-img"
              loading="lazy"
              decoding="async"
              :alt="img.name"
              :src="toImageThumbnailUrl(img)"
            />
          </div>
          <div class="ig-info">
            <div class="ig-name-title" data-testid="tile-title">{{ kindText(grp) }}</div>
            <div class="ig-sub" data-testid="tile-sub">
              <span class="ig-count">{{ countText(grp) }}</span>
              <span v-if="grp.kind === 'near'" class="ig-dist" data-testid="distance">{{ distanceText(grp) }}</span>
            </div>
          </div>
        </div>
      </div>
      <div v-else-if="!loading && !jobRunning && !error && !jobError" class="ig-empty" data-testid="empty">
        {{ $t('imageGroupsEmpty') }}
      </div>
      <div v-if="pager.hasMore" class="ig-more">
        <a-button :loading="loading" @click="loadGroups('more')" data-testid="more">
          {{ $t('imageGroupsLoadMore') }}
        </a-button>
      </div>
    </a-spin>
  </div>
</template>

<style scoped lang="scss">
.ig-root {
  height: var(--pane-max-height);
  overflow: auto;
  padding: 12px;
}

.ig-toolbar {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  background: var(--zp-primary-background);
  border-radius: 12px;
}

.ig-row {
  display: flex;
  align-items: center;
  gap: 8px 12px;
  flex-wrap: wrap;
}

.ig-title-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
}

.ig-threshold {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
}

.ig-slider {
  width: 160px;
  margin: 0 6px;
}

.ig-spacer {
  flex: 1 1 0;
}

.ig-label {
  color: #888;
}

.ig-help,
.ig-summary {
  font-size: 12px;
  opacity: 0.8;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.ig-help {
  width: 100%;
}

.ig-note {
  opacity: 0.85;
  margin-left: 6px;
}

.ig-compute {
  margin-left: 8px;
}

.ig-job {
  margin-top: 10px;
  padding: 8px 12px;
  background: var(--zp-primary-background);
  border-radius: 12px;
}

.ig-job-text {
  font-size: 13px;
}

.ig-job :deep(.ant-btn) {
  margin-top: 4px;
}

.ig-alert {
  margin-top: 10px;
}

// Group tiles follow the folder tiles of the file grid (FileItem): square cover with the
// 2x2 preview mosaic, the same border, radius and background, name block underneath.
.ig-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(var(--ig-cell), 100%), var(--ig-cell)));
  justify-content: start;
  gap: 12px 8px;
}

// Like a folder tile: cursor only, no hover effect. The keyboard focus ring (tiles are buttons) is
// the same blue outline a selected file gets.
.ig-tile {
  min-width: 0;
  cursor: pointer;
  border-radius: 8px;

  &:focus {
    outline: none;
  }

  &:focus-visible {
    outline: #0084ff solid 2px;
  }
}

.ig-cover {
  display: grid;
  // same spacing as the folder tiles (FileItem .dir-cover-container: padding 4px + image margin 4px): 8px to the
  // border and 8px between the thumbnails
  gap: 8px;
  padding: 8px;
  box-sizing: border-box;
  width: 100%;
  aspect-ratio: 1;
  border: 1px solid var(--zp-secondary);
  background-color: var(--zp-secondary-variant-background);
  border-radius: 8px;
  overflow: hidden;

  &.n1 {
    grid-template: 1fr / 1fr;
  }
  &.n2 {
    grid-template: 1fr / 1fr 1fr;
  }
  &.n3 {
    grid-template: 1fr 1fr / 1fr 1fr;

    .ig-cover-img:first-child {
      grid-row: span 2;
    }
  }
  &.n4 {
    grid-template: 1fr 1fr / 1fr 1fr;
  }
}

.ig-cover-img {
  display: block;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
  object-fit: cover;
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.15);
}

.ig-info {
  padding: 2px 4px 0;
  min-width: 0;
}

.ig-name-title {
  font-weight: 500;
  overflow-wrap: anywhere;
}

// Numbers must stay readable on narrow tiles: no ellipsis, the line may wrap between the parts.
.ig-sub {
  display: flex;
  flex-wrap: wrap;
  column-gap: 6px;
  font-size: 0.7em;
  opacity: 0.8;
  min-width: 0;
}

.ig-count,
.ig-dist {
  white-space: nowrap;
}

.ig-dist::before {
  content: '·';
  margin-right: 6px;
}

.ig-empty {
  margin-top: 48px;
  text-align: center;
  font-size: 16px;
  opacity: 0.7;
}

.ig-more {
  margin: 16px 0;
  text-align: center;
}
</style>
