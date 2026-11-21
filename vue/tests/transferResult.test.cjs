const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')
const vue = require('vue')

// Run the real transferResult.tsx with a mocked module graph. If it gains or changes an import,
// the `modules` table in setup() must be updated too (the require() assertion fails loudly otherwise).
const source = ts.transpileModule(
  readFileSync(join(__dirname, '../src/page/fileTransfer/hooks/transferResult.tsx'), 'utf8'),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.React,
      jsxFactory: 'h'
    }
  }
).outputText

// The real path helpers (normalize, join, basename); only lodash's memoize is replaced.
const pathSource = ts.transpileModule(
  readFileSync(join(__dirname, '../src/util/path.ts'), 'utf8'),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }
).outputText
const pathModule = {}
runInNewContext(pathSource, {
  exports: pathModule,
  require: (name) => {
    assert.equal(name, 'lodash-es')
    return { memoize: (fn) => fn }
  }
})

function setup({ moveFiles, checkPathExists, cache = {} } = {}) {
  const tagMap = vue.reactive(new Map(Object.entries(cache)))
  const events = []
  const failures = []
  const modules = {
    'ant-design-vue': {
      message: { success: () => {} },
      Modal: { warning: (options) => failures.push(options) }
    },
    '@/api': { checkPathExists: checkPathExists ?? (async () => ({})) },
    '@/api/files': { moveFiles: moveFiles ?? (async () => ({})), copyFiles: async () => ({}) },
    '@/i18n': { t: (key) => key },
    '@/util/path': pathModule,
    '.': {
      events: { emit: (name, payload) => events.push([name, payload, [...tagMap.keys()]]) },
      tagStore: { tagMap }
    }
  }
  const exports = {}
  runInNewContext(source, {
    exports,
    h: () => ({}),
    console: { error() {} },
    require: (name) => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected dependency: ${name}`)
      return modules[name]
    }
  })
  return { exports, tagMap, events, failures }
}

const tag = (name) => [{ id: name, name }]

test('move: forgets the tags at the old and the new path, keeps the others', async () => {
  const c = setup({
    cache: {
      '/images/a/x.png': tag('old'),
      '/images/a/b/x.png': tag('stale'),
      '/images/a/other.png': tag('keep'),
      '/images/a/b/other.png': tag('keep')
    }
  })
  await c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/x.png'], from: '/images/a', dest: '/images/a/b' })
  assert.deepEqual([...c.tagMap.keys()].sort(), ['/images/a/b/other.png', '/images/a/other.png'])
})

test('move: the cache is clean before the destination view is reloaded', async () => {
  const c = setup({ cache: { '/images/a/b/x.png': tag('stale') } })
  await c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/x.png'], from: '/images/a', dest: '/images/a/b' })
  const reload = c.events.find(([name]) => name === 'refreshLocation')
  assert.deepEqual(reload[2], [])
})

test('move: a folder takes the entries of the files in it along, not those of a sibling with the same prefix', async () => {
  const c = setup({
    cache: {
      '/images/a/b/x.png': tag('1'),
      '/images/c/b/y.png': tag('2'),
      '/images/c/b/deep/z.png': tag('3'),
      '/images/a/bb/x.png': tag('keep')
    }
  })
  await c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/b'], from: '/images/a', dest: '/images/c' })
  assert.deepEqual([...c.tagMap.keys()], ['/images/a/bb/x.png'])
})

test('move: windows separators in the cache match', async () => {
  const c = setup({ cache: { 'C:\\images\\a\\x.png': tag('old') } })
  await c.exports.moveFilesAndUpdateViews({ paths: ['C:/images/a/x.png'], from: 'C:/images/a', dest: 'C:/images/b' })
  assert.equal(c.tagMap.size, 0)
})

test('partial failure: only the files that are gone are forgotten', async () => {
  const c = setup({
    moveFiles: async () => ({ errors: ['conflict'] }),
    checkPathExists: async () => ({ '/images/a/moved.png': false, '/images/a/stays.png': true }),
    cache: {
      '/images/a/moved.png': tag('1'),
      '/images/a/stays.png': tag('2'),
      '/images/b/moved.png': tag('stale'),
      '/images/b/stays.png': tag('3')
    }
  })
  await c.exports.moveFilesAndUpdateViews({
    paths: ['/images/a/moved.png', '/images/a/stays.png'],
    from: '/images/a',
    dest: '/images/b'
  })
  assert.deepEqual([...c.tagMap.keys()].sort(), ['/images/a/stays.png', '/images/b/stays.png'])
  assert.equal(c.failures.length, 1)
})

test('partial failure: if the check fails, all files are forgotten and the source is reloaded', async () => {
  const c = setup({
    moveFiles: async () => ({ errors: ['conflict'] }),
    checkPathExists: async () => { throw new Error('offline') },
    cache: { '/images/a/x.png': tag('1'), '/images/b/y.png': tag('2'), '/images/a/z.png': tag('keep') }
  })
  await c.exports.moveFilesAndUpdateViews({
    paths: ['/images/a/x.png', '/images/a/y.png'],
    from: '/images/a',
    dest: '/images/b'
  })
  assert.deepEqual([...c.tagMap.keys()], ['/images/a/z.png'])
  assert.deepEqual(c.events.filter(([name]) => name === 'refreshLocation').map(([, p]) => p.loc), ['/images/a', '/images/b'])
})

test('request fails: files moved before the error are forgotten, the error is rethrown', async () => {
  const c = setup({
    moveFiles: async () => { throw new Error('boom') },
    checkPathExists: async () => ({ '/images/a/x.png': false, '/images/a/y.png': true }),
    cache: { '/images/a/x.png': tag('1'), '/images/a/y.png': tag('2') }
  })
  await assert.rejects(
    c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/x.png', '/images/a/y.png'], from: '/images/a', dest: '/images/b' }),
    /boom/
  )
  assert.deepEqual([...c.tagMap.keys()], ['/images/a/y.png'])
})

test('copy: does not touch the cache', async () => {
  const c = setup({ cache: { '/images/b/x.png': tag('1') } })
  await c.exports.copyFilesAndUpdateViews({ paths: ['/images/a/x.png'], from: '/images/a', dest: '/images/b' })
  assert.deepEqual([...c.tagMap.keys()], ['/images/b/x.png'])
})

test('move: destination with a trailing slash, the root and a windows drive', async () => {
  for (const [dest, stale] of [
    ['/images/b/', '/images/b/x.png'],
    ['/', '/x.png'],
    ['C:\\', 'C:/x.png'],
    ['C:\\images\\b', 'C:/images/b/x.png']
  ]) {
    const c = setup({ cache: { [stale]: tag('stale'), '/keep/y.png': tag('keep') } })
    await c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/x.png'], from: '/images/a', dest })
    assert.deepEqual([...c.tagMap.keys()], ['/keep/y.png'], `dest ${dest}`)
  }
})

test('partial failure: the cache is clean before the destination view is reloaded', async () => {
  const c = setup({
    moveFiles: async () => ({ errors: ['conflict'] }),
    checkPathExists: async () => ({ '/images/a/x.png': false }),
    cache: { '/images/b/x.png': tag('stale') }
  })
  await c.exports.moveFilesAndUpdateViews({ paths: ['/images/a/x.png'], from: '/images/a', dest: '/images/b' })
  const reload = c.events.filter(([name, payload]) => name === 'refreshLocation' && payload.loc === '/images/b')
  assert.equal(reload.length, 1)
  assert.deepEqual(reload[0][2], [])
})

test('move: a cache of many entries and many moved files is handled quickly', async () => {
  const cache = {}
  for (let i = 0; i < 30000; i++) cache[`/images/a/f${i}.png`] = tag('x')
  const paths = Array.from({ length: 1000 }, (_, i) => `/images/a/f${i}.png`)
  const c = setup({ cache })
  const start = Date.now()
  await c.exports.moveFilesAndUpdateViews({ paths, from: '/images/a', dest: '/images/b' })
  assert.equal(c.tagMap.size, 29000)
  assert.ok(Date.now() - start < 500, `took ${Date.now() - start} ms`)
})
