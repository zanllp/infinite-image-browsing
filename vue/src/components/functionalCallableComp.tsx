import { Button, Input, Modal, message, Spin } from 'ant-design-vue'
import { StyleValue, ref } from 'vue'
import * as Path from '@/util/path'
import { FileNodeInfo, mkdirs } from '@/api/files'
import { setTargetFrameAsCover, getImageGenerationInfo, openWithDefaultApp } from '@/api'
import { parse } from '@/util/stable-diffusion-image-metadata'
import { t } from '@/i18n'
import { downloadFiles, formatDuration, globalEvents, toRawFileUrl, toStreamVideoUrl, toStreamAudioUrl } from '@/util'
import { getCachedVideoDuration } from '@/util/videoDuration'
import { DownloadOutlined, FileTextOutlined, EditOutlined } from '@/icon'
import { isStandalone } from '@/util/env'
import { addCustomTag, getDbBasicInfo, rebuildImageIndex, renameFile } from '@/api/db'
import { useTagStore } from '@/store/useTagStore'
import { useGlobalStore } from '@/store/useGlobalStore'
import { base64ToFile, video2base64 } from '@/util/video'
import { closeImageFullscreenPreview } from '@/util/imagePreviewOperation'

export const openCreateFlodersModal = (base: string) => {
  const floderName = ref('')
  return new Promise<void>((resolve) => {
    Modal.confirm({
      title: t('inputFolderName'),
      content: () => <Input v-model:value={floderName.value} />,
      async onOk() {
        if (!floderName.value) {
          return
        }
        const dest = Path.join(base, floderName.value)
        await mkdirs(dest)
        resolve()
      }
    })
  })
}

export const MultiSelectTips = () => (
  <p
    style={{
      background: 'var(--zp-secondary-background)',
      padding: '8px',
      borderLeft: '4px solid var(--primary-color)'
    }}
  >
    Tips: {t('multiSelectTips')}
  </p>
)

