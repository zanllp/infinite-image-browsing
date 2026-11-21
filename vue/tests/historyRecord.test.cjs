const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const ts = require('typescript')

// Run the real class without the app: transpile it to CJS and stub its only runtime import.
const source = ts.transpileModule(
  readFileSync(join(__dirname, '../src/util/HistoryRecord.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }
).outputText
const mod = { exports: {} }
let n = 0
const modules = { 'lodash-es': { uniqueId: () => String(++n) } }
new Function('require', 'module', 'exports', source)(
  (name) => {
    assert.ok(name in modules, `unexpected import ${name}`)
    return modules[name]
  },
  mod,
  mod.exports
)
const { HistoryRecord } = mod.exports

const setup = () => {
  const history = new HistoryRecord()
  history.add({ q: 'a' })
  history.add({ q: 'b' })
  history.add({ q: 'c' })
  const [c, b, a] = history.getRecords()
  history.pin(b)
  return { history, a, b, c }
}

test('remove drops an unpinned record and keeps the others', () => {
  const { history, a, b, c } = setup()
  history.remove(c)
  assert.deepEqual(history.getRecords().map((r) => r.q), ['b', 'a'])
  assert.ok(history.isPinned(b))
  assert.ok(!history.isPinned(a))
})

test('remove drops a pinned record', () => {
  const { history, b } = setup()
  history.remove(b)
  assert.deepEqual(history.getRecords().map((r) => r.q), ['c', 'a'])
  assert.equal(history.getPinnedValues().length, 0)
})

test('remove of an unknown record changes nothing', () => {
  const { history } = setup()
  history.remove({ id: 'missing', time: '' })
  assert.deepEqual(history.getRecords().map((r) => r.q), ['b', 'c', 'a'])
})
