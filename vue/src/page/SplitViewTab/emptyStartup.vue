<script lang="ts" setup>
import { useGlobalStore, type TabPane } from '@/store/useGlobalStore'
import { Snapshot, useWorkspeaceSnapshot } from '@/store/useWorkspeaceSnapshot'
import { uniqueId } from 'lodash-es'
import { computed, ref, watch } from 'vue'
import { ok } from 'vue3-ts-util'
import { FileDoneOutlined, BarChartOutlined, GithubOutlined, LockOutlined, MailOutlined, PlusOutlined, QuestionCircleOutlined, SettingOutlined, HolderOutlined, EditOutlined, DeleteOutlined, EyeOutlined, EyeInvisibleOutlined, CheckOutlined, UndoOutlined } from '@/icon'
import { t } from '@/i18n'
import { cloneDeep } from 'lodash-es'
import { useImgSliStore } from '@/store/useImgSli'
import { addToExtraPath, onAliasExtraPathClick, onRemoveExtraPathClick } from './extraPathControlFunc'
import actionContextMenu from './actionContextMenu.vue'
import { ExtraPathType } from '@/api/db'
import { onMounted, onUnmounted } from 'vue'
import { hasNewRelease, version, latestCommit } from '@/util/versionManager'
import { isTauri } from '@/util/env'
import { message } from 'ant-design-vue'
import { useSettingSync } from '@/util'
import { orderStartupItems, startupBlockIds, useStartupLayout, type StartupBlockId } from '@/util/startupLayout'
import Sortable from 'sortablejs'

const global = useGlobalStore()
const imgsli = useImgSliStore()
const workspaceSnapshot = useWorkspeaceSnapshot()
const props = defineProps<{
  tabIdx: number; paneIdx: number, popAddPathModal?: {
    path: string
    type: ExtraPathType
  }
}>()

onMounted(() => {
  if (props.popAddPathModal) {
    addToExtraPath(props.popAddPathModal.type, props.popAddPathModal.path)
  }
})

const sync = useSettingSync()

const helpModalOpen = ref(false)
const FAQ_URL = 'https://github.com/zanllp/sd-webui-infinite-image-browsing/issues/90'
const ISSUES_SEARCH_URL = 'https://github.com/zanllp/sd-webui-infinite-image-browsing/issues?q='
const NEW_ISSUE_URL = 'https://github.com/zanllp/sd-webui-infinite-image-browsing/issues/new'
const FEEDBACK_MAIL = 'mailto:qc@zanllp.cn'


const compCnMap: Partial<Record<TabPane['type'], string>> = {
  local: t('local'),
  'tag-search': t('imgSearch'),
  'fuzzy-search': t('fuzzy-search'),
  'topic-search': t('topicSearchExperimental'),
  'batch-download': t('batchDownload') + ' / ' + t('archive'),
  'workspace-snapshot': t('WorkspaceSnapshot'),
  'random-image': t('randomImage'),
  'global-setting': t('globalSettings'),
  'trend': t('trend'),
}
type FileTransModeIn = 'preset' | ExtraPathType
const createPane = (type: TabPane['type'], path?: string, mode?: FileTransModeIn) => {
  let pane: TabPane
  switch (type) {
    case 'grid-view':
    case 'tag-search-matched-image-grid':
    case 'topic-search-matched-image-grid':
    case 'img-sli':
      return
    case 'global-setting':
    case 'tag-search':
    case 'batch-download':
    case 'workspace-snapshot':
    case 'fuzzy-search':
    case 'topic-search':
    case 'random-image':
    case 'trend':
    case 'empty':
      pane = { type, name: compCnMap[type]!, key: Date.now() + uniqueId() }
      break
    case 'local':
      pane = {
        type,
        name: compCnMap[type]!,
        key: Date.now() + uniqueId(),
        path,
        mode: mode === 'scanned-fixed' || mode === 'walk' ? mode : 'scanned'
      }
  }
  return pane
}

const openTrend = () => {
  const pane = createPane('trend')
  if (pane) {
    const tab = global.tabList[0]
    if (tab) { tab.panes.push(pane); tab.key = pane.key }
  }
}

const openInCurrentTab = (type: TabPane['type'], path?: string, mode?: FileTransModeIn) => {
  const pane = createPane(type, path, mode)
  if (!pane) return
  const tab = global.tabList[props.tabIdx]
  tab.panes.splice(props.paneIdx, 1, pane)
  tab.key = pane.key
}

