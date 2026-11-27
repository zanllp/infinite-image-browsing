import assert from 'node:assert/strict'
import { test } from 'node:test'
import {
  AUTO_START_MAX_PENDING,
  PARTIAL_REFRESH_MS,
  STATUS_RETRY_DELAYS_MS,
  clampThreshold,
  createGeneration,
  decideJobAction,
  emptyPager,
  jobPercent,
  loadPager,
  shouldPartialRefresh,
  statusErrorPlan,
  unreadableCounts,
  type GroupPage
} from '../src/page/ImageGroups/imageGroupsLogic'

test('threshold is clamped to 0..12 and falls back to 8', () => {
  const cases: [unknown, number][] = [
    [0, 0], [12, 12], [13, 12], [99, 12], [-3, 0], [7.6, 8], ['5', 5],
    [null, 8], [undefined, 8], ['', 8], ['abc', 8]
  ]
  for (const [value, expected] of cases) assert.equal(clampThreshold(value), expected, String(value))
})

test('job decision: attach to a live job first, start only with work and when allowed', () => {
  const live = { job_id: 'a', status: 'running', done: 1, total: 2 } as const
  const st = (pending: number, job: any = null) => ({ pending, job })
  const auto = { readonly: false, manual: false, autoAllowed: true }
  const noAuto = { ...auto, autoAllowed: false } // refresh, or after the first automatic start
  const manual = { ...noAuto, manual: true }
  const readonly = { readonly: true, manual: true, autoAllowed: true }
  const none = { action: 'none' }
  const start = { action: 'start' }
  const attach = { action: 'attach', jobId: 'a' }
  const cases: [ReturnType<typeof st> | null, typeof auto, object][] = [
    [null, auto, none],
    [st(0), auto, none],
    [st(3), auto, start],
    [st(0, live), auto, attach],
    [st(5, live), noAuto, attach],
    [st(0, { ...live, status: 'queued' }), auto, attach],
    [st(0, { ...live, status: 'done' }), auto, none],
    [st(0, { ...live, status: 'cancelled' }), auto, none],
    // automatic starts stop at the limit, manual ones do not
    [st(AUTO_START_MAX_PENDING), auto, start],
    [st(AUTO_START_MAX_PENDING + 1), auto, none],
    [st(AUTO_START_MAX_PENDING + 1), manual, start],
    [st(3), noAuto, none],
    [st(0), manual, none],
    // read-only never starts, but may still attach
    [st(3), readonly, none],
    [st(3, live), readonly, attach]
  ]
  cases.forEach(([state, opts, expected], i) => assert.deepEqual(decideJobAction(state, opts), expected, `case ${i}`))
})

test('status errors: growing waits for about a minute, 404 recovers twice, then fail', () => {
  const n = STATUS_RETRY_DELAYS_MS.length
  assert.deepEqual(statusErrorPlan(undefined, 0, 0), { action: 'retry', delayMs: 2000 })
  assert.deepEqual(statusErrorPlan(500, 4, 0), { action: 'retry', delayMs: 6000 })
  assert.deepEqual(statusErrorPlan(undefined, n - 1, 0), { action: 'retry', delayMs: 10000 })
  assert.deepEqual(statusErrorPlan(500, n, 0), { action: 'fail' })
  assert.deepEqual(statusErrorPlan(404, 9, 1), { action: 'recover' })
  assert.deepEqual(statusErrorPlan(404, 0, 2), { action: 'fail' })
  const total = STATUS_RETRY_DELAYS_MS.reduce((a, b) => a + b, 0)
  assert.ok(total >= 55000 && total <= 65000, `total patience ${total} ms`)
  assert.deepEqual(STATUS_RETRY_DELAYS_MS, [...STATUS_RETRY_DELAYS_MS].sort((a, b) => a - b))
})

test('job percent, partial refresh and unreadable counts', () => {
  assert.deepEqual([jobPercent(0, 0), jobPercent(1, 3), jobPercent(5, 3), jobPercent(-1, 3)], [0, 33, 100, 0])
  // throttled, and only in the cheap pixel view
  assert.equal(shouldPartialRefresh(PARTIAL_REFRESH_MS - 1, 0, 'pixel'), false)
  assert.equal(shouldPartialRefresh(PARTIAL_REFRESH_MS, 0, 'pixel'), true)
  assert.equal(shouldPartialRefresh(10 ** 9, 0, 'near'), false)
  // skipped and failed add up, the job's skipped are not counted twice
  assert.deepEqual(unreadableCounts(null, null), { skipped: 0, failed: 0, total: 0 })
  assert.deepEqual(unreadableCounts({ skipped: 3, failed: 2 }, { skipped: 3 }), { skipped: 3, failed: 2, total: 5 })
  assert.deepEqual(unreadableCounts({ skipped: 1, failed: 0 }, { skipped: 4 }), { skipped: 4, failed: 0, total: 4 })
})

