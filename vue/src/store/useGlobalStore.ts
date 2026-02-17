import type { GlobalConf } from '@/api'
import type { ExtraPathType, MatchImageByTagsReq, Tag } from '@/api/db'
import type { OrganizeJobProgress, OrganizeFilesPreviewResp } from '@/api/organize'
import { FileNodeInfo } from '@/api/files'
import { i18n, t } from '@/i18n'
import { getPreferredLang } from '@/i18n'
import { SortMethod } from '@/page/fileTransfer/fileSort'
import { Props as FileTransferProps } from '@/page/fileTransfer/hooks'
import type { getQuickMovePaths } from '@/page/taskRecord/autoComplete'
import { type Dict, type ReturnTypeAsync } from '@/util'
import { AnyFn, usePreferredDark } from '@vueuse/core'
import { cloneDeep, uniqueId, last } from 'lodash-es'
import { defineStore } from 'pinia'
import { VNode, computed, onMounted, reactive, toRaw, watch } from 'vue'
import { ref } from 'vue'
import { WithRequired } from 'vue3-ts-util'
import * as Path from '../util/path'
import { prefix } from '@/util/const'

interface TabPaneBase {
  name: string | VNode
  nameFallbackStr?: string
  readonly key: string
}

interface OtherTabPane extends TabPaneBase {
  type: 'global-setting' | 'tag-search' |  'batch-download' | 'workspace-snapshot' | 'random-image' | 'topic-search'
}

export interface EmptyStartTabPane extends TabPaneBase  {
  type: 'empty' 
  popAddPathModal?: {
    path: string
    type: ExtraPathType
  }
}

export type GridViewFileTag = WithRequired<Partial<Tag>, 'name'>;

export interface GridViewFile extends FileNodeInfo {
  /**
   * Tags for displaying the file. The 'name' property is required,
   * while the other properties are optional.
   */
  tags?: GridViewFileTag[];
}

/**
 * A tab pane that displays files in a grid view.
 */
interface GridViewTabPane extends TabPaneBase {
  type: 'grid-view'
  /**
   * Indicates whether the files in the grid view can be deleted.
   */
  removable?: boolean
  /**
   * Indicates whether files can be dragged and dropped from other pages into the grid view.
   */
  allowDragAndDrop?: boolean,
  files: GridViewFile[]
}


export interface GridViewFile extends FileNodeInfo {
  /**
   * Tags for displaying the file. The 'name' property is required,
   * while the other properties are optional.
   */
  tags?: GridViewFileTag[];
}

/**
 * A tab pane that displays files in a grid view.
 */
interface GridViewTabPane extends TabPaneBase {
  type: 'grid-view'
  /**
   * Indicates whether the files in the grid view can be deleted.
   */
  removable?: boolean
  /**
   * Indicates whether files can be dragged and dropped from other pages into the grid view.
   */
  allowDragAndDrop?: boolean,
  files: GridViewFile[]
}

interface TagSearchMatchedImageGridTabPane extends TabPaneBase {
  type: 'tag-search-matched-image-grid'
  selectedTagIds: MatchImageByTagsReq
  id: string
}

interface TopicSearchMatchedImageGridTabPane extends TabPaneBase {
  type: 'topic-search-matched-image-grid'
  id: string
  title: string
  paths: string[]
}
export interface ImgSliTabPane extends TabPaneBase {
  type: 'img-sli'
  left: FileNodeInfo
  right: FileNodeInfo
}

export interface FileTransferTabPane extends TabPaneBase {
  type: 'local'
  path?: string
  mode?: FileTransferProps['mode']
  stackKey?: string
}

export interface TagSearchTabPane extends TabPaneBase {
  type: 'tag-search'
  searchScope?: string
}

export interface FuzzySearchTabPane extends TabPaneBase {
  type: 'fuzzy-search'
  searchScope?: string
}

