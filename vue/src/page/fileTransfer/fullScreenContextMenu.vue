<script setup lang="ts">
import { getImageGenerationInfo, getImageExif } from '@/api'
import type { FileNodeInfo } from '@/api/files'
import ExifBrowser from '@/components/ExifBrowser.vue'
import DraggableImage from '@/components/DraggableImage.vue'
import { useGlobalStore } from '@/store/useGlobalStore'
import { useLocalStorage } from '@vueuse/core'
import type { MenuInfo } from 'ant-design-vue/lib/menu/src/interface'
import { debounce, throttle, last } from 'lodash-es'
import { computed, watch, onMounted } from 'vue'
import { ref } from 'vue'
import { copy2clipboardI18n, type Dict } from '@/util'
import { useResizeAndDrag } from './useResize'
import {
  DragOutlined,
  FullscreenExitOutlined,
  FullscreenOutlined,
  ArrowsAltOutlined,
  EllipsisOutlined,
  fullscreen,
  SortAscendingOutlined,
  AppstoreOutlined,
  EditOutlined
} from '@/icon'
import { t } from '@/i18n'
import { createReactiveQueue, unescapeHtml } from '@/util'
import ContextMenu from '@/components/ContextMenu.vue'
import { useWatchDocument } from 'vue3-ts-util'
import { useTagStore } from '@/store/useTagStore'
import { parse } from '@/util/stable-diffusion-image-metadata'
import { useFullscreenLayout } from '@/util/useFullscreenLayout'
import { useMouseInElement } from '@vueuse/core'
import { closeImageFullscreenPreview } from '@/util/imagePreviewOperation'
import { openAddNewTagModal, openEditPromptModal } from '@/components/functionalCallableComp'
import { prefix } from '@/util/const'
// @ts-ignore
import * as Pinyin from 'jian-pinyin'
import { Tag } from '@/api/db'
import { aiChat } from '@/api'
import { message } from 'ant-design-vue'

const global = useGlobalStore()

