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
// the global store: only the setting that the dialog reads
const previewStore = vue.reactive({ previewInitialZoom: 'shrinkToFit' })
let dialogAttrs
const PreviewDialog = loadSfc('page/fileTransfer/PreviewDialog.vue', {
  vue,
  'ant-design-vue/es/vc-dialog': {
    default: { render() { dialogAttrs = this.$attrs; return vue.h('div', { class: 'dialog' }, this.$slots.default()) } }
  },
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
  '@/store/useGlobalStore': { useGlobalStore: () => previewStore },
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

// minimal renderer without a DOM; `sizes` gives the elements of a type fixed layout properties
function createDomlessRenderer(sizes) {
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
  return { render, el, find, findAll }
}

test('switching images resets zoom, rotation and position; the same src keeps them', async () => {
  // the image and its area get fixed layout sizes
  const { render, el, find, findAll } = createDomlessRenderer({
    img: { offsetWidth: 400, offsetHeight: 300, naturalWidth: 800, offsetLeft: 0, offsetTop: 0 },
    div: { clientWidth: 1000, clientHeight: 800 }
  })

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

// ---- PreviewDialog: the zoom the preview opens with (global setting previewInitialZoom) ----

// image box 400x300 (800 px wide, so actual size is scale 2) in an area of 1000x800:
// fit is min(1000/400, 800/300) = 2.5, the centre of the image is at (-300, -250) from the centre of the area
const mountZoomDialog = async (mode, { complete = false, naturalWidth = 800, visible = true, src = '/original/a.png' } = {}) => {
  previewStore.previewInitialZoom = mode
  const { render, el, find, findAll } = createDomlessRenderer({
    img: { offsetWidth: 400, offsetHeight: 300, naturalWidth, complete, offsetLeft: 0, offsetTop: 0 },
    div: { clientWidth: 1000, clientHeight: 800 }
  })
  const state = vue.reactive({ src, visible })
  const root = el('root')
  render(vue.h({ render: () => vue.h(PreviewDialog, { src: state.src, visible: state.visible }) }), root)
  await nextTick()
  const img = find(root, 'img')
  const wrapper = findAll(root, 'div').find(d => String(d.props.class).includes('img-wrapper'))
  return {
    state,
    img,
    tools: Object.fromEntries(findAll(root, 'li').map(li => [li.props.title, li.props.onClick])),
    view: () => ({ image: img.props.style.transform, wrapper: wrapper.props.style.transform }),
    afterClose: () => (dialogAttrs['after-close'] ?? dialogAttrs.afterClose)(),
    unmount: () => render(null, root)
  }
}
// the browser marks the image complete before its load event
const load = d => {
  d.img.complete = true
  d.img.props.onLoad()
}
// the visible watcher applies the setting in a nextTick, the render follows one tick later
const settle = async () => { for (let i = 0; i < 3; i++) await nextTick() }
const viewOf = (scale, x, y, rotate = 0) => ({
  image: `scale3d(${scale}, ${scale}, 1) rotate(${rotate}deg)`,
  wrapper: `translate3d(${x}px, ${y}px, 0)`
})

test('previewInitialZoom: the state after the image has loaded follows the setting', async () => {
  const expected = {
    shrinkToFit: viewOf(1, 0, 0),
    fit: viewOf(2.5, 300, 250),
    oneToOne: viewOf(2, 300, 250)
  }
  for (const [mode, view] of Object.entries(expected)) {
    const d = await mountZoomDialog(mode)
    // nothing is applied before the image is there
    assert.deepEqual(d.view(), viewOf(1, 0, 0), mode)
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), view, mode)
    // later loads of the same image (e.g. a reload) do not reset a view the user has changed
    d.tools.previewZoomOut()
    await nextTick()
    const changed = d.view()
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), changed, mode)
    d.unmount()
  }
})

test('previewInitialZoom: the opening zoom follows layout changes until the user zooms, drags or rotates', async () => {
  const d = await mountZoomDialog('fit')
  const area = d.img.parent
  load(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(2.5, 300, 250))
  // the area gets narrower (e.g. the info panel opens beside the preview): fit is min(600/400, 800/300) = 1.5
  area.clientWidth = 600
  load(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1.5, 100, 250))
  // after a zoom step the layout no longer changes the view
  d.tools.previewZoomIn()
  await nextTick()
  const zoomed = d.view()
  area.clientWidth = 1000
  load(d)
  await nextTick()
  assert.equal(d.view().image, zoomed.image)
  d.unmount()
})

test('previewInitialZoom: actual size of a big image extends beyond the area, centred', async () => {
  const d = await mountZoomDialog('oneToOne', { naturalWidth: 1600 })
  load(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(4, 300, 250))
  d.unmount()
})

