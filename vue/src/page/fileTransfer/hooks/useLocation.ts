import { getTargetFolderFiles, FileNodeInfo } from '@/api/files'
import { openCreateFlodersModal } from '@/components/functionalCallableComp'
import { computeSortedFiles } from '../sortedFiles'
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
import { getVisibleRange } from '@/util/scroller'
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
    previewing,
    previewIdx,
    sortMethod
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

  let autoRefreshVersion = 0
  let pendingAutoRefresh: (() => void) | undefined
  // 全屏预览期间发现更新时只提醒一次，避免每个轮询周期都弹一条提示
  let previewUpdateNotified = false
  const invalidateAutoRefresh = () => {
    autoRefreshVersion++
    pendingAutoRefresh = undefined
  }
  const notifyPreviewPendingUpdate = () => {
    if (previewUpdateNotified) return
    previewUpdateNotified = true
    message.info(t('autoUpdatePendingPreview'))
  }
  /**
   * 更新列表后，正在全屏查看的预览组件是否还会存在。
   *
   * 全屏预览不是独立组件：它挂在列表项自己的 a-image 上，而列表项由虚拟滚动按索引复用、
   * 图片容器又以 fullpath 做 key。所以只有当「更新后的列表在 previewIdx 位置上还是同一个
   * fullpath」时，那个 a-image 实例才不会被卸载、预览才不会被销毁。
   */
  const previewSurvivesListUpdate = (nextFiles: FileNodeInfo[]) => {
    if (!previewing.value) return true
    const idx = previewIdx.value
    if (idx < 0) return false
    const current = sortedFiles.value[idx]
    if (!current) return false
    const next = computeSortedFiles(
      nextFiles,
      sortMethod.value,
      global.fileTypeFilter,
      deletedFiles.value
    )
    return next[idx]?.fullpath === current.fullpath
  }
  const previewSurvivesWalkerUpdate = (nextWalker: { images: FileNodeInfo[] }) => {
    if (!previewing.value) return true
    const idx = previewIdx.value
    if (idx < 0) return false
    const current = sortedFiles.value[idx]
    if (!current) return false
    // walk 模式下 sortedFiles 直接取 walker.images（过滤本地删除），不重新排序
    const next = nextWalker.images.filter((v) => !deletedFiles.value.has(v.fullpath))
    return next[idx]?.fullpath === current.fullpath
  }
  /**
   * @param defer 是否推迟到预览关闭后再应用。传 false = 走 legacy 行为，立即更新。
   *              默认按“当前是否在预览”决定。
   */
  const applyOrDeferAutoRefresh = (version: number, update: () => void, defer = previewing.value) => {
    if (version !== autoRefreshVersion) return
    if (defer) {
      pendingAutoRefresh = update
      // 预览期间列表不会动，告诉用户"有新内容，关掉预览就会刷新"
      notifyPreviewPendingUpdate()
    } else {
      update()
    }
  }

  watch(previewing, (visible) => {
    // 每次预览会话允许提醒一次
    previewUpdateNotified = false
    if (visible) return
    const update = pendingAutoRefresh
    pendingAutoRefresh = undefined
    update?.()
  }, { flush: 'post' })
  // 切换目录、模式或手动刷新后，旧请求和预取结果都不能再覆盖当前列表。
  watch([currLocation, currPage, walker, () => props.value.mode], invalidateAutoRefresh, { flush: 'sync' })
  // 关掉自动刷新开关之后，之前预取到、还没应用的结果也不能再落盘。
  watch(
    () => [global.autoRefreshNormalFixedMode, global.autoRefreshWalkMode],
    ([normalFixed, walkMode]) => {
      if (!normalFixed || !walkMode) {
        invalidateAutoRefresh()
      }
    }
  )
  onUnmounted(invalidateAutoRefresh)

  const refresh = makeAsyncFunctionSingle(async () => {
    invalidateAutoRefresh()
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
  const lazyRefresh = (async () => {
    // 预览期间也照常拉取（预览关闭时列表能立刻是新的），但结果只缓冲不应用；
    // 长时间预览时每个轮询周期都会请求一次，这是有意为之的预取取舍。
    // 注意：这里只递增版本号、不清空 pendingAutoRefresh —— 已有缓冲要留到新请求
    // 真正拿到结果时再替换（新请求失败或没变化时，之前预取的结果仍然有效）。
    const version = ++autoRefreshVersion
    if (props.value.mode === 'walk' && walker.value) {
      const currentWalker = walker.value
      const currpos = getVisibleRange(scroller.value).end || 64
      if (global.autoRefreshWalkMode &&
        currpos < global.autoRefreshWalkModePosLimit &&
        await currentWalker.isExpired() && version === autoRefreshVersion) {
        const taskCancelled = ref(false)
        const onDisable = () => {
          taskCancelled.value = true
          invalidateAutoRefresh()
          global.autoRefreshWalkMode = false
          hide()
          message.success(t('walkModeAutoRefreshDisabled'))
        }
        const hide = message.loading(h('span', {}, [
          t('autoUpdate'),
          h('span', { onClick: onDisable, style: { paddingLeft: '16px', cursor: 'pointer', color: 'var(--primary-color)' } }, t('disable'))
        ]), 0)
        try {
          const updatePromise = currentWalker.seamlessRefresh(currpos, taskCancelled).then((newWalker) => {
            if (taskCancelled.value) return
            // 新 walker 下预览项还在同一个位置 → 立即更新（legacy），否则推迟
            applyOrDeferAutoRefresh(version, () => {
              walker.value = newWalker
              eventEmitter.value.emit('loadNextDir') // 确认铺满和更新tag，显示期间还能执行
            }, !previewSurvivesWalkerUpdate(newWalker))
          })
          await Promise.all([updatePromise, delay(1500)]) // 最少显示1.5s
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
      // 先比较有没有变化：没变化就不缓冲、也不提醒
      const currFiles = last(stack.value)!.files
      if (currFiles.map((v) => v.date).join() === files.map((v) => v.date).join()) {
        return
      }
      // 更新后预览组件还能活下来 → 直接更新（legacy 行为）；会被卸载 → 推迟到预览关闭
      applyOrDeferAutoRefresh(version, () => {
        last(stack.value)!.files = files
        message.success(t('autoUpdate'))
      }, !previewSurvivesListUpdate(files))
    } finally {
      np.value?.done()
    }
  })

  // 轮询和全局事件都没有调用方接收错误，这里兜底，避免自动刷新失败变成 unhandled rejection
  const lazyRefreshSafely = () =>
    lazyRefresh().catch((error) => {
      console.error('auto refresh failed', error)
    })

  useGlobalEventListen('returnToIIB', lazyRefreshSafely)

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
    ...usePollRefresh(lazyRefreshSafely)
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
