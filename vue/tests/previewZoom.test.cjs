const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const source = ts.transpileModule(readFileSync(join(__dirname, '../src/util/previewZoom.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
runInNewContext(source, { exports: mod.exports, Math })
const {
  centerPosition, clampPosition, fitScale, oneToOneScale, scaleLimits, visualSize, wheelFactor, zoomAt, zoomKeyAction, ZOOM_STEP
} = mod.exports

// objects from the vm context have another Object.prototype
const plain = v => JSON.parse(JSON.stringify(v))
const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`)

// 3000x2000 image in a 1440x900 area: laid out 1350x900 by max-width/max-height
const wide = { baseW: 1350, baseH: 900, naturalW: 3000, areaW: 1440, areaH: 900, offsetX: 0, offsetY: 0, rotate: 0 }
// 1200x2400 image: laid out 450x900
const tall = { baseW: 450, baseH: 900, naturalW: 1200, areaW: 1440, areaH: 900, offsetX: 0, offsetY: 0, rotate: 0 }
// 400x300 image keeps its size
const small = { baseW: 400, baseH: 300, naturalW: 400, areaW: 1440, areaH: 900, offsetX: 0, offsetY: 0, rotate: 0 }

test('fitScale: big images are ~1, small images grow, rotation swaps the axes', () => {
  close(fitScale(wide), 1)
  close(fitScale(tall), 1)
  close(fitScale(small), 3) // 900 / 300
  // 90 degrees: the 450x900 box is seen as 900x450 -> limited by the width
  close(fitScale({ ...tall, rotate: 90 }), 1440 / 900)
  close(fitScale({ ...tall, rotate: -90 }), 1440 / 900)
  close(fitScale({ ...tall, rotate: 180 }), 1)
  close(fitScale({ ...wide, rotate: 90 }), 900 / 1350)
  // narrow area (lr layout)
  close(fitScale({ ...wide, areaW: 700 }), 700 / 1350)
})

test('oneToOneScale: natural pixels per layout pixel', () => {
  close(oneToOneScale(wide), 3000 / 1350)
  close(oneToOneScale(small), 1)
  // at that scale the image is shown with its natural width
  close(visualSize(wide, oneToOneScale(wide)).w, 3000)
  close(visualSize({ ...wide, rotate: 90 }, oneToOneScale(wide)).h, 3000)
})

test('scaleLimits: min below fit/1:1/1, max 16x actual size or fit', () => {
  let l = scaleLimits(wide)
  close(l.min, 1)
  close(l.max, (3000 / 1350) * 16)
  l = scaleLimits(small)
  close(l.min, 1)
  close(l.max, 16) // fit 3 < 16x actual size
  // image smaller than the area in the fitted layout: fit < 1 (area narrower than the layout box)
  l = scaleLimits({ ...wide, areaW: 675 })
  close(l.min, 0.5)
  // a downscaled image: 1:1 is below 1 (natural smaller than layout is impossible, but a tiny natural width is not)
  l = scaleLimits({ ...small, naturalW: 200 })
  close(l.min, 0.5)
  // fit larger than 16x actual size
  const tiny = { baseW: 10, baseH: 10, naturalW: 10, areaW: 1440, areaH: 900, offsetX: 0, offsetY: 0, rotate: 0 }
  close(scaleLimits(tiny).max, 90)
})

test('clampPosition: centered when smaller than the area, bounded otherwise', () => {
  assert.deepEqual(plain(clampPosition({ x: 50, y: -50 }, 1, small)), { x: 0, y: 0 })
  assert.deepEqual(plain(clampPosition({ x: 50, y: -50 }, 1, wide)), { x: 0, y: 0 }) // 1350 < 1440, 900 = 900
  // scale 2: visual 2700x1800 -> x within +-630, y within +-450
  assert.deepEqual(plain(clampPosition({ x: 1000, y: -1000 }, 2, wide)), { x: 630, y: -450 })
  assert.deepEqual(plain(clampPosition({ x: 100, y: 20 }, 2, wide)), { x: 100, y: 20 })
  // rotated tall image at scale 2: visual 1800x900 -> x within +-180, y fixed
  assert.deepEqual(plain(clampPosition({ x: 500, y: 500 }, 2, { ...tall, rotate: 90 })), { x: 180, y: 0 })
})

test('an image box that sits off-center (baseline alignment) is centered and bounded around its own center', () => {
  const d = { ...wide, offsetY: -4 } // box center is 4 px above the area center
  assert.deepEqual(plain(centerPosition(d)), { x: 0, y: 4 })
  // smaller than the area on x, equal on y: centered means pushed down by 4
  assert.deepEqual(plain(clampPosition({ x: 50, y: 0 }, 1, d)), { x: 0, y: 4 })
  // scale 2: y range is c.y +- 450 -> [-446, 454], so the bottom edge of the image never leaves a gap
  assert.deepEqual(plain(clampPosition({ x: 0, y: -1000 }, 2, d)), { x: 0, y: -446 })
  assert.deepEqual(plain(clampPosition({ x: 0, y: 1000 }, 2, d)), { x: 0, y: 454 })
  // the point under the anchor still stays fixed
  const state = { scale: 2, position: { x: 100, y: 0 } }
  const p = { x: 200, y: 100 }
  const next = zoomAt(state, 1.5, p, d)
  const at = (st) => ({ x: (p.x - d.offsetX - st.position.x) / st.scale, y: (p.y - d.offsetY - st.position.y) / st.scale })
  close(at(next).x, at(state).x)
  close(at(next).y, at(state).y)
})

test('zoomAt: the point under the anchor stays fixed', () => {
  const state = { scale: 2, position: { x: 100, y: -40 } }
  const p = { x: 200, y: 100 }
  const next = zoomAt(state, 1.5, p, wide)
  close(next.scale, 3)
  // image coordinate under p before and after: (p - pos) / scale
  const before = { x: (p.x - state.position.x) / state.scale, y: (p.y - state.position.y) / state.scale }
  const after = { x: (p.x - next.position.x) / next.scale, y: (p.y - next.position.y) / next.scale }
  close(after.x, before.x)
  close(after.y, before.y)
})

test('zoomAt: center anchor keeps the shown spot in the middle', () => {
  const state = { scale: 2, position: { x: 300, y: 100 } }
  const next = zoomAt(state, ZOOM_STEP, { x: 0, y: 0 }, wide)
  close(next.scale, 2 * ZOOM_STEP)
  close(next.position.x, 300 * ZOOM_STEP)
  close(next.position.y, 100 * ZOOM_STEP)
})

test('zoomAt: limits the scale and the position', () => {
  let s = zoomAt({ scale: 1, position: { x: 0, y: 0 } }, 0.5, { x: 100, y: 100 }, wide)
  close(s.scale, 1)
  assert.deepEqual(plain(s.position), { x: 0, y: 0 })
  const max = scaleLimits(wide).max
  s = zoomAt({ scale: max, position: { x: 0, y: 0 } }, 2, { x: 100, y: 100 }, wide)
  close(s.scale, max)
  // zoom out toward the limit pulls the position back inside
  s = zoomAt({ scale: 2, position: { x: 630, y: 450 } }, 0.5, { x: 0, y: 0 }, wide)
  close(s.scale, 1)
  assert.deepEqual(plain(s.position), { x: 0, y: 0 })
  // zooming in far at a point near the corner never leaves a gap
  s = zoomAt({ scale: 1, position: { x: 0, y: 0 } }, 4, { x: 700, y: 440 }, wide)
  const c = clampPosition(s.position, s.scale, wide)
  assert.deepEqual(plain(c), plain(s.position))
})

test('wheelFactor: direction, smoothness and deltaMode', () => {
  assert.ok(wheelFactor({ deltaY: -100, deltaMode: 0 }, 900) > 1)
  assert.ok(wheelFactor({ deltaY: 100, deltaMode: 0 }, 900) < 1)
  close(wheelFactor({ deltaY: 0, deltaMode: 0 }, 900), 1)
  close(wheelFactor({ deltaY: -100, deltaMode: 0 }, 900) * wheelFactor({ deltaY: 100, deltaMode: 0 }, 900), 1)
  // lines count 16 px, pages count the area height
  close(wheelFactor({ deltaY: 3, deltaMode: 1 }, 900), wheelFactor({ deltaY: 48, deltaMode: 0 }, 900))
  close(wheelFactor({ deltaY: 1, deltaMode: 2 }, 900), wheelFactor({ deltaY: 900, deltaMode: 0 }, 900))
  close(wheelFactor({ deltaY: -100, deltaMode: 0 }, 900), Math.exp(0.3))
})

test('wheelFactor: trackpad pinch (ctrl) is more sensitive, one event is capped', () => {
  close(wheelFactor({ deltaY: -5, deltaMode: 0, ctrlKey: true }, 900), Math.exp(0.05))
  assert.ok(wheelFactor({ deltaY: -5, deltaMode: 0, ctrlKey: true }, 900) > wheelFactor({ deltaY: -5, deltaMode: 0 }, 900))
  close(wheelFactor({ deltaY: -100, deltaMode: 0, ctrlKey: true }, 900), Math.exp(0.5))
  close(wheelFactor({ deltaY: 5000, deltaMode: 0 }, 900), Math.exp(-0.5))
})

test('zoomKeyAction: keys, modifiers, editable targets, handled events', () => {
  const k = (key, extra = {}) => zoomKeyAction({ key, target: { tagName: 'DIV' }, ...extra })
  assert.equal(k('+'), 'in')
  assert.equal(k('='), 'in')
  assert.equal(k('-'), 'out')
  assert.equal(k('1'), 'oneToOne')
  assert.equal(k('0'), 'fit')
  assert.equal(k('a'), null)
  assert.equal(k('2'), null)
  assert.equal(k('ArrowLeft'), null)
  // shift is fine (+ is shift+= on US layouts)
  assert.equal(k('+', { shiftKey: true }), 'in')
  // modifiers keep the browser zoom free
  assert.equal(k('+', { ctrlKey: true }), null)
  assert.equal(k('=', { metaKey: true }), null)
  assert.equal(k('-', { ctrlKey: true }), null)
  assert.equal(k('1', { altKey: true }), null)
  // a configured shortcut already handled it
  assert.equal(k('1', { defaultPrevented: true }), null)
  // typing in the pane
  assert.equal(k('1', { target: { tagName: 'INPUT' } }), null)
  assert.equal(k('1', { target: { tagName: 'TEXTAREA' } }), null)
  assert.equal(k('1', { target: { tagName: 'DIV', isContentEditable: true } }), null)
  assert.equal(zoomKeyAction({ key: '0' }), 'fit')
  assert.equal(zoomKeyAction({ key: '0', target: null }), 'fit')
})
