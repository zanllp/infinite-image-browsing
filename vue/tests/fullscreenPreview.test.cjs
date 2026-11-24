const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const { parse, compileScript } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')
const { createRenderer, nextTick } = require('@vue/runtime-core')
const ts = require('typescript')
const vue = require('vue')

// compile a SFC (script setup + template inlined) and evaluate it with mocked dependencies
function loadSfc(file, modules, win = { addEventListener() {}, removeEventListener() {} }) {
  const filename = join(__dirname, '../src', file)
  const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
  const script = compileScript(descriptor, { id: 'preview-test', inlineTemplate: true })
  const source = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
  }).outputText
  const exportsObject = {}
  runInNewContext(source, {
    exports: exportsObject,
    window: win,
    require: name => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected dependency: ${name}`)
      return modules[name]
    }
  })
  return exportsObject.default
}

// ---- FullscreenPreview: host of the dialog ----

let dialogEmit
const DialogStub = {
  props: ['src', 'visible'],
  emits: ['close', 'afterClose'],
  setup(props, { emit }) {
    dialogEmit = emit
    return () => vue.h('div', { class: 'dialog-stub', 'data-src': props.src, 'data-visible': String(props.visible) })
  }
}
const FullscreenPreview = loadSfc('page/fileTransfer/FullscreenPreview.vue', {
  vue,
  '@/util/file': { toImageUrl: file => `/original/${file.name}` },
  '@/util/imagePreviewOperation': { registerFullscreenPreviewCloser: () => () => {} },
  './PreviewDialog.vue': { default: DialogStub }
})

const files = [{ name: 'test.png', fullpath: '/images/test.png' }]

test('the host renders the dialog for the selected file, without a hidden thumbnail', async () => {
  const html = await renderToString(vue.createSSRApp(FullscreenPreview, { files, previewIdx: 0, previewing: false }))
  assert.match(html, /<div class="dialog-stub" data-src="\/original\/test.png" data-visible="false"><\/div>/)
  assert.doesNotMatch(html, /ant-image|thumbnail|<img/)
})

test('no dialog is rendered before an image is selected', async () => {
  const html = await renderToString(vue.createSSRApp(FullscreenPreview, { files, previewIdx: -1, previewing: false }))
  assert.doesNotMatch(html, /dialog-stub|ant-image/)
})

test('visibleChange follows previewing (new, old) and a close request is (false, true)', async () => {
  // minimal renderer without a DOM
  const el = type => ({ type, children: [], props: {}, parent: null })
  const { render } = createRenderer({
    createElement: el,
    createText: text => ({ type: '#text', text, children: [], parent: null }),
    createComment: text => ({ type: '#comment', text, children: [], parent: null }),
    setText: (node, text) => { node.text = text },
    setElementText: () => {},
    insert: (child, parent, anchor) => {
      child.parent = parent
      const i = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(i < 0 ? parent.children.length : i, 0, child)
    },
    remove: child => {
      child.parent?.children.splice(child.parent.children.indexOf(child), 1)
    },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: () => {}
  })
  const calls = []
  const state = vue.reactive({ previewing: false })
  const Root = {
    render: () =>
      vue.h(FullscreenPreview, {
        files,
        previewIdx: 0,
        previewing: state.previewing,
        onVisibleChange: (...args) => calls.push(args)
      })
  }
  render(vue.h(Root), el('root'))
  state.previewing = true
  await nextTick()
  assert.deepEqual(calls, [[true, false]])
  // closing from the dialog (x, mask, Esc) asks the owner to close
  dialogEmit('close')
  assert.deepEqual(calls.at(-1), [false, true])
  // the owner closes: the prop change is reported as well (needed to scroll the list on close)
  state.previewing = false
  await nextTick()
  assert.deepEqual(calls.at(-1), [false, true])
})

// ---- PreviewDialog: toolbar and markup that other parts of IIB rely on ----

const icon = name => ({ render: () => vue.h('span', { 'data-icon': name }) })
const windowListeners = {}
const fakeWindow = {
  addEventListener: (name, fn) => { (windowListeners[name] ??= new Set()).add(fn) },
  removeEventListener: (name, fn) => { windowListeners[name]?.delete(fn) }
}
const PreviewDialog = loadSfc('page/fileTransfer/PreviewDialog.vue', {
  vue,
  'ant-design-vue/es/vc-dialog': { default: { render() { return vue.h('div', { class: 'dialog' }, this.$slots.default()) } } },
  'ant-design-vue/es/image/style': {},
  '@/icon': {
    CloseOutlined: icon('close'),
    ExpandOutlined: icon('expand'),
    OneToOneOutlined: icon('one-to-one'),
    RotateLeftOutlined: icon('rotate-left'),
    RotateRightOutlined: icon('rotate-right'),
    ZoomInOutlined: icon('zoom-in'),
    ZoomOutOutlined: icon('zoom-out')
  },
  '@/i18n': { t: key => key },
  '@/util/previewZoom': require('./helpers/previewZoomModule.cjs')
}, fakeWindow)

test('the dialog keeps the antd markup and has the seven tools in row-reverse order', async () => {
  const html = await renderToString(vue.createSSRApp(PreviewDialog, { src: '/original/test.png', visible: true }))
  const items = [...html.matchAll(/<li class="([^"]*)" title="([^"]*)"><span[^>]*data-icon="([^"]*)"/g)]
  assert.deepEqual(
    items.map(m => [m[2], m[3]]),
    [
      ['close', 'close'],
      ['previewZoomIn', 'zoom-in'],
      ['previewFitToWindow', 'expand'],
      ['previewActualSize', 'one-to-one'],
      ['previewZoomOut', 'zoom-out'],
      ['previewRotateRight', 'rotate-right'],
      ['previewRotateLeft', 'rotate-left']
    ]
  )
  assert.ok(items.every(m => m[1].split(' ').includes('ant-image-preview-operations-operation')))
  assert.match(html, /<ul class="ant-image-preview-operations">/)
  assert.match(html, /<div class="ant-image-preview-img-wrapper"/)
  // fullScreenContextMenu.vue matches the image by this exact className
  assert.match(html, /<img class="ant-image-preview-img" src="\/original\/test.png"/)
})

test('switching images resets zoom, rotation and position; the same src keeps them', async () => {
  // minimal renderer without a DOM; the image and its area get fixed layout sizes
  const sizes = {
    img: { offsetWidth: 400, offsetHeight: 300, naturalWidth: 800, offsetLeft: 0, offsetTop: 0 },
    div: { clientWidth: 1000, clientHeight: 800 }
  }
  const el = type => ({ type, children: [], props: {}, parent: null, ...sizes[type] })
  const { render } = createRenderer({
    createElement: el,
    createText: text => ({ type: '#text', text, children: [], parent: null }),
    createComment: text => ({ type: '#comment', text, children: [], parent: null }),
    setText: (node, text) => { node.text = text },
    setElementText: () => {},
    insert: (child, parent, anchor) => {
      child.parent = parent
      const i = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(i < 0 ? parent.children.length : i, 0, child)
    },
    remove: child => {
      child.parent?.children.splice(child.parent.children.indexOf(child), 1)
    },
    parentNode: node => node.parent,
    nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] ?? null,
    patchProp: (node, key, _prev, next) => { node.props[key] = next }
  })
  const find = (node, type) =>
    node.type === type ? node : node.children.map(c => find(c, type)).find(Boolean)
  const findAll = (node, type) =>
    (node.type === type ? [node] : []).concat(...node.children.map(c => findAll(c, type)))

  const state = vue.reactive({ src: '/original/a.png', renders: 0 })
  const root = el('root')
  // `renders` makes the host re-render without changing src
  render(vue.h({ render: () => (state.renders, vue.h(PreviewDialog, { src: state.src, visible: true })) }), root)
  await nextTick()
  const img = find(root, 'img')
  const wrapper = find(root, 'div') && findAll(root, 'div').find(d => String(d.props.class).includes('img-wrapper'))
  const tools = Object.fromEntries(findAll(root, 'li').map(li => [li.props.title, li.props.onClick]))
  const view = () => ({
    image: img.props.style.transform,
    wrapper: wrapper.props.style.transform
  })
  const start = { image: 'scale3d(1, 1, 1) rotate(0deg)', wrapper: 'translate3d(0px, 0px, 0)' }
  assert.deepEqual(view(), start)

  // zoom in, rotate and drag
  const move = (type, x, y) => [...windowListeners[type]].forEach(fn => fn({ pageX: x, pageY: y }))
  tools.previewZoomIn()
  tools.previewZoomIn()
  tools.previewRotateRight()
  await nextTick()
  img.props.onMousedown({ button: 0, pageX: 0, pageY: 0, preventDefault() {}, stopPropagation() {} })
  move('mousemove', 30, 20)
  await nextTick()
  assert.notDeepEqual(view(), start)
  assert.match(img.props.style.transform, /rotate\(90deg\)/)

  // the same src does not reset anything
  state.renders++
  await nextTick()
  assert.match(img.props.style.transform, /rotate\(90deg\)/)

  // a new src (also while dragging) goes back to the opening state
  state.src = '/original/b.png'
  await nextTick()
  assert.deepEqual(view(), start)
  // the drag ended with the switch: moving and releasing change nothing
  move('mousemove', 90, 90)
  move('mouseup')
  await nextTick()
  assert.deepEqual(view(), start)
  // the controls keep working for the new image
  tools.previewZoomIn()
  await nextTick()
  assert.notEqual(img.props.style.transform, start.image)
})
