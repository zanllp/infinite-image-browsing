const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test, afterEach } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

// Exercise the real hook without mounting the file browser or calling its APIs.
// The hook source is transpiled to CJS and run inside a vm with the mocked module
// graph in setup(); if useLocation.ts gains/changes an import, the `modules` table
// below must be updated too (the require() assertion fails loudly otherwise).
const source = ts.transpileModule(
  readFileSync(join(__dirname, '../src/page/fileTransfer/hooks/useLocation.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }
).outputText

// The real path helpers; only lodash's memoize is replaced.
const pathSource = ts.transpileModule(
  readFileSync(join(__dirname, '../src/util/path.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }
).outputText
const Path = {}
runInNewContext(pathSource, {
  exports: Path,
  require: (name) => {
    assert.equal(name, 'lodash-es')
    return { memoize: (fn) => fn }
  }
})

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

function setup({ mode = 'scanned', previewing = false, fetchFiles, walker, stack, isWin = false } = {}) {
  const state = {
    scroller: vue.ref({ scrollToItem() {} }),
    stackViewEl: vue.ref(),
    stack: vue.ref(stack ?? [{ curr: '/images', files: oldFiles }]),
    currLocation: vue.ref('/images'),
    props: vue.ref({ mode }),
    deletedFiles: vue.ref(new Set()),
    walker: vue.shallowRef(walker),
    previewing: vue.ref(previewing),
    previewIdx: vue.ref(-1),
    sortMethod: vue.ref('date-desc'),
    multiSelectedIdxs: vue.ref([])
  }
  if (stack) {
    // as in index.tsx (scanned mode): the path is built from the curr of the pages
    state.currLocation = vue.computed(() => state.stack.value.length === 1
      ? '/'
      : Path.join(...state.stack.value.map(v => v.curr).slice(isWin ? 1 : 0)))
  }
  state.currPage = vue.computed(() => state.stack.value.at(-1))
  // 真实实现里 sortedFiles 由 currPage.files 派生，这里保持同样的派生关系
  state.sortedFiles = vue.computed(() => state.stack.value.at(-1)?.files ?? [])
  const listeners = new Map()
  const fetched = []
  const scrolledTo = []
  state.scroller.value.scrollToItem = idx => scrolledTo.push(idx)
  const events = []
  const messages = []
  let fetchCount = 0
  let poll
  let confirm
  let loadingContent
  const unmountCallbacks = []
  state.useEventListen = vue.ref(() => {})
  state.eventEmitter = vue.ref({ emit: event => events.push(event) })
  const global = vue.reactive({
    autoRefreshNormalFixedMode: true,
    autoRefreshWalkMode: true,
    autoRefreshWalkModePosLimit: 100,
    quickMovePaths: [],
    conf: { is_win: isWin, sd_cwd: '/' }
  })
  const modules = {
    '@/api/files': { getTargetFolderFiles: async (path) => {
      fetchCount++
      fetched.push(path)
      return fetchFiles ? fetchFiles(path) : { files: newFiles }
    } },
    '@/components/functionalCallableComp': {},
    '../sortedFiles': { computeSortedFiles: files => files.slice() },
    '@/i18n': { t: key => key },
    '@/icon': {},
    '@/util': {
      makeAsyncFunctionSingle: fn => fn,
      useGlobalEventListen: (event, fn) => listeners.set(event, fn)
    },
    'ant-design-vue': {
      message: {
        success: text => messages.push(text),
        info: text => messages.push(String(text)),
        error: text => messages.push(String(text)),
        loading: content => { loadingContent = content; return () => {} }
      },
      Modal: { confirm: options => { confirm = options } }
    },
    vue: { ...vue, onMounted() {}, onUnmounted: fn => unmountCallbacks.push(fn) },
    'vue3-ts-util': {
      delay: async () => {},
      ok: (value, text) => { if (!value) throw new Error(text) },
      useWatchDocument() {},
      Task: { run: ({ action }) => { poll = action; return { clearTask() {} } } }
    },
    '.': { useHookShareState: () => ({ toRefs: () => state }), stackCache: { get: () => undefined }, global },
    '@/components/numInput.vue': {},
    '@/util/path': Path,
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
    fetched,
    scrolledTo,
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
  test(`${mode}: automatic refresh applies immediately`, async () => {
    const c = setup({ mode })
    await c.returnToIIB()
    assert.equal(c.fetchCount(), 1)
    assert.deepEqual(c.state.currPage.value.files, newFiles)
    assert.deepEqual(c.messages, ['autoUpdate'])
  })
}

test('polling refresh applies immediately', async () => {
  const c = setup()
  await c.poll()
  assert.deepEqual(c.state.currPage.value.files, newFiles)
})

test('unchanged results neither replace the list nor show a message', async () => {
  const c = setup({ fetchFiles: async () => ({ files: oldFiles.slice() }) })
  const files = c.state.currPage.value.files
  await c.returnToIIB()
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
})

// ---- 全屏预览：列表立即更新，预览由应用级组件托管，不会因为列表更新被销毁 ----

test('previewing: applies immediately and keeps the preview on the same image', async () => {
  // 新图插到前面，预览的 old.png 从下标 0 挪到 1
  const next = [newFiles[0], oldFiles[0]]
  const c = setup({ previewing: true, fetchFiles: async () => ({ files: next }) })
  c.state.previewIdx.value = 0
  await c.returnToIIB()
  assert.deepEqual(c.state.currPage.value.files, next)
  assert.equal(c.state.previewIdx.value, 1) // 预览跟到同一张图的新位置
  assert.equal(c.state.previewing.value, true) // 预览保持打开
  assert.deepEqual(c.messages, ['autoUpdate'])
})

test('previewing: closes the preview when the viewed image disappears', async () => {
  const c = setup({ previewing: true })
  c.state.previewIdx.value = 0
  await c.returnToIIB()
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.equal(c.state.previewing.value, false)
  assert.deepEqual(c.messages, ['autoUpdate'])
})

test('previewing walk: applies immediately and keeps the preview on the same image', async () => {
  const replacement = { images: [newFiles[0], oldFiles[0]] }
  const walker = { isExpired: async () => true, seamlessRefresh: async () => replacement }
  const c = setup({ mode: 'walk', previewing: true, walker })
  c.state.previewIdx.value = 0
  await c.returnToIIB()
  assert.equal(c.state.walker.value, replacement)
  assert.equal(c.state.previewIdx.value, 1)
  assert.equal(c.state.previewing.value, true)
  assert.deepEqual(c.events, ['loadNextDir'])
})

test('previewing walk: closes the preview when the viewed image disappears', async () => {
  const replacement = { images: [newFiles[0]] }
  const walker = { isExpired: async () => true, seamlessRefresh: async () => replacement }
  const c = setup({ mode: 'walk', previewing: true, walker })
  c.state.previewIdx.value = 0
  await c.returnToIIB()
  assert.equal(c.state.walker.value, replacement)
  assert.equal(c.state.previewing.value, false)
})

test('walk refresh disabled does not touch the walker', async () => {
  const walker = { isExpired: async () => true, seamlessRefresh: async () => ({ images: newFiles }) }
  const c = setup({ mode: 'walk', walker })
  c.global.autoRefreshWalkMode = false
  await c.returnToIIB()
  assert.equal(c.state.walker.value, walker)
  assert.deepEqual(c.events, [])
})

// ---- 竞态保护 ----

test('only the newest request may apply, even when responses arrive out of order', async () => {
  const first = deferred()
  const second = deferred()
  let calls = 0
  const latestFiles = [{ fullpath: '/images/latest.png', date: 'latest' }]
  const c = setup({ fetchFiles: () => ++calls === 1 ? first.promise : second.promise })
  const oldRequest = c.returnToIIB()
  const newRequest = c.returnToIIB()
  second.resolve({ files: latestFiles })
  await newRequest
  first.resolve({ files: newFiles })
  await oldRequest
  assert.deepEqual(c.state.currPage.value.files, latestFiles)
})

test('navigation invalidates in-flight refreshes', async () => {
  const response = deferred()
  const c = setup({ fetchFiles: () => response.promise })
  const files = c.state.currPage.value.files
  const refresh = c.returnToIIB()
  c.state.currLocation.value = '/elsewhere'
  response.resolve({ files: newFiles })
  await refresh
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.messages, [])
})

test('unmount invalidates in-flight refreshes', async () => {
  const response = deferred()
  const c = setup({ fetchFiles: () => response.promise })
  const files = c.state.currPage.value.files
  const refresh = c.returnToIIB()
  c.unmount()
  response.resolve({ files: newFiles })
  await refresh
  assert.equal(c.state.currPage.value.files, files)
})

test('a failed refresh does not reject the caller', async () => {
  const c = setup({ fetchFiles: async () => { throw new Error('boom') } })
  const originalError = console.error
  console.error = () => {}
  try {
    await assert.doesNotReject(async () => { await c.returnToIIB() })
  } finally {
    console.error = originalError
  }
})

test('manual refresh still fetches and reports completion', async () => {
  const c = setup()
  await c.location.refresh()
  assert.equal(c.fetchCount(), 1)
  assert.deepEqual(c.state.currPage.value.files, newFiles)
  assert.deepEqual(c.messages, ['refreshCompleted'])
})

// ---- 返回上层目录：静默重新加载（移动/复制/删除之后，缓存的列表已经过时） ----

const settle = () => new Promise(resolve => setTimeout(resolve, 0))
const file = (fullpath, date = 'd') => ({ fullpath, date, name: fullpath.split('/').pop(), type: 'file' })
const dir = (fullpath) => ({ ...file(fullpath), type: 'dir' })
const page = (curr, files = oldFiles) => ({ curr, files })
const named = [file('/images/old.png')] // handleToScannedOnly compares names
const posixStack = () => [page('/', named), page('images', named), page('a', named), page('b', named)]
const withMoved = [file('/images/a/moved.png', 'new'), ...named]

test('going back reloads the folder and replaces a changed list without scrolling', async () => {
  const c = setup({ stack: posixStack(), fetchFiles: async () => ({ files: withMoved }) })
  c.global.autoRefreshNormalFixedMode = false
  await c.location.back(2)
  assert.deepEqual(c.fetched, ['/images/a'])
  assert.deepEqual(c.state.currPage.value.files, withMoved)
  assert.deepEqual(c.scrolledTo, [])
  assert.deepEqual(c.events, [])
  assert.deepEqual(c.messages, []) // silent
})

test('going back to an unchanged folder keeps the list, the selection and the position', async () => {
  const c = setup({ stack: posixStack(), fetchFiles: async () => ({ files: named.slice() }) })
  const files = c.state.stack.value[2].files
  c.state.multiSelectedIdxs.value = [0]
  await c.location.back(2)
  assert.equal(c.fetchCount(), 1)
  assert.equal(c.state.currPage.value.files, files)
  assert.deepEqual(c.state.multiSelectedIdxs.value, [0])
  assert.deepEqual(c.events, [])
  assert.deepEqual(c.messages, [])
})

test('going back detects a replaced file even if the timestamp did not change', async () => {
  const replacement = [file('/images/a/replacement.png', named[0].date)]
  const c = setup({ stack: posixStack(), fetchFiles: async () => ({ files: replacement }) })
  await c.location.back(2)
  assert.deepEqual(c.state.currPage.value.files, replacement)
})

test('going back to the root and to a windows drive loads the right folder', async () => {
  const root = setup({ stack: posixStack() })
  await root.location.back(0)
  assert.deepEqual(root.fetched, ['/'])
  const win = setup({ isWin: true, stack: [page('/'), page('C:/'), page('img'), page('a')] })
  await win.location.back(2)
  assert.deepEqual(win.fetched, ['C:/img'])
  await win.location.back(1)
  assert.deepEqual(win.fetched, ['C:/img', 'C:/'])
})

test('the current breadcrumb, the fixed and the walk mode do not reload', async () => {
  const c = setup({ stack: posixStack() })
  await c.location.back(3)
  const fixed = setup({ mode: 'scanned-fixed', stack: [page('/images/a')] })
  await fixed.location.back(0)
  assert.equal(c.fetchCount() + fixed.fetchCount(), 0)
})

for (const order of [['first', 'second'], ['second', 'first']]) {
  test(`overlapping reloads of a folder: the newer one wins (answers ${order.join(', ')})`, async () => {
    const answers = { first: deferred(), second: deferred() }
    const sequence = [answers.first.promise, async () => ({ files: oldFiles }), answers.second.promise]
    const c = setup({ stack: posixStack(), fetchFiles: () => { const v = sequence.shift(); return typeof v === 'function' ? v() : v } })
    const first = c.location.back(2) // a, reload #1
    await settle()
    await c.location.openNext(dir('/images/a/b')) // and back again: reload #2
    const second = c.location.back(2)
    await settle()
    const files = { first: [file('/images/a/first.png')], second: [file('/images/a/second.png')] }
    for (const name of order) {
      answers[name].resolve({ files: files[name] })
      await settle()
    }
    await Promise.all([first, second])
    assert.deepEqual(c.fetched, ['/images/a', '/images/a/b', '/images/a'])
    assert.deepEqual(c.state.currPage.value.files, files.second)
  })
}

test('a refresh that was started before going back updates its own folder, not the one shown', async () => {
  const pending = deferred()
  const parent = [file('/images/a/p.png')]
  const child = [file('/images/a/b/c.png')]
  const c = setup({ stack: posixStack(), fetchFiles: (path) => path === '/images/a/b' ? pending.promise : { files: parent } })
  const b = c.state.stack.value[3]
  const refresh = c.location.refresh()
  await c.location.back(2)
  pending.resolve({ files: child })
  await refresh
  assert.deepEqual(c.state.currPage.value.files, parent)
  assert.deepEqual(b.files, child)
  assert.deepEqual(c.scrolledTo, []) // and it does not scroll the folder shown now
})

test('previewing: a reload keeps the preview on the same image', async () => {
  const c = setup({ previewing: true, stack: posixStack(), fetchFiles: async () => ({ files: withMoved }) })
  const back = c.location.back(2)
  c.state.previewIdx.value = 0 // old.png, moves to 1
  await back
  assert.equal(c.state.previewIdx.value, 1)
  assert.equal(c.state.previewing.value, true)
})

test('previewing: a reload closes the preview when the image is gone', async () => {
  const c = setup({ previewing: true, stack: posixStack(), fetchFiles: async () => ({ files: [file('/images/a/other.png')] }) })
  const back = c.location.back(2)
  c.state.previewIdx.value = 0
  await back
  assert.equal(c.state.previewing.value, false)
})

test('navigate up loads the folder once (by refresh)', async () => {
  const c = setup({ stack: posixStack() })
  await c.location.backToLastUseTo()
  await settle()
  assert.deepEqual(c.fetched, ['/images/a'])
  assert.deepEqual(c.messages, ['refreshCompleted'])
})