const openInNewTab = (type: TabPane['type'], path?: string, mode?: FileTransModeIn) => {
  const pane = createPane(type, path, mode)
  if (!pane) return
  const tab = global.tabList[props.tabIdx]
  tab.panes.push(pane)
}

const openOnTheRight = (type: TabPane['type'], path?: string, mode?: FileTransModeIn) => {
  const pane = createPane(type, path, mode)
  if (!pane) return
  let tab = global.tabList[props.tabIdx + 1]
  if (!tab) {
    tab = { panes: [], key: '', id: uniqueId() }
    global.tabList[props.tabIdx + 1] = tab
  }
  tab.panes.push(pane)
  tab.key = pane.key
}

const lastRecord = computed(() => global.tabListHistoryRecord?.[1])


const walkModeSupportedDir = computed(() =>
  global.quickMovePaths.filter(
    ({ key: k, types }) =>
      k === 'outdir_txt2img_samples' ||
      k === 'outdir_img2img_samples' ||
      k === 'outdir_txt2img_grids' ||
      k === 'outdir_img2img_grids' ||
      types.includes('walk')
  )
)

// ---- 启动页列表的自定义：隐藏预设 + 拖拽排序（存在后端 KV）----
const startupLayout = useStartupLayout()
const editingBlock = ref<StartupBlockId | null>(null)
const toggleEdit = (block: StartupBlockId) => {
  editingBlock.value = editingBlock.value === block ? null : block
}
/** 内置预设项（用户自己加的路径 types 里没有 preset） */
const isPresetDir = (dir: any) => ((dir?.types ?? []) as string[]).includes('preset')

const walkItems = computed(() => orderStartupItems('walkMode', walkModeSupportedDir.value, (d: any) => d.key))
const walkPresetIds = computed(() => walkModeSupportedDir.value.filter(isPresetDir).map((d: any) => d.key))

// 「Normal / Fixed」列表：一行 = 一个目录 × 一种类型（同一目录可能同时出现在 walk/scanned/scanned-fixed 里）
const normalFixedRows = computed(() => {
  const rows: { id: string, dir: any, type: FileTransModeIn }[] = []
  global.quickMovePaths
    .filter(({ types: ts }) => ts.includes('cli_access_only') || ts.includes('preset') || ts.includes('scanned') || ts.includes('scanned-fixed'))
    .forEach((dir: any) => {
      dir.types
        .filter((v: string) => v !== 'walk')
        .forEach((type: string) => rows.push({ id: `${dir.key}#${type}`, dir, type: type as FileTransModeIn }))
    })
  return rows
})
const normalFixedItems = computed(() => orderStartupItems('normalFixed', normalFixedRows.value, (r) => r.id))
const normalFixedPresetIds = computed(() => normalFixedRows.value.filter((r) => isPresetDir(r.dir)).map((r) => r.id))

// 「启动」列表里的内置入口
const launchComps = computed(() =>
  (Object.keys(compCnMap) as TabPane['type'][]).map((k) => ({ id: k, label: compCnMap[k]! }))
)
const launchItems = computed(() => orderStartupItems('launch', launchComps.value, (v) => v.id))
const launchAllIds = computed(() => launchComps.value.map((v) => v.id))

const hideAllPresets = (block: StartupBlockId, ids: string[]) => startupLayout.setHidden(block, ids, true)
const showAllPresets = (block: StartupBlockId, ids: string[]) => startupLayout.setHidden(block, ids, false)

// ---- 拖拽排序：sortablejs，只有编辑模式下的拖拽把手能拖 ----
const walkUl = ref<HTMLElement>()
const normalUl = ref<HTMLElement>()
const launchUl = ref<HTMLElement>()
const listRefs = { walkMode: walkUl, normalFixed: normalUl, launch: launchUl }
const sortables: Partial<Record<StartupBlockId, Sortable>> = {}

const initSortable = (block: StartupBlockId) => {
  const el = listRefs[block].value
  if (!el || sortables[block]) {
    return
  }
  sortables[block] = Sortable.create(el, {
    animation: 150,
    handle: '.drag-handle',
    draggable: '.item[data-item-id]',
    ghostClass: 'drag-ghost',
    disabled: editingBlock.value !== block,
    onEnd: () => {
      const ids = [...el.querySelectorAll('.item[data-item-id]')].map((li) => (li as HTMLElement).dataset.itemId!)
      startupLayout.setOrder(block, ids)
    }
  })
}

