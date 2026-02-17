import { getTargetFolderFiles, FileNodeInfo } from '@/api/files'
import { openCreateFlodersModal } from '@/components/functionalCallableComp'
import { t } from '@/i18n'
import { DatabaseOutlined } from '@/icon'
import { TagSearchTabPane, FuzzySearchTabPane, FileTransferTabPane, EmptyStartTabPane } from '@/store/useGlobalStore'
import { copy2clipboardI18n, makeAsyncFunctionSingle, useGlobalEventListen } from '@/util'
import { message, Modal } from 'ant-design-vue'
import { ref, watch, onMounted, h, computed, onUnmounted } from 'vue'
import { delay, ok, Task, useWatchDocument } from 'vue3-ts-util'
import { useHookShareState, stackCache, global } from '.'
import NumInput from '@/components/numInput.vue'

import * as Path from '@/util/path'
import type Progress from 'nprogress'
// @ts-ignore
import NProgress from 'multi-nprogress'

import { cloneDeep, debounce, last, uniqueId } from 'lodash-es'
import { useLocalStorage } from '@vueuse/core'
import { prefix } from '@/util/const'


/**
 * 路径栏相关
 */
export function useLocation () {
  const np = ref<Progress.NProgress>()
  const {
    scroller,
    stackViewEl,
    stack,
    currPage,
    currLocation,
    useEventListen,
    eventEmitter,
    getPane,
    props,
    deletedFiles,
    walker,
    sortedFiles,
    previewing
  } = useHookShareState().toRefs()

  watch(
    () => stack.value.length,
    debounce((v, lv) => {
      if (v === lv) {
        return
      }
      if (v > lv) {
        scroller.value?.scrollToItem(0)
        return
      }
      const target = last(stack.value)?.scrollIndex ?? 0
      delay(0).then(() => scroller.value?.scrollToItem(target))
    }, 300)
  )
  onMounted(async () => {
    if (!stack.value.length) {
      // 有传入stack时直接使用传入的
      if (props.value.mode === 'scanned-fixed' || props.value.mode === 'walk') {
        stack.value = [{ files: [], curr: props.value.path ?? '' }]
      } else {
        const resp = await getTargetFolderFiles('/')
        stack.value.push({
          files: resp.files,
          curr: '/'
        })
      }
    }
    np.value = new NProgress()
    np.value!.configure({ parent: stackViewEl.value as any })
    if (props.value.path && props.value.path !== '/') {
      await handleMultiModeTo(props.value.path)
    } else {
      global.conf?.home && handleMultiModeTo(global.conf.home)
    }
  })

  watch(
    currLocation,
    debounce((loc) => {
      const pane = getPane.value()
      if (!pane) {
        return
      }
      pane.path = loc
      const dirname = Path.splitPath(loc).pop() ?? ''
      const getTitle = () => {
        const prefix = {
          walk: 'Walk',
          'scanned-fixed': 'Fixed',
          scanned: null
        }[props.value.mode ?? 'scanned']
        const wrap = (v: string) => prefix ? `${prefix}: ${v}` : v
        const shortPath = global.getShortPath(loc)
        return wrap(shortPath.length > 24 && dirname ? dirname : shortPath)
      }
      const title = getTitle()
      pane.name = h('div', { style: 'display:flex;align-items:center' }, [
        h(DatabaseOutlined),
        h('span', { class: 'line-clamp-1', style: 'max-width: 256px' }, title)
      ])
      pane.nameFallbackStr = title
      global.recent = global.recent.filter((v) => v.key !== pane.key)
      global.recent.unshift({ path: loc, key: pane.key, mode: props.value.mode })
      if (global.recent.length > 20) {
        global.recent = global.recent.slice(0, 20)
      }
    }, 300)
  )

  const copyLocation = () => copy2clipboardI18n(currLocation.value)

  const openNext = async (file: FileNodeInfo) => {
    if (file.type !== 'dir') {
      return
    }
    try {
      np.value?.start()
      const { files } = await getTargetFolderFiles(file.fullpath)
      if (props.value.mode == 'scanned-fixed') {
        stack.value = [{
          files,
          curr: file.fullpath
        }]
        // fixed模式下stack.length始终为1，watch不会触发滚动，需要手动滚动到顶部
        scroller.value?.scrollToItem(0)
      } else {
        stack.value.push({
          files,
          curr: file.name
        })
      }
    } finally {
      np.value?.done()
    }
  }

  const back = (idx: number) => {
    if (props.value.mode == 'walk') {
      return
    }
    while (idx < stack.value.length - 1) {
      stack.value.pop()
    }
  }

  const backToLastUseTo = () => {
    handleMultiModeTo(Path.getParentDirectory(currLocation.value))
  }

  const isDirNameEqual = (a: string, b: string) => {
    ok(global.conf, 'global.conf load failed')
    if (global.conf.is_win) {
      // window下忽略
      return a.toLowerCase() == b.toLowerCase()
    }
    return a == b
  }



  const handleMultiModeTo = async (path: string) => {
    // console.log('call handleMultiModeTo', path)
    if (props.value.mode === 'walk') {
      getPane.value().path = path
    } else if (props.value.mode === 'scanned-fixed') {
      await openNext({ fullpath: path, name: path, type: 'dir' } as FileNodeInfo)
    } else {
      await handleToScannedOnly(path)
    }
    // 初始化页面的tag,diff,文件夹方面，有些加载时间长的无法依靠watch location
    delay(500).then(() => eventEmitter.value.emit('viewableAreaFilesChange'))
  }


  const handleToScannedOnly = async (dir: string) => {
    const backup = stack.value.slice()
    try {
      if (!Path.isAbsolute(dir)) {
        // 相对路径
        dir = Path.join(global.conf?.sd_cwd ?? '/', dir)
      }
      const frags = Path.splitPath(dir)
      const currPaths = stack.value.map((v) => v.curr)
      currPaths.shift() // 是 /
      while (currPaths[0] && frags[0]) {
        if (!isDirNameEqual(currPaths[0], frags[0])) {
          break
        } else {
          currPaths.shift()
          frags.shift()
        }
      }
      for (let index = 0; index < currPaths.length; index++) {
        stack.value.pop()
      }
      if (!frags.length) {
        return refresh()
      }
      for (const frag of frags) {
        const target = currPage.value?.files.find((v) => isDirNameEqual(v.name, frag))
        if (!target) {
          console.error({ frags, frag, stack: cloneDeep(stack.value) })
          throw new Error(`${frag} not found`)
        }
        await openNext(target)
      }
    } catch (error) {
      message.error(t('moveFailedCheckPath') + (error instanceof Error ? error.message : ''))
      console.error(dir, Path.splitPath(dir), currPage.value)
      stack.value = backup
      throw error
    }
  }

  const refresh = makeAsyncFunctionSingle(async () => {
    try {
      np.value?.start()
      if (walker.value) {
        await walker.value.reset()
        eventEmitter.value.emit('loadNextDir')
      } else {

        const { files } = await getTargetFolderFiles(currLocation.value)
        last(stack.value)!.files = files
      }

      deletedFiles.value.clear()
      scroller.value?.scrollToItem(0)
      message.success(t('refreshCompleted'))
    } finally {
      np.value?.done()
    }
  })


  /**
   * 上面那个是Force
   */
  const lazyRefresh = (async (isPollRefresh = false) => {
    
    if (isPollRefresh === true && previewing.value) {
      return // fullscreen previewing时不刷新
    }
    if (props.value.mode === 'walk' && walker.value) {
      const currpos = scroller.value?.$_endIndex ?? 64
      if (global.autoRefreshWalkMode &&
        currpos < global.autoRefreshWalkModePosLimit &&
        await walker.value.isExpired()) {
        const taskCancelled = ref(false)
        const onDisable = () => {
          taskCancelled.value = true
          global.autoRefreshWalkMode = false
          hide()
          message.success(t('walkModeAutoRefreshDisabled'))
        }
        const hide = message.loading(h('span', {}, [
          t('autoUpdate'),
          h('span', { onClick: onDisable, style: { paddingLeft: '16px', cursor: 'pointer', color: 'var(--primary-color)' } }, t('disable'))
        ]), 0)
        try {
          const updatePromsie = new Promise<void>(resolve => {
            walker.value!.seamlessRefresh(currpos, taskCancelled).then((newWalker) => {
              if (taskCancelled.value) return
              walker.value = newWalker
              eventEmitter.value.emit('loadNextDir') // 确认铺满和更新tag，显示期间还能执行
              resolve()
            })
          })
          await Promise.all([updatePromsie, delay(1500)]) // 最少显示1.5s
        } finally {
          hide()
        }
      }
      return
    }
    try {
      if (!global.autoRefreshNormalFixedMode) {
        return
      }
      np.value?.start()
      const { files } = await getTargetFolderFiles(currLocation.value)
      const currFiles = last(stack.value)!.files
      if (currFiles.map((v) => v.date).join() !== files.map((v) => v.date).join()) {
        last(stack.value)!.files = files
        message.success(t('autoUpdate'))
      }
    } finally {
      np.value?.done()
    }
  })

  useGlobalEventListen('returnToIIB', lazyRefresh)

  // Listen for refreshFileView event (e.g., after smart organize completes)
  useGlobalEventListen('refreshFileView', (data?: { paths?: string[] }) => {
    // Check if current location is in the affected paths
    const paths = data?.paths || []
    const shouldRefresh = paths.length === 0 || paths.some(p =>
      currLocation.value.startsWith(p) || p.startsWith(currLocation.value)
    )
    if (shouldRefresh) {
      refresh()
    }
  })

  useEventListen.value('refresh', refresh)
  useEventListen.value('navigateUp', backToLastUseTo)

  const quickMoveTo = (path: string) => {
    // todo
    handleMultiModeTo(path)
  }

  const normalizedScandPath = computed(() => {
    return global.quickMovePaths.map((v) => ({ ...v, path: Path.normalize(v.dir) }))
  })

  const searchPathInfo = computed(() => {
    const c = Path.normalize(currLocation.value)
    const path = normalizedScandPath.value.find((v) => v.path === c)
    return path
  })

  const addToSearchScanPathAndQuickMove = async () => {
    const tab = global.tabList[props.value.tabIdx]
    const pane: EmptyStartTabPane = {
      type: 'empty',
      name: t('emptyStartPage'),
      key: Date.now() + uniqueId(),
      popAddPathModal: {
        path: currLocation.value,
        type: 'scanned-fixed'
      }
    }
    tab.panes.push(pane)
    tab.key = pane.key
  }

  const isLocationEditing = ref(false)
  const locInputValue = ref(currLocation.value)
  const onEditBtnClick = () => {
    isLocationEditing.value = true
    locInputValue.value = currLocation.value
  }

  const onLocEditEnter = async () => {
    await handleMultiModeTo(locInputValue.value)
    isLocationEditing.value = false
  }

  useWatchDocument('click', (e) => {
    if (!(e.target as HTMLElement)?.className?.includes?.('ant-input')) {
      isLocationEditing.value = false
    }
  })

  const share = () => {
    const loc = parent.location
    const baseUrl = loc.href.substring(0, loc.href.length - loc.search.length)
    const params = new URLSearchParams(loc.search)
    params.set('action', 'open')
    params.set('path', currLocation.value)
    params.set('mode', props.value.mode ?? 'scanned')
    const url = `${baseUrl}?${params.toString()}`
    copy2clipboardI18n(url, t('copyLocationUrlSuccessMsg'))
  }

  const searchInCurrentDir = (type: (TagSearchTabPane | FuzzySearchTabPane)['type'] = 'tag-search') => {
    const tab = global.tabList[props.value.tabIdx]
    const pane = {
      type,
      key: uniqueId(),
      searchScope: currLocation.value,
      name: t(type === 'tag-search' ? 'imgSearch' : 'fuzzy-search'),
    }
    tab.panes.push(pane)
    tab.key = pane.key
  }

  const selectAll = () => eventEmitter.value.emit('selectAll')

  const onCreateFloderBtnClick = async () => {
    await openCreateFlodersModal(currLocation.value)
    await refresh()
  }

  const onWalkBtnClick = () => {
    const path = currLocation.value
    stackCache.set(path, stack.value)
    const tab = global.tabList[props.value.tabIdx]
    const pane: FileTransferTabPane = {
      type: 'local',
      key: uniqueId(),
      path: path,
      name: t('local'),
      stackKey: path,
      mode: 'walk'
    }
    tab.panes.push(pane)
    tab.key = pane.key
  }

  const showWalkButton = computed(() => !walker.value && sortedFiles.value.some(v => v.type === 'dir'))

  return {
    locInputValue,
    isLocationEditing,
    onLocEditEnter,
    onEditBtnClick,
    addToSearchScanPathAndQuickMove,
    searchPathInfo,
    refresh,
    copyLocation,
    back,
    openNext,
    currPage,
    currLocation,
    stack,
    scroller,
    share,
    selectAll,
    quickMoveTo,
    onCreateFloderBtnClick,
    onWalkBtnClick,
    showWalkButton,
    searchInCurrentDir,
    backToLastUseTo,
    ...usePollRefresh(() => lazyRefresh(true))
  }
}

