import { watch, ref } from 'vue'
import {
  useWatchDocument
} from 'vue3-ts-util'
import { FileTransferData, getFileTransferDataFromDragEvent } from '@/util/file'
import type { FileNodeInfo } from '@/api/files'
import { useHookShareState, global, sli } from '.'
import { copyFilesAndUpdateViews, moveFilesAndUpdateViews } from './transferResult'
import { MultiSelectTips } from '@/components/functionalCallableComp'
import { t } from '@/i18n'
import { createReactiveQueue } from '@/util'
import { Modal, Button, Checkbox } from 'ant-design-vue'
import * as Path from '@/util/path'

import { cloneDeep, uniqBy } from 'lodash-es'

export function useFileTransfer () {
  const { currLocation, sortedFiles, currPage, multiSelectedIdxs, walker } =
    useHookShareState().toRefs()
  const recover = () => {
    multiSelectedIdxs.value = []
  }
  useWatchDocument('click', () => {
    if (!global.keepMultiSelect) {
      recover()
    }
  })
  useWatchDocument('blur', () => {
    if (!global.keepMultiSelect) {
      recover()
    }
  })
  watch(currPage, recover)

  const onFileDragStart = (e: DragEvent, idx: number) => {
    const file = cloneDeep(sortedFiles.value[idx])
    sli.fileDragging = true
    console.log('onFileDragStart set drag file ', e, idx, file)
    const files = [file]
    let includeDir = file.type === 'dir'
    if (multiSelectedIdxs.value.includes(idx)) {
      const selectedFiles = multiSelectedIdxs.value.map((idx) => sortedFiles.value[idx])
      files.push(...selectedFiles)
      includeDir = selectedFiles.some((v) => v.type === 'dir')
    }
    const data: FileTransferData = {
      includeDir,
      loc: currLocation.value || 'search-result',
      path: uniqBy(files, 'fullpath').map((f) => f.fullpath),
      nodes: uniqBy(files, 'fullpath'),
      __id: 'FileTransferData'
    }
    e.dataTransfer!.setData('text/plain', JSON.stringify(data))
  }

  const onFileDragEnd = () => {
    sli.fileDragging = false
  }

  const onDrop = async (e: DragEvent) => {
    if (walker.value) {
      return
    }
    const data = getFileTransferDataFromDragEvent(e)
    if (!data) {
      return
    }
    const toPath = currLocation.value
    if (data.loc === toPath) {
      return
    }
    openMoveOrCopyConfirm(data, toPath)
  }

  const onFileDropToFolder = async (e: DragEvent, target: FileNodeInfo) => {
    if (walker.value || target.type !== 'dir') {
      return false
    }
    const data = getFileTransferDataFromDragEvent(e)
    if (!data) {
      return false
    }
    const fromPath = Path.normalize(data.loc)
    const currPath = Path.normalize(currLocation.value || '')
    if (fromPath !== currPath) {
      return false
    }
    const toPath = Path.normalize(target.fullpath)
    const filtered = data.path
      .map(Path.normalize)
      .filter((p) => p !== toPath && !toPath.startsWith(p + '/'))
    if (!filtered.length) {
      return false
    }
    e.preventDefault()
    openMoveOrCopyConfirm({ ...data, path: filtered }, toPath)
    return true
  }

  const openMoveOrCopyConfirm = (data: FileTransferData, toPath: string) => {
    const q = createReactiveQueue()
    const continueOnError = ref(true)
    const transferArgs = () => ({ paths: data.path, from: data.loc, dest: toPath, continueOnError: continueOnError.value })
    // close only this dialog, not the summary of skipped files; also on failure, its list is outdated then
    const onCopyBtnClick = async () => q.pushAction(async () => {
      try {
        await copyFilesAndUpdateViews(transferArgs())
      } finally {
        modal.destroy()
      }
    })

    const onMoveBtnClick = () => q.pushAction(async () => {
      try {
        await moveFilesAndUpdateViews(transferArgs())
      } finally {
        modal.destroy()
      }
    })
    const modal = Modal.confirm({
      title: t('confirm') + '?',
      width: '60vw',
      content: () => <div>
        <div>
          {`${t('moveSelectedFilesTo')} ${toPath}`}
          <ol style={{ maxHeight: '50vh', overflow: 'auto' }}>
            {data.path.map((v) => <li>{v.split(/[/\\]/).pop()}</li>)}
          </ol>
        </div>
        <MultiSelectTips />
        <div style={{ marginTop: '8px' }}>
          <Checkbox v-model:checked={continueOnError.value}>{t('continueOnError')}</Checkbox>
          <div style={{ color: '#888', fontSize: '12px', marginTop: '4px' }}>{t('continueOnErrorDesc')}</div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end' }} class="actions">
          <Button onClick={() => modal.destroy()}>{t('cancel')}</Button>
          <Button type="primary" loading={!q.isIdle} onClick={onCopyBtnClick}>{t('copy')}</Button>
          <Button type="primary" loading={!q.isIdle} onClick={onMoveBtnClick}>{t('move')}</Button>
        </div>
      </div>,
      maskClosable: true,
      wrapClassName: 'hidden-antd-btns-modal'
    })
  }
  return {
    onFileDragStart,
    onDrop,
    multiSelectedIdxs,
    onFileDragEnd,
    onFileDropToFolder
  }
}
