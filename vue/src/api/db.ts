import { Dict } from '@/util'
import type { FileNodeInfo } from './files'
import { axiosInst } from './index'
import { PageCursor } from 'vue3-ts-util'

export interface Tag {
  name: string
  id: number | string
  display_name: string | null
  type: string
  color: string
  count: number
}

export type DataBaseBasicInfo = {
  img_count: number
  tags: Tag[]
  expired: boolean
  expired_dirs: string[]
}

export const getDbBasicInfo = async () => {
  const resp = await axiosInst.value.get('/db/basic_info')
  return resp.data as DataBaseBasicInfo
}

export const getExpiredDirs = async () => {
  const resp = await axiosInst.value.get('/db/expired_dirs')
  return resp.data as Pick<DataBaseBasicInfo, 'expired' | 'expired_dirs'>
}

export const updateImageData = async () => {
  await axiosInst.value.post('/db/update_image_data', {}, { timeout: Infinity })
}

export const updateTag = async (tag: Tag) => {
  await axiosInst.value.post('/db/update_tag', tag)
}

export type TagId = number | string
export interface MatchImageByTagsReq {
  folder_paths_str?: string
  and_tags: TagId[]
  or_tags: TagId[]
  not_tags: TagId[]
  random_sort?: boolean
}

export const getImagesByTags = async (req: MatchImageByTagsReq, cursor: string) => {
  const resp = await axiosInst.value.post('/db/match_images_by_tags', {
    ...req,
    folder_paths: (req.folder_paths_str ?? '').split(/,|\n/).map(v => v.trim()).filter(v => v),
    cursor
  })
  return resp.data as {
    files: FileNodeInfo[],
    cursor: PageCursor
  }
}

export const addCustomTag = async (req: { tag_name: string }) => {
  const resp = await axiosInst.value.post('/db/add_custom_tag', req)
  return resp.data as Tag
}

export const toggleCustomTagToImg = async (req: { tag_id: TagId; img_path: string }) => {
  const resp = await axiosInst.value.post('/db/toggle_custom_tag_to_img', req)
  return resp.data as { is_remove: boolean }
}

export const removeCustomTag = async (req: { tag_id: TagId }) => {
  await axiosInst.value.post('/db/remove_custom_tag', req)
}

export const removeCustomTagToImg = async (req: { tag_id: TagId; img_id: TagId }) => {
  await axiosInst.value.post('/db/add_custom_tag_from_img', req)
}

export const getImageSelectedCustomTag = async (path: string) => {
  const resp = await axiosInst.value.get('/db/img_selected_custom_tag', { params: { path } })
  return resp.data as Tag[]
}


export const getRandomImages = async () => {
  const resp = await axiosInst.value.get('/db/random_images')
  return resp.data as FileNodeInfo[]
}

export interface SearchBySubstrReq {
  surstr: string;
  cursor: string;
  regexp: string;
  path_only?: boolean;
  folder_paths?: string[];
  size?: number;
  media_type?: string;  // "all", "image", "video"
}


export const getImagesBySubstr = async (req: SearchBySubstrReq) => {
  const resp = await axiosInst.value.post('/db/search_by_substr', req)
  return resp.data as {
    files: FileNodeInfo[],
    cursor: PageCursor
  }
}

const extraPaths = '/db/extra_paths'
export type ExtraPathType =  'scanned' | 'walk' | 'cli_access_only' | '' | 'scanned-fixed'

export interface ExtraPathModel {
  path: string
  alias?: string
  types: ExtraPathType[]
}

export const getExtraPath = async () => {
  const resp = await axiosInst.value.get(extraPaths)
  return resp.data as ExtraPathModel[]
}

export const addExtraPath = async (model: ExtraPathModel) => {
  await axiosInst.value.post(extraPaths, model)
}
export const removeExtraPath = async (req: ExtraPathModel) => {
  await axiosInst.value.delete(extraPaths, { data: req })
}

export interface ExtraPathAliasModel {
  path: string
  alias: string
}

export const aliasExtraPath = async (model: ExtraPathAliasModel) => {
  await axiosInst.value.post('/db/alias_extra_path', model)
}

export const batchGetTagsByPath = async (paths: string[]) => {
  const resp = await axiosInst.value.post('/db/get_image_tags', { paths })
  return resp.data as Dict<Tag[]>
}

export const rebuildImageIndex = () => axiosInst.value.post('/db/rebuild_index')

export interface BatchUpdateTagParams {
  img_paths: string[]
  action: 'add' | 'remove'
  tag_id: number
}

export const batchUpdateImageTag = (data: BatchUpdateTagParams) => {
  return axiosInst.value.post('/db/batch_update_image_tag', data)
}

export interface RenameFileParams {
  path: string
  name: string
}

export const renameFile = async  (data: RenameFileParams) => {
  const resp = await axiosInst.value.post('/db/rename', data)
  return resp.data  as Promise<{ new_path:string }>
}

// ===== Natural language topic clustering =====
export interface BuildIibOutputEmbeddingsReq {
  folder?: string
  model?: string
  force?: boolean
  batch_size?: number
  max_chars?: number
  /** If true, include files in subfolders. Default true for backward compatibility. */
  recursive?: boolean
}

