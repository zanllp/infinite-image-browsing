import type { FileTransferTabPane, TabPane, useGlobalStore, TagSearchMatchedImageGridTabPane, TopicSearchMatchedImageGridTabPane, GridViewTabPane, ImgSliTabPane, TagSearchTabPane, FuzzySearchTabPane } from './store/useGlobalStore'
import { Dict, removeQueryParams, switch2IIB } from './util'
import { uniqueId } from 'lodash-es'
import { getParentDirectory, basename, normalize } from './util/path'

const createPaneFromType = (type: TabPane['type'], props: any): TabPane | null => {
  const base = {
    key: uniqueId(),
    name: props.name ?? ''
  }

  switch (type) {
    case 'local': {
      const pane: FileTransferTabPane = {
        ...base,
        type,
        path: props.path,
        mode: props.mode,
        stackKey: props.stackKey,
        targetFile: props.targetFile,
        openPreview: props.openPreview
      }
      return pane
    }
    case 'tag-search': {
      const pane: TagSearchTabPane = {
        ...base,
        type,
        searchScope: props.searchScope
      }
      return pane
    }
    case 'fuzzy-search': {
      const pane: FuzzySearchTabPane = {
        ...base,
        type,
        searchScope: props.searchScope,
        initialSubstr: props.substr,
        initialIsRegex: props.isRegex,
        initialPathOnly: props.pathOnly,
        initialMediaType: props.mediaType,
        autoSearch: props.autoSearch
      }
      return pane
    }
    case 'tag-search-matched-image-grid': {
      const pane: TagSearchMatchedImageGridTabPane = {
        ...base,
        type,
        selectedTagIds: props.selectedTagIds,
        id: props.id ?? uniqueId()
      }
      return pane
    }
    case 'topic-search-matched-image-grid': {
      const pane: TopicSearchMatchedImageGridTabPane = {
        ...base,
        type,
        id: props.id ?? uniqueId(),
        title: props.title ?? '',
        paths: props.paths ?? []
      }
      return pane
    }
    case 'grid-view': {
      const pane: GridViewTabPane = {
        ...base,
        type,
        files: props.files ?? [],
        removable: props.removable,
        allowDragAndDrop: props.allowDragAndDrop
      }
      return pane
    }
    case 'img-sli': {
      const pane: ImgSliTabPane = {
        ...base,
        type,
        left: props.left,
        right: props.right
      }
      return pane
    }
    case 'random-image': {
      const pane: TabPane = {
        ...base,
        type
      }
      return pane
    }
    case 'topic-search':
    case 'batch-download':
    case 'workspace-snapshot':
    case 'global-setting': {
      const pane: TabPane = {
        ...base,
        type
      }
      return pane
    }
    default:
      return null
  }
}

export const resolveQueryActions = async (g: ReturnType<typeof useGlobalStore>) => {
  const paths = g.conf?.global_setting
  const params = new URLSearchParams(parent.location.search)
  const action = params.get('action')

  switch (action) {
    case 'view': {
      // Quick view action: open image in fullscreen preview
      // Usage: ?action=view&path=/path/to/image.png
      let imagePath = params.get('path')
      if (!imagePath) {
        console.error('[IIB] view action requires path parameter')
        return
      }
      imagePath = normalize(imagePath)

      // Get parent folder and use scanned-fixed mode
      const folderPath = getParentDirectory(imagePath)
      const imageName = basename(imagePath)

      const tab = g.tabList[0]
      const pane: FileTransferTabPane = {
        type: 'local',
        path: folderPath,
        key: uniqueId(),
        name: imageName,
        mode: 'scanned-fixed',
        targetFile: imagePath,
        openPreview: true
      }

      tab.panes.unshift(pane)
      tab.key = pane.key
      switch2IIB()
      removeQueryParams(['action', 'path'])
      break
    }
    case 'open': {
      let path = params.get('path')

      if (!path || !paths) return
      const map: Dict<string> = {
        extra: paths.outdir_extras_samples,
        save: paths.outdir_save,
        txt2img: paths.outdir_txt2img_samples,
        img2img: paths.outdir_img2img_samples
      }
      if (map[path]) {
        path = map[path]
      }
      const tab = g.tabList[0]
      const mode = params.get('mode') as FileTransferTabPane['mode']
      const pane: FileTransferTabPane = {
        type: 'local',
        path,
        key: uniqueId(),
        name: '',
        mode: (['scanned', 'walk', 'scanned-fixed'] as const).includes(mode || 'scanned') ? mode : 'scanned'
      }
      tab.panes.unshift(pane)
      tab.key = pane.key
      switch2IIB()
      removeQueryParams(['action', 'path', 'mode'])
      break
    }
    case 'pane': {
      const type = params.get('type') as TabPane['type']
      const propsJson = params.get('props')

      // Validate pane type
      const validTypes: TabPane['type'][] = [
        'local',
        'tag-search',
        'fuzzy-search',
        'tag-search-matched-image-grid',
        'topic-search-matched-image-grid',
        'grid-view',
        'img-sli',
        'random-image',
        'topic-search',
        'batch-download',
        'workspace-snapshot',
        'global-setting',
        'empty'
      ]

      if (!type || !validTypes.includes(type)) {
        console.error('[IIB] Invalid or missing pane type:', type)
        return
      }

      let props: any = {}
      try {
        if (propsJson) {
          props = JSON.parse(decodeURIComponent(propsJson))
        }
      } catch (e) {
        console.error('[IIB] Failed to parse pane props:', e)
        return
      }

      const pane = createPaneFromType(type, props)
      if (pane) {
        const tab = g.tabList[0]
        tab.panes.unshift(pane)
        tab.key = pane.key
        switch2IIB()
      }
      removeQueryParams(['action', 'type', 'props'])
      break
    }
  }
}