export type TabPane =
  | EmptyStartTabPane
  | FileTransferTabPane
  | OtherTabPane
  | TagSearchMatchedImageGridTabPane
  | TopicSearchMatchedImageGridTabPane
  | ImgSliTabPane
  | TagSearchTabPane
  | FuzzySearchTabPane
  | GridViewTabPane

/**
 * This interface represents a tab, which contains an array of panes, an ID, and a key
 */
export interface Tab {
  /**
   * An array of panes that belong to this tab
   */
  panes: TabPane[]
  /**
   * A unique identifier for this tab
   */
  id: string
  /**
   * A value indicating which pane is currently selected within the tab
   */
  key: string
}

export type Shortcut = Record<`toggle_tag_${string}` | 'delete' | 'download' | `copy_to_${string}`| `move_to_${string}`, string | undefined> 

export type DefaultInitinalPage = `workspace_snapshot_${string}` | 'empty' | 'last-workspace-state'

export const copyPane = (pane: TabPane) => {
  return cloneDeep({
    ...pane,
    name: typeof pane.name === 'string' ? pane.name : pane.nameFallbackStr ?? ''
  })
}

export const copyTab = (tab: Tab): Tab => {
  return {
    ...tab,
    panes: tab.panes.map(copyPane)
  }
}

export const copyTabFilterWorkspaceSnapShot = (tab: Tab): Tab => {
  if (!tab.panes.some(v => v.type === 'workspace-snapshot')) {
    return copyTab(tab)
  }
  const newPanes = tab.panes.filter(v => v.type !== 'workspace-snapshot').map(copyPane)
  return {
    ...tab,
    panes: newPanes,
    key: last(newPanes)?.key ?? ''
  }
}


export type ActionConfirmRequired = 'deleteOneOnly'

export const presistKeys = [
  'defaultChangeIndchecked',
  'defaultSeedChangeChecked',
  'darkModeControl',
  'dontShowAgainNewImgOpts',
  'defaultSortingMethod',
  'defaultGridCellWidth',
  'dontShowAgain',
  'lang',
  'enableThumbnail',
  'tabListHistoryRecord',
  'recent',
  'gridThumbnailResolution',
  'longPressOpenContextMenu',
  'onlyFoldersAndImages',
  'fileTypeFilter',
  'shortcut',
  'ignoredConfirmActions',
  'previewBgOpacity',
  'defaultInitinalPage',
  'autoRefreshWalkMode',
  'autoRefreshWalkModePosLimit',
  'autoRefreshNormalFixedMode',
  'showCommaInInfoPanel',
  'batchDownloadCompress',
  'batchDownloadPackOnly',
  'magicSwitchTiktokView',
  'showRandomImageInStartup',
  'showTiktokNavigator',
  'autoUpdateIndex'
]

function cellWidthMap(x: number): number {
  if (x < 768) {
    return 176;
  } else {
    const y = 160 + Math.floor((x - 768) / 128) * 16;
    return Math.min(y, 256);
  }
}