const tagStore = useTagStore()
const el = ref<HTMLElement>()
const props = defineProps<{
  file: FileNodeInfo
  idx: number
}>()
const selectedTag = computed(() => tagStore.tagMap.get(props.file.fullpath) ?? [])
const currImgResolution = ref('')
const q = createReactiveQueue()
const imageGenInfo = ref('')
const exifData = ref<Record<string, string>>({})
const exifDataLoading = ref(false)
const cleanImageGenInfo = computed(() => imageGenInfo.value.replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')
  .replace(/'/g, '&#39;'))
const geninfoFrags = computed(() => cleanImageGenInfo.value.split('\n'))
const geninfoStruct = computed(() => parse(cleanImageGenInfo.value))

const geninfoStructNoPrompts = computed(() => {
  let p = parse(cleanImageGenInfo.value)
  delete p.prompt
  delete p.negativePrompt
  delete p.extraJsonMetaInfo
  return p
})

// extraJsonMetaInfo 是需要额外显示的meta字段，使用原始 imageGenInfo 解析以避免 HTML 转义问题
const extraJsonMetaInfo = computed(() => {
  const p = parse(imageGenInfo.value) // 使用原始的 imageGenInfo，而不是 cleanImageGenInfo
  return p.extraJsonMetaInfo as Record<string, any> | undefined
})

const emit = defineEmits<{
  (type: 'contextMenuClick', e: MenuInfo, file: FileNodeInfo, idx: number): void
}>()

const promptTabActivedKey = useLocalStorage('iib@fullScreenContextMenu.prompt-tab', 'structedData' as 'structedData' | 'sourceText' | 'exif')

async function loadExifData() {
  if (!props?.file?.fullpath) {
    return
  }
  exifDataLoading.value = true
  try {
    exifData.value = await getImageExif(props.file.fullpath)
  } catch (error) {
    console.error('Failed to get EXIF data:', error)
  } finally {
    exifDataLoading.value = false
  }
}

watch(
  () => props?.file?.fullpath,
  async (path) => {
    if (!path) {
      return
    }
    q.tasks.forEach((v) => v.cancel())
    q.pushAction(() => getImageGenerationInfo(path)).res.then((v) => {
      imageGenInfo.value = v
    })
    exifData.value = {}
    if (promptTabActivedKey.value === 'exif') {
      loadExifData()
    }
  },
  { immediate: true }
)

watch(promptTabActivedKey, async (tabKey) => {
  if (tabKey === 'exif') {
    loadExifData()
  }
})

onMounted(() => {
  if (promptTabActivedKey.value === 'exif' && props?.file?.fullpath) {
    loadExifData()
  }
})

const resizeHandle = ref<HTMLElement>()
const dragHandle = ref<HTMLElement>()
const dragInitParams = {
  left: 100,
  top: 100,
  width: 512,
  height: 384,
  expanded: true
}
const state = useLocalStorage('fullScreenContextMenu.vue-drag', dragInitParams)
if (state.value && (state.value.left < 0 || state.value.top < 0)) {
  state.value = { ...dragInitParams }
}


const { isLeftRightLayout, lrLayoutInfoPanelWidth, lrMenuAlwaysOn } = useFullscreenLayout()
const lr = isLeftRightLayout
useResizeAndDrag(el, resizeHandle, dragHandle, {
  disbaled: lr,
  ...state.value,
  onDrag: debounce(function (left, top) {
    state.value = {
      ...state.value,
      left,
      top
    }
  }, 300),
  onResize: debounce(function (width, height) {
    state.value = {
      ...state.value,
      width,
      height
    }
  }, 300)
})


// 处理在isOutside赋值前引用
const isInside = ref(false)

const { isOutside } = useMouseInElement(computed(() => {
  if (!lr.value || lrMenuAlwaysOn.value) {
    return null as any
  }

  const isIn = isInside.value as boolean
  return isIn ? el.value : last(document.querySelectorAll('.iib-tab-edge-trigger'))
}))


watch(isOutside, throttle((v) => {
  isInside.value = !v
}, 300))


function getParNode (p: any) {
  return p.parentNode as HTMLDivElement
}

function getTextLength(text: string): number {
  // chinese characters are counted as 3 English letters
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

function isTagStylePrompt(tags: string[]): boolean {
  if (tags.length === 0) return false
  
  let totalLength = 0
  for (const tag of tags) {
    const tagLength = getTextLength(tag)
    totalLength += tagLength
    
    // 如果存在长度大于50的tag，返回false（自然语言）
    if (tagLength > 50) {
      return false
    }
  }
  
  // 如果平均长度大于30，返回false（自然语言）
  const avgLength = totalLength / tags.length
  if (avgLength > 30) {
    return false
  }
  
  return true
}

function spanWrap (text: string) {
  if (!text) {
    return ''
  }
  
  const specBreakTag = 'BREAK'
  const values = text.replace(/&gt;\s/g, '> ,').replace(/\sBREAK\s/g, ',' + specBreakTag + ',').split(/[\n,]+/).map(v => v.trim()).filter(v => v)
  // 判断是否为tag形式
  if (!isTagStylePrompt(values)) {
    // 自然语言形式：直接显示，保留段落结构
    return text
      .split('\n')
      .map(line => line.trim())
      .filter(line => line)
      .map(line => `<p class="natural-text">${line}</p>`)
      .join('')
  }
  
  // Tag形式：使用原有的标签样式
  const frags = [] as string[]
  let parenthesisActive = false
  for (let i = 0; i < values.length; i++) {
    if (values[i] === specBreakTag) {
      frags.push('<br><span class="tag" style="color:var(--zp-secondary)">BREAK</span><br>')
      continue

    }
    const trimmedValue = values[i]
    if (!parenthesisActive) parenthesisActive = trimmedValue.includes('(')
    const classList = ['tag']
    if (parenthesisActive) classList.push('has-parentheses')
    if (trimmedValue.length < 32) classList.push('short-tag')

    frags.push(`<span class="${classList.join(' ')}">${trimmedValue}</span>`)
    if (parenthesisActive) parenthesisActive = !trimmedValue.includes(')')
  }
  return frags.join(global.showCommaInInfoPanel ? ',' : ' ')
}

useWatchDocument('load', e => {
  const el = e.target as HTMLImageElement
  if (el.className === 'ant-image-preview-img') {
    currImgResolution.value = `${el.naturalWidth} x ${el.naturalHeight}`
  }
}, { capture: true })

const baseInfoTags = computed(() => {
  const tags: { val: string, name: string }[] = [{ name: t('fileSize'), val: props.file.size }]
  if (currImgResolution.value) {
    tags.push({ name: t('resolution'), val: currImgResolution.value })
  }
  return tags
})

const copyPositivePrompt = () => {
  const neg = 'Negative prompt:'
  const text = imageGenInfo.value.includes(neg) ? imageGenInfo.value.split(neg)[0] : geninfoFrags.value[0] ?? ''
  copy2clipboardI18n(unescapeHtml(text.trim()))
}
const requestFullscreen = () => document.body.requestFullscreen()

const copy = (val: any) => {
  copy2clipboardI18n(typeof val === 'object' ? JSON.stringify(val, null, 4) : val)
}



const onKeydown = (e: KeyboardEvent) => {
  if (e.key.startsWith('Arrow')) {
    e.stopPropagation()
    e.preventDefault()
    document.dispatchEvent(new KeyboardEvent('keydown', e))
  }
  else if (e.key === 'Escape') {
    // 判断是不是全屏如果是退出
    if (document.fullscreenElement) {
      document.exitFullscreen()
    }

  }
}

useWatchDocument('dblclick', e => {
  if ((e.target as HTMLDivElement)?.className === 'ant-image-preview-img') {
    closeImageFullscreenPreview()
  }
})


const showFullContent = computed(() => lr.value || state.value.expanded)
const showFullPath = useLocalStorage(prefix + 'contextShowFullPath', false)
const fileTagValue = computed(() => showFullPath.value ? props.file.fullpath : props.file.name)

const tagA2ZClassify = useLocalStorage(prefix + 'tagA2ZClassify', false)
const tagAlphabet = computed(() => {
  const tags = global.conf?.all_custom_tags.map(v => {
    const char = v.display_name?.[0] || v.name?.[0]
    return {
      char,
      ...v
    }
  }).reduce((p: Dict<Tag[]>, c: Tag & { char: string }) => {
    let pos = '#'
    if (/[a-z]/i.test(c.char)) {
      pos = c.char.toUpperCase()
    } else if (/[\u4e00-\u9fa5]/.test(c.char)) {
      try {
        pos = /^\[?(\w)/.exec((Pinyin.getSpell(c.char) + ''))?.[1] ?? '#'
        // eslint-disable-next-line no-empty
      } catch (error) {
        console.log('err', error)
      }
    }
    pos = pos.toUpperCase()
    p[pos] ||= []
    p[pos].push(c)
    return p
  }, {} as Dict<Tag[]>)
  const res = Object.entries(tags ?? {}).sort((a, b) => a[0].charCodeAt(0) - b[0].charCodeAt(0))
  return res
})

// 抖音风格浏览处理函数
const onTiktokViewClick = () => {
  // 从当前文件开始浏览，需要获取当前文件所在的文件列表
  // 这里我们只能浏览单个文件，因为没有完整的文件列表上下文
  closeImageFullscreenPreview()
  emit('contextMenuClick', { key: 'tiktokView' } as any, props.file, props.idx)
}

// AI分析tag功能
const analyzingTags = ref(false)
const analyzeTagsWithAI = async () => {
  if (!geninfoStruct.value.prompt) {
    message.warning(t('aiAnalyzeTagsNoPrompt'))
    return
  }

  if (!global.conf?.all_custom_tags?.length) {
    message.warning(t('aiAnalyzeTagsNoCustomTags'))
    return
  }

  analyzingTags.value = true
  try {
    const prompt = geninfoStruct.value.prompt
    const availableTags = global.conf.all_custom_tags.map(tag => tag.name).join(', ')

    const systemMessage = `You are a professional AI assistant responsible for analyzing Stable Diffusion prompts and categorizing them into appropriate tags.

Your task is:
1. Analyze the given prompt
2. Find all relevant tags from the provided tag list
3. Return only the matching tag names, separated by commas
4. If no tags match, return an empty string
5. Tag matching should be based on semantic similarity and thematic relevance

Available tags: ${availableTags}

Please return only tag names, do not include any other content.`

    const response = await aiChat({
      messages: [
        { role: 'system', content: systemMessage },
        { role: 'user', content: `Please analyze this prompt and return matching tags: ${prompt}` }
      ],
      temperature: 0.3,
      max_tokens: 200
    })

    const matchedTagsText = response.choices[0].message.content.trim()
    if (!matchedTagsText) {
      message.info(t('aiAnalyzeTagsNoMatchedTags'))
      return
    }

    // 解析返回的标签
    const matchedTagNames = matchedTagsText.split(',').map((name: string) => name.trim()).filter((name: string) => name)
    
    // 找到对应的tag对象
    const matchedTags = global.conf.all_custom_tags.filter((tag: Tag) => 
      matchedTagNames.some((matchedName: string) => 
        tag.name.toLowerCase() === matchedName.toLowerCase() ||
        tag.name.toLowerCase().includes(matchedName.toLowerCase()) ||
        matchedName.toLowerCase().includes(tag.name.toLowerCase())
      )
    )

    // 过滤掉已经添加到图像上的标签
    const existingTagIds = new Set(selectedTag.value.map((t: Tag) => t.id))
    const tagsToAdd = matchedTags.filter((tag: Tag) => !existingTagIds.has(tag.id))

    if (tagsToAdd.length === 0) {
      if (matchedTags.length > 0) {
        message.info(t('aiAnalyzeTagsAllTagsAlreadyAdded'))
      } else {
        message.info(t('aiAnalyzeTagsNoValidTags'))
      }
      return
    }

    // 为每个匹配的tag发送添加请求（只添加新标签）
    for (const tag of tagsToAdd) {
      emit('contextMenuClick', { key: `toggle-tag-${tag.id}` } as any, props.file, props.idx)
    }

    message.success(t('aiAnalyzeTagsSuccess', [tagsToAdd.length.toString(), tagsToAdd.map(t => t.name).join(', ')]))

  } catch (error) {
    console.error('AI分析标签失败:', error)
    message.error(t('aiAnalyzeTagsFailed'))
  } finally {
    analyzingTags.value = false
  }
}

// 编辑提示词并重新加载
const editPromptAndReload = async () => {
  await openEditPromptModal(props.file)
  const path = props.file?.fullpath
  if (path) {
    q.tasks.forEach((v) => v.cancel())
    q.pushAction(() => getImageGenerationInfo(path)).res.then((v) => {
      imageGenInfo.value = v
    })
  }
}

</script>

<template>
  <div ref="el" class="full-screen-menu" @wheel.capture.stop @keydown.capture="onKeydown"
    :class="{ 'unset-size': !state.expanded, lr, 'always-on': lrMenuAlwaysOn, 'mouse-in': isInside }">

    <div v-if="lr">
    </div>

    <div class="container">
      <div class="action-bar">
        <div v-if="!lr" ref="dragHandle" class="icon" style="cursor: grab" :title="t('dragToMovePanel')">
          <DragOutlined />
        </div>

        <div v-if="!lr" class="icon" style="cursor: pointer" @click="state.expanded = !state.expanded"
          :title="t('clickToToggleMaximizeMinimize')">
          <FullscreenExitOutlined v-if="showFullContent" />
          <FullscreenOutlined v-else />
        </div>
        <div style="display: flex; flex-direction: column; align-items: center; cursor: grab" class="icon"
          :title="t('fullscreenview')" @click="requestFullscreen">
          <img :src="fullscreen" style="width: 21px;height: 21px;padding-bottom: 2px;" alt="">
        </div>
        <a-dropdown :get-popup-container="getParNode">
          <div class="icon" style="cursor: pointer" v-if="!state.expanded">
            <ellipsis-outlined />
          </div>
          <template #overlay>
            <context-menu :file="file" :idx="idx" :selected-tag="selectedTag"
              @context-menu-click="(e, f, i) => emit('contextMenuClick', e, f, i)" />
          </template>
        </a-dropdown>
        <div flex-placeholder v-if="showFullContent" />
        <div v-if="showFullContent" class="action-bar">

          <a-dropdown :trigger="['hover']" :get-popup-container="getParNode">
            <a-button>{{ t('openContextMenu') }}</a-button>
            <template #overlay>
              <a-menu @click="emit('contextMenuClick', $event, file, idx)">
                <template v-if="global.conf?.launch_mode !== 'server'">
                  <a-menu-item key="send2txt2img">{{ $t('sendToTxt2img') }}</a-menu-item>
                  <a-menu-item key="send2img2img">{{ $t('sendToImg2img') }}</a-menu-item>
                  <a-menu-item key="send2inpaint">{{ $t('sendToInpaint') }}</a-menu-item>
                  <a-menu-item key="send2extras">{{ $t('sendToExtraFeatures') }}</a-menu-item>
                  <a-sub-menu key="sendToThirdPartyExtension" :title="$t('sendToThirdPartyExtension')">
                    <a-menu-item key="send2controlnet-txt2img">ControlNet - {{ $t('t2i') }}</a-menu-item>
                    <a-menu-item key="send2controlnet-img2img">ControlNet - {{ $t('i2i') }}</a-menu-item>
                    <a-menu-item key="send2outpaint">openOutpaint</a-menu-item>
                  </a-sub-menu>
                </template>
                <a-menu-item key="send2BatchDownload">{{ $t('sendToBatchDownload') }}</a-menu-item>
                <a-sub-menu key="copy2target" :title="$t('copyTo')">
                  <a-menu-item v-for="path in global.quickMovePaths" :key="`copy-to-${path.dir}`">{{ path.zh }}
                  </a-menu-item>
                </a-sub-menu>
                <a-sub-menu key="move2target" :title="$t('moveTo')">
                  <a-menu-item v-for="path in global.quickMovePaths" :key="`move-to-${path.dir}`">{{ path.zh }}
                  </a-menu-item>
                </a-sub-menu>
                <a-menu-divider />
                <a-menu-item key="deleteFiles">
                  {{ $t('deleteSelected') }}
                </a-menu-item>
                <a-menu-item key="openWithDefaultApp">{{ $t('openWithDefaultApp') }}</a-menu-item>
                <a-menu-item key="previewInNewWindow">{{ $t('previewInNewWindow') }}</a-menu-item>
                <a-menu-item key="copyPreviewUrl">{{ $t('copySourceFilePreviewLink') }}</a-menu-item>
                <a-menu-item key="copyFilePath">{{ $t('copyFilePath') }}</a-menu-item>
                <a-menu-divider />
                <a-menu-item key="tiktokView" @click="onTiktokViewClick">{{ $t('tiktokView') }}</a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
          <AButton @click="emit('contextMenuClick', { key: 'download' } as MenuInfo, props.file, props.idx)">{{
            $t('download') }}</AButton>
          <a-button @click="copy2clipboardI18n(imageGenInfo)" v-if="imageGenInfo">{{
            $t('copyPrompt')
          }}</a-button>
          <a-button @click="copyPositivePrompt" v-if="imageGenInfo">{{
            $t('copyPositivePrompt')
          }}</a-button>
          <a-button 
            @click="analyzeTagsWithAI"
            :loading="analyzingTags"
            v-if="imageGenInfo && global.conf?.all_custom_tags?.length"
          >
            {{ $t('aiAnalyzeTags') }}
          </a-button>
          <a-button 
            @click="onTiktokViewClick" 
            @touchstart.prevent="onTiktokViewClick"
            type="default"
          >
            {{ $t('tiktokView') }}
          </a-button>         <a-button
            @click="editPromptAndReload"
          >
            <template #icon><EditOutlined /></template>
            {{ $t('editPrompt') }}
          </a-button>
        </div>
      </div>    
      <div class="gen-info" v-if="showFullContent">
    
        <div class="info-tags">
          <span class="info-tag">
            <span class="name">
              {{ $t('fileName') }}
            </span>
            <span class="value" :title="fileTagValue" @dblclick="copy2clipboardI18n(fileTagValue)">
              {{ fileTagValue }}
            </span>
            <span :style="{ margin: '0 8px', cursor: 'pointer' }" title="Click to expand full path"
              @click="showFullPath = !showFullPath">
              <EllipsisOutlined />
            </span>
          </span>
          <span class="info-tag" v-for="tag in baseInfoTags" :key="tag.name">
            <span class="name">
              {{ tag.name }}
            </span>
            <span class="value" :title="tag.val" @dblclick="copy2clipboardI18n(tag.val)">
              {{ tag.val }}
            </span>
          </span>
        </div>
        <div class="tags-container" v-if="global.conf?.all_custom_tags">
          <div class="sort-tag-switch" @click="tagA2ZClassify = !tagA2ZClassify">
            <SortAscendingOutlined v-if="!tagA2ZClassify" />
            <AppstoreOutlined v-else />
          </div>
          <div class="tag" @click="openAddNewTagModal" :style="{ '--tag-color': 'var(--zp-luminous)' }">+ {{ $t('add')
            }}
          </div>
          <template v-if="tagA2ZClassify">
            <div v-for="([char, item]) in tagAlphabet" :key="char" class="tag-alpha-item">
              <h4 style="display: inline-block; width: 32px;">{{ char }} : </h4>
              <div><div class="tag" v-for="tag in item"
                @click="emit('contextMenuClick', { key: `toggle-tag-${tag.id}` } as any, file, idx)"
                :class="{ selected: selectedTag.some(v => v.id === tag.id) }" :key="tag.id"
                :style="{ '--tag-color': tagStore.getColor(tag) }">
                {{ tag.name }}
              </div></div>
            </div>
          </template>
          <template v-else>

            <div class="tag" v-for="tag in global.conf.all_custom_tags"
              @click="emit('contextMenuClick', { key: `toggle-tag-${tag.id}` } as any, file, idx)"
              :class="{ selected: selectedTag.some(v => v.id === tag.id) }" :key="tag.id"
              :style="{ '--tag-color': tagStore.getColor(tag) }">
              {{ tag.name }}
            </div>
          </template>
        </div>
        <div class="lr-layout-control">
          <div class="ctrl-item">
            {{ $t('experimentalLRLayout') }}： <a-switch v-model:checked="lr" size="small" />
          </div>
          <template v-if="lr">

            <div class="ctrl-item">
              {{ $t('width') }}: <a-input-number v-model:value="lrLayoutInfoPanelWidth" style="width:64px" :step="16"
                :min="128" :max="1024" />
            </div>
            <a-tooltip :title="$t('alwaysOnTooltipInfo')">
              <div class="ctrl-item">
                {{ $t('alwaysOn') }}： <a-switch v-model:checked="lrMenuAlwaysOn" size="small" />
              </div>
            </a-tooltip>
          </template>
        </div>
        <!-- 可拖拽的原图 -->
        <DraggableImage :file="file">
          <div class="custom-drag-trigger">
            <DragOutlined class="trigger-icon" />
            <span class="trigger-text">{{ $t('dragImageToTransfer') }}</span>
          </div>
        </DraggableImage>

        <a-tabs v-model:activeKey="promptTabActivedKey">
          <a-tab-pane key="structedData" :tab="$t('structuredData')">
            <div>
              <template v-if="geninfoStruct.prompt">
                <br />
                <div class="section-header">
                  <h3>Prompt</h3>
                  <button
                    class="edit-section-btn"
                    @click="editPromptAndReload"
                    :title="$t('editPrompt')"
                  >
                    <EditOutlined />
                  </button>
                </div>
                <code v-html="spanWrap(geninfoStruct.prompt ?? '')"></code>
              </template>
              <template v-if="geninfoStruct.negativePrompt">
                <br />
                <div class="section-header">
                  <h3>Negative Prompt</h3>
                  <button
                    class="edit-section-btn"
                    @click="editPromptAndReload"
                    :title="$t('editPrompt')"
                  >
                    <EditOutlined />
                  </button>
                </div>
                <code v-html="spanWrap(geninfoStruct.negativePrompt ?? '')"></code> 
              </template>
            </div>
            <template v-if="Object.keys(geninfoStructNoPrompts).length"> <br />
              <div class="section-header">
                <h3>Params</h3>
                <button
                  class="edit-section-btn"
                  @click="editPromptAndReload"
                  :title="$t('editPrompt')"
                >
                  <EditOutlined />
                </button>
              </div>
              <table>
                <tr v-for="txt, key in geninfoStructNoPrompts" :key="key" class="gen-info-frag">
                  <td style="font-weight: 600;text-transform: capitalize;">{{ key }}</td>
                  <td style="cursor: pointer;" v-if="typeof txt == 'object'" @dblclick="copy(txt)">
                    <code>{{ txt }}</code>
                  </td>
                  <td v-else style="cursor: pointer;" @dblclick="copy(unescapeHtml(txt))">
                    {{ unescapeHtml(txt) }}
                  </td>
                </tr>
              </table>
            </template>
            <template v-if="extraJsonMetaInfo && Object.keys(extraJsonMetaInfo).length"> <br />
              <div class="section-header">
                <h3>Extra Meta Info</h3>
                <button
                  class="edit-section-btn"
                  @click="editPromptAndReload"
                  :title="$t('editPrompt')"
                >
                  <EditOutlined />
                </button>
              </div>
              <table class="extra-meta-table">
                <tr v-for="(val, key) in extraJsonMetaInfo" :key="key" class="gen-info-frag">
                  <td style="font-weight: 600;text-transform: capitalize;">{{ key }}</td>
                  <td style="cursor: pointer;" @dblclick="copy(val)">
                    <code class="extra-meta-value">{{ typeof val === 'string' ? val : JSON.stringify(val, null, 2) }}</code>
                  </td>
                </tr>
              </table>
            </template>
          </a-tab-pane>
          <a-tab-pane key="sourceText" :tab="$t('sourceText')">
            <code>{{ imageGenInfo }}</code>
          </a-tab-pane>
          <a-tab-pane key="exif" :tab="'EXIF'">
            <a-spin :spinning="exifDataLoading">
              <div v-if="exifData && Object.keys(exifData).length">
                <ExifBrowser :data="exifData" />
              </div>
              <div v-else-if="!exifDataLoading">
                <a-empty description="No EXIF data available" />
              </div>
            </a-spin>
          </a-tab-pane>
        </a-tabs>
      </div>
    </div>

    <div class="mouse-sensor" ref="resizeHandle" v-if="state.expanded && !lr" :title="t('dragToResizePanel')">
      <ArrowsAltOutlined />
    </div>
  </div>
</template>

<style scoped lang="scss">
.full-screen-menu {
  position: fixed;
  z-index: 9999;
  background: var(--zp-primary-background);
  padding: 8px 16px;
  box-shadow: 0px 0px 4px var(--zp-secondary);
  border-radius: 4px;

  .tags-container {
    margin: 4px 0;

    .tag {
      margin-right: 4px;
      margin-bottom: 4px;
      padding: 2px 16px;
      border-radius: 4px;
      display: inline-block;
      cursor: pointer;
      font-weight: bold;
      transition: .5s all ease;
      border: 2px solid var(--tag-color);
      color: var(--tag-color);
      background: var(--zp-primary-background);
      user-select: none;

      &.selected {
        background: var(--tag-color);
        color: white;
      }
    }
  }

  .container {
    height: 100%;
    display: flex;
    overflow: hidden;
    flex-direction: column;
  }

  .gen-info {
    padding-top: 8px;
    flex: 1;
    word-break: break-all;
    white-space: pre-line;
    overflow: auto;
    z-index: 1;
    padding-top: 4px;
    position: relative;

    code {
      font-size: 0.9em;
      display: block;
      padding: 4px;
      background: var(--zp-primary-background);
      border-radius: 4px;
      margin-right: 20px;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.78em;

      :deep() {
        .natural-text {
          margin: 0.5em 0;
          line-height: 1.6em;
          color: var(--zp-primary);
        }

        .short-tag {
          word-break: break-all;
          white-space: nowrap;
        }

        span.tag {

          background: var(--zp-secondary-variant-background);
          color: var(--zp-primary);
          padding: 2px 4px;
          border-radius: 6px;
          margin-right: 6px;
          margin-top: 4px;
          line-height: 1.3em;
          display: inline-block;
        }

        .has-parentheses.tag {
          background: rgba(255, 100, 100, 0.14);
        }

        span.tag:hover {
          background: rgba(120, 0, 0, 0.15);
        }
      }
    }

    table {
      font-size: 1em;
      border-radius: 4px;
      border-collapse: separate;
      margin-bottom: 3em;

      tr td:first-child {
        white-space: nowrap;
        vertical-align: top;
      }
    }

    table.extra-meta-table {
      .extra-meta-value {
        display: block;
        max-height: 200px;
        overflow: auto;
        white-space: pre-wrap;
        word-break: break-word;
        font-size: 0.85em;
        background: var(--zp-secondary-variant-background);
        padding: 8px;
        border-radius: 4px;
      }
    }

    table td {
      padding-right: 14px;
      padding-left: 4px;
      border-bottom: 1px solid var(--zp-secondary);
      border-collapse: collapse;
    }

    .info-tags {
      .info-tag {
        display: inline-block;
        overflow: hidden;
        border-radius: 4px;
        margin-right: 8px;
        border: 2px solid var(--zp-primary);
      }


      .name {
        background-color: var(--zp-primary);
        color: var(--zp-primary-background);
        padding: 4px;
        border-bottom-right-radius: 4px;
      }

      .value {
        padding: 4px;
      }

    }
  }

  &.unset-size {
    width: unset !important;
    height: unset !important;
  }

  .mouse-sensor {
    position: absolute;
    bottom: 0;
    right: 0;
    transform: rotate(90deg);
    cursor: se-resize;
    z-index: 1;
    background: var(--zp-primary-background);
    border-radius: 2px;

    &>* {
      font-size: 18px;
      padding: 4px;
    }
  }

  .action-bar {
    display: flex;
    align-items: center;
    user-select: none;
    gap: 4px;

    .icon {
      font-size: 1.5em;
      padding: 2px 4px;
      border-radius: 4px;

      &:hover {
        background: var(--zp-secondary-variant-background);
      }
    }

    &>* {
      flex-wrap: wrap;
    }
  }
}

.full-screen-menu.lr {
  top: v-bind("lrMenuAlwaysOn ? 0 : '46px'") !important;
  right: 0 !important;
  bottom: 0 !important;
  left: 100vw !important;
  height: unset !important;
  width: v-bind("lrLayoutInfoPanelWidth + 'px'") !important;
  transition: left ease 0.3s;

  &.always-on,
  &.mouse-in {
    left: v-bind("`calc(100vw - ${lrLayoutInfoPanelWidth}px)`") !important;
  }
}
.tag-alpha-item {
  display: flex;
  h4 {
    width: 32px;
    flex-shrink: 0;
  }
  margin-top: 4px;
}
.sort-tag-switch {
  display: inline-block;
  padding-right: 16px;
  padding-left: 8px;
  cursor: pointer;
  user-select: none;

  span {
    transition: all ease .3s;
    transform: scale(1.2);
  }

  &:hover span {
    transform: scale(1.3);
  }
}

.lr-layout-control {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 4px 8px;
  flex-wrap: wrap;
  border-radius: 2px;
  border-left: 3px solid var(--zp-luminous);
  background-color: var(--zp-secondary-background);

  .ctrl-item {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: nowrap;
  }
}

.section-header {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
  gap: 6px;

  h3 {
    margin: 0;
  }

  .edit-section-btn {
    margin: 0;
    padding: 2px 6px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.15);
    border-radius: 4px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s ease;
    color: var(--zp-primary);
    font-size: 12px;
    line-height: 1;
    min-width: 22px;
    min-height: 22px;

    &:hover {
      background: rgba(255, 255, 255, 0.1);
      border-color: rgba(255, 255, 255, 0.25);
      color: var(--zp-luminous);
      transform: scale(1.05);
    }

    &:active {
      transform: scale(0.95);
    }

    :deep(.anticon) {
      font-size: 12px;
    }
  }
}
</style>