watch([walkUl, normalUl, launchUl], () => startupBlockIds.forEach(initSortable), { immediate: true })
watch(editingBlock, (block) => {
  startupBlockIds.forEach((id) => sortables[id]?.option('disabled', id !== block))
})
onUnmounted(() => {
  startupBlockIds.forEach((id) => sortables[id]?.destroy())
})
const canpreviewInNewWindow = window.parent !== window
const previewInNewWindow = () => window.parent.open('/infinite_image_browsing' + (window.parent.location.href.includes('theme=dark') ? '?__theme=dark' : ''))

const restoreRecord = () => {
  ok(lastRecord.value)
  global.tabList = cloneDeep(lastRecord.value.tabs)
}

const restoreWorkspaceSnapshot = (item: Snapshot) => {
  global.tabList = cloneDeep(item.tabs)
}

const machine = computed(() => {
  if (isTauri) return 'desktop application'
  if ( global.conf?.launch_mode === 'sd') return 'sd-webui extension'
  return 'standalone'
})

const modePrefix = (mode?: FileTransModeIn) => {
  if (!mode || mode === 'scanned') return ''
  if (mode === 'walk') return 'Walk: '
  return 'Fixed: '

}

const modes = computed(() => {
  const modes = [] as string[]
  if (global.conf?.enable_access_control) {
    modes.push('accessLimited')
  }
  if(global.conf?.is_readonly) {
    modes.push('readonly')
  }
  return modes.map(v => t(v)).join(' + ')
})

