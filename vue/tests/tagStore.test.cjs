const strict = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

// transpile the real store and run it with stubbed dependencies; every request is a deferred to settle by hand
function loadStore() {
  const source = ts.transpileModule(readFileSync(join(__dirname, '../src/store/useTagStore.ts'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
  }).outputText
  const requests = []
  const events = []
  const ctx = {}
  const batchGetTagsByPath = (paths) => new Promise((resolve, reject) => {
    requests.push({ paths: [...paths], resolve, reject })
  })
  const modules = {
    '@/api/db': { batchGetTagsByPath },
    pinia: { defineStore: (_name, setup) => setup },
    sjcl: {},
    vue: { reactive: (x) => x, ref: (v) => ({ value: v }) },
    '@/util': { globalEvents: { emit: (name) => events.push(name) } },
    '@/util/crossTab': { onCrossTab: (handler) => { ctx.crossTab = handler } }
  }
  const exports = {}
  runInNewContext(source, { exports, require: (name) => modules[name] })
  return { store: exports.useTagStore(), requests, events, ctx }
}

const tag = (id) => ({ id, name: `t${id}`, count: 0, display_name: null, type: 'custom', color: '' })
const x = tag('x')
// values from the vm context have another Array prototype, compare them as plain JSON
const same = (actual, expected, msg) => strict.deepEqual(actual && JSON.parse(JSON.stringify(actual)), expected, msg)
// let pending promise continuations run
const tick = () => new Promise((resolve) => setImmediate(resolve))

test('loading puts no placeholder into tagMap, caches missing paths as untagged and dedupes', async () => {
  const { store, requests } = loadStore()
  const p1 = store.fetchImageTags(['/a', '/a', '/b'])
  const p2 = store.fetchImageTags(['/a', '/c'])
  strict.equal(store.tagMap.size, 0)
  same(requests.map((r) => r.paths), [['/a', '/b'], ['/c']])
  requests[0].resolve({ '/a': [tag(1)] })
  requests[1].resolve({})
  await Promise.all([p1, p2])
  same(store.tagMap.get('/a'), [tag(1)])
  same(store.tagMap.get('/b'), [])
  await store.fetchImageTags(['/a', '/b', '/c'])
  strict.equal(requests.length, 2)
})

test('refresh, invalidation and cross-tab changes keep the old value visible until the refetch replaces it', async () => {
  for (const outdate of [
    (s) => s.store.refreshTags(['/a']),
    (s) => s.store.invalidateTagCache(),
    (s) => s.ctx.crossTab({ type: 'tagsChanged', paths: ['/a'] })
  ]) {
    const s = loadStore()
    s.store.set('/a', [x])
    s.store.set('/b', [x])
    outdate(s)
    same(s.store.tagMap.get('/a'), [x])
    const p = s.store.fetchImageTags(['/a'])
    strict.equal(s.requests.length, 1)
    same(s.store.tagMap.get('/a'), [x])
    s.requests[0].resolve({ '/a': [tag(1)] })
    await p
    same(s.store.tagMap.get('/a'), [tag(1)])
    await s.store.fetchImageTags(['/a'])
    strict.equal(s.requests.length, 1)
  }
})

test('a response is discarded when the path was refreshed, invalidated, set or changed in another tab meanwhile', async () => {
  for (const [name, outdate, expected] of [
    ['refresh', (s) => s.store.refreshTags(['/a']), [tag(2)]],
    ['invalidate', (s) => s.store.invalidateTagCache(), undefined],
    ['cross-tab', (s) => s.ctx.crossTab({ type: 'tagsChanged', paths: ['/a'] }), undefined],
    ['set', (s) => s.store.set('/a', [x]), [x]]
  ]) {
    const s = loadStore()
    const p = s.store.fetchImageTags(['/a'])
    outdate(s)
    s.requests[1]?.resolve({ '/a': [tag(2)] })
    await tick()
    s.requests[0].resolve({ '/a': [tag(1)] })
    await p
    same(s.store.tagMap.get('/a'), expected, name)
  }
})

test('a failure keeps the old value, leaves other requests alone and is retried on the next fetch', async () => {
  const { store, requests, ctx } = loadStore()
  store.set('/a', [x])
  store.refreshTags(['/a'])
  // an unknown path marked by another tab stays absent
  ctx.crossTab({ type: 'tagsChanged', paths: ['/b'] })
  store.fetchImageTags(['/b'])
  const c = store.fetchImageTags(['/c'])
  const d = store.fetchImageTags(['/d'])
  store.refreshTags(['/d'])
  requests[0].reject(new Error('network'))
  requests[1].reject(new Error('network'))
  requests[2].resolve({ '/c': [tag(3)] })
  // an older request of /d fails, the newer one still owns the path
  requests[3].reject(new Error('network'))
  await Promise.all([c, d])
  same(store.tagMap.get('/a'), [x])
  strict.equal(store.tagMap.has('/b'), false)
  same(store.tagMap.get('/c'), [tag(3)])
  store.fetchImageTags(['/a', '/b', '/c', '/d'])
  same(requests.slice(5).map((r) => r.paths), [['/a', '/b']])
})

test('getTags fetches when needed and awaits a request in flight', async () => {
  const { store, requests } = loadStore()
  store.set('/a', [x])
  same(await store.getTags('/a'), [x])
  strict.equal(requests.length, 0)
  const absent = store.getTags('/b')
  requests[0].resolve({})
  same(await absent, [])
  store.invalidateTagCache()
  store.fetchImageTags(['/a'])
  const waiting = store.getTags('/a')
  strict.equal(requests.length, 2)
  requests[1].resolve({ '/a': [tag(1)] })
  same(await waiting, [tag(1)])
})

test('getTags rejects instead of returning [] when the tags are unknown', async () => {
  const { store, requests } = loadStore()
  const absent = store.getTags('/a')
  requests[0].reject(new Error('network'))
  await strict.rejects(absent, /network/)
  store.set('/b', [x])
  store.invalidateTagCache()
  store.fetchImageTags(['/b'])
  const stale = store.getTags('/b')
  requests[1].reject(new Error('network'))
  await strict.rejects(stale, /network/)
})

test('getTags waits for a refetch that starts while it waits', async () => {
  for (const outdate of [(s) => s.store.invalidateTagCache(), (s) => s.store.refreshTags(['/a'])]) {
    const s = loadStore()
    s.store.set('/a', [x])
    s.store.refreshTags(['/a'])
    const p = s.store.getTags('/a')
    outdate(s)
    s.requests[0].resolve({ '/a': [tag(1)] })
    await tick()
    strict.equal(s.requests.length, 2)
    s.requests[1].resolve({ '/a': [tag(2)] })
    same(await p, [tag(2)])
  }
})

test('getTags gives up when it keeps being invalidated', async () => {
  const { store, requests } = loadStore()
  const p = strict.rejects(store.getTags('/a'), /Failed to load tags: \/a/)
  for (let i = 0; i < 3; i++) {
    store.invalidateTagCache()
    requests[i].resolve({ '/a': [tag(i)] })
    await tick()
  }
  await p
})

test('cross-tab: a change of the whole tag library invalidates everything, other messages are ignored', () => {
  const { store, events, ctx } = loadStore()
  store.set('/a', [x])
  ctx.crossTab({ type: 'other' })
  strict.equal(events.length, 0)
  ctx.crossTab({ type: 'tagsChanged', all: true })
  ctx.crossTab({ type: 'tagsChanged', paths: [] })
  same(events, ['tagsInvalidated', 'backgroundMaintenanceUpdated', 'tagsInvalidated', 'backgroundMaintenanceUpdated'])
  same(store.tagMap.get('/a'), [x])
})