export interface BuildIibOutputEmbeddingsResp {
  folder: string
  count: number
  updated: number
  skipped: number
  model: string
}

export const buildIibOutputEmbeddings = async (req: BuildIibOutputEmbeddingsReq) => {
  const resp = await axiosInst.value.post('/db/build_iib_output_embeddings', req)
  return resp.data as BuildIibOutputEmbeddingsResp
}

export interface ClusterIibOutputReq {
  folder?: string
  folder_paths?: string[]
  model?: string
  force_embed?: boolean
  threshold?: number
  batch_size?: number
  max_chars?: number
  min_cluster_size?: number
  lang?: string
  // advanced (backend-supported; optional)
  force_title?: boolean
  use_title_cache?: boolean
  assign_noise_threshold?: number
}

export interface ClusterIibOutputResp {
  folder: string
  model: string
  threshold: number
  min_cluster_size: number
  count: number
  clusters: Array<{
    id: string
    title: string
    size: number
    paths: string[]
    sample_prompt: string
  }>
  noise: string[]
}

// ===== Async clustering job (progress polling) =====
export interface ClusterIibOutputJobStartResp {
  job_id: string
}

export interface ClusterIibOutputJobStatusResp {
  job_id: string
  status: 'queued' | 'running' | 'done' | 'error'
  stage?: string
  folders?: string[]
  progress?: {
    // embedding totals
    scanned?: number
    to_embed?: number
    embedded_done?: number
    updated?: number
    skipped?: number
    folder?: string
    // clustering
    items_total?: number
    items_done?: number
    // titling
    clusters_total?: number
    clusters_done?: number
  }
  error?: string
  result?: ClusterIibOutputResp
}

export const startClusterIibOutputJob = async (req: ClusterIibOutputReq) => {
  const resp = await axiosInst.value.post('/db/cluster_iib_output_job_start', req)
  return resp.data as ClusterIibOutputJobStartResp
}

export const getClusterIibOutputJobStatus = async (job_id: string) => {
  const resp = await axiosInst.value.get('/db/cluster_iib_output_job_status', { params: { job_id } })
  return resp.data as ClusterIibOutputJobStatusResp
}

export interface ClusterIibOutputCachedResp {
  cache_key: string
  cache_hit: boolean
  cached_at?: string
  stale: boolean
  stale_reason?: {
    folders_changed?: boolean
    reason?: string
    path?: string
    stored?: string
    current?: string
    embeddings_changed?: boolean
    embeddings_count?: number
    embeddings_max_updated_at?: string
  }
  result?: ClusterIibOutputResp | null
}

export const getClusterIibOutputCached = async (req: ClusterIibOutputReq) => {
  const resp = await axiosInst.value.post('/db/cluster_iib_output_cached', req)
  return resp.data as ClusterIibOutputCachedResp
}

// ===== Natural language prompt query (RAG-like retrieval) =====
export interface PromptSearchReq {
  query: string
  folder?: string
  folder_paths?: string[]
  model?: string
  top_k?: number
  min_score?: number
  ensure_embed?: boolean
  max_chars?: number
}

export interface PromptSearchResp {
  query: string
  folder: string
  model: string
  count: number
  top_k: number
  results: Array<{
    id: number
    path: string
    score: number
    sample_prompt: string
  }>
}

export const searchIibOutputByPrompt = async (req: PromptSearchReq) => {
  const resp = await axiosInst.value.post('/db/search_iib_output_by_prompt', req, { timeout: Infinity })
  return resp.data as PromptSearchResp
}

// ===== Hierarchical Tag Graph =====
export interface TagGraphReq {
  folder_paths: string[]
  lang?: string
}

export interface LayerNode {
  id: string
  label: string
  size: number
  metadata?: {
    type: string
    image_count?: number
    cluster_count?: number
    level?: number
  }
}

export interface GraphLayer {
  level: number
  name: string
  nodes: LayerNode[]
}

export interface GraphLink {
  source: string
  target: string
  weight: number
}

export interface TagGraphResp {
  layers: GraphLayer[]
  links: GraphLink[]
  stats: {
    total_clusters: number
    selected_clusters: number
    total_tags: number
    selected_tags: number
    abstraction_layers: number
    total_links: number
    topic_cluster_cache_key?: string
  }
}

export const getClusterTagGraph = async (req: TagGraphReq) => {
  // Large datasets can take longer to build / transfer; keep a generous timeout.
  const resp = await axiosInst.value.post('/db/cluster_tag_graph', req, { timeout: 300000 })
  return resp.data as TagGraphResp
}

export interface TagGraphClusterPathsReq {
  topic_cluster_cache_key: string
  cluster_id: string
}

export interface TagGraphClusterPathsResp {
  paths: string[]
}

export const getClusterTagGraphClusterPaths = async (req: TagGraphClusterPathsReq) => {
  const resp = await axiosInst.value.post('/db/cluster_tag_graph_cluster_paths', req, { timeout: 300000 })
  return resp.data as TagGraphClusterPathsResp
}