</script>
<template>
  <div class="container">
    <div class="header">
      <div class="header-left">
        <h1>{{ $t('welcome') }}</h1>
        <!-- Compact Magic Switch with Welcome -->
        <div class="magic-switch-compact">
          <a-tooltip>
            <template #title>
              <div class="switch-tooltip">
                <div class="tooltip-title">{{ $t('magicSwitchTiktokView') }}</div>
                <div class="tooltip-status">{{ global.magicSwitchTiktokView ? $t('magicSwitchEnabled') : $t('magicSwitchDisabled') }}</div>
                <div class="tooltip-desc">{{ $t('magicSwitchDetailDesc') }}</div>
              </div>
            </template>
            <div class="ultra-cool-switch" :class="{ active: global.magicSwitchTiktokView }" @click="global.magicSwitchTiktokView = !global.magicSwitchTiktokView">
              <div class="switch-bg">
                <div class="switch-track"></div>
                <div class="switch-thumb" :class="{ active: global.magicSwitchTiktokView }">
                  <span class="switch-icon">{{ global.magicSwitchTiktokView ? '🎬' : '📁' }}</span>
                </div>
                <div class="switch-glow"></div>
              </div>
              <span class="switch-label">{{ $t('tiktokView') }}</span>
            </div>
          </a-tooltip>
        </div>

        <a-tooltip :title="$t('trendPanel')">
          <div class="trend-icon-btn" @click="openTrend">
            <bar-chart-outlined />
          </div>
        </a-tooltip>

      </div>

      <div v-if="global.conf?.enable_access_control && global.dontShowAgain"
        style="margin-left: 16px;font-size: 1.5em;">
        <LockOutlined title="Access Control mode" style="vertical-align: text-bottom;" />
      </div>
      <div flex-placeholder />
      <a href="https://github.com/zanllp/sd-webui-infinite-image-browsing" target="_blank"
        class="quick-action">Github</a>
      <a href="https://github.com/zanllp/sd-webui-infinite-image-browsing/blob/main/.env.example" target="_blank"
        class="quick-action">{{ $t('privacyAndSecurity') }}</a>
      <a-badge :count="hasNewRelease ? 'new' : null" :offset="[2,0]" color="geekblue">
        <a href="https://github.com/zanllp/sd-webui-infinite-image-browsing/releases" target="_blank"
          class="quick-action">Releases</a>
      </a-badge>
      <a href="https://github.com/zanllp/sd-webui-infinite-image-browsing/wiki/Change-log" target="_blank"
        class="quick-action">{{ $t('changlog') }}</a>
      <a href="#" class="quick-action" @click.prevent="helpModalOpen = true">{{ $t('helpFeedback') }}</a>
      <div class="quick-action" v-if="!isTauri">
        {{ $t('sync') }}  <a-tooltip :title="$t('syncDesc')">
          <QuestionCircleOutlined/>
        </a-tooltip>  :  <a-switch v-model:checked="sync" />
      </div>
      <a-radio-group v-model:value="global.darkModeControl" button-style="solid">
        <a-radio-button value="light">Light</a-radio-button>
        <a-radio-button value="auto">Auto</a-radio-button>
        <a-radio-button value="dark">Dark</a-radio-button>
      </a-radio-group>
    </div>

    <a-modal
      v-model:visible="helpModalOpen"
      :title="$t('helpFeedback')"
      :footer="null"
      :mask-closable="true"
      width="520px"
    >
      <div style="display: grid; gap: 10px;">
        <div style="display: flex; gap: 10px; align-items: flex-start;">
          <QuestionCircleOutlined style="margin-top: 2px; opacity: 0.85;" />
          <div style="flex: 1; min-width: 0;">
            <div style="font-weight: 600;">{{ $t('helpFeedbackWay1') }}</div>
            <div style="margin-top: 6px; display: flex; gap: 10px; flex-wrap: wrap;">
              <a :href="FAQ_URL" target="_blank" rel="noopener noreferrer">{{ $t('faq') }}</a>
              <a :href="ISSUES_SEARCH_URL" target="_blank" rel="noopener noreferrer">{{ $t('helpFeedbackSearchIssues') }}</a>
            </div>
          </div>
        </div>

        <div style="display: flex; gap: 10px; align-items: flex-start;">
          <GithubOutlined style="margin-top: 2px; opacity: 0.85;" />
          <div style="flex: 1; min-width: 0;">
            <div style="font-weight: 600;">{{ $t('helpFeedbackWay2') }}</div>
            <div style="margin-top: 6px;">
              <a :href="NEW_ISSUE_URL" target="_blank" rel="noopener noreferrer">{{ $t('helpFeedbackNewIssue') }}</a>
            </div>
          </div>
        </div>

        <div style="display: flex; gap: 10px; align-items: flex-start;">
          <MailOutlined style="margin-top: 2px; opacity: 0.85;" />
          <div style="flex: 1; min-width: 0;">
            <div style="font-weight: 600;">{{ $t('helpFeedbackWay3') }}</div>
            <div style="margin-top: 6px;">
              <a :href="FEEDBACK_MAIL">qc@zanllp.cn</a>
            </div>
          </div>
        </div>
      </div>
    </a-modal>

    <a-alert show-icon v-if="global.conf?.enable_access_control && !global.dontShowAgain">
      <template #message>
        <div class="access-mode-message">
          <div>
            {{ $t('accessControlModeTips') }}
          </div>
          <div flex-placeholder />
          <a @click.prevent="global.dontShowAgain = true">{{ $t('dontShowAgain') }}</a>
        </div>
      </template>
      <template #icon>
        <LockOutlined></LockOutlined>
      </template>
    </a-alert>
    <!--a-alert show-icon v-if="!global.dontShowAgainNewImgOpts">
      <template #message>
        <div class="access-mode-message">
          <div>
            {{ $t('majorUpdateCustomCellSizeTips') }}
          </div>
          <div flex-placeholder />
          <a @click.prevent="global.dontShowAgainNewImgOpts = true">{{ $t('dontShowAgain') }}</a>
        </div>
      </template>
    </a-alert-->
    <div class="content">
      <div class="feature-item">
        <div class="feature-head">
          <h2>{{ $t('walkMode') }}</h2>
          <AButton type="text" size="small"
            :title="editingBlock === 'walkMode' ? $t('startupListDone') : $t('startupListEdit')"
            @click="toggleEdit('walkMode')">
            <CheckOutlined v-if="editingBlock === 'walkMode'" />
            <SettingOutlined v-else />
          </AButton>
        </div>
        <div class="list-edit-bar" v-if="editingBlock === 'walkMode'">
          <AButton size="small" :title="$t('startupListHideAllPresets')" @click="hideAllPresets('walkMode', walkPresetIds)"><EyeInvisibleOutlined /></AButton>
          <AButton size="small" :title="$t('startupListShowAllPresets')" @click="showAllPresets('walkMode', walkPresetIds)"><EyeOutlined /></AButton>
          <AButton size="small" :title="$t('startupListReset')" @click="startupLayout.resetBlock('walkMode')"><UndoOutlined /></AButton>
          <span class="list-edit-hint">{{ $t('startupListEditHint') }}</span>
        </div>
        <ul ref="walkUl">
          <li @click="addToExtraPath('walk')" class="item">
            <span class="text line-clamp-1">
              <PlusOutlined /> {{ $t('add') }}
            </span>
          </li>
            
          <a-button v-if="global.showRandomImageInStartup" @click="openInCurrentTab('random-image')" type="primary" style="border-radius:100vw;margin-bottom: 8px;" ghost><span style="margin:0 6px;"><span style="margin-right: 8px;">🎲</span>{{ $t('tryMyLuck') }}</span></a-button>
          <actionContextMenu v-for="dir in walkItems" :key="dir.key"
            @open-in-new-tab="openInNewTab('local', dir.dir, 'walk')"
            @open-on-the-right="openOnTheRight('local', dir.dir, 'walk')">
            <li v-show="editingBlock === 'walkMode' || !startupLayout.isHidden('walkMode', dir.key)"
              class="item rem" :data-item-id="dir.key"
              :class="{ 'is-hidden': startupLayout.isHidden('walkMode', dir.key) }"
              @click.prevent="editingBlock !== 'walkMode' && openInCurrentTab('local', dir.dir, 'walk')">
              <HolderOutlined v-if="editingBlock === 'walkMode'" class="drag-handle" />
              <span class="text line-clamp-2">{{ dir.zh }}</span>
              <template v-if="editingBlock === 'walkMode'">
                <AButton v-if="isPresetDir(dir)" type="link"
                  :title="startupLayout.isHidden('walkMode', dir.key) ? $t('startupListShowItem') : $t('startupListHideItem')"
                  @click.stop="startupLayout.toggleHidden('walkMode', dir.key)">
                  <EyeOutlined v-if="startupLayout.isHidden('walkMode', dir.key)" />
                  <EyeInvisibleOutlined v-else />
                </AButton>
                <template v-else-if="dir.can_delete">
                  <AButton type="link" :title="$t('alias')" @click.stop="onAliasExtraPathClick(dir.dir)">
                    <EditOutlined />
                  </AButton>
                  <AButton type="link" :title="$t('remove')" @click.stop="onRemoveExtraPathClick(dir.dir, 'walk')">
                    <DeleteOutlined />
                  </AButton>
                </template>
              </template>
            </li>
          </actionContextMenu>
        </ul>
      </div>
      <div class="feature-item" v-if="global.quickMovePaths.length">
        <div class="feature-head">
          <h2>{{ $t('launchFromNormalAndFixed') }}</h2>
          <AButton type="text" size="small"
            :title="editingBlock === 'normalFixed' ? $t('startupListDone') : $t('startupListEdit')"
            @click="toggleEdit('normalFixed')">
            <CheckOutlined v-if="editingBlock === 'normalFixed'" />
            <SettingOutlined v-else />
          </AButton>
        </div>
        <div class="list-edit-bar" v-if="editingBlock === 'normalFixed'">
          <AButton size="small" :title="$t('startupListHideAllPresets')"
            @click="hideAllPresets('normalFixed', normalFixedPresetIds)"><EyeInvisibleOutlined /></AButton>
          <AButton size="small" :title="$t('startupListShowAllPresets')"
            @click="showAllPresets('normalFixed', normalFixedPresetIds)"><EyeOutlined /></AButton>
          <AButton size="small" :title="$t('startupListReset')" @click="startupLayout.resetBlock('normalFixed')"><UndoOutlined /></AButton>
          <span class="list-edit-hint">{{ $t('startupListEditHint') }}</span>
        </div>
        <ul ref="normalUl">
          <li @click="addToExtraPath('scanned-fixed')" class="item">
            <span class="text line-clamp-1">
              <PlusOutlined /> {{ $t('add') }}
            </span>
          </li>
          <actionContextMenu v-for="row in normalFixedItems" :key="row.id"
            @open-in-new-tab="openInNewTab('local', row.dir.dir, row.type)"
            @open-on-the-right="openOnTheRight('local', row.dir.dir, row.type)">

            <li v-show="editingBlock === 'normalFixed' || !startupLayout.isHidden('normalFixed', row.id)"
              class="item rem" :data-item-id="row.id"
              :class="{ 'is-hidden': startupLayout.isHidden('normalFixed', row.id) }"
              @click.prevent="editingBlock !== 'normalFixed' && openInCurrentTab('local', row.dir.dir, row.type)">
              <HolderOutlined v-if="editingBlock === 'normalFixed'" class="drag-handle" />
              <span class="text line-clamp-2"><span v-if="row.type == 'scanned-fixed'" class="fixed">Fixed</span>{{
                row.dir.zh }}</span>
              <template v-if="editingBlock === 'normalFixed'">
                <AButton v-if="isPresetDir(row.dir)" type="link"
                  :title="startupLayout.isHidden('normalFixed', row.id) ? $t('startupListShowItem') : $t('startupListHideItem')"
                  @click.stop="startupLayout.toggleHidden('normalFixed', row.id)">
                  <EyeOutlined v-if="startupLayout.isHidden('normalFixed', row.id)" />
                  <EyeInvisibleOutlined v-else />
                </AButton>
                <template v-else-if="row.dir.can_delete">
                  <AButton type="link" :title="$t('alias')" @click.stop="onAliasExtraPathClick(row.dir.dir)">
                    <EditOutlined />
                  </AButton>
                  <AButton type="link" :title="$t('remove')" @click.stop="onRemoveExtraPathClick(row.dir.dir, row.type as ExtraPathType)">
                    <DeleteOutlined />
                  </AButton>
                </template>
              </template>
            </li>
          </actionContextMenu>
        </ul>
      </div>
      <div class="feature-item">
        <div class="feature-head">
          <h2>{{ $t('launch') }}</h2>
          <AButton type="text" size="small"
            :title="editingBlock === 'launch' ? $t('startupListDone') : $t('startupListEdit')"
            @click="toggleEdit('launch')">
            <CheckOutlined v-if="editingBlock === 'launch'" />
            <SettingOutlined v-else />
          </AButton>
        </div>
        <div class="list-edit-bar" v-if="editingBlock === 'launch'">
          <AButton size="small" :title="$t('startupListHideAllPresets')" @click="hideAllPresets('launch', launchAllIds)"><EyeInvisibleOutlined /></AButton>
          <AButton size="small" :title="$t('startupListShowAllPresets')" @click="showAllPresets('launch', launchAllIds)"><EyeOutlined /></AButton>
          <AButton size="small" :title="$t('startupListReset')" @click="startupLayout.resetBlock('launch')"><UndoOutlined /></AButton>
          <span class="list-edit-hint">{{ $t('startupListEditHint') }}</span>
        </div>
        <ul ref="launchUl">
          <li v-for="comp in launchItems" :key="comp.id" class="item rem" :data-item-id="comp.id"
            v-show="editingBlock === 'launch' || !startupLayout.isHidden('launch', comp.id)"
            :class="{ 'is-hidden': startupLayout.isHidden('launch', comp.id) }"
            @click.prevent="editingBlock !== 'launch' && openInCurrentTab(comp.id)">
            <HolderOutlined v-if="editingBlock === 'launch'" class="drag-handle" />
            <span class="text line-clamp-1">{{ comp.label }}</span>
            <template v-if="editingBlock === 'launch'">
              <AButton type="link"
                :title="startupLayout.isHidden('launch', comp.id) ? $t('startupListShowItem') : $t('startupListHideItem')"
                @click.stop="startupLayout.toggleHidden('launch', comp.id)">
                <EyeOutlined v-if="startupLayout.isHidden('launch', comp.id)" />
                <EyeInvisibleOutlined v-else />
              </AButton>
            </template>
          </li>
          <li class="item" @click="imgsli.opened = true">
            <span class="text line-clamp-1">{{ $t('imgCompare') }}</span>
          </li>
          <li class="item" v-if="canpreviewInNewWindow" @click="previewInNewWindow">
            <span class="text line-clamp-1">{{ $t('openThisAppInNewWindow') }}</span>
          </li>
          <li class="item" v-if="lastRecord?.tabs.length" @click="restoreRecord">
            <span class="text line-clamp-1">{{ $t('restoreLastWorkspaceState') }}</span>
          </li>
          <li class="item" v-for="item in workspaceSnapshot.snapshots" :key="item.id" @click="restoreWorkspaceSnapshot(item)">
            <span class="text line-clamp-1">{{ $t('restoreWorkspaceSnapshot', [item.name]) }}</span>
          </li>
        </ul>
      </div>
      <div class="feature-item recent" v-if="global.showRecentInStartup && global.recent.length">
        <div class="title">
          <h2>{{ $t('recent') }}</h2>
          <AButton @click="global.recent = []" type="link">{{ $t('clear') }}</AButton>
        </div>
        <ul>
          <li v-for="item in global.recent" :key="item.key" class="item"
            @click.prevent="openInCurrentTab('local', item.path, item.mode)">
            <FileDoneOutlined class="icon" />
            <span class="text line-clamp-1">{{modePrefix(item.mode)}}{{ global.getShortPath(item.path) }}</span>
          </li>
        </ul>
      </div>
    </div>

    <div class="ver-info" @dblclick="message.info('Ciallo～(∠・ω< )⌒☆')">
      <div v-if="modes">
        Mode: {{ modes }}
      </div>
      <div>
        Version: {{ version.tag }} ({{machine}})
      </div>
      <div v-if="version.hash">
        Hash: {{ version.hash }}
      </div>
      <div v-if="latestCommit && version.hash && latestCommit.sha !== version.hash">
        Not the latest commit
      </div>
      <div v-if="latestCommit">
        Latest Commit: {{ latestCommit.sha }} (Updated at {{ latestCommit.commit.author?.date }})
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.access-mode-message {
  display: flex;
  flex-direction: row;
  align-items: center;

  a {
    margin-left: 16px;
  }
}

