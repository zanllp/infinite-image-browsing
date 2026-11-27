const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

function load(file, modules) {
  const source = ts.transpileModule(readFileSync(join(__dirname, '../src', file), 'utf8'), {
    fileName: file,
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.React }
  }).outputText
  const exports = {}
  runInNewContext(source, { exports, require: name => modules[name] ?? {}, parent: {}, window: { innerHeight: 800 } })
  return exports
}

// a folder view (pane) that listens for removeFiles like the real one
function setup(currLocation) {
  const ref = value => ({ value })
  const state = {
    currLocation: ref(currLocation),
    stack: ref([{ curr: currLocation, files: [] }]),
    deletedFiles: ref(new Set())
  }
  const listeners = {}
  const modules = {
    vue: { ref, nextTick: async () => {} },
    'lodash-es': { memoize: fn => fn, last: items => items.at(-1) },
    '@vueuse/core': { useMouseInElement: () => ({}) },
    'vue3-ts-util': { useWatchDocument() {} },
    '@/util': { createReactiveQueue: () => ({}), isImageFile: path => /\.(png|jpe?g|webp)$/i.test(path) },
    '.': {
      useEventListen: (name, fn) => { listeners[name] = fn },
      useHookShareState: () => ({ toRefs: () => state })
    }
  }
  modules['@/util/path'] = load('util/path.ts', modules)
  load('page/fileTransfer/hooks/useFileItemActions.tsx', modules).useFileItemActions({})
  return { state, removeFiles: payload => listeners.removeFiles(payload) }
}

test('a folder view hides the files removed from its own folder', () => {
  const pane = setup('/images/a')
  pane.removeFiles({ paths: ['/images/a/1.png'], loc: '/images/a' })
  assert.deepEqual([...pane.state.deletedFiles.value].sort(), ['/images/a/1.png', '/images/a/1.txt'])
})

test('a folder view ignores files removed from another folder', () => {
  const pane = setup('/images/a')
  pane.removeFiles({ paths: ['/images/b/1.png'], loc: '/images/b' })
  assert.deepEqual([...pane.state.deletedFiles.value], [])
})

test('files removed from a search result grid (location "/") are hidden in the folder view of their folder', () => {
  const pane = setup('/images/a')
  pane.removeFiles({ paths: ['/images/a/1.png', '/images/b/2.png', '/images/a/3.png'], loc: '/' })
  assert.deepEqual([...pane.state.deletedFiles.value].sort(), [
    '/images/a/1.png', '/images/a/1.txt', '/images/a/3.png', '/images/a/3.txt'
  ])
})

test('files removed from a search result grid do not touch a folder view of another folder', () => {
  const pane = setup('/images/c')
  pane.removeFiles({ paths: ['/images/a/1.png'], loc: '/' })
  assert.deepEqual([...pane.state.deletedFiles.value], [])
})
