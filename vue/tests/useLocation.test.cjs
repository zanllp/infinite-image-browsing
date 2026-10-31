const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test, afterEach } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

// Exercise the real hook without mounting the file browser or calling its APIs.
const source = ts.transpileModule(
  readFileSync(join(__dirname, '../src/page/fileTransfer/hooks/useLocation.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }
).outputText

const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const oldFiles = [{ fullpath: '/images/old.png', date: 'old' }]
const newFiles = [{ fullpath: '/images/new.png', date: 'new' }]
const cleanups = []
afterEach(() => {
  cleanups.splice(0).forEach(cleanup => cleanup())
})

function setup({ mode = 'scanned', previewing = false, fetchFiles, walker } = {}) {
  const state = {
    scroller: vue.ref({ scrollToItem() {} }),
    stackViewEl: vue.ref(),
    stack: vue.ref([{ curr: '/images', files: oldFiles }]),
    currLocation: vue.ref('/images'),
    props: vue.ref({ mode }),
    deletedFiles: vue.ref(new Set()),
    walker: vue.shallowRef(walker),
    sortedFiles: vue.ref(oldFiles),
    previewing: vue.ref(previewing)
  }
  state.currPage = vue.computed(() => state.stack.value.at(-1))
  const listeners = new Map()
  const events = []
  const messages = []
  let fetchCount = 0
  let hideCount = 0
  let poll
  let confirm
  let loadingContent
  const unmountCallbacks = []
  state.useEventListen = vue.ref(() => {})
  state.eventEmitter = vue.ref({ emit: event => events.push(event) })
  const global = {
    autoRefreshNormalFixedMode: true,
    autoRefreshWalkMode: true,
    autoRefreshWalkModePosLimit: 100,
    quickMovePaths: []
  }
  const modules = {
    '@/api/files': { getTargetFolderFiles: async () => {
      fetchCount++
      return fetchFiles ? fetchFiles() : { files: newFiles }
    } },
    '@/components/functionalCallableComp': {},
    '@/i18n': { t: key => key },
    '@/icon': {},
    '@/util': {
      makeAsyncFunctionSingle: fn => fn,
      useGlobalEventListen: (event, fn) => listeners.set(event, fn)
    },
    'ant-design-vue': {
      message: {
        success: text => messages.push(text),
        loading: content => { loadingContent = content; return () => { hideCount++ } }
      },
      Modal: { confirm: options => { confirm = options } }
    },
    vue: { ...vue, onMounted() {}, onUnmounted: fn => unmountCallbacks.push(fn) },
    'vue3-ts-util': {
      delay: async () => {},
      useWatchDocument() {},
      Task: { run: ({ action }) => { poll = action; return { clearTask() {} } } }
    },
    '.': { useHookShareState: () => ({ toRefs: () => state }), global },
    '@/components/numInput.vue': {},
    '@/util/path': { normalize: value => value },
    '@/util/scroller': { getVisibleRange: () => ({ start: 0, end: 20 }) },
    'multi-nprogress': {},
    'lodash-es': { last: items => items.at(-1), debounce: () => () => {} },
    '@vueuse/core': { useLocalStorage: (_, initial) => vue.ref(initial) },
    '@/util/const': { prefix: 'test-' }
  }
  const exports = {}
  runInNewContext(source, {
    exports,
    require: name => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected dependency: ${name}`)
      return modules[name]
    }
  })
  const scope = vue.effectScope()
  const location = scope.run(() => exports.useLocation())
  const unmount = () => {
    unmountCallbacks.splice(0).forEach(fn => fn())
    scope.stop()
  }
  cleanups.push(unmount)
  return {
    state, global, events, messages, location, unmount,
    fetchCount: () => fetchCount,
    hideCount: () => hideCount,
    disableWalkRefresh: () => loadingContent.children[1].props.onClick(),
    returnToIIB: () => listeners.get('returnToIIB')(),
    poll: () => {
      location.onPollRefreshClick()
      confirm.onOk()
      return poll()
    }
  }
}

for (const mode of ['scanned', 'scanned-fixed']) {
  test(`${mode} prefetches during preview and applies the result on close without refetching`, async () => {
    const c = setup({ mode, previewing: true })
    const files = c.state.currPage.value.files
    await c.returnToIIB()
    assert.equal(c.fetchCount(), 1)
    assert.equal(c.state.currPage.value.files, files)
    assert.equal(c.state.previewing.value, true)
    assert.deepEqual(c.messages, [])
    c.state.previewing.value = false
    await vue.nextTick()
    assert.deepEqual(c.state.currPage.value.files, newFiles)
    assert.equal(c.fetchCount(), 1)
    assert.deepEqual(c.messages, ['autoUpdate'])
  })
}

test('polling also prefetches without changing the preview list', async () => {
  const c = setup({ previewing: true })
  const files = c.state.currPage.value.files
  await c.poll()
  assert.equal(c.fetchCount(), 1)
  assert.equal(c.state.currPage.value.files, files)
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, newFiles)
})

test('auto-refresh applies immediately when the preview is closed', async () => {
  const c = setup()
  await c.returnToIIB()
  assert.equal(c.fetchCount(), 1)
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.deepEqual(c.messages, ['autoUpdate'])
})

test('an in-flight folder refresh cannot replace the list after a preview opens', async () => {
  const response = deferred()
  const c = setup({ fetchFiles: () => response.promise })
  const files = c.state.currPage.value.files
  const refresh = c.returnToIIB()
  c.state.previewing.value = true
  response.resolve({ files: newFiles })
  await refresh
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.equal(c.fetchCount(), 1)
})

test('a response arriving after preview close applies immediately', async () => {
  const response = deferred()
  const c = setup({ previewing: true, fetchFiles: () => response.promise })
  const refresh = c.returnToIIB()
  c.state.previewing.value = false
  await vue.nextTick()
  response.resolve({ files: newFiles })
  await refresh
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.equal(c.fetchCount(), 1)
})

test('only the newest request can supply a deferred result, even when responses arrive out of order', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const latestFiles = [{ fullpath: '/images/latest.png', date: 'latest' }]
  const c = setup({ previewing: true, fetchFiles: () => ++calls === 1 ? first.promise : second.promise })
  const oldRequest = c.returnToIIB()
  const newRequest = c.returnToIIB()
  second.resolve({ files: latestFiles })
  await newRequest
  first.resolve({ files: newFiles })
  await oldRequest
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, latestFiles)
  assert.deepEqual(c.messages, ['autoUpdate'])
})

test('a newer result supersedes an already buffered result', async () => {
  let calls = 0
  const c = setup({ previewing: true, fetchFiles: async () => ({ files: ++calls === 1 ? newFiles : [] }) })
  await c.returnToIIB()
  await c.returnToIIB()
  assert.deepEqual(c.state.currPage.value.files, oldFiles)
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, [])
  assert.deepEqual(c.messages, ['autoUpdate'])
})

test('unchanged results do not replace the list or show an update message', async () => {
  const c = setup({ previewing: true, fetchFiles: async () => ({ files: oldFiles }) })
  const files = c.state.currPage.value.files
  await c.returnToIIB()
  c.state.previewing.value = false
  await vue.nextTick()
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
})

test('walk refresh prefetches even if a preview opens during the expiry check', async () => {
  const expired = deferred()
  let replacements = 0
  const c = setup({ mode: 'walk', walker: {
    isExpired: () => expired.promise,
    seamlessRefresh: async () => { replacements++ }
  } })
  const refresh = c.returnToIIB()
  c.state.previewing.value = true
  expired.resolve(true)
  await refresh
  assert.equal(replacements, 1)
  assert.deepEqual(c.events, [])
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.events, ['loadNextDir'])
})

for (const previewOpens of [false, true]) {
  test(`walk refresh ${previewOpens ? 'defers' : 'applies'} its result when a preview ${previewOpens ? 'opens' : 'stays closed'}`, async () => {
    const response = deferred()
    const started = deferred()
    const walker = {
      isExpired: async () => true,
      seamlessRefresh: () => { started.resolve(); return response.promise }
    }
    const c = setup({ mode: 'walk', walker })
    const refresh = c.returnToIIB()
    await started.promise
    c.state.previewing.value = previewOpens
    const replacement = {}
    response.resolve(replacement)
    await refresh
    assert.equal(c.state.walker.value, previewOpens ? walker : replacement)
    assert.deepEqual(c.events, previewOpens ? [] : ['loadNextDir'])
    assert.equal(c.hideCount(), 1)
    if (previewOpens) {
      c.state.previewing.value = false
      await vue.nextTick()
      assert.equal(c.state.walker.value, replacement)
      assert.deepEqual(c.events, ['loadNextDir'])
    }
  })
}

test('walk mode prefetches an independent walker while a preview is already open', async () => {
  let replacements = 0
  const replacement = {}
  const walker = {
    isExpired: async () => true,
    seamlessRefresh: async () => { replacements++; return replacement }
  }
  const c = setup({ mode: 'walk', previewing: true, walker })
  await c.returnToIIB()
  assert.equal(replacements, 1)
  assert.equal(c.state.walker.value, walker)
  c.state.previewing.value = false
  await vue.nextTick()
  assert.equal(c.state.walker.value, replacement)
  assert.equal(replacements, 1)
  assert.deepEqual(c.events, ['loadNextDir'])
})

test('disabling walk auto-refresh discards an already prefetched walker', async () => {
  const walker = {
    isExpired: async () => true,
    seamlessRefresh: async () => ({})
  }
  const c = setup({ mode: 'walk', previewing: true, walker })
  await c.returnToIIB()
  c.disableWalkRefresh()
  c.state.previewing.value = false
  await vue.nextTick()
  assert.equal(c.state.walker.value, walker)
  assert.deepEqual(c.events, [])
})

test('disabled auto-refresh does not prefetch during preview', async () => {
  const c = setup({ previewing: true })
  c.global.autoRefreshNormalFixedMode = false
  await c.returnToIIB()
  assert.equal(c.fetchCount(), 0)
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, oldFiles)
})

for (const responseReady of [false, true]) {
  test(`navigation invalidates ${responseReady ? 'buffered' : 'in-flight'} results, including a return to the same path`, async () => {
    const response = deferred()
    const c = setup({ previewing: true, fetchFiles: () => response.promise })
    const files = c.state.currPage.value.files
    const refresh = c.returnToIIB()
    if (responseReady) {
      response.resolve({ files: newFiles })
      await refresh
    }
    c.state.currLocation.value = '/elsewhere'
    c.state.currLocation.value = '/images'
    if (!responseReady) {
      response.resolve({ files: newFiles })
      await refresh
    }
    c.state.previewing.value = false
    await vue.nextTick()
    assert.equal(c.state.currPage.value.files, files)
    assert.deepEqual(c.messages, [])
  })
}

test('unmount invalidates in-flight refreshes', async () => {
  const response = deferred()
  const c = setup({ fetchFiles: () => response.promise })
  const files = c.state.currPage.value.files
  const refresh = c.returnToIIB()
  c.unmount()
  response.resolve({ files: newFiles })
  await refresh
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
})

test('switching mode invalidates buffered results even when the path is unchanged', async () => {
  const c = setup({ previewing: true })
  const files = c.state.currPage.value.files
  await c.returnToIIB()
  c.state.props.value.mode = 'walk'
  c.state.previewing.value = false
  await vue.nextTick()
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
})

test('manual refresh invalidates buffered auto-refresh data', async () => {
  let calls = 0
  const c = setup({ previewing: true, fetchFiles: async () => ({ files: ++calls === 1 ? newFiles : [] }) })
  await c.returnToIIB()
  await c.location.refresh()
  c.state.previewing.value = false
  await vue.nextTick()
  assert.deepEqual(c.state.currPage.value.files, [])
  assert.deepEqual(c.messages, ['refreshCompleted'])
})

test('explicit manual refresh remains available', async () => {
  const c = setup({ previewing: true })
  await c.location.refresh()
  assert.equal(c.fetchCount(), 1)
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.deepEqual(c.messages, ['refreshCompleted'])
})
