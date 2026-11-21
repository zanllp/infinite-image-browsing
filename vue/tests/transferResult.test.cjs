const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

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

function setup({ moveFiles, checkPathExists } = {}) {
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
    '.': {
      events: { emit: (name, payload) => events.push([name, payload]) },
      tagStore: { invalidateTagCache: () => events.push(['invalidateTagCache']) }
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
  return { exports, events, failures }
}

const args = { paths: ['/src/a.png', '/src/b.png'], from: '/src', dest: '/dest' }

test('move: invalidate tags once before reloading the destination', async () => {
  const c = setup()
  await c.exports.moveFilesAndUpdateViews(args)
  assert.deepEqual(c.events.map(([name]) => name), ['removeFiles', 'invalidateTagCache', 'refreshLocation'])
})

for (const moved of [true, false]) {
  test('partial failure: invalidate tags only if a file moved (' + moved + ')', async () => {
    const c = setup({
      moveFiles: async () => ({ errors: ['conflict'] }),
      checkPathExists: async () => ({ '/src/a.png': !moved, '/src/b.png': true })
    })
    await c.exports.moveFilesAndUpdateViews(args)
    assert.deepEqual(Array.from(c.events[0][1].paths), moved ? ['/src/a.png'] : [])
    assert.equal(c.events.filter(([name]) => name === 'invalidateTagCache').length, moved ? 1 : 0)
    assert.equal(c.failures.length, 1)
  })
}

test('unknown move result: invalidate tags, refresh both folders and propagate the error', async () => {
  const c = setup({
    moveFiles: async () => { throw new Error('move failed') },
    checkPathExists: async () => { throw new Error('offline') }
  })
  await assert.rejects(c.exports.moveFilesAndUpdateViews(args), /move failed/)
  assert.deepEqual(c.events.map(([name, payload]) => [name, payload?.loc]), [
    ['refreshLocation', '/src'], ['invalidateTagCache', undefined], ['refreshLocation', '/dest']
  ])
})

test('copy: reload the destination without invalidating tags', async () => {
  const c = setup()
  await c.exports.copyFilesAndUpdateViews(args)
  assert.deepEqual(c.events.map(([name]) => name), ['refreshLocation'])
})
