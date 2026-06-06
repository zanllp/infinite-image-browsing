import { type FileTransferTabPane, type Shortcut  } from '@/store/useGlobalStore'
import { useMouseInElement } from '@vueuse/core'
import { ref } from 'vue'
import { genInfoCompleted, getImageGenerationInfo, openFolder, openWithDefaultApp, setImgPath } from '@/api'
import {
  delay,
  useWatchDocument} from 'vue3-ts-util'
import {
  createReactiveQueue,
  isImageFile,
  copy2clipboardI18n} from '@/util'
import { type FileNodeInfo, deleteFiles, moveFiles, copyFiles } from '@/api/files'
import { last, range, uniqueId } from 'lodash-es'
import * as Path from '@/util/path'
import { Checkbox, Modal, message } from 'ant-design-vue'
import type { MenuInfo } from 'ant-design-vue/lib/menu/src/interface'
import { t } from '@/i18n'
import { batchUpdateImageTag, toggleCustomTagToImg } from '@/api/db'
import { downloadFileInfoJSON, downloadFiles, toRawFileUrl } from '@/util/file'
import { getShortcutStrFromEvent } from '@/util/shortcut'
import { MultiSelectTips, openAddNewTagModal, openRenameFileModal } from '@/components/functionalCallableComp'
import { batchDownload, events, imgTransferBus, stackCache, tagStore, useEventListen, useHookShareState, global } from '.'
import { closeImageFullscreenPreview, openImageFullscreenPreview } from '@/util/imagePreviewOperation'
import { openTiktokViewWithFiles } from '@/util/tiktokHelper'


