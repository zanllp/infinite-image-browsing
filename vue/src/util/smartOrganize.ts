/**
 * Smart organize utility functions
 * Handles starting organize jobs and polling for status
 */

import { message } from 'ant-design-vue'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { startOrganizeFiles, getOrganizeFilesStatus } from '@/api/organize'
import type { OrganizeFilesReq } from '@/api/organize'
import { globalEvents } from '@/util'
import { useTagStore } from '@/store/useTagStore'
import { normalize } from '@/util/path'

export interface SmartOrganizeConfig {
  destFolder: string
  recursive: boolean
  minClusterSize: number
  action: 'move' | 'copy'
}

/**
 * Open the smart organize config modal
 * @param folderPath - Folder path to organize
 */
export function openSmartOrganizeConfig(folderPath: string) {
  const globalStore = useGlobalStore()
  globalStore.smartOrganizeConfigPath = folderPath
  globalStore.showSmartOrganizeConfig = true
}

/**
 * Start a smart organize job with config from modal
 * @param paths - Array of folder paths or file paths to organize
 * @param config - Config from modal
 */
export async function startSmartOrganizeWithConfig(
  paths: string[],
  config: SmartOrganizeConfig
) {
  const globalStore = useGlobalStore()

  try {
    // Build request with config options
    const req: OrganizeFilesReq = {
      folder_paths: paths,
      lang: globalStore.lang,
      dest_folder: config.destFolder || undefined,
      min_cluster_size: config.minClusterSize,
      action: config.action,
      recursive: config.recursive
    }

    const { job_id } = await startOrganizeFiles(req)

    // Add job to global store for tracking
    globalStore.addOrganizeJob({
      job_id,
      status: 'queued',
      progress: {
        stage: 'embedding',
        embedded_done: 0,
        to_embed: 0,
        clusters_done: 0,
        clusters_total: 0,
        moved_done: 0,
        moved_total: 0,
        current_file: '',
        created_folders: [],
        errors: []
      },
      startedAt: Date.now(),
      folder_paths: paths
    })

    message.success(t('organizeJobStarted'))

    // Start polling for status
    pollOrganizeStatus(job_id, paths)

    return job_id
  } catch (e: any) {
    message.error(`${t('error')}: ${e.message || e}`)
    throw e
  }
}


/**
 * After an organize job moved or copied files: files left the source folders (and their subfolders, a copy
 * leaves them) and arrived in the destination, so the cached tags under both may belong to other files now.
 * The tag cache is dropped (the grids fetch the tags of the files they show) and the views of both folders reload;
 * without a known folder every view reloads (an empty list).
 * @param folderPaths - Folders that were organized
 * @param destFolder - Destination of the job; the first folder if it was not set (like the server)
 */
export function finishOrganize(folderPaths: string[], destFolder?: string) {
  // the server answers with its own path form (backslashes on Windows), the views use slashes
  const folders = [...new Set([...folderPaths, destFolder || folderPaths[0]].filter(Boolean).map(normalize))]
  useTagStore().invalidateTagCache()
  globalEvents.emit('refreshFileView', { paths: folders })
}

/**
 * Poll for organize job status until completion
 * @param job_id - Job ID to poll
 * @param folderPaths - Original folder paths (for refreshing view after completion)
 */
async function pollOrganizeStatus(job_id: string, folderPaths?: string[]) {
  const globalStore = useGlobalStore()
  const pollInterval = 2000 // 2 seconds

  const poll = async () => {
    try {
      const status = await getOrganizeFilesStatus(job_id)
      console.log('Poll response:', 'status:', status.status, 'stage:', status.progress?.stage, 'preview:', status.preview ? `yes (${status.preview.total_files} files, ${status.preview.clusters?.length} clusters)` : 'no')

      // Update job in store with all available data
      const updateData: any = {
        status: status.status,
        progress: status.progress
      }

      // Only include preview if it exists in the response
      if (status.preview) {
        updateData.preview = status.preview
        console.log('Preview data received from API:', status.preview.total_files, 'files')
      }

      globalStore.updateOrganizeJob(job_id, updateData)

      // Continue polling if not done or error
      if (!['done', 'error', 'preview_ready'].includes(status.status)) {
        setTimeout(poll, pollInterval)
      } else {
        console.log('Poll stopped at status:', status.status)
        if (status.status === 'error') {
          console.error('Organize job failed:', status.error)
        } else if (status.status === 'preview_ready') {
          console.log('Preview ready - user can now view and confirm')
        } else if (status.status === 'done') {
          // Refresh file view after completion
          console.log('Organize done, emitting refresh event')
          finishOrganize(folderPaths ?? [], status.preview?.dest_folder ?? status.req?.dest_folder)
        }
      }
    } catch (e) {
      console.error('Poll organize status error:', e)
      // Retry on error
      setTimeout(poll, pollInterval * 2)
    }
  }

  // Start polling
  poll()
}

