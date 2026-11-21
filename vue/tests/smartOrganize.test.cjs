const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

const transpile = (file) =>
  ts.transpileModule(readFileSync(join(__dirname, file), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
  }).outputText

const pathModule = {}
runInNewContext(transpile('../src/util/path.ts'), {
  exports: pathModule,
  require: () => ({ memoize: (fn) => fn })
})

function setup() {
  const emitted = []
  const modules = {
    'ant-design-vue': { message: {} },
    '@/i18n': { t: (key) => key },
    '@/store/useGlobalStore': { useGlobalStore: () => ({}) },
    '@/api/organize': {},
    '@/util': { globalEvents: { emit: (name, payload) => emitted.push([name, payload]) } },
    '@/store/useTagStore': { useTagStore: () => ({ invalidateTagCache: () => emitted.push(['invalidateTagCache']) }) },
    '@/util/path': pathModule
  }
  const exports = {}
  runInNewContext(transpile('../src/util/smartOrganize.ts'), {
    exports,
    console,
    require: (name) => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected dependency: ${name}`)
      return modules[name]
    }
  })
  return { exports, emitted }
}

// arrays made inside the vm have another prototype: compare as JSON
const json = (value) => JSON.parse(JSON.stringify(value))

test('finishOrganize drops the tag cache, then reloads the sources and the destination', () => {
  const c = setup()
  c.exports.finishOrganize(['/images/src'], '/images/dest')
  assert.deepEqual(json(c.emitted), [
    ['invalidateTagCache'],
    ['refreshFileView', { paths: ['/images/src', '/images/dest'] }]
  ])
})

test('finishOrganize: without a destination the first source folder is the destination', () => {
  const c = setup()
  c.exports.finishOrganize(['/images/src'])
  assert.deepEqual(json(c.emitted[1][1]), { paths: ['/images/src'] })
})

test('finishOrganize: a destination in the path form of a Windows server matches the views', () => {
  const c = setup()
  c.exports.finishOrganize(['C:/images/in'], 'C:\\images\\out')
  assert.deepEqual(json(c.emitted[1][1]), { paths: ['C:/images/in', 'C:/images/out'] })
})

test('finishOrganize: without folders the cache is dropped and every view reloads', () => {
  const c = setup()
  c.exports.finishOrganize([])
  assert.deepEqual(json(c.emitted), [['invalidateTagCache'], ['refreshFileView', { paths: [] }]])
})
