const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { test } = require('node:test')
const { runInNewContext } = require('node:vm')
const { parse, compileScript, compileStyle } = require('@vue/compiler-sfc')
const { renderToString } = require('@vue/server-renderer')
const ts = require('typescript')
const vue = require('vue')
const AImage = require('ant-design-vue/lib/image').default

const filename = join(__dirname, '../src/page/fileTransfer/FullscreenPreview.vue')
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const scopeId = 'data-v-preview-test'
const script = compileScript(descriptor, { id: scopeId, inlineTemplate: true })
const source = ts.transpileModule(script.content, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const modules = {
  vue,
  '@/util/file': {
    toImageUrl: file => `/original/${file.name}`,
    toImageThumbnailUrl: file => `/thumbnail/${file.name}`
  },
  '@/util/imagePreviewOperation': { registerFullscreenPreviewCloser: () => () => {} }
}
const exportsObject = {}
runInNewContext(source, {
  exports: exportsObject,
  require: name => {
    assert.ok(Object.hasOwn(modules, name), `Unexpected dependency: ${name}`)
    return modules[name]
  }
})
const FullscreenPreview = exportsObject.default
FullscreenPreview.__scopeId = scopeId

async function render(previewIdx) {
  const app = vue.createSSRApp(FullscreenPreview, {
    files: [{ name: 'test.png', fullpath: '/images/test.png' }],
    previewIdx,
    previewing: false
  })
  app.component('a-image', AImage)
  return renderToString(app)
}

test('preview host hides a native wrapper, not the Ant Design inner image', async () => {
  const html = await render(0)
  assert.match(html, new RegExp(`^<div class="iib-fullscreen-preview-host" ${scopeId}>`))
  assert.match(html, /<img class="ant-image-img"[^>]*src="\/thumbnail\/test.png"/)
  const { code, errors } = compileStyle({
    source: descriptor.styles[0].content,
    filename,
    id: scopeId,
    scoped: true
  })
  assert.deepEqual(errors, [])
  assert.match(code, new RegExp(`\\.iib-fullscreen-preview-host\\[${scopeId}\\]\\s*\\{\\s*display:\\s*none\\s*;?\\s*\\}`))
})

test('no preview host is rendered before an image is selected', async () => {
  assert.doesNotMatch(await render(-1), /ant-image|iib-fullscreen-preview-host/)
})
