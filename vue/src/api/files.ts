import { Dict } from '@/util'
import { axiosInst } from '.'

export interface FileNodeInfo {
  size: string
  type: 'file' | 'dir'
  created_time: string
  name: string
  date: string
  bytes: number
  fullpath: string
  is_under_scanned_path: boolean
  gen_info_raw?: string
  gen_info_obj?: object
  cover_url?: string
}

export interface GenDiffInfo {
  empty: boolean
  ownFile: string
  otherFile: string
  diff: any
}

export const getTargetFolderFiles = async (folder_path: string) => {
  const resp = await axiosInst.value.get('/files', { params: { folder_path } })
  return resp.data as { files: FileNodeInfo[] }
}


export const deleteFiles = async (file_paths: string[], permanent = false) => {
  const resp = await axiosInst.value.post('/delete_files', { file_paths, permanent })
  return resp.data as { ok: true; trashed?: boolean }
}

/** 打开系统回收站窗口（仅 Windows，需后端支持） */
export const openRecycleBin = async () => {
  const resp = await axiosInst.value.post('/open_recycle_bin')
  return resp.data as { ok: true }
}

export const moveFiles = async (
  file_paths: string[],
  dest: string,
  create_dest_folder?: boolean,
  continue_on_error?: boolean
) => {
  const resp = await axiosInst.value.post('/move_files', { file_paths, dest, create_dest_folder, continue_on_error })
  return resp.data as { files: FileNodeInfo[] }
}

export const copyFiles = async (
  file_paths: string[],
  dest: string,
  create_dest_folder?: boolean,
  continue_on_error?: boolean
) => {
  const resp = await axiosInst.value.post('/copy_files', { file_paths, dest, create_dest_folder, continue_on_error })
  return resp.data as { files: FileNodeInfo[] }
}

export const mkdirs = async (dest_folder: string) => {
  await axiosInst.value.post('/mkdirs', { dest_folder })
}


export const  batchGetFilesInfo = async (paths: string[]) => {
  const resp = await axiosInst.value.post('/batch_get_files_info', { paths }) 
  return  resp.data as Dict<FileNodeInfo>
}