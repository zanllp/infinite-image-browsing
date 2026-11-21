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
  runInNewContext(source, {
    exports, require: name => modules[name] ?? {}, parent: {}, window: { innerHeight: 800 },
    React: { createElement: (type, props, ...children) => ({ type, props, children }) }
  })
  return exports
}

// the code under test runs in its own vm context: compare plain copies
const plain = value => JSON.parse(JSON.stringify(value))

// all text of a rendered tree, dialog content is plain createElement objects
const text = node => {
  if (node === null || node === undefined || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(text).join('')
  return text(node.children)
}
const has = (node, type) => {
  if (!node || typeof node !== 'object') return false
  if (Array.isArray(node)) return node.some(n => has(n, type))
  return node.type === type || has(node.children, type)
}

// content: folder path -> { files, folders }; a path that is not listed counts as empty
function setup({ names, content = {}, skipConfirm = false, countsFail = false }) {
  const ref = value => ({ value })
  const files = names.map(name => ({
    fullpath: '/images/' + name,
    name,
    type: name.includes('.') ? 'file' : 'dir'
  }))
  const state = {
    sortedFiles: ref(files), multiSelectedIdxs: ref(files.map((_, i) => i)), currLocation: ref('/images'),
    previewing: ref(false), previewIdx: ref(0)
  }
  const calls = []
  const emitted = []
  let dialog
  const Checkbox = {}
  const modules = {
    vue: { ref, nextTick: () => Promise.resolve() },
    'lodash-es': { memoize: fn => fn, last: a => a[a.length - 1], range: () => [], uniqueId: () => '1' },
    'ant-design-vue': {
      Checkbox,
      Modal: { confirm: config => { dialog = config } },
      message: { success: msg => calls.push(['message', msg]) }
    },
    '@/i18n': { t: (key, args) => (args ? `${key} ${JSON.stringify(args)}` : key) },
    '@/api/files': {
      getFolderContent: async paths => {
        calls.push(['content', paths])
        if (countsFail) throw new Error('403')
        return { folders: paths.map(path => ({ path, files: 0, folders: 0, ...content[path] })) }
      },
      deleteFiles: async (paths, recursive) => { calls.push(['delete', paths, recursive]) }
    },
    '@/util': { createReactiveQueue: () => ({}), isImageFile: () => false },
    '@/store/useGlobalStore': {},
    '@/util/file': { toRawFileUrl: () => '' },
    '@vueuse/core': { useMouseInElement: () => ({}) },
    'vue3-ts-util': { useWatchDocument() {} },
    '@/components/functionalCallableComp': { MultiSelectTips: 'MultiSelectTips' },
    '.': {
      useEventListen() {},
      useHookShareState: () => ({ toRefs: () => state }),
      events: { emit: (name, args) => emitted.push([name, args]) },
      global: { ignoredConfirmActions: { deleteOneOnly: skipConfirm } }
    }
  }
  modules['@/util/path'] = load('util/path.ts', modules)
  const { onContextMenuClick } = load('page/fileTransfer/hooks/useFileItemActions.tsx', modules).useFileItemActions({})
  return {
    files, calls, emitted, Checkbox,
    dialog: () => dialog,
    run: () => onContextMenuClick({ key: 'deleteFiles' }, files[0], 0)
  }
}

test('a folder that is not empty is deleted recursively after the confirmation says what is in it', async () => {
  const ctx = setup({ names: ['a.png', 'full'], content: { '/images/full': { files: 12, folders: 3 } } })
  const pending = ctx.run()
  await new Promise(resolve => setImmediate(resolve))
  const dialog = ctx.dialog()
  assert.deepEqual(plain(ctx.calls), [['content', ['/images/full']]], 'only folders are asked for, nothing is deleted yet')
  const shown = text(dialog.content())
  assert.match(shown, /a\.png/)
  assert.match(shown, /full \(folderContent \{"files":12,"folders":3\}\)/)
  assert.match(shown, /deleteFoldersWithContent/)
  assert.equal(dialog.okType, 'danger')
  assert.equal(dialog.maskClosable, false)
  assert.equal(dialog.autoFocusButton, 'cancel', 'Enter must not delete the folder')
  assert.equal(has(dialog.content(), ctx.Checkbox), false, 'no way to skip the confirmation')
  await dialog.onOk()
  await pending
  assert.deepEqual(plain(ctx.calls[1]), ['delete', ['/images/a.png', '/images/full'], true])
  assert.deepEqual(plain(ctx.emitted), [['removeFiles', { paths: ['/images/a.png', '/images/full'], loc: '/images' }]])
})

test('a single folder that is not empty is confirmed even when confirming one file is switched off', async () => {
  const ctx = setup({ names: ['full'], skipConfirm: true, content: { '/images/full': { files: 1, folders: 0 } } })
  const pending = ctx.run()
  await new Promise(resolve => setImmediate(resolve))
  assert.ok(ctx.dialog(), 'the dialog is shown')
  assert.equal(ctx.calls.some(c => c[0] === 'delete'), false)
  await ctx.dialog().onOk()
  await pending
  assert.deepEqual(plain(ctx.calls.find(c => c[0] === 'delete')), ['delete', ['/images/full'], true])
})

test('an empty folder is deleted like a file, not recursively', async () => {
  const ctx = setup({ names: ['empty'], skipConfirm: true })
  await ctx.run()
  assert.deepEqual(plain(ctx.calls).slice(0, 2), [['content', ['/images/empty']], ['delete', ['/images/empty'], false]])
  assert.equal(ctx.dialog(), undefined, 'no dialog when confirming one file is switched off')
})

test('files only: no folder lookup and the usual confirmation', async () => {
  const ctx = setup({ names: ['a.png', 'b.png'] })
  const pending = ctx.run()
  await new Promise(resolve => setImmediate(resolve))
  const dialog = ctx.dialog()
  assert.deepEqual(plain(ctx.calls), [])
  assert.equal(dialog.okType, 'primary')
  assert.equal(dialog.autoFocusButton, undefined, 'the usual dialog keeps its default')
  assert.equal(dialog.maskClosable, true)
  assert.equal(has(dialog.content(), ctx.Checkbox), true)
  assert.doesNotMatch(text(dialog.content()), /deleteFoldersWithContent/)
  await dialog.onOk()
  await pending
  assert.deepEqual(plain(ctx.calls), [['delete', ['/images/a.png', '/images/b.png'], false], ['message', 'deleteSuccess']])
})

test('nothing is deleted when the content of the folders cannot be read', async () => {
  const ctx = setup({ names: ['full'], countsFail: true })
  await assert.rejects(ctx.run())
  assert.equal(ctx.dialog(), undefined)
  assert.equal(ctx.calls.some(c => c[0] === 'delete'), false)
})
