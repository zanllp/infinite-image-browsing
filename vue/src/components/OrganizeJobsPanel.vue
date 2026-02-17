<template>
  <div class="organize-jobs-wrapper">
    <!-- 浮动提示按钮：当面板关闭但有任务时显示 -->
    <div
      v-if="globalStore.activeOrganizeJobs.length && !globalStore.showOrganizePanel"
      class="organize-jobs-fab"
      @click="globalStore.showOrganizePanel = true"
    >
      <span class="fab-icon">📁</span>
      <a-badge :count="globalStore.activeOrganizeJobs.length" :offset="[-2, 2]" />
    </div>

    <!-- 主面板 -->
    <div v-if="globalStore.activeOrganizeJobs.length && globalStore.showOrganizePanel" class="organize-jobs-panel">
    <div class="panel-header">
      <span class="panel-title">{{ t('organizeJobs') }}</span>
      <div class="panel-header-right">
        <a-badge :count="globalStore.activeOrganizeJobs.length" />
        <a-button type="text" size="small" class="close-btn" @click="globalStore.showOrganizePanel = false">✕</a-button>
      </div>
    </div>

    <div v-for="job in globalStore.activeOrganizeJobs" :key="job.job_id" class="job-item">
      <div class="job-header">
        <span class="job-icon">📁</span>
        <span class="job-folders">{{ formatFolders(job.folder_paths) }}</span>
        <a-tag :color="statusColor(job.status)">{{ statusText(job.status) }}</a-tag>
      </div>

      <div class="job-progress">
        <!-- Clustering stage -->
        <template v-if="isClusteringStage(job)">
          <div class="stage-label">{{ stageText(job.progress?.stage) }}</div>
          <a-progress
            :percent="clusteringPercent(job.progress)"
            :status="job.status === 'error' ? 'exception' : 'active'"
            size="small"
          />
        </template>

        <!-- Preview ready -->
        <template v-if="job.status === 'preview_ready'">
          <div class="preview-ready-hint">
            {{ t('organizePreviewReady', {
              files: job.preview?.total_files ?? 0,
              clusters: job.preview?.clusters?.length ?? 0
            }) }}
          </div>
          <a-button type="primary" size="small" @click="openPreview(job)">
            {{ t('viewPreview') }}
          </a-button>
        </template>

        <!-- Moving stage -->
        <template v-if="job.progress?.stage === 'moving'">
          <div class="stage-label">{{ t('movingFiles') }}</div>
          <a-progress
            :percent="movingPercent(job.progress)"
            size="small"
            status="active"
          />
          <div class="current-file" v-if="job.progress.current_file">
            {{ job.progress.current_file }}
          </div>
        </template>

        <!-- Done -->
        <template v-if="job.status === 'done'">
          <div class="result-summary success">
            {{ t('organizeComplete', {
              moved: job.progress?.moved_done ?? 0,
              folders: job.progress?.created_folders?.length ?? 0
            }) }}
          </div>
          <a-button size="small" @click="dismiss(job.job_id)">{{ t('close') }}</a-button>
        </template>

        <!-- Error -->
        <template v-if="job.status === 'error'">
          <div class="result-summary error">
            {{ t('organizeFailed') }}
          </div>
          <a-button size="small" @click="dismiss(job.job_id)">{{ t('close') }}</a-button>
        </template>
      </div>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useGlobalStore } from '@/store/useGlobalStore'
import { t } from '@/i18n'
import type { OrganizeJobProgress, OrganizeFilesPreviewResp } from '@/api/organize'

interface OrganizeJob {
  job_id: string
  status: string
  progress: OrganizeJobProgress
  startedAt: number
  folder_paths: string[]
  preview?: OrganizeFilesPreviewResp
}

const globalStore = useGlobalStore()
const emit = defineEmits<{
  'open-preview': [job: OrganizeJob]
}>()

const formatFolders = (paths: string[]) => {
  if (!paths || paths.length === 0) return ''
  if (paths.length === 1) {
    return paths[0].split(/[/\\]/).pop() || paths[0]
  }
  return `${paths.length} ${t('folders')}`
}

