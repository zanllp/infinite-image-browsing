<template>
  <div class="organize-preview">
    <!-- Image preview tooltip -->
    <div
      v-if="hoverImageUrl"
      class="image-preview-tooltip"
      :style="{ top: tooltipPos.y + 'px', left: tooltipPos.x + 'px' }"
    >
      <img :src="hoverImageUrl" @error="hoverImageUrl = ''" />
    </div>

    <div class="preview-header">
      <h3>{{ t('smartOrganizePreview') }}</h3>
      <div class="summary">
        {{ t('organizePreviewSummary', {
          files: preview.total_files,
          folders: preview.clusters.length + (preview.noise.size > 0 ? 1 : 0)
        }) }}
      </div>
      <div class="dest-folder">
        {{ t('destFolder') }}: <code>{{ preview.dest_folder }}</code>
      </div>
    </div>

    <!-- Cluster list -->
    <a-collapse v-model:activeKey="activeKeys" class="cluster-list">
      <a-collapse-panel
        v-for="cluster in allClusters"
        :key="cluster.cluster_id"
        :class="{ 'skipped': skippedIds.has(cluster.cluster_id) }"
      >
        <template #header>
          <div class="cluster-header">
            <span class="folder-icon">📁</span>
            <span class="folder-name" v-if="editingId !== cluster.cluster_id">
              {{ getFolderName(cluster) }}
            </span>
            <a-input
              v-else
              v-model:value="editingName"
              size="small"
              style="width: 200px"
              @pressEnter="saveEdit"
              @blur="saveEdit"
              @click.stop
            />
            <span class="file-count">({{ cluster.size }} {{ t('files') }})</span>
            <a-tag v-if="cluster.cluster_id === '__noise__'" color="orange">{{ t('unsorted') }}</a-tag>
          </div>
        </template>

        <template #extra>
          <a-space @click.stop>
            <a-button size="small" @click="startEdit(cluster)">
              {{ t('rename') }}
            </a-button>
            <a-button
              size="small"
              :type="skippedIds.has(cluster.cluster_id) ? 'primary' : 'default'"
              @click="toggleSkip(cluster.cluster_id)"
            >
              {{ skippedIds.has(cluster.cluster_id) ? t('cancelSkip') : t('skip') }}
            </a-button>
          </a-space>
        </template>

        <!-- File mappings -->
        <div class="file-mappings">
          <div
            v-for="m in getVisibleMappings(cluster)"
            :key="m.src_path"
            class="file-mapping"
          >
            <span
              class="src-path"
              @mouseenter="showImagePreview($event, m.src_path)"
              @mouseleave="hideImagePreview"
              @mousemove="updateTooltipPos($event)"
            >
              {{ getFileName(m.src_path) }}
            </span>
            <span class="arrow">→</span>
            <span class="dest-path" :title="m.dest_path">
              {{ getFolderName(cluster) }}/{{ getFileName(m.src_path) }}
            </span>
          </div>

          <div
            v-if="cluster.file_mappings.length > 10 && !expandedClusters.has(cluster.cluster_id)"
            class="show-more"
            @click="expandedClusters.add(cluster.cluster_id)"
          >
            {{ t('showMore', { count: cluster.file_mappings.length - 10 }) }}
          </div>
        </div>
      </a-collapse-panel>
    </a-collapse>

    <!-- Actions -->
    <div class="preview-actions">
      <div class="action-summary">
        {{ t('willMove', { count: effectiveFileCount }) }}
        <span v-if="skippedIds.size">({{ t('skipped', { count: skippedFileCount }) }})</span>
      </div>
      <a-space>
        <a-button @click="cancel">{{ t('cancel') }}</a-button>
        <a-button type="primary" :loading="confirming" @click="confirm">
          {{ t('confirmOrganize') }}
        </a-button>
      </a-space>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive } from 'vue'
import { t } from '@/i18n'
import type { OrganizeFilesPreviewResp, OrganizePreviewCluster, OrganizeFolderEdit } from '@/api/organize'
import { confirmOrganizeFiles } from '@/api/organize'
import { message } from 'ant-design-vue'
import { apiBase } from '@/api'

const props = defineProps<{
  preview: OrganizeFilesPreviewResp
}>()

const emit = defineEmits<{
  cancel: []
  confirmed: []
}>()

// State
const activeKeys = ref<string[]>([])
const skippedIds = reactive(new Set<string>())
const folderEdits = reactive(new Map<string, string>())
const expandedClusters = reactive(new Set<string>())
const editingId = ref<string | null>(null)
const editingName = ref('')
const confirming = ref(false)

// Image preview tooltip state
const hoverImageUrl = ref('')
const tooltipPos = reactive({ x: 0, y: 0 })
let hoverTimer: ReturnType<typeof setTimeout> | null = null

// All clusters (including noise)
const allClusters = computed(() => {
  const clusters = [...props.preview.clusters]
  if (props.preview.noise.size > 0) {
    clusters.push(props.preview.noise)
  }
  return clusters
})

// Get folder name (consider user edits)
const getFolderName = (cluster: OrganizePreviewCluster) => {
  return folderEdits.get(cluster.cluster_id) || cluster.suggested_folder_name
}

// Get file name from path
const getFileName = (path: string) => {
  return path.split(/[/\\]/).pop() || path
}

