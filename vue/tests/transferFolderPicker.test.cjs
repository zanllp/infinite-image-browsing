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
    React: { createElement: (_, props, ...children) => ({ props, children }) }
  })
  return exports
}

function setup(isWin = false) {
  const ref = value => ({ value })
  const files = ['a.png', 'b.png'].map(name => ({ fullpath: '/images/' + name }))
  const state = { sortedFiles: ref(files), multiSelectedIdxs: ref([0, 1]), currLocation: ref('/images') }
  const calls = []
  let dialog
  const modules = {
    vue: { ref },
    'lodash-es': { memoize: fn => fn, debounce: () => Object.assign(() => {}, { cancel() {} }) },
    'ant-design-vue': { Modal: { confirm: config => {
      dialog = config
      return { update: changes => Object.assign(dialog, changes) }
    } } },
    '@/i18n': { t: key => key },
    '@/store/useGlobalStore': { useGlobalStore: () => ({ quickMovePaths: [], conf: { is_win: isWin } }) },
    '.': { useEventListen() {}, useHookShareState: () => ({ toRefs: () => state }) },
    '@/util/file': { toRawFileUrl: () => '' },
    '@/util': { createReactiveQueue: () => ({}) },
    '@vueuse/core': { useMouseInElement: () => ({}) },
    'vue3-ts-util': { useWatchDocument() {} },
    './transferResult': {
      copyFilesAndUpdateViews: args => calls.push(['copy', args]),
      moveFilesAndUpdateViews: args => calls.push(['move', args])
    }
  }
  modules['@/util/path'] = load('util/path.ts', modules)
  const picker = modules['@/components/functionalCallableComp'] = load('components/functionalCallableComp.tsx', modules)
  const { onContextMenuClick } = load('page/fileTransfer/hooks/useFileItemActions.tsx', modules).useFileItemActions({})
  return { picker, onContextMenuClick, state, files, calls, confirm(input) {
    dialog.content().children[0].props['onUpdate:value'](input)
    assert.equal(dialog.okButtonProps.disabled, false)
    dialog.onOk()
  }, rejectInput(input) {
    const field = dialog.content().children[0].props
    field['onUpdate:value'](input)
    assert.equal(dialog.okButtonProps.disabled, true)
    field.onPressEnter({ isComposing: false })
    dialog.onCancel()
  } }
}

for (const mode of ['copy', 'move']) {
  test(mode + ' keeps the original selection while the picker is open', async () => {
    const ctx = setup()
    const pending = ctx.onContextMenuClick({ key: mode + 'ToOtherFolder' }, ctx.files[0], 0)
    ctx.state.multiSelectedIdxs.value = []
    ctx.state.sortedFiles.value = []
    ctx.state.currLocation.value = '/other'
    ctx.confirm('/archive')
    await pending
    assert.equal(ctx.calls.length, 1)
    const [[action, args]] = ctx.calls
    assert.equal(action, mode)
    assert.deepEqual(Array.from(args.paths), ['/images/a.png', '/images/b.png'])
    assert.equal(args.dest, '/archive')
    assert.equal(args.from, '/images')
  })
}

for (const [isWin, base, absolute, relative] of [
  [true, 'C:/inbox', ['D:/archive', 'C:\\keep', '\\\\nas\\photos'], ['..', '../keep', '/']],
  [false, '/inbox', ['/archive', '/'], ['keep', '../keep']]
]) {
  test((isWin ? 'Windows' : 'POSIX') + ' picker only accepts absolute paths', async () => {
    const ctx = setup(isWin)
    for (const path of absolute) {
      const pending = ctx.picker.openTransferToFolderModal('move', base)
      ctx.confirm(path)
      assert.equal(await pending, path)
    }
    for (const path of relative) {
      const pending = ctx.picker.openTransferToFolderModal('copy', base)
      ctx.rejectInput(path)
      assert.equal(await pending, undefined)
    }
  })
}
