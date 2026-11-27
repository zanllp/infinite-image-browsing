import type {
  ImageGroup,
  ImageGroupKind,
  ImageGroupsJobStatusResp,
  ImageGroupsResp,
  ImageGroupsStateResp
} from '@/api/db'

export const DEFAULT_THRESHOLD = 8
export const MAX_THRESHOLD = 12
export const PAGE_LIMIT = 200
/** The server caps `limit` at 1000. */
export const SERVER_MAX_LIMIT = 1000
/** While a job runs, the group list is refreshed at most this often. */
export const PARTIAL_REFRESH_MS = 15000
/** Opening the view starts the job by itself only up to this many open images; above it the user starts it. */
export const AUTO_START_MAX_PENDING = 20000
/**
 * Waits before each retry of a failed status request (network trouble, server restarting):
 * about a minute of patience in total, with growing gaps, before the job counts as failed.
 */
export const STATUS_RETRY_DELAYS_MS = [2000, 2000, 4000, 4000, 6000, 6000, 8000, 8000, 10000, 10000]
/** A job the server does not know (404, e.g. after a restart) is looked up again at most this often in a row. */
export const MAX_JOB_RECOVERIES = 2

/** Hamming distance threshold as an integer in 0..12; anything unusable falls back to the default. */
export const clampThreshold = (v: unknown): number => {
  const n = v === null || v === undefined || v === '' ? NaN : Math.round(Number(v))
  return Number.isFinite(n) ? Math.max(0, Math.min(MAX_THRESHOLD, n)) : DEFAULT_THRESHOLD
}

export const isJobActive = (status?: string | null) => status === 'queued' || status === 'running'

export type JobDecision = { action: 'attach'; jobId: string } | { action: 'start' } | { action: 'none' }

/**
 * What to do with the server state: attach to a live job, start one, or nothing.
 * manual: the user asked for it (button); autoAllowed: the view may start by itself (once per open view).
 * Read-only never starts anything; automatic starts stop at AUTO_START_MAX_PENDING open images.
 */
export const decideJobAction = (
  state: Pick<ImageGroupsStateResp, 'pending' | 'job'> | null,
  opts: { readonly: boolean; manual: boolean; autoAllowed: boolean }
): JobDecision => {
  const job = state?.job
  if (job && isJobActive(job.status) && job.job_id) return { action: 'attach', jobId: job.job_id }
  if (!state || opts.readonly || !(state.pending > 0)) return { action: 'none' }
  const autoStart = opts.autoAllowed && state.pending <= AUTO_START_MAX_PENDING
  return opts.manual || autoStart ? { action: 'start' } : { action: 'none' }
}

export type StatusErrorPlan = { action: 'retry'; delayMs: number } | { action: 'recover' } | { action: 'fail' }

/**
 * A failed status request: 404 = job unknown, look at the state again (a few times at most);
 * anything else is retried after STATUS_RETRY_DELAYS_MS[failures] until the list is used up.
 * failures: failed requests in a row before this one; notFound: recoveries in a row before this one.
 */
export const statusErrorPlan = (
  httpStatus: number | undefined,
  failures: number,
  notFound: number
): StatusErrorPlan => {
  if (httpStatus === 404) return notFound < MAX_JOB_RECOVERIES ? { action: 'recover' } : { action: 'fail' }
  return failures < STATUS_RETRY_DELAYS_MS.length
    ? { action: 'retry', delayMs: STATUS_RETRY_DELAYS_MS[failures] }
    : { action: 'fail' }
}

export const jobPercent = (done: number, total: number) =>
  total > 0 ? Math.max(0, Math.min(100, Math.floor((done / total) * 100))) : 0

/**
 * Partial refresh while a job runs: the near view is recomputed by the server on every change,
 * so it is only reloaded once after the job; the pixel view is cheap and may refresh now and then.
 */
export const shouldPartialRefresh = (now: number, last: number, kind: ImageGroupKind) =>
  kind === 'pixel' && now - last >= PARTIAL_REFRESH_MS

// ---- paging ----
// The server pages over a raw offset; groups with missing files are left out of a page, so the
// number of shown groups says nothing about the next offset. Only next_offset / has_more do.

export type GroupPage = Omit<ImageGroupsResp, 'kind' | 'threshold' | 'offset'>

export type Pager = {
  groups: ImageGroup[]
  nextOffset: number
  hasMore: boolean
  groupCount: number
  imageCount: number
  /** Counts reported with the last page (null before the first one). */
  hashed: number | null
  pending: number | null
}

export const emptyPager = (): Pager => ({
  groups: [],
  nextOffset: 0,
  hasMore: false,
  groupCount: 0,
  imageCount: 0,
  hashed: null,
  pending: null
})

/** Add a page: replace = it starts at offset 0 (the list is rebuilt), else it is appended without duplicates. */
const applyPage = (pager: Pager, page: GroupPage, replace: boolean): Pager => {
  const seen = new Set(replace ? [] : pager.groups.map((g) => g.id))
  return {
    groups: (replace ? [] : pager.groups).concat(page.groups.filter((g) => !seen.has(g.id))),
    nextOffset: page.next_offset,
    hasMore: page.has_more,
    groupCount: page.group_count,
    imageCount: page.image_count,
    hashed: page.hashed,
    pending: page.pending
  }
}

export type FetchPage = (offset: number, limit: number) => Promise<GroupPage>
export type LoadMode = 'first' | 'more' | 'keep'

/**
 * Load groups. first: page 1; more: the page at nextOffset; keep: reload everything up to the raw
 * offset reached so far (at least one page) in server-sized requests, so no loaded group is lost.
 * Returns null if isStale() turned true while waiting (a newer request owns the list).
 */
export const loadPager = async (
  fetchPage: FetchPage,
  mode: LoadMode,
  current: Pager,
  isStale: () => boolean = () => false
): Promise<Pager | null> => {
  if (mode === 'more') {
    const page = await fetchPage(current.nextOffset, PAGE_LIMIT)
    return isStale() ? null : applyPage(current, page, false)
  }
  const target = mode === 'keep' ? Math.max(PAGE_LIMIT, current.nextOffset) : PAGE_LIMIT
  let pager = emptyPager()
  let offset = 0
  for (let first = true; first || (offset < target && pager.hasMore); first = false) {
    const page = await fetchPage(offset, Math.min(SERVER_MAX_LIMIT, target - offset))
    if (isStale()) return null
    pager = applyPage(pager, page, first)
    if (!(pager.nextOffset > offset)) break // no progress: stop instead of asking again
    offset = pager.nextOffset
  }
  return pager
}

/** Unreadable images: skipped in this server process (or by the running job) plus those saved as failed. */
export const unreadableCounts = (
  state: Pick<ImageGroupsStateResp, 'skipped' | 'failed'> | null,
  job: Pick<ImageGroupsJobStatusResp, 'skipped'> | null
) => {
  const skipped = Math.max(state?.skipped ?? 0, job?.skipped ?? 0)
  const failed = state?.failed ?? 0
  return { skipped, failed, total: skipped + failed }
}

/**
 * Generation counter for the poll chain: every chain remembers the generation it was started
 * in and must check it after each await; invalidate() (stop / restart) retires all older chains.
 */
export const createGeneration = () => {
  let gen = 0
  return {
    current: () => gen,
    invalidate: () => ++gen,
    isCurrent: (g: number) => g === gen
  }
}