const statusColor = (status: string) => ({
  queued: 'default',
  running: 'processing',
  preview_ready: 'warning',
  moving: 'processing',
  done: 'success',
  error: 'error'
}[status] || 'default')

const statusText = (status: string) => ({
  queued: t('queued'),
  running: t('analyzing'),
  preview_ready: t('waitingConfirm'),
  moving: t('moving'),
  done: t('completed'),
  error: t('error')
}[status] || status)

const stageText = (stage?: string) => {
  if (!stage) return ''
  return ({
    embedding: t('generatingEmbeddings'),
    clustering: t('clusteringAnalysis'),
    titling: t('generatingTitles'),
    preview_ready: t('previewReady'),
    moving: t('movingFiles'),
    done: t('completed')
  }[stage] || stage)
}

const isClusteringStage = (job: OrganizeJob) => {
  return ['queued', 'running'].includes(job.status) &&
         ['embedding', 'clustering', 'titling'].includes(job.progress?.stage || '')
}

const clusteringPercent = (progress?: OrganizeJobProgress) => {
  if (!progress) return 0
  const { stage, embedded_done, to_embed, clusters_done, clusters_total } = progress
  if (stage === 'embedding' && to_embed > 0) {
    return Math.round(embedded_done / to_embed * 40)
  }
  if (stage === 'clustering') {
    return 50
  }
  if (stage === 'titling' && clusters_total > 0) {
    return 60 + Math.round(clusters_done / clusters_total * 40)
  }
  return 0
}

const movingPercent = (progress?: OrganizeJobProgress) => {
  if (!progress) return 0
  const { moved_done, moved_total } = progress
  if (moved_total > 0) {
    return Math.round(moved_done / moved_total * 100)
  }
  return 0
}

const openPreview = (job: OrganizeJob) => {
  // Deep clone to ensure all reactive properties are captured
  const jobCopy = JSON.parse(JSON.stringify(job))
  console.log('openPreview called', 'job_id:', jobCopy.job_id, 'status:', jobCopy.status, 'preview:', jobCopy.preview ? `has preview (${jobCopy.preview.total_files} files)` : 'no preview')
  emit('open-preview', jobCopy)
}

const dismiss = (job_id: string) => {
  globalStore.removeOrganizeJob(job_id)
}
</script>

<style scoped lang="scss">
.organize-jobs-fab {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 56px;
  height: 56px;
  background: var(--zp-primary-background);
  border-radius: 50%;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  z-index: 1000;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--zp-border);
  transition: transform 0.2s, box-shadow 0.2s;

  &:hover {
    transform: scale(1.05);
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.25);
  }

  .fab-icon {
    font-size: 24px;
  }
}

.organize-jobs-panel {
  position: fixed;
  bottom: 20px;
  right: 20px;
  width: 360px;
  background: var(--zp-primary-background);
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  z-index: 1000;
  max-height: 400px;
  overflow-y: auto;
  border: 1px solid var(--zp-border);
}

.panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--zp-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 500;

  .panel-header-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .close-btn {
    padding: 0 4px;
    min-width: auto;
    color: var(--zp-secondary-text);

    &:hover {
      color: var(--zp-primary-text);
    }
  }
}

.job-item {
  padding: 12px 16px;
  border-bottom: 1px solid var(--zp-border-light);

  &:last-child {
    border-bottom: none;
  }
}

.job-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.job-icon {
  font-size: 16px;
}

.job-folders {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.stage-label {
  font-size: 12px;
  color: var(--zp-secondary-text);
  margin-bottom: 4px;
}

.current-file {
  font-size: 11px;
  color: var(--zp-tertiary-text);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result-summary {
  padding: 8px;
  border-radius: 4px;
  margin-bottom: 8px;
  font-size: 13px;

  &.success {
    background: rgba(82, 196, 26, 0.1);
    color: #52c41a;
  }

  &.error {
    background: rgba(255, 77, 79, 0.1);
    color: #ff4d4f;
  }
}

.preview-ready-hint {
  font-size: 13px;
  color: var(--zp-secondary-text);
  margin-bottom: 8px;
}
</style>