.container {
  padding: 20px;
  background-color: var(--zp-secondary-background);
  height: 100%;
  overflow: auto;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
}

.header h1 {
  font-size: 28px;
  font-weight: bold;
  color: var(--zp-primary);
  margin: 0;
}

.quick-action {
  margin-right: 16px;
  font-size: 14px;
  color: var(--zp-secondary);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 4px;

}

.quick-action a {
  text-decoration: none;
  color: var(--zp-secondary);
}

.quick-action a:hover {
  color: var(--zp-primary);
}

.content {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(384px, 1fr));
  grid-gap: 20px;
  margin-top: 16px;
}

.feature-item {
  background-color: var(--zp-primary-background);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
  padding: 20px;

  ul {
    list-style: none;
    padding: 6px;
    max-height: 70vh;
    overflow-y: auto;
  }

  &.recent {
    .title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 20px;

      h2 {
        margin: 0;
      }
    }
  }

  .item {
    margin-bottom: 8px;
    padding: 6px 8px;
    display: flex;
    align-items: center;
    position: relative;

    &.rem {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }

    &:hover {
      background: var(--zp-secondary-background);
      border-radius: 4px;
      color: var(--primary-color);
      cursor: pointer;
    }

    /* 类型提示：低标识度，描边小标签，不再是一块实心红 */
    .fixed {
      display: inline-block;
      flex: none;
      font-size: 10px;
      line-height: 16px;
      padding: 0 5px;
      border-radius: 4px;
      border: 1px solid var(--zp-secondary);
      color: var(--zp-secondary);
      margin-right: 6px;
      vertical-align: 1px;
    }
  }

  .icon {
    margin-right: 8px;
  }
}

