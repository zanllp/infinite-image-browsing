import { Modal, message } from 'ant-design-vue'
import { checkPathExists } from '@/api'
import { copyFiles, moveFiles } from '@/api/files'
import { t } from '@/i18n'
import { events, tagStore } from '.'

interface TransferArgs {
  paths: string[]
  /** folder (or 'search-result') the files are shown in */
  from: string
  dest: string
  createDestFolder?: boolean
  continueOnError?: boolean
}

const showFailures = (action: 'move' | 'copy', total: number, done: number, errors: string[]) => {
  Modal.warning({
    title: t(action === 'move' ? 'moveIncomplete' : 'copyIncomplete'),
    width: '60vw',
    content: () => (
      <div>
        <div>{t(action === 'move' ? 'moveSummary' : 'copySummary', { done, total, failed: total - done })}</div>
        <ol style={{ maxHeight: '50vh', overflow: 'auto', wordBreak: 'break-all' }}>
          {errors.map((v) => <li>{v}</li>)}
        </ol>
      </div>
    )
  })
}

/**
 * Moves the files, then updates the views: the moved files leave the source view, and views that
 * show the target folder are reloaded. Files that could not be moved (e.g. name conflicts) stay where
 * they are and are listed at the end. If the request fails (e.g. it stops at the first error), files
 * moved before that are removed from the view all the same, then the error is rethrown.
 */
export const moveFilesAndUpdateViews = async ({ paths, from, dest, createDestFolder, continueOnError }: TransferArgs) => {
  let errors: string[] = []
  try {
    errors = (await moveFiles(paths, dest, createDestFolder, continueOnError)).errors ?? []
  } catch (error) {
    await syncViewsAfterPartialMove(paths, from, dest)
    throw error
  }
  if (!errors.length) {
    events.emit('removeFiles', { paths, loc: from })
    tagStore.invalidateTagCache()
    events.emit('refreshLocation', { loc: dest })
    message.success(t('moveSuccess'))
    return
  }
  const moved = await syncViewsAfterPartialMove(paths, from, dest)
  showFailures('move', paths.length, moved?.length ?? paths.length - errors.length, errors)
}

/**
 * The response does not say which files were moved: a source path that is gone was moved.
 * If that cannot be checked, the source view is reloaded instead.
 */
const syncViewsAfterPartialMove = async (paths: string[], from: string, dest: string) => {
  let moved: string[] | undefined
  try {
    const exists = await checkPathExists(paths)
    moved = paths.filter((v) => !exists[v])
    events.emit('removeFiles', { paths: moved, loc: from })
  } catch (error) {
    console.error('check_path_exists failed', error)
    events.emit('refreshLocation', { loc: from })
  }
  if (moved === undefined || moved.length > 0) tagStore.invalidateTagCache()
  events.emit('refreshLocation', { loc: dest })
  return moved
}

/** Copies the files, then reloads the views of the target folder; files that could not be copied are listed at the end. */
export const copyFilesAndUpdateViews = async ({ paths, dest, createDestFolder, continueOnError }: TransferArgs) => {
  let errors: string[] = []
  try {
    errors = (await copyFiles(paths, dest, createDestFolder, continueOnError)).errors ?? []
  } catch (error) {
    events.emit('refreshLocation', { loc: dest })
    throw error
  }
  events.emit('refreshLocation', { loc: dest })
  if (!errors.length) {
    message.success(t('copySuccess'))
    return
  }
  showFailures('copy', paths.length, paths.length - errors.length, errors)
}