const usePollRefresh = (lazyRefresh: () => Promise<any>) => {
  const clearCbs = ref([] as (() => void)[])
  const polling = computed(() => clearCbs.value.length > 0)
  onUnmounted(() => {
    clearCbs.value.forEach(v => v())

  })
  const interval = useLocalStorage(prefix + 'poll-interval', 3)
  const onPollRefreshClick = () => {
    if (clearCbs.value.length) {
      clearCbs.value.forEach(v => v())
      clearCbs.value = []
      return
    }
    Modal.confirm({
      title: t('pollRefresh'),
      width: 640,
      content: () => h('div', {}, [
        h('p', { class: 'uni-desc primary-bg' }, t('pollRefreshTip')),
        h('div', { style: { display: 'flex', alignItems: 'center', gap: '4px' } }, [
          h('span', {}, t('pollInterval') + '(s): '),
          h(NumInput as any, {
            min: 1,
            max: 60 * 10,
            modelValue: interval.value,
            'onUpdate:modelValue': (v: number) => {
              interval.value = v
            }
          })
        ])
      ]),
      onOk: () => {
        const { clearTask } = Task.run({ pollInterval: interval.value * 1000, action: lazyRefresh })
        clearCbs.value.push(clearTask)
      }
    })
  }
  return {
    onPollRefreshClick,
    polling
  }
}