.feature-item h2 {
  margin-top: 0;
  margin-bottom: 20px;
  font-size: 20px;
  font-weight: bold;
  color: var(--zp-primary);
}

.feature-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;

  h2 {
    margin-bottom: 12px;
  }
}

.list-edit-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--zp-secondary-background);

  .list-edit-hint {
    font-size: 12px;
    opacity: .7;
  }
}

/* 编辑模式下：拖拽把手 + 被隐藏的条目半透明留在原位 */
.drag-handle {
  margin-right: 8px;
  color: var(--zp-secondary);
  cursor: grab;

  &:active {
    cursor: grabbing;
  }
}

.item.is-hidden {
  opacity: .45;
}

.drag-ghost {
  opacity: .4;
  background: var(--zp-secondary-background);
  border-radius: 4px;
}


.text {
  flex: 1;
  font-size: 16px;
  word-break: break-all;
}

.ver-info {
  display: flex;
  align-items: center;
  flex-direction: row;
  justify-content: center;
  color: var(--zp-secondary);
  gap: 16px;
  padding: 32px;
  flex-wrap: wrap;
  font-size: 0.9em;
}

/* Compact Magic Switch Styles */
.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.magic-switch-compact {
  flex-shrink: 0;
}

.trend-icon-btn {
  flex-shrink: 0;
  margin-left: 12px;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: var(--zp-secondary);
  background: var(--zp-tertiary-background, rgba(128, 128, 128, 0.1));
  transition: all 0.2s;
  font-size: 16px;

  &:hover {
    color: var(--zp-primary);
    background: var(--zp-hover, rgba(128, 128, 128, 0.2));
    transform: scale(1.08);
  }
}