test('only the newest poll chain stays current', () => {
  const gen = createGeneration()
  const a = gen.current() // chain A started
  assert.equal(gen.isCurrent(a), true)
  gen.invalidate() // pane hidden: stop
  gen.invalidate() // pane shown again: restart
  const b = gen.current() // chain B
  assert.equal(gen.isCurrent(a), false) // A, still awaiting its request, must not schedule again
  assert.equal(gen.isCurrent(b), true)
})

// A server whose pages leave out groups (missing files) but keep counting raw offsets.
const fakeServer = (total: number, omitted: (n: number) => boolean, opts: { stuck?: boolean } = {}) => {
  const requests: { offset: number; limit: number }[] = []
  const fetchPage = async (offset: number, limit: number): Promise<GroupPage> => {
    requests.push({ offset, limit })
    const end = Math.min(total, offset + limit)
    const groups = []
    for (let n = offset; n < end; n++) {
      if (!omitted(n)) groups.push({ id: `g${n}`, kind: 'pixel', size: 2, max_distance: 0, images: [], paths: [] })
    }
    return {
      groups: groups as GroupPage['groups'],
      group_count: total,
      image_count: total * 2,
      next_offset: opts.stuck ? offset : end,
      has_more: opts.stuck || end < total,
      hashed: 5,
      pending: 0
    }
  }
  return { fetchPage, requests }
}
const ids = (p: { groups: { id: string }[] }) => p.groups.map((x) => x.id)

test('pager: groups left out of a page are neither lost nor loaded twice, load more ends', async () => {
  const omit = (n: number) => n % 67 === 0 // 3 of the first 200 raw groups are missing
  const srv = fakeServer(530, omit)
  let pager = (await loadPager(srv.fetchPage, 'first', emptyPager()))!
  assert.deepEqual([pager.groups.length, pager.nextOffset, pager.hasMore], [197, 200, true])
  while (pager.hasMore) pager = (await loadPager(srv.fetchPage, 'more', pager))!
  // offsets follow next_offset, not groups.length
  assert.deepEqual(srv.requests.map((r) => r.offset), [0, 200, 400])
  const expected = Array.from({ length: 530 }, (_, n) => n).filter((n) => !omit(n)).map((n) => `g${n}`)
  assert.deepEqual(ids(pager), expected)
  assert.deepEqual([pager.groupCount, pager.imageCount, pager.hashed, pager.pending], [530, 1060, 5, 0])
  // a page that repeats a group (the list changed meanwhile) adds it only once
  const again = (await loadPager(srv.fetchPage, 'more', { ...pager, nextOffset: 529 }))!
  assert.deepEqual(ids(again), expected)
})

test('pager: reload in keep mode loses no group, in server-sized requests', async () => {
  const srv = fakeServer(1700, (n) => n % 11 === 0)
  let pager = (await loadPager(srv.fetchPage, 'first', emptyPager()))!
  for (let i = 0; i < 5; i++) pager = (await loadPager(srv.fetchPage, 'more', pager))! // raw 1200 reached
  const before = ids(pager)
  srv.requests.length = 0
  const again = (await loadPager(srv.fetchPage, 'keep', pager))!
  assert.deepEqual(ids(again), before)
  assert.deepEqual([again.nextOffset, again.hasMore], [1200, true])
  // more than the server accepts at once: 1000 + 200
  assert.deepEqual(srv.requests, [{ offset: 0, limit: 1000 }, { offset: 1000, limit: 200 }])
  // keep from an empty list is one page
  srv.requests.length = 0
  await loadPager(srv.fetchPage, 'keep', emptyPager())
  assert.deepEqual(srv.requests, [{ offset: 0, limit: 200 }])
})

test('pager: keep stops at the end of the data and when the server makes no progress', async () => {
  const small = fakeServer(150, () => false)
  const p = (await loadPager(small.fetchPage, 'keep', { ...emptyPager(), nextOffset: 900 }))!
  assert.deepEqual([p.groups.length, p.hasMore, small.requests.length], [150, false, 1])
  const stuck = fakeServer(5000, () => false, { stuck: true })
  const q = (await loadPager(stuck.fetchPage, 'keep', { ...emptyPager(), nextOffset: 600 }))!
  assert.deepEqual([stuck.requests.length, q.groups.length > 0], [1, true])
})

test('pager: a stale answer is dropped', async () => {
  const srv = fakeServer(500, () => false)
  assert.equal(await loadPager(srv.fetchPage, 'first', emptyPager(), () => true), null)
  assert.equal(await loadPager(srv.fetchPage, 'more', emptyPager(), () => true), null)
})