// Get thumbnail URL for image preview
const getThumbnailUrl = (path: string) => {
  return `${apiBase.value}/image-thumbnail?path=${encodeURIComponent(path)}&t=${Date.now()}&size=512x512`
}

// Image preview tooltip handlers
const showImagePreview = (e: MouseEvent, path: string) => {
  // Check if it's an image file
  const ext = path.split('.').pop()?.toLowerCase() || ''
  const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'ico', 'svg']
  if (!imageExts.includes(ext)) {
    return
  }

  // Delay showing the preview
  if (hoverTimer) clearTimeout(hoverTimer)
  hoverTimer = setTimeout(() => {
    hoverImageUrl.value = getThumbnailUrl(path)
    updateTooltipPos(e)
  }, 300)
}

const hideImagePreview = () => {
  if (hoverTimer) {
    clearTimeout(hoverTimer)
    hoverTimer = null
  }
  hoverImageUrl.value = ''
}

const updateTooltipPos = (e: MouseEvent) => {
  // Position tooltip to the right of cursor, with some offset
  tooltipPos.x = e.clientX + 20
  tooltipPos.y = e.clientY - 100
  // Keep tooltip within viewport
  if (tooltipPos.y < 10) tooltipPos.y = 10
}

// Get visible mappings (with expand control)
const getVisibleMappings = (cluster: OrganizePreviewCluster) => {
  if (expandedClusters.has(cluster.cluster_id)) {
    return cluster.file_mappings
  }
  return cluster.file_mappings.slice(0, 10)
}

// Effective file count (excluding skipped)
const effectiveFileCount = computed(() => {
  return allClusters.value
    .filter(c => !skippedIds.has(c.cluster_id))
    .reduce((sum, c) => sum + c.file_mappings.filter(m => m.dest_path !== m.src_path).length, 0)
})

// Skipped file count
const skippedFileCount = computed(() => {
  return allClusters.value
    .filter(c => skippedIds.has(c.cluster_id))
    .reduce((sum, c) => sum + c.size, 0)
})

// Edit folder name
const startEdit = (cluster: OrganizePreviewCluster) => {
  editingId.value = cluster.cluster_id
  editingName.value = getFolderName(cluster)
}

const saveEdit = () => {
  if (editingId.value && editingName.value.trim()) {
    folderEdits.set(editingId.value, editingName.value.trim())
  }
  editingId.value = null
  editingName.value = ''
}

// Toggle skip
const toggleSkip = (clusterId: string) => {
  if (skippedIds.has(clusterId)) {
    skippedIds.delete(clusterId)
  } else {
    skippedIds.add(clusterId)
  }
}

// Cancel
const cancel = () => {
  emit('cancel')
}

// Confirm
const confirm = async () => {
  confirming.value = true
  try {
    const edits: OrganizeFolderEdit[] = []
    folderEdits.forEach((name, id) => {
      edits.push({ cluster_id: id, new_folder_name: name })
    })

    await confirmOrganizeFiles({
      job_id: props.preview.job_id,
      folder_edits: edits.length > 0 ? edits : undefined,
      skip_cluster_ids: skippedIds.size > 0 ? Array.from(skippedIds) : undefined
    })

    message.success(t('startMovingFiles'))
    emit('confirmed')
  } catch (e: any) {
    message.error(`${t('confirmFailed')}: ${e.message || e}`)
  } finally {
    confirming.value = false
  }
}
</script>

<style scoped lang="scss">
.organize-preview {
  padding: 16px;
  max-height: 80vh;
  overflow-y: auto;
}

.preview-header {
  margin-bottom: 16px;

  h3 {
    margin: 0 0 8px 0;
  }

  .summary {
    color: var(--zp-secondary-text);
    margin-bottom: 4px;
  }

  .dest-folder {
    font-size: 12px;
    color: var(--zp-tertiary-text);

    code {
      background: var(--zp-secondary-background);
      padding: 2px 6px;
      border-radius: 4px;
    }
  }
}

.cluster-list {
  margin-bottom: 16px;
}

.cluster-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.folder-icon {
  font-size: 16px;
}

.folder-name {
  font-weight: 500;
}

.file-count {
  color: var(--zp-secondary-text);
  font-size: 12px;
}

.file-mappings {
  font-size: 13px;
}

.file-mapping {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  border-bottom: 1px solid var(--zp-border-light);

  &:last-child {
    border-bottom: none;
  }
}

.src-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--zp-secondary-text);
}

.arrow {
  color: #1890ff;
  flex-shrink: 0;
}

.dest-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #52c41a;
}

.show-more {
  padding: 8px;
  text-align: center;
  color: #1890ff;
  cursor: pointer;

  &:hover {
    background: var(--zp-secondary-background);
  }
}

.skipped {
  opacity: 0.5;
}

.preview-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-top: 16px;
  border-top: 1px solid var(--zp-border);
}

.action-summary {
  color: var(--zp-secondary-text);
}

.image-preview-tooltip {
  position: fixed;
  z-index: 9999;
  pointer-events: none;
  background: var(--zp-primary-background);
  border: 1px solid var(--zp-border);
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  padding: 8px;

  img {
    max-width: 256px;
    max-height: 256px;
    object-fit: contain;
    border-radius: 4px;
    display: block;
  }
}

.src-path {
  cursor: pointer;

  &:hover {
    text-decoration: underline;
  }
}
</style>