.ultra-cool-switch {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  background: var(--zp-primary-background);
  border: 1px solid transparent;
  border-radius: 25px;
  cursor: pointer;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  font-size: 12px;
  white-space: nowrap;
  position: relative;
  overflow: hidden;
  
  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: -100%;
    width: 100%;
    height: 100%;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
    transition: left 0.5s;
  }
  
  &:hover {
    background: var(--zp-secondary-background);
    border-color: var(--primary-color);
    transform: translateY(-2px) scale(1.02);
    box-shadow: 0 8px 25px rgba(0, 0, 0, 0.15), 0 4px 10px rgba(0, 0, 0, 0.1);
    
    &::before {
      left: 100%;
    }
  }
  
  &.active {
    background: linear-gradient(135deg, #ff8c42 0%, #ff6b35 50%, #ff4757 100%);
    border-color: #ff8c42;
    color: white;
    box-shadow: 0 8px 25px rgba(255, 107, 53, 0.4), 0 4px 15px rgba(255, 140, 66, 0.3);
    
    &::before {
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.3), transparent);
    }
    
    .switch-label {
      color: white;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
    }
  }
}

.switch-bg {
  position: relative;
  width: 44px;
  height: 22px;
  border-radius: 11px;
  overflow: hidden;
  background: linear-gradient(45deg, rgba(0, 0, 0, 0.1), rgba(0, 0, 0, 0.05));
  box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.1);
}