test('previewInitialZoom: switching images applies the setting to the new image again', async () => {
  for (const [mode, view] of [['fit', viewOf(2.5, 300, 250)], ['oneToOne', viewOf(2, 300, 250)]]) {
    const d = await mountZoomDialog(mode)
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), view)
    d.tools.previewRotateRight()
    await nextTick()
    d.img.complete = false
    d.state.src = '/original/b.png'
    await nextTick()
    // until the new image has loaded it is the opening state of the browser layout
    assert.deepEqual(d.view(), viewOf(1, 0, 0), mode)
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), view, mode)
    d.unmount()
  }
})

test('previewInitialZoom: reopening with the same, already loaded image applies the setting without a load', async () => {
  const d = await mountZoomDialog('fit', { complete: true, visible: false })
  // closed: nothing happens
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  d.state.visible = true
  await settle()
  assert.deepEqual(d.view(), viewOf(2.5, 300, 250))
  // close (the dialog resets the view after its animation) and reopen with another setting
  d.state.visible = false
  await nextTick()
  d.afterClose()
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  previewStore.previewInitialZoom = 'oneToOne'
  d.state.visible = true
  await settle()
  assert.deepEqual(d.view(), viewOf(2, 300, 250))
  // shrinkToFit leaves the reopened view as it is
  d.state.visible = false
  await nextTick()
  d.afterClose()
  previewStore.previewInitialZoom = 'shrinkToFit'
  d.state.visible = true
  await settle()
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  d.unmount()
})

test('previewInitialZoom: an image that is not loaded yet is not measured on reopening', async () => {
  const d = await mountZoomDialog('fit', { complete: false, visible: false })
  d.state.visible = true
  await settle()
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  load(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(2.5, 300, 250))
  d.unmount()
})

// a double-click as the browser sends it: mousedown, mouseup, mousedown, mouseup, dblclick
const doubleClick = d => {
  const down = { button: 0, pageX: 10, pageY: 10, preventDefault() {}, stopPropagation() {} }
  for (let i = 0; i < 2; i++) {
    d.img.props.onMousedown(down)
    ;[...windowListeners.mouseup].forEach(fn => fn({}))
  }
  d.img.props.onDblclick()
}

test('previewInitialZoom: a double-click returns to the configured zoom, which keeps following layout changes', async () => {
  const cases = [
    ['fit', viewOf(2.5, 300, 250), viewOf(1.5, 100, 250)],
    ['oneToOne', viewOf(2, 300, 250), viewOf(2, 100, 250)]
  ]
  for (const [mode, opening, narrow] of cases) {
    // the view is untouched (opened fitted, the user never zoomed)
    let d = await mountZoomDialog(mode)
    load(d)
    await nextTick()
    doubleClick(d)
    await nextTick()
    assert.deepEqual(d.view(), opening, mode)
    d.img.parent.clientWidth = 600
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), narrow, mode)
    d.unmount()

    // zoomed first: the double-click gives the same state as above, including following the layout
    d = await mountZoomDialog(mode)
    load(d)
    await nextTick()
    d.tools.previewZoomIn()
    await nextTick()
    assert.notEqual(d.view().image, opening.image, mode)
    doubleClick(d)
    await nextTick()
    assert.deepEqual(d.view(), opening, mode)
    d.img.parent.clientWidth = 600
    load(d)
    await nextTick()
    assert.deepEqual(d.view(), narrow, mode)
    d.unmount()
  }
})

test('previewInitialZoom: a double-click after a drag follows the layout again, and a repeated one changes nothing', async () => {
  const d = await mountZoomDialog('fit')
  load(d)
  await nextTick()
  d.img.props.onMousedown({ button: 0, pageX: 0, pageY: 0, preventDefault() {}, stopPropagation() {} })
  ;[...windowListeners.mousemove].forEach(fn => fn({ pageX: 40, pageY: 40 }))
  ;[...windowListeners.mouseup].forEach(fn => fn({}))
  // the drag took over: a layout change no longer refits
  d.img.parent.clientWidth = 600
  load(d)
  await nextTick()
  assert.equal(d.view().image, viewOf(2.5, 0, 0).image)
  doubleClick(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1.5, 100, 250))
  doubleClick(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1.5, 100, 250))
  d.unmount()
})

test('previewInitialZoom: with shrinkToFit a double-click still resets to scale 1 and position 0', async () => {
  const d = await mountZoomDialog('shrinkToFit')
  load(d)
  await nextTick()
  d.tools.previewZoomIn()
  d.img.props.onMousedown({ button: 0, pageX: 0, pageY: 0, preventDefault() {}, stopPropagation() {} })
  ;[...windowListeners.mousemove].forEach(fn => fn({ pageX: 15, pageY: 5 }))
  ;[...windowListeners.mouseup].forEach(fn => fn({}))
  await nextTick()
  assert.notEqual(d.view().image, viewOf(1, 0, 0).image)
  doubleClick(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  // the layout change leaves the opening state to the browser
  d.img.parent.clientWidth = 600
  load(d)
  await nextTick()
  assert.deepEqual(d.view(), viewOf(1, 0, 0))
  d.unmount()
})
