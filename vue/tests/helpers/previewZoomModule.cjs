const { readFileSync } = require('node:fs')
const { join } = require('node:path')
const { runInNewContext } = require('node:vm')
const ts = require('typescript')

// previewZoom.ts evaluated as CommonJS
const source = ts.transpileModule(readFileSync(join(__dirname, '../../src/util/previewZoom.ts'), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }
}).outputText
const mod = { exports: {} }
runInNewContext(source, { exports: mod.exports, Math })
module.exports = mod.exports