export function useFileItemActions (
  { openNext }: { openNext: (file: FileNodeInfo) => Promise<void> }
) {
  const showGenInfo = ref(false)
  const imageGenInfo = ref('')
  const {
    sortedFiles,
    previewIdx,
    multiSelectedIdxs,
    stack,
    currLocation,
    spinning,
    previewing,
    scroller,
    stackViewEl,
    eventEmitter,
    props,
    deletedFiles
  } = useHookShareState().toRefs()
  const nor = Path.normalize
  useEventListen('removeFiles', ({ paths, loc }) => {
    if (nor(loc) !== nor(currLocation.value)) {
      return
    }
    const top = last(stack.value)
    if (!top) {
      return
    }
    paths.forEach(path => deletedFiles.value.add(path))
    paths.filter(isImageFile).forEach(path => deletedFiles.value.add(path.replace(/\.\w+$/, '.txt')))
  })

  useEventListen('addFiles', ({ files, loc }) => {
    if (nor(loc) !== nor(currLocation.value)) {
      return
    }
    const top = last(stack.value)
    if (!top) {
      return
    }

    top.files.unshift(...files)
  })

  const q = createReactiveQueue()
  const onFileItemClick = async (e: MouseEvent, file: FileNodeInfo, idx: number) => {
    previewIdx.value = idx
    global.fullscreenPreviewInitialUrl = toRawFileUrl(file)
    const idxInSelected = multiSelectedIdxs.value.indexOf(idx)
    if (e.shiftKey) {
      if (idxInSelected !== -1) {
        multiSelectedIdxs.value.splice(idxInSelected, 1)
      } else {
        multiSelectedIdxs.value.push(idx)
        multiSelectedIdxs.value.sort((a, b) => a - b)
        const first = multiSelectedIdxs.value[0]
        const last = multiSelectedIdxs.value[multiSelectedIdxs.value.length - 1]
        multiSelectedIdxs.value = range(first, last + 1)
      }
      e.stopPropagation()
    } else if (e.ctrlKey || e.metaKey) {
      if (idxInSelected !== -1) {
        multiSelectedIdxs.value.splice(idxInSelected, 1)
      } else {
        multiSelectedIdxs.value.push(idx)
      }
      e.stopPropagation()
    } else if (file.type === 'dir') {
      await openNext(file)
    }
  }

  const onContextMenuClick = async (e: MenuInfo, file: FileNodeInfo, idx: number) => {
    const url = toRawFileUrl(file)
    const path = currLocation.value
    const preset = { IIB_container_id: parent.IIB_container_id } 

    /**
     * 获取选中的图片信息
     *  选中的图片信息数组
     */
    const getSelectedImg = () => {
      let selectedFiles: FileNodeInfo[] = []
      if (multiSelectedIdxs.value.includes(idx)) {
        // 如果索引已被选中，则获取所有已选中的图片信息
        selectedFiles = multiSelectedIdxs.value.map((idx) => sortedFiles.value[idx])
      } else {
        // 否则，只获取当前图片信息
        selectedFiles.push(file)
      }
      return selectedFiles
    }
    const copyImgTo = async (tab: ['txt2img', 'img2img', 'inpaint', 'extras'][number]) => {
      if (spinning.value) {
        return
      }
      try {
        spinning.value = true
        await setImgPath(file.fullpath) // 设置图像路径
        imgTransferBus.postMessage({ ...preset, event: 'click_hidden_button', btnEleId: 'iib_hidden_img_update_trigger' }) // 触发图像组件更新
        // ok(await genInfoCompleted(), 'genInfoCompleted timeout') // 等待消息生成完成
        await genInfoCompleted() // 等待消息生成完成
        imgTransferBus.postMessage({ ...preset, event: 'click_hidden_button', btnEleId: `iib_hidden_tab_${tab}` }) // 触发粘贴
      } catch (error) {
        console.error(error)
        message.error('发送图像失败，请携带console的错误消息找开发者')
      } finally {
        spinning.value = false
      }
    }
    const key = `${e.key}`
    
    if (key.startsWith('toggle-tag-')) {
      const tagId = +key.split('toggle-tag-')[1]
      const { is_remove } = await toggleCustomTagToImg({ tag_id: tagId, img_path: file.fullpath })
      const tag = global.conf?.all_custom_tags.find((v) => v.id === tagId)?.name!
      await tagStore.refreshTags([file.fullpath])
      message.success(t(is_remove ? 'removedTagFromImage' : 'addedTagToImage', { tag }))
      return
    } else if (key === 'add-custom-tag') {
      openAddNewTagModal()
    } else if (key.startsWith('batch-add-tag-') || key.startsWith('batch-remove-tag-')) {
      const tagId = +key.split('-tag-')[1]
      const action = key.includes('add') ? 'add' : 'remove'
      const paths = getSelectedImg().map(v => v.fullpath)
      await batchUpdateImageTag({
        tag_id: tagId,
        img_paths: paths,
        action
      })
      await tagStore.refreshTags(paths)
      message.success(t(action === 'add' ? 'addCompleted' : 'removeCompleted'))
      return 
    } else if (key.startsWith('copy-to-')){
      const targetPath = key.split('copy-to-')[1]
      const selectedFiles = getSelectedImg()
      const paths = selectedFiles.map((v) => v.fullpath)
      await copyFiles(paths, targetPath, true)
      events.emit('addFiles', { files: selectedFiles, loc: targetPath })
      message.success(t('copySuccess'))
      return
    } else if (key.startsWith('move-to-')){
      const targetPath = key.split('move-to-')[1]
      const selectedFiles = getSelectedImg()
      const paths = selectedFiles.map((v) => v.fullpath)
      await moveFiles(paths, targetPath, true)
      events.emit('removeFiles', { paths, loc: currLocation.value })
      events.emit('addFiles', { files: selectedFiles, loc: targetPath })
      message.success(t('moveSuccess'))
      return
    }

    switch (e.key) {
      case 'previewInNewWindow':
        return window.open(url)
      case 'copyFilePath':
        return copy2clipboardI18n(file.fullpath)
      case 'saveSelectedAsJson':
        return downloadFileInfoJSON(getSelectedImg())
      case 'openWithDefaultApp':
        return openWithDefaultApp(file.fullpath)
      case 'download':{
        const selectedFiles = getSelectedImg()
        downloadFiles(selectedFiles.map(file => toRawFileUrl(file, true)))
        break
      }
      case 'copyPreviewUrl': {
        return copy2clipboardI18n(parent.document.location.origin + url)
      }
      case 'rename': {
        let newPath = await openRenameFileModal(file.fullpath)
        newPath = Path.normalize(newPath)
        const map = tagStore.tagMap
        map.set(newPath, map.get(file.fullpath) ?? [])
        map.delete(file.fullpath)
        file.fullpath = newPath
        file.name =  newPath.split(/[\\/]/).pop() ?? ''
        return 
      }
      case 'send2txt2img':
        return copyImgTo('txt2img')
      case 'send2img2img':
        return copyImgTo('img2img')
      case 'send2inpaint':
        return copyImgTo('inpaint')
      case 'send2extras':
        return copyImgTo('extras')
      case 'send2savedDir': {
        const dir = global.quickMovePaths.find((v) => v.key === 'outdir_save')
        if (!dir) {
          return message.error(t('unknownSavedDir'))
        }
        const absolutePath = Path.normalizeRelativePathToAbsolute(dir.dir, global.conf?.sd_cwd!)
        const selectedImg = getSelectedImg()
        await moveFiles(
          selectedImg.map((v) => v.fullpath),
          absolutePath,
          true
        )
        events.emit('removeFiles', {
          paths: selectedImg.map((v) => v.fullpath),
          loc: currLocation.value
        })
        events.emit('addFiles', { files: selectedImg, loc: absolutePath })
        break
      }
      case 'send2controlnet-img2img':
      case 'send2controlnet-txt2img': {
        const type = e.key.split('-')[1] as 'img2img' | 'txt2img'
        imgTransferBus.postMessage({ ...preset, event: 'send_to_control_net', type, url: toRawFileUrl(file) })
        break
      }
      case 'send2outpaint': {

        imageGenInfo.value = await q.pushAction(() => getImageGenerationInfo(file.fullpath)).res
        const [prompt, negPrompt] = (imageGenInfo.value || '').split('\n')
        imgTransferBus.postMessage({
          ...preset,
          event: 'send_to_outpaint',
          url: toRawFileUrl(file),
          prompt,
          negPrompt: negPrompt.slice('Negative prompt: '.length)
        })

        break
      }
      case 'openWithWalkMode': {
        stackCache.set(path, stack.value)
        const tab = global.tabList[props.value.tabIdx]
        const pane: FileTransferTabPane = {
          type: 'local',
          key: uniqueId(),
          path: file.fullpath,
          name: t('local'),
          stackKey: path,
          mode: 'walk'
        }
        tab.panes.push(pane)
        tab.key = pane.key
        break
      }
      case 'openFileLocationInNewTab':
      case 'openInNewTab': {
        const tab = global.tabList[props.value.tabIdx]
        const pane: FileTransferTabPane = {
          type: 'local',
          key: uniqueId(),
          path: e.key === 'openInNewTab' ? file.fullpath : Path.getParentDirectory(file.fullpath),
          name: t('local'),
          mode: 'scanned-fixed'
        }
        tab.panes.push(pane)
        tab.key = pane.key
        break
      }
      case 'openOnTheRight': {
        stackCache.set(path, stack.value)
        let tab = global.tabList[props.value.tabIdx + 1]
        if (!tab) {
          tab = { panes: [], key: '', id: uniqueId() }
          global.tabList[props.value.tabIdx + 1] = tab
        }
        const targetPath = file.type === 'dir'
          ? file.fullpath
          : Path.getParentDirectory(file.fullpath)
        const pane: FileTransferTabPane = {
          type: 'local',
          key: uniqueId(),
          path: targetPath,
          name: t('local'),
          stackKey: path,
          mode: props.value.mode ?? 'scanned'
        }
        tab.panes.push(pane)
        tab.key = pane.key
        break
      }
      case 'send2BatchDownload': {
        batchDownload.addFiles(getSelectedImg())
        break
      }
      case 'viewGenInfo': {
        showGenInfo.value = true
        imageGenInfo.value = await q.pushAction(() => getImageGenerationInfo(file.fullpath)).res
        break
      }
      case 'tiktokView': {
        // 打开 TikTok 式观看，从当前文件开始
        openTiktokViewWithFiles(sortedFiles.value, idx)
        break
      }
      case 'openWithLocalFileBrowser': {
        await openFolder(file.fullpath)
        break
      }
      case 'deleteFiles': {
        const selectedFiles = getSelectedImg()
        const removeFile = async () => {
          const paths = selectedFiles.map((v) => v.fullpath)
          await deleteFiles(paths)
          message.success(t('deleteSuccess'))
          if (previewing.value) {
            const isFullscreenFirst = toRawFileUrl(file) === global.fullscreenPreviewInitialUrl
            const isEnd = previewIdx.value === sortedFiles.value.length - 1
            if (isFullscreenFirst || isEnd) {
              closeImageFullscreenPreview()
              await delay(100)
              if (isFullscreenFirst && sortedFiles.value.length > 1) {
                const nextIdx = previewIdx.value
                delay(0).then(() => openImageFullscreenPreview(nextIdx, stackViewEl.value!))
              }
            }
          }
          events.emit('removeFiles', { paths: paths, loc: currLocation.value })
        }
        if (selectedFiles.length === 1 && global.ignoredConfirmActions.deleteOneOnly) {
          return removeFile()
        }
        await new Promise<void>((resolve) => {
          Modal.confirm({
            title: t('confirmDelete'),
            maskClosable: true,
            width: '60vw',
            content:() =>
              <div>
                <ol style={{ maxHeight: '50vh', overflow: 'auto' }}>
                  {selectedFiles.map((v) => <li>{v.fullpath.split(/[/\\]/).pop()}</li>)}
                </ol>
                <MultiSelectTips />
                <Checkbox v-model:checked={global.ignoredConfirmActions.deleteOneOnly}>{t('deleteOneOnlySkipConfirm')} ({t('resetOnGlobalSettingsPage')})</Checkbox>
              </div>,
            async onOk () {
              await removeFile()
              resolve()
            }
          })
        })
        break
      }
    }
    return {}
  }

  const { isOutside } = useMouseInElement(stackViewEl)

  useWatchDocument('keydown', (e) => {
    const isEditableTarget = (target: EventTarget | null) => {
      const el = target as HTMLElement | null
      if (!el) {
        return false
      }
      const tagName = el.tagName?.toLowerCase()
      return tagName === 'input' || tagName === 'textarea' || el.isContentEditable
    }
    const keysStr = getShortcutStrFromEvent(e)
    if (previewing.value) {
      if (keysStr === 'Esc') {
        closeImageFullscreenPreview()
      }
      const action = Object.entries(global.shortcut).find(
        (v) => v[1] === keysStr && v[1]
      )?.[0] as keyof Shortcut
      if (action) {
        e.stopPropagation()
        e.preventDefault()
        const idx = previewIdx.value
        const file = sortedFiles.value[idx]
        switch (action) {
          case 'delete': {
            return onContextMenuClick({ key: 'deleteFiles' } as MenuInfo, file, idx)
          }
          case 'download': {
            return onContextMenuClick({ key: 'download' } as MenuInfo, file, idx)
          }
          default: {
            const name = /^toggle_tag_(.*)$/.exec(action)?.[1]
            const tag = global.conf?.all_custom_tags.find((v) => v.name === name)
            if (tag) {
              return onContextMenuClick({ key: `toggle-tag-${tag.id}` } as MenuInfo, file, idx)
            }
            if (action.startsWith('copy_to_')) {
              const path = action.split('copy_to_')[1]
              return onContextMenuClick({ key: `copy-to-${path}` } as MenuInfo, file, idx)
            }
            if (action.startsWith('move_to_')) {
              const path = action.split('move_to_')[1]
              return onContextMenuClick({ key: `move-to-${path}` } as MenuInfo, file, idx)
              
            }
          }
        }
      }
    } else if (!isOutside.value && !isEditableTarget(e.target)) {
      if (!e.altKey && !e.ctrlKey && !e.metaKey) {
        const s = scroller.value
        const total = sortedFiles.value.length
        const maxIdx = Math.max(total - 1, 0)
        const scrollToIndex = (idx: number) => {
          if (!s || total === 0) {
            return
          }
          const target = Math.min(Math.max(idx, 0), maxIdx)
          s.scrollToItem(target)
        }
        switch (e.key) {
          case 'PageUp': {
            e.preventDefault()
            e.stopPropagation()
            const step = s ? Math.max(s.$_endIndex - s.$_startIndex, 1) : 1
            const curr = s?.$_startIndex ?? 0
            return scrollToIndex(curr - step)
          }
          case 'PageDown': {
            e.preventDefault()
            e.stopPropagation()
            const step = s ? Math.max(s.$_endIndex - s.$_startIndex, 1) : 1
            const curr = s?.$_startIndex ?? 0
            return scrollToIndex(curr + step)
          }
          case 'Home': {
            e.preventDefault()
            e.stopPropagation()
            return scrollToIndex(0)
          }
          case 'End': {
            e.preventDefault()
            e.stopPropagation()
            return scrollToIndex(maxIdx)
          }
          case 'Backspace': {
            e.preventDefault()
            e.stopPropagation()
            return eventEmitter.value.emit('navigateUp')
          }
        }
      }
      if (['Ctrl + KeyA', 'Cmd + KeyA'].includes(keysStr)) {
        e.preventDefault()
        e.stopPropagation()
        eventEmitter.value.emit('selectAll')
      }
    }
  })

  return {
    onFileItemClick,
    onContextMenuClick,
    showGenInfo,
    imageGenInfo,
    q
  }
}