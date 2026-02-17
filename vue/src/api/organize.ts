/**
 * Smart file organization API
 * Organizes scattered image files into folders based on semantic clustering
 */

import { axiosInst } from './index'

// ========== Type Definitions ==========

export interface OrganizeFileMapping {
  src_path: string
  dest_folder_name: string
  dest_path: string
  cluster_id: string
  is_noise: boolean
}

export interface OrganizePreviewCluster {
  cluster_id: string
  suggested_folder_name: string
  title: string
  keywords: string[]
  size: number
  file_mappings: OrganizeFileMapping[]
}

export interface OrganizeFilesReq {
  folder_paths: string[]
  dest_folder?: string
  threshold?: number
  min_cluster_size?: number
  lang?: string
  recursive?: boolean
  folder_naming?: 'title' | 'keywords' | 'id'
  action?: 'move' | 'copy'
  handle_noise?: 'skip' | 'unsorted' | 'leave'
  noise_folder_name?: string
  max_folder_name_length?: number
}

export interface OrganizeJobProgress {
  stage: string
  embedded_done: number
  to_embed: number
  clusters_done: number
  clusters_total: number
  moved_done: number
  moved_total: number
  current_file: string
  created_folders: string[]
  errors: Array<{ src?: string; dest?: string; error: string; count?: number }>
}

export interface OrganizeFilesPreviewResp {
  job_id: string
  dest_folder: string
  total_files: number
  clusters: OrganizePreviewCluster[]
  noise: OrganizePreviewCluster
  all_mappings: OrganizeFileMapping[]
}

export interface OrganizeJobStatusResp {
  job_id: string
  status: 'queued' | 'running' | 'preview_ready' | 'moving' | 'done' | 'error'
  progress: OrganizeJobProgress
  preview?: OrganizeFilesPreviewResp
  result?: {
    moved_count: number
    created_folders: string[]
    errors: Array<{ src?: string; dest?: string; error: string; count?: number }>
  }
  error?: string
  req?: OrganizeFilesReq
}

export interface OrganizeFolderEdit {
  cluster_id: string
  new_folder_name: string
}

export interface OrganizeFilesConfirmReq {
  job_id: string
  folder_edits?: OrganizeFolderEdit[]
  skip_cluster_ids?: string[]
}

// ========== API Calls ==========

/**
 * Start a file organization task (runs in background)
 * @returns job_id for progress polling
 */
export const startOrganizeFiles = async (req: OrganizeFilesReq) => {
  const resp = await axiosInst.value.post('/db/organize_files_start', req)
  return resp.data as { job_id: string }
}

/**
 * Get the status and progress of an organization task
 * Status flow: queued -> running -> preview_ready -> (confirm) -> moving -> done
 */
export const getOrganizeFilesStatus = async (job_id: string) => {
  const resp = await axiosInst.value.get('/db/organize_files_status', { params: { job_id } })
  return resp.data as OrganizeJobStatusResp
}

/**
 * Confirm and execute file organization
 * Can modify folder names via folder_edits
 * Can skip clusters via skip_cluster_ids
 */
export const confirmOrganizeFiles = async (req: OrganizeFilesConfirmReq) => {
  const resp = await axiosInst.value.post('/db/organize_files_confirm', req)
  return resp.data as { ok: boolean; job_id: string }
}