// 合并的视频/音频 modal 实现
const openMediaModalImpl = (
  file: FileNodeInfo,
  onTagClick?: (id: string| number) => void,
  onTiktokView?: () => void,
  mediaType: 'video' | 'audio' = 'video'
) => {
  const tagStore = useTagStore()
  const global = useGlobalStore()
  const isSelected = (id: string | number) => {
    return !!tagStore.tagMap.get(file.fullpath)?.some(v => v.id === id)
  }
  const videoRef = ref<HTMLVideoElement | null>(null)
  // 网格里通常已经探测过，直接复用；否则等 loadedmetadata
  const videoDuration = ref(mediaType === 'video' ? getCachedVideoDuration(file) ?? 0 : 0)
  const imageGenInfo = ref('')
  const promptLoading = ref(false)

  // 加载提示词
  const loadPrompt = async () => {
    promptLoading.value = true
    try {
      const info = await getImageGenerationInfo(file.fullpath)
      imageGenInfo.value = info
    } catch (error) {
      console.error('Load prompt error:', error)
      imageGenInfo.value = ''
    } finally {
      promptLoading.value = false
    }
  }

  const tagBaseStyle: StyleValue = {
    margin: '2px',
    padding: '2px 16px',
    'border-radius': '4px',
    display: 'inline-block',
    cursor: 'pointer',
    'font-weight': 'bold',
    transition: '.5s all ease',
    'user-select': 'none',
  }

  // 解析提示词结构
  const geninfoStruct = () => parse(imageGenInfo.value)

  // 计算文本长度（中文算3个字符）
  const getTextLength = (text: string): number => {
    let length = 0
    for (const char of text) {
      if (/[\u4e00-\u9fa5]/.test(char)) {
        length += 3
      } else {
        length += 1
      }
    }
    return length
  }

  // 判断是否为 tag 风格的提示词
  const isTagStylePrompt = (tags: string[]): boolean => {
    if (tags.length === 0) return false

    let totalLength = 0
    for (const tag of tags) {
      const tagLength = getTextLength(tag)
      totalLength += tagLength

      if (tagLength > 50) {
        return false
      }
    }

    const avgLength = totalLength / tags.length
    if (avgLength > 30) {
      return false
    }

    return true
  }

  // 提示词包装函数（支持 tag 风格和自然语言风格）
  const spanWrap = (text: string) => {
    if (!text) return ''

    const specBreakTag = 'BREAK'
    const values = text.replace(/&gt;\s/g, '> ,').replace(/\sBREAK\s/g, ',' + specBreakTag + ',')
      .split(/[\n,]+/)
      .map(v => v.trim())
      .filter(v => v)

    // 判断是否为 tag 风格
    if (!isTagStylePrompt(values)) {
      // 自然语言风格
      return text
        .split('\n')
        .map(line => line.trim())
        .filter(line => line)
        .map(line => `<p style="margin:0; padding:4px 0;">${line}</p>`)
        .join('')
    }

    // Tag 风格
    const frags: string[] = []
    let parenthesisActive = false
    for (let i = 0; i < values.length; i++) {
      if (values[i] === specBreakTag) {
        frags.push('<br><span style="color:var(--zp-secondary); font-weight:bold;">BREAK</span><br>')
        continue
      }
      const trimmedValue = values[i]
      if (!parenthesisActive) parenthesisActive = trimmedValue.includes('(')
      const styles = ['background: var(--zp-secondary-variant-background)', 'color: var(--zp-primary)', 'padding: 2px 6px', 'border-radius: 4px', 'margin-right: 6px', 'margin-top: 4px', 'display: inline-block']
      if (parenthesisActive) styles.push('border: 1px solid var(--zp-secondary)')
      if (getTextLength(trimmedValue) < 32) styles.push('font-size: 0.9em')
      frags.push(`<span style="${styles.join('; ')}">${trimmedValue}</span>`)
      if (parenthesisActive) parenthesisActive = !trimmedValue.includes(')')
    }
    return frags.join(' ')
  }

  // 加载提示词
  loadPrompt()

  const onTiktokViewWrapper = () => {
    onTiktokView?.()
    closeImageFullscreenPreview()
    modal.destroy()
  }

  const modal = Modal.confirm({
    width: mediaType === 'video' ? '80vw' : '70vw',
    title: file.name,
    icon: null,
    content: () => (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexDirection: 'column'
        }}
      >
        {mediaType === 'video' ? (
          <video ref={videoRef} style={{ maxHeight: isStandalone ? '80vh' : '60vh', maxWidth: '100%', minWidth: '70%' }} src={toStreamVideoUrl(file)} controls autoplay
            onLoadedmetadata={() => {
              const duration = videoRef.value?.duration
              if (typeof duration === 'number' && Number.isFinite(duration) && duration > 0) {
                videoDuration.value = duration
              }
            }}></video>
        ) : (
          <>
            <div style={{ fontSize: '80px', marginBottom: '16px' }}>🎵</div>
            <audio style={{ width: '100%', maxWidth: '500px' }} src={toStreamAudioUrl(file)} controls autoplay></audio>
          </>
        )}

        {mediaType === 'video' && videoDuration.value > 0 && (
          <div style={{ marginTop: '8px', fontSize: '13px', color: 'var(--zp-secondary)' }}>
            {formatDuration(videoDuration.value)}
          </div>
        )}

        {/* 标签选择区域 */}
        <div style={{ marginTop: '16px' }}>
          <div onClick={openAddNewTagModal}  style={{
            background: 'var(--zp-primary-background)',
            color: 'var(--zp-luminous)',
            border: '2px solid var(--zp-luminous)',
            ...tagBaseStyle
          }}>
            { t('addNewCustomTag') }
          </div>
          {global.conf!.all_custom_tags.map((tag) =>
            <div key={tag.id} onClick={() => onTagClick?.(tag.id)}  style={{
              background: isSelected(tag.id) ? tagStore.getColor(tag) : 'var(--zp-primary-background)',
              color: !isSelected(tag.id) ? tagStore.getColor(tag) : 'white',
              border: `2px solid ${tagStore.getColor(tag)}`,
              ...tagBaseStyle
            }}>
              { tag.name }
            </div>)}
        </div>

        {/* 操作按钮 */}
        <div class="actions" style={{ marginTop: '16px' }}>
          <Button onClick={() => downloadFiles([toRawFileUrl(file, true)])}>
            {{
              icon: <DownloadOutlined/>,
              default: t('download')
            }}
          </Button>
          {onTiktokView && (
            <Button onClick={onTiktokViewWrapper} type="primary">
              {{
                default: t('tiktokView')
              }}
            </Button>
          )}
          {mediaType === 'video' && (
            <Button onClick={async () => {
              if (!videoRef.value) return
              const video = videoRef.value
              video.pause()
              const base64 = video2base64(video)
              await setTargetFrameAsCover({ path: file.fullpath, base64_img: base64, updated_time: file.date })
              file.cover_url = URL.createObjectURL(await base64ToFile(base64, 'cover'))
              message.success(t('success') + '!  ' + t('clearCacheIfNotTakeEffect'))
            }}>
              {{ default: t('setCurrFrameAsVideoPoster') }}
            </Button>
          )}
          <Button onClick={async () => {
            await openEditPromptModal(file)
            await loadPrompt()
          }} icon={<EditOutlined />}>
            {{ default: t('editPrompt') }}
          </Button>
        </div>

        {/* 提示词显示区域 */}
        {promptLoading.value ? (
          <div style={{ marginTop: '24px', width: '100%', textAlign: 'center' }}>
            <Spin />
          </div>
        ) : imageGenInfo.value ? (
          <div style={{ marginTop: '24px', width: '100%', maxWidth: mediaType === 'video' ? '1000px' : '900px', alignSelf: 'flex-start' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px', color: 'var(--zp-primary)', fontSize: '14px', fontWeight: 500 }}>
              <FileTextOutlined />
              <span>Prompt</span>
            </div>
            {geninfoStruct().prompt && (
              <div style={{ marginBottom: '12px' }}>
                <div style={{ fontSize: '12px', color: 'var(--zp-primary)', marginBottom: '6px' }}>Positive</div>
                <code style={{ fontSize: '13px', display: 'block', padding: '10px 12px', background: 'var(--zp-primary-background)', borderRadius: '8px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: '1.6em' }} innerHTML={spanWrap(geninfoStruct().prompt ?? '')}></code>
              </div>
            )}
            {geninfoStruct().negativePrompt && (
              <div style={{ marginBottom: '12px' }}>
                <div style={{ fontSize: '12px', color: 'var(--zp-primary)', marginBottom: '6px' }}>Negative</div>
                <code style={{ fontSize: '13px', display: 'block', padding: '10px 12px', background: 'var(--zp-primary-background)', borderRadius: '8px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: '1.6em' }} innerHTML={spanWrap(geninfoStruct().negativePrompt ?? '')}></code>
              </div>
            )}
            {/* Meta 信息 */}
            {Object.entries(geninfoStruct()).filter(([key]) => key !== 'prompt' && key !== 'negativePrompt').length > 0 && (
              <div>
                <div style={{ fontSize: '12px', color: 'var(--zp-primary)', marginBottom: '6px' }}>Meta</div>
                <code style={{ fontSize: '12px', display: 'block', padding: '8px 12px', background: 'var(--zp-secondary-background)', borderRadius: '6px', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: '1.5em', color: 'var(--zp-primary)', opacity: 0.7 }}>
                  {Object.entries(geninfoStruct())
                    .filter(([key]) => key !== 'prompt' && key !== 'negativePrompt')
                    .map(([key, value]) => `${key.charAt(0).toUpperCase() + key.slice(1)}: ${value}`)
                    .join('\n')}
                </code>
              </div>
            )}
          </div>
        ) : null}
      </div>
    ),
    maskClosable: true,
    wrapClassName: 'hidden-antd-btns-modal'
  })
}

export const openVideoModal = (
  file: FileNodeInfo,
  onTagClick?: (id: string| number) => void,
  onTiktokView?: () => void
) => openMediaModalImpl(file, onTagClick, onTiktokView, 'video')

export const openAudioModal = (
  file: FileNodeInfo,
  onTagClick?: (id: string| number) => void,
  onTiktokView?: () => void
) => openMediaModalImpl(file, onTagClick, onTiktokView, 'audio')

// ===== 轻量文件预览（json/txt/md 等文本、svg 等图片、pdf），统一走 modal，不引入额外依赖 =====

const FILE_PREVIEW_MAX_BYTES = 512 * 1024

const filePreviewTextExts = new Set([
  'txt', 'text', 'log', 'md', 'markdown', 'rst',
  'json', 'jsonc', 'json5', 'geojson', 'ndjson',
  'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'properties', 'env',
  'csv', 'tsv', 'xml', 'html', 'htm', 'xhtml',
  'css', 'scss', 'sass', 'less',
  'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'vue', 'svelte',
  'py', 'rb', 'php', 'go', 'rs', 'java', 'kt', 'kts', 'swift',
  'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'sql', 'r', 'lua', 'pl',
  'sh', 'bash', 'zsh', 'fish', 'bat', 'cmd', 'ps1', 'psm1',
  'dart', 'scala', 'clj', 'ex', 'exs', 'erl', 'hs', 'jl', 'nim', 'zig',
  'patch', 'diff', 'srt', 'vtt', 'ass', 'tex', 'bib',
  'gitignore', 'gitattributes', 'dockerignore', 'editorconfig'
])

const filePreviewImageExts = new Set(['svg', 'ico', 'cur'])

const getFileExt = (name: string) => {
  const idx = name.lastIndexOf('.')
  return idx < 0 ? '' : name.slice(idx + 1).toLowerCase()
}

export type FilePreviewKind = 'text' | 'image' | 'pdf' | 'other'

/** 大致判断文件能用哪种方式预览，供 UI 显示对应提示 */
export const getFilePreviewKind = (name: string): FilePreviewKind => {
  const ext = getFileExt(name)
  if (ext === 'pdf') {
    return 'pdf'
  }
  if (filePreviewImageExts.has(ext)) {
    return 'image'
  }
  return filePreviewTextExts.has(ext) ? 'text' : 'other'
}

// 二进制文件通常包含 \0，用它先挡掉大部分非文本；再按 utf-8 -> gbk 的顺序解码
const decodeFileText = (bytes: Uint8Array, forceDecode: boolean): string | null => {
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
    return new TextDecoder('utf-16le').decode(bytes)
  }
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
    return new TextDecoder('utf-16be').decode(bytes)
  }
  if (bytes.indexOf(0) !== -1) {
    return null
  }
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch (e) {
    // 不是 utf-8，继续尝试 gbk
  }
  try {
    const gbk = new TextDecoder('gbk').decode(bytes)
    if (!gbk.includes('\uFFFD')) {
      return gbk
    }
  } catch (e) {
    // 当前环境不支持 gbk
  }
  if (!forceDecode) {
    return null
  }
  try {
    return new TextDecoder('windows-1252').decode(bytes)
  } catch (e) {
    return null
  }
}

// 只取前 N 字节，避免把超大文件整个读进内存
const readFilePrefix = async (resp: Response, max: number) => {
  const reader = resp.body?.getReader()
  if (!reader) {
    const buf = new Uint8Array(await resp.arrayBuffer())
    return { bytes: buf.slice(0, max), truncated: buf.byteLength > max }
  }
  const chunks: Uint8Array[] = []
  let size = 0
  let truncated = false
  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      break
    }
    if (!value?.byteLength) {
      continue
    }
    chunks.push(value)
    size += value.byteLength
    if (size >= max) {
      truncated = true
      await reader.cancel()
      break
    }
  }
  const bytes = new Uint8Array(Math.min(size, max))
  let offset = 0
  for (const chunk of chunks) {
    if (offset >= bytes.length) {
      break
    }
    const part = chunk.subarray(0, bytes.length - offset)
    bytes.set(part, offset)
    offset += part.byteLength
  }
  return { bytes, truncated }
}