.switch-track {
  position: absolute;
  top: 1px;
  left: 1px;
  width: 42px;
  height: 20px;
  background: linear-gradient(45deg, rgba(0, 0, 0, 0.15), rgba(0, 0, 0, 0.08));
  border-radius: 10px;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
}

.ultra-cool-switch.active .switch-track {
  background: linear-gradient(45deg, rgba(255, 255, 255, 0.3), rgba(255, 255, 255, 0.15));
  box-shadow: 0 0 10px rgba(255, 140, 66, 0.5);
}

.switch-thumb {
  position: absolute;
  top: 1px;
  left: 1px;
  width: 20px;
  height: 20px;
  background: linear-gradient(145deg, #ffffff, #f0f0f0);
  border-radius: 50%;
  transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
  box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2), 0 2px 4px rgba(0, 0, 0, 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
  
  &.active {
    transform: translateX(22px) rotate(360deg);
    background: linear-gradient(145deg, #fff, #ffeaa6);
    box-shadow: 0 4px 12px rgba(255, 140, 66, 0.4), 0 2px 6px rgba(255, 107, 53, 0.3);
  }
}

.switch-icon {
  font-size: 10px;
  transition: all 0.3s ease;
  filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.2));
}

.switch-label {
  color: var(--zp-primary);
  font-weight: 600;
  transition: all 0.3s ease;
  letter-spacing: 0.5px;
}

.switch-glow {
  position: absolute;
  top: -1px;
  left: -1px;
  width: calc(100% + 2px);
  height: calc(100% + 2px);
  background: linear-gradient(45deg, transparent, rgba(255, 140, 66, 0.2), transparent);
  border-radius: 12px;
  opacity: 0;
  transition: all 0.4s ease;
}

.ultra-cool-switch.active .switch-glow {
  opacity: 1;
  animation: glowPulse 2s ease-in-out infinite;
}

@keyframes glowPulse {
  0%, 100% {
    opacity: 0.3;
    transform: scale(1);
  }
  50% {
    opacity: 0.6;
    transform: scale(1.05);
  }
}

.switch-tooltip {
  max-width: 240px;
  line-height: 1.5;
}

.tooltip-title {
  font-weight: 600;
  margin-bottom: 4px;
  color: var(--primary-color);
}

.tooltip-status {
  margin-bottom: 6px;
  font-size: 13px;
}

.tooltip-desc {
  font-size: 12px;
  opacity: 0.8;
  line-height: 1.4;
}

@media (max-width: 768px) {
  .header-left {
    gap: 12px;
  }
  
  .ultra-cool-switch {
    padding: 6px 10px;
    gap: 8px;
    font-size: 11px;
  }
  
  .switch-bg {
    width: 36px;
    height: 18px;
  }
  
  .switch-track {
    width: 34px;
    height: 16px;
  }
  
  .switch-thumb {
    width: 16px;
    height: 16px;
    
    &.active {
      transform: translateX(18px) rotate(360deg);
    }
  }
  
  .switch-icon {
    font-size: 8px;
  }
}
</style>