export const useGlobalStore = defineStore(
  prefix + 'useGlobalStore',
  () => {
    const conf = ref<GlobalConf>()
    const quickMovePaths = ref([] as ReturnTypeAsync<typeof getQuickMovePaths>)

    const enableThumbnail = ref(true)
    const gridThumbnailResolution = ref(512)
    const defaultSortingMethod = ref(SortMethod.CREATED_TIME_DESC)
    const defaultGridCellWidth = ref(256)

    try {
      if (typeof parent !== 'undefined' && parent.window) {
        defaultGridCellWidth.value = cellWidthMap(parent.window.innerHeight)
      }
    } catch (error) {
      console.error(error)
    }
    
    const darkModeControl = ref<'light' | 'dark' | 'auto'>('auto')

    const createEmptyPane = (): TabPane => ({
      type: 'empty',
      name: t('emptyStartPage'),
      key: uniqueId()
    })
    const tabList = ref<Tab[]>([])
    onMounted(() => {
      const emptyPane = createEmptyPane()
      tabList.value.push({ panes: [emptyPane], key: emptyPane.key, id: uniqueId() })
    })
    const dragingTab = ref<{ tabIdx: number; paneIdx: number }>()
    const recent = ref(new Array<{ path: string; key: string, mode: FileTransferTabPane['mode'] }>())
    const time = Date.now()
    const tabListHistoryRecord = ref<{ time: number; tabs: Tab[] }[]>() // [curr,last]
    const saveRecord = () => {
      const tabs = toRaw(tabList.value).map(copyTab)
      if (tabListHistoryRecord.value?.[0].time !== time) {
        tabListHistoryRecord.value = [{ tabs, time }, ...(tabListHistoryRecord.value ?? [])]
      } else {
        tabListHistoryRecord.value[0].tabs = tabs
      }
      tabListHistoryRecord.value = tabListHistoryRecord.value.slice(0, 2)
    }

    const openTagSearchMatchedImageGridInRight = async (
      tabIdx: number,
      id: string,
      tagIds: MatchImageByTagsReq
    ) => {
      let pane = tabList.value
        .map((v) => v.panes)
        .flat()
        .find(
          (v) => v.type === 'tag-search-matched-image-grid' && v.id === id
        ) as TagSearchMatchedImageGridTabPane
      if (pane) {
        pane.selectedTagIds = cloneDeep(tagIds)
        return
      } else {
        pane = {
          type: 'tag-search-matched-image-grid',
          id: id,
          selectedTagIds: cloneDeep(tagIds),
          key: uniqueId(),
          name: t('searchResults')
        }
      }

      const tab = tabList.value[tabIdx + 1]
      if (!tab) {
        tabList.value.push({ panes: [pane], key: pane.key, id: uniqueId() })
      } else {
        tab.key = pane.key
        tab.panes.push(pane)
      }
    }

    const lang = ref(getPreferredLang())
    watch(lang, (v) => (i18n.global.locale.value = v as any))

    const longPressOpenContextMenu = ref(false)

    const shortcut = ref<Shortcut>({
      delete: '',
      download: ''
    })

    const extraPathAliasMap = ref({} as Dict<string>)
    const pathAliasMap = computed((): Dict<string> => {
      const keys = [
        'outdir_extras_samples',
        'outdir_save',
        'outdir_txt2img_samples',
        'outdir_img2img_samples',
        'outdir_img2img_grids',
        'outdir_txt2img_grids'
      ]
      const res = quickMovePaths.value.filter((v) => keys.includes(v.key)).map((v) => [v.zh, v.dir])
      return {...Object.fromEntries(res), ...extraPathAliasMap.value}
    })

    const pageFuncExportMap = new Map<string, Dict<AnyFn>>()
    const ignoredConfirmActions = reactive<Record<ActionConfirmRequired, boolean>>({ deleteOneOnly: false })

    const dark = usePreferredDark()

    const computedTheme = computed(() =>  {
      const getParDark = () => {
        try {
          return parent.location.search.includes('theme=dark') // sd-webui的
        } catch (error) {
          return false
        }
      }
      const isDark = darkModeControl.value === 'auto' ? (dark.value || getParDark()) : (darkModeControl.value === 'dark')
      return isDark ? 'dark' : 'light'
    })

    // 简化路径
    const getShortPath = (loc: string) => {
      try {
        loc = loc.trim()
        const map = pathAliasMap.value
        const np = Path.normalize(loc)
        const replacedPaths = [] as string[]
        for (const [k, v] of Object.entries(map)) {
          if (k && v) {
            if (loc === v || np === v) return k
            replacedPaths.push(np.replace(v, '$' + k))
          }
        }
        return replacedPaths.sort((a, b) => a.length - b.length)?.[0] ?? loc
      } catch (error) {
        console.error(error)
        return loc
      }
    }
    const previewBgOpacity = ref(0.6)
    const magicSwitchTiktokView = ref(false)
    const showRandomImageInStartup = ref(true)
    const showTiktokNavigator = ref(false)

    // ===== Organize Jobs Management =====
    interface OrganizeJob {
      job_id: string
      status: string
      progress: OrganizeJobProgress
      startedAt: number
      folder_paths: string[]
      preview?: OrganizeFilesPreviewResp
    }

    const activeOrganizeJobs = ref<OrganizeJob[]>([])
    const showOrganizePanel = ref(true) // 控制面板显示/隐藏

    // Smart organize config modal state
    const showSmartOrganizeConfig = ref(false)
    const smartOrganizeConfigPath = ref('')

    const addOrganizeJob = (job: OrganizeJob) => {
      activeOrganizeJobs.value.push(job)
      showOrganizePanel.value = true // 添加任务时自动显示面板
    }

    const updateOrganizeJob = (job_id: string, update: Partial<OrganizeJob>) => {
      const idx = activeOrganizeJobs.value.findIndex(j => j.job_id === job_id)
      if (idx >= 0) {
        // Deep clone to ensure Vue reactivity works properly
        const existingJob = JSON.parse(JSON.stringify(activeOrganizeJobs.value[idx]))
        const newJob = { ...existingJob, ...update }
        // Force Vue reactivity by replacing the entire array item
        activeOrganizeJobs.value.splice(idx, 1, newJob)
        console.log('Updated job:', job_id, 'status:', newJob.status, 'preview:', newJob.preview ? `has preview (${newJob.preview.total_files} files)` : 'no preview')
      }
    }

    const removeOrganizeJob = (job_id: string) => {
      activeOrganizeJobs.value = activeOrganizeJobs.value.filter(j => j.job_id !== job_id)
    }

    const getOrganizeJob = (job_id: string) => {
      return activeOrganizeJobs.value.find(j => j.job_id === job_id)
    }

    return {
      computedTheme,
      showTiktokNavigator,
      darkModeControl,
      defaultSortingMethod,
      defaultGridCellWidth,
      defaultChangeIndchecked: ref(true),
      defaultSeedChangeChecked: ref(false),
      pathAliasMap,
      createEmptyPane,
      lang,
      tabList,
      conf,
      quickMovePaths,
      enableThumbnail,
      dragingTab,
      saveRecord,
      recent,
      tabListHistoryRecord,
      gridThumbnailResolution,
      longPressOpenContextMenu,
      openTagSearchMatchedImageGridInRight,
      onlyFoldersAndImages: ref(true), // 保留用于向后兼容
      fileTypeFilter: ref<('image' | 'video' | 'audio' | 'all')[]>(['image', 'video', 'audio']), // 新的多选过滤
      keepMultiSelect: ref(false),
      fullscreenPreviewInitialUrl: ref(''),
      shortcut,
      pageFuncExportMap,
      dontShowAgain: ref(false),
      dontShowAgainNewImgOpts: ref(false),
      ignoredConfirmActions,
      getShortPath,
      extraPathAliasMap,
      previewBgOpacity,
      defaultInitinalPage: ref<DefaultInitinalPage>('empty'),
      autoRefreshWalkMode: ref(true),
      autoRefreshWalkModePosLimit: ref(128),
      autoRefreshNormalFixedMode: ref(true),
      showCommaInInfoPanel: ref(false),
      batchDownloadCompress: ref(false),
      batchDownloadPackOnly: ref(false),
      magicSwitchTiktokView,
      showRandomImageInStartup,
      autoUpdateIndex: ref(true),
      // Organize jobs
      activeOrganizeJobs,
      showOrganizePanel,
      addOrganizeJob,
      updateOrganizeJob,
      removeOrganizeJob,
      getOrganizeJob,
      // Smart organize config modal
      showSmartOrganizeConfig,
      smartOrganizeConfigPath
    }
  },
  {
    persist: {
      // debug: true,
      paths: presistKeys
    }
  }
)