export const openFilePreviewModal = (file: FileNodeInfo) => {
  const global = useGlobalStore()
  const ext = getFileExt(file.name)
  const fileUrl = toRawFileUrl(file)
  const previewKind = getFilePreviewKind(file.name)
  // other 也要 fetch 一次按 content-type 再判断
  const initialKind: 'text' | 'image' | 'pdf' = previewKind === 'other' ? 'text' : previewKind
  const kind = ref<'text' | 'image' | 'pdf' | 'unsupported'>(initialKind)
  const loading = ref(initialKind === 'text')
  const content = ref('')
  const truncated = ref(false)
  const error = ref('')

  const load = async () => {
    try {
      const resp = await fetch(fileUrl)
      if (!resp.ok) {
        throw new Error(`${resp.status} ${resp.statusText}`)
      }
      const mime = (resp.headers.get('content-type') ?? '').split(';')[0].trim().toLowerCase()
      if (mime.startsWith('image/')) {
        kind.value = 'image'
        return
      }
      if (mime.includes('pdf')) {
        kind.value = 'pdf'
        return
      }
      const { bytes, truncated: isTruncated } = await readFilePrefix(resp, FILE_PREVIEW_MAX_BYTES)
      const knownText = filePreviewTextExts.has(ext) || mime.startsWith('text/') ||
        ['application/json', 'application/xml', 'application/javascript', 'application/x-yaml', 'application/x-sh'].includes(mime)
      const text = decodeFileText(bytes, knownText)
      if (text == null) {
        kind.value = 'unsupported'
        return
      }
      let out = text
      if (['json', 'jsonc', 'geojson'].includes(ext) || mime === 'application/json') {
        try {
          out = JSON.stringify(JSON.parse(out), null, 2)
        } catch (e) {
          // 不是合法 json 或者内容被截断，按原文显示
        }
      }
      content.value = out
      truncated.value = isTruncated
    } catch (e: any) {
      error.value = e?.message ?? String(e)
    } finally {
      loading.value = false
    }
  }

  if (kind.value === 'text') {
    load()
  }

  const renderBody = () => {
    if (loading.value) {
      return <div style={{ textAlign: 'center', padding: '32px' }}><Spin /></div>
    }
    if (error.value) {
      return <div style={{ color: '#ff4d4f' }}>{t('filePreviewLoadFailed')}: {error.value}</div>
    }
    if (kind.value === 'unsupported') {
      return <div style={{ padding: '8px 0', color: 'var(--zp-secondary)' }}>{t('filePreviewUnsupported')}</div>
    }
    if (kind.value === 'image') {
      return (
        <div style={{ textAlign: 'center' }}>
          <img src={fileUrl} style={{ maxWidth: '100%', maxHeight: '70vh' }} />
        </div>
      )
    }
    if (kind.value === 'pdf') {
      return <iframe src={fileUrl} style={{ width: '100%', height: '70vh', border: 'none' }}></iframe>
    }
    return (
      <div>
        {truncated.value && (
          <div style={{ color: 'var(--zp-secondary)', marginBottom: '6px' }}>
            {t('filePreviewTruncated', { size: `${Math.round(FILE_PREVIEW_MAX_BYTES / 1024)}KB` })}
          </div>
        )}
        {content.value
          ? (
            <pre style={{
              margin: 0,
              maxHeight: '70vh',
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              background: 'var(--zp-secondary-background)',
              color: 'var(--zp-primary)',
              padding: '12px',
              borderRadius: '8px',
              fontSize: '13px',
              lineHeight: '1.6em'
            }}>{content.value}</pre>
          )
          : <div style={{ color: 'var(--zp-secondary)' }}>{t('filePreviewEmpty')}</div>}
      </div>
    )
  }

  Modal.confirm({
    width: '80vw',
    title: file.name,
    icon: null,
    content: () => (
      <div>
        <div style={{ color: 'var(--zp-secondary)', fontSize: '0.85em', marginBottom: '8px', wordBreak: 'break-all' }}>
          {file.fullpath}
        </div>
        {renderBody()}
        <div style={{ display: 'flex', gap: '8px', marginTop: '12px', justifyContent: 'flex-end' }}>
          <Button size="small" onClick={() => downloadFiles([toRawFileUrl(file, true)])}>{t('download')}</Button>
          {global.conf && !global.conf.is_readonly && (
            <Button size="small" onClick={() => openWithDefaultApp(file.fullpath)}>{t('openWithDefaultApp')}</Button>
          )}
        </div>
      </div>
    ),
    maskClosable: true,
    wrapClassName: 'hidden-antd-btns-modal'
  })
}

export const openRebuildImageIndexModal = () => {
  Modal.confirm({
    title: t('confirmRebuildImageIndex'),
    onOk: async () => {
      await rebuildImageIndex()
      globalEvents.emit('searchIndexExpired')
      message.success(t('rebuildComplete'))
    }
  })
}


export const openRenameFileModal = (path: string) => {
  const name = ref(path.split(/[\\/]/).pop() ?? '')
  return new Promise<string>((resolve) => {
    Modal.confirm({
      title: t('rename'),
      content: () => <Input v-model:value={name.value} />,
      async onOk() {
        if (!name.value) {
          return
        }
        const resp = await renameFile({ path, name: name.value })
        resolve(resp.new_path)
      }
    })
  })
}


export const openAddNewTagModal = () => {
  const name = ref('')
  const global = useGlobalStore()
  return new Promise<string>((resolve) => {
    Modal.confirm({
      title: t('addNewCustomTag'),
      content: () => <Input v-model:value={name.value} />,
      async onOk() {
        if (!name.value) {
          return
        }
        const info = await getDbBasicInfo()
        const tag = await addCustomTag({ tag_name: name.value })
        if (tag.type !== 'custom') {
          message.error(t('existInOtherType'))
          throw new Error(t('existInOtherType'))
        }
        if (info.tags.find((v) => v.id === tag.id)) {
          message.error(t('alreadyExists'))
          throw new Error(t('alreadyExists'))
        } else {
          global.conf?.all_custom_tags.push(tag)
          message.success(t('success'))
        }
        resolve(name.value)
      }
    })
  })
}

export const openEditPromptModal = async (file: FileNodeInfo) => {
  globalEvents.off('promptEditorUpdated') // 确保事件监听器不会重复绑定
  return new Promise<void>((resolve) => {
    const handler = () => {
      globalEvents.off('promptEditorUpdated', handler)
      resolve()
    }

    globalEvents.on('promptEditorUpdated', handler)
    globalEvents.emit('openPromptEditor', { file })
  })
}

