<script setup lang="ts">
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { exportUserData, importUserData } from '@/api/db'
import type { UserDataExportDoc, UserDataImportStats } from '@/api/db'
import { globalEvents } from '@/util'

const globalStore = useGlobalStore()
const readonly = computed(() => globalStore.conf?.is_readonly === true)

const pad = (n: number) => String(n).padStart(2, '0')
const fileStamp = () => {
  const d = new Date()
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
}

const formatSize = (bytes: number) => {
  if (bytes >= 1024 * 1024) {
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  }
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

const errMsg = (e: unknown) => {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail === 'string' && detail) {
    return detail
  }
  if (detail) {
    return JSON.stringify(detail)
  }
  return e instanceof Error ? e.message : String(e)
}

// ===== export =====
const exporting = ref(false)

const onExport = async () => {
  if (exporting.value) {
    return
  }
  exporting.value = true
  try {
    const doc = await exportUserData()
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `iib-user-data-${fileStamp()}.json`
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
    const summary = doc.summary
    message.success(
      t('dataBackupExportSuccess', {
        tags: summary?.tags ?? 0,
        images: summary?.tagged_images ?? 0,
        size: formatSize(blob.size)
      })
    )
  } catch (e) {
    message.error(`${t('dataBackupExportFailed')}: ${errMsg(e)}`)
  } finally {
    exporting.value = false
  }
}

// ===== import =====
const importVisible = ref(false)
const analyzing = ref(false)
const importing = ref(false)
const fileName = ref('')
const pendingDoc = ref<UserDataExportDoc | null>(null)
const preview = ref<UserDataImportStats | null>(null)
const result = ref<UserDataImportStats | null>(null)
const mode = ref<'merge' | 'overwrite'>('merge')
const fileInput = ref<HTMLInputElement | null>(null)

const resetImport = () => {
  analyzing.value = false
  importing.value = false
  fileName.value = ''
  pendingDoc.value = null
  preview.value = null
  result.value = null
  mode.value = 'merge'
  if (fileInput.value) {
    fileInput.value.value = ''
  }
}

const openImport = () => {
  resetImport()
  importVisible.value = true
}

const closeImport = () => {
  if (importing.value) {
    return
  }
  importVisible.value = false
}

const previewDoc = async () => {
  if (!pendingDoc.value) {
    return
  }
  analyzing.value = true
  preview.value = null
  result.value = null
  try {
    preview.value = await importUserData({ data: pendingDoc.value, mode: mode.value, dry_run: true })
  } catch (e) {
    message.error(errMsg(e))
    pendingDoc.value = null
    fileName.value = ''
  } finally {
    analyzing.value = false
  }
}

const loadFile = async (file: File) => {
  analyzing.value = true
  pendingDoc.value = null
  preview.value = null
  result.value = null
  fileName.value = file.name
  try {
    const text = await file.text()
    const doc = JSON.parse(text) as UserDataExportDoc
    if (!doc || doc.format !== 'iib-user-data') {
      message.error(t('dataBackupInvalidFile'))
      fileName.value = ''
      return
    }
    pendingDoc.value = doc
    await previewDoc()
  } catch (e) {
    message.error(`${t('dataBackupInvalidFile')}: ${errMsg(e)}`)
    fileName.value = ''
  } finally {
    analyzing.value = false
  }
}

const onFileChange = async (ev: Event) => {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    await loadFile(file)
  }
}

const onDrop = async (ev: DragEvent) => {
  const file = ev.dataTransfer?.files?.[0]
  if (file) {
    await loadFile(file)
  }
}

const pickFile = () => fileInput.value?.click()

const onModeChange = () => {
  if (pendingDoc.value) {
    void previewDoc()
  }
}

const runImport = async () => {
  if (!pendingDoc.value || importing.value) {
    return
  }
  importing.value = true
  try {
    result.value = await importUserData({ data: pendingDoc.value, mode: mode.value, dry_run: false })
    preview.value = null
    message.success(t('dataBackupImportSuccess'))
    // 设置 / 标签 / 扫描路径都可能发生变化，刷新全局配置
    globalEvents.emit('updateGlobalSetting')
  } catch (e) {
    message.error(`${t('dataBackupImportFailed')}: ${errMsg(e)}`)
  } finally {
    importing.value = false
  }
}

const settingsChanged = computed(() => {
  const stats = result.value
  if (!stats) {
    return false
  }
  return (
    stats.settings.added + stats.settings.updated > 0 ||
    stats.extra_paths.added + stats.extra_paths.updated > 0
  )
})

const reloadPage = () => window.location.reload()
</script>

<template>
  <div class="data-backup">
    <div class="db-actions">
      <a-button type="primary" :loading="exporting" @click="onExport">
        {{ t('dataBackupExport') }}
      </a-button>
      <a-button :disabled="readonly" @click="openImport">
        {{ t('dataBackupImport') }}
      </a-button>
    </div>
    <div class="db-hint">{{ t('dataBackupDesc') }}</div>
    <div v-if="readonly" class="db-readonly">{{ t('dataBackupReadonlyHint') }}</div>

    <a-modal
      v-model:visible="importVisible"
      :title="t('dataBackupImportTitle')"
      :width="'660px'"
      :footer="null"
      :mask-closable="!importing"
      :closable="!importing"
      :keyboard="!importing"
      @cancel="closeImport"
    >
      <div class="db-modal">
        <input
          ref="fileInput"
          type="file"
          accept=".json,application/json"
          style="display: none"
          @change="onFileChange"
        />

        <!-- result view -->
        <template v-if="result">
          <div class="db-section-title">{{ t('dataBackupResultTitle') }}</div>
          <div class="db-grid">
            <span>{{ t('dataBackupTags') }}</span>
            <span>{{ t('dataBackupTagsCreated', { n: result.tags.created }) }}</span>
            <span>{{ t('dataBackupAssignments') }}</span>
            <span>{{ t('dataBackupAssignmentsAdded', { n: result.tag_assignments.added }) }}</span>
            <span>{{ t('dataBackupImagesMatched') }}</span>
            <span>{{ result.images.matched }}</span>
            <span v-if="result.settings.added + result.settings.updated > 0">{{ t('dataBackupSettings') }}</span>
            <span v-if="result.settings.added + result.settings.updated > 0">
              {{ t('dataBackupSettingsAdded', { n: result.settings.added }) }}
              / {{ t('dataBackupSettingsUpdated', { n: result.settings.updated }) }}
            </span>
            <span v-if="result.pending_tags > 0">{{ t('dataBackupPendingQueued') }}</span>
            <span v-if="result.pending_tags > 0">{{ result.pending_tags }}</span>
            <span v-if="result.pending_applied > 0">{{ t('dataBackupPendingApplied') }}</span>
            <span v-if="result.pending_applied > 0">{{ result.pending_applied }}</span>
          </div>
          <a-alert
            v-if="result.images.unmatched > 0"
            type="info"
            show-icon
            class="db-alert"
            :message="t('dataBackupPendingHint', { n: result.images.unmatched })"
          />
          <a-alert
            v-if="settingsChanged"
            type="warning"
            show-icon
            class="db-alert"
            :message="t('dataBackupNeedReload')"
          />
          <div class="db-modal-actions">
            <a-button @click="closeImport">{{ t('dataBackupClose') }}</a-button>
            <a-button v-if="settingsChanged" type="primary" @click="reloadPage">
              {{ t('dataBackupReloadNow') }}
            </a-button>
          </div>
        </template>

        <!-- pick + preview view -->
        <template v-else>
          <div
            class="db-drop"
            :class="{ 'is-loaded': !!pendingDoc }"
            @click="pickFile"
            @dragover.prevent
            @drop.prevent="onDrop"
          >
            <div class="db-drop-title">{{ pendingDoc ? fileName : t('dataBackupPickFile') }}</div>
            <div class="db-drop-sub">{{ t('dataBackupPickFileHint') }}</div>
          </div>

          <div v-if="analyzing" class="db-analyzing">
            <a-spin size="small" />
            <span>{{ t('dataBackupPreviewing') }}</span>
          </div>

          <template v-if="preview">
            <div class="db-section-title">{{ t('dataBackupPreviewTitle') }}</div>
            <div class="db-grid">
              <span>{{ t('dataBackupExportedAt') }}</span>
              <span>{{ preview.export_created_at || '-' }}</span>
              <span>{{ t('dataBackupSourceImages') }}</span>
              <span>{{ preview.source_image_count ?? '-' }}</span>
              <span>{{ t('dataBackupTags') }}</span>
              <span>
                {{ t('dataBackupTagsCreated', { n: preview.tags.created }) }}
                / {{ t('dataBackupTagsExisting', { n: preview.tags.existing }) }}
                <template v-if="preview.tags.color_updated > 0">
                  / {{ t('dataBackupTagColors', { n: preview.tags.color_updated }) }}
                </template>
              </span>
              <span>{{ t('dataBackupAssignments') }}</span>
              <span>
                {{ t('dataBackupAssignmentsAdded', { n: preview.tag_assignments.added }) }}
                / {{ t('dataBackupAssignmentsExisting', { n: preview.tag_assignments.existing }) }}
              </span>
              <span>{{ t('dataBackupImagesMatched') }}</span>
              <span>
                {{ preview.images.matched }}
                <template v-if="preview.images.unmatched > 0">
                  / {{ t('dataBackupImagesUnmatched', { n: preview.images.unmatched }) }}
                </template>
              </span>
              <span>{{ t('dataBackupSettings') }}</span>
              <span>
                {{ t('dataBackupSettingsAdded', { n: preview.settings.added }) }}
                / {{ t('dataBackupSettingsSkipped', { n: preview.settings.skipped }) }}
              </span>
              <span>{{ t('dataBackupPromptEdits') }}</span>
              <span>{{ t('dataBackupPromptEditsApplied', { n: preview.prompt_edits.applied }) }}</span>
            </div>

            <a-alert
              v-if="preview.images.unmatched > 0"
              type="info"
              show-icon
              class="db-alert"
              :message="t('dataBackupPendingHint', { n: preview.images.unmatched })"
            />

            <div class="db-section-title">{{ t('dataBackupModeLabel') }}</div>
            <a-radio-group v-model:value="mode" @change="onModeChange">
              <a-radio value="merge">{{ t('dataBackupModeMerge') }}</a-radio>
              <a-radio value="overwrite">{{ t('dataBackupModeOverwrite') }}</a-radio>
            </a-radio-group>
            <div class="db-mode-desc">
              {{ mode === 'merge' ? t('dataBackupModeMergeDesc') : t('dataBackupModeOverwriteDesc') }}
            </div>

            <div class="db-modal-actions">
              <a-button :disabled="importing" @click="closeImport">{{ t('dataBackupCancel') }}</a-button>
              <a-button type="primary" :loading="importing" @click="runImport">
                {{ t('dataBackupStartImport') }}
              </a-button>
            </div>
          </template>
        </template>
      </div>
    </a-modal>
  </div>
</template>

<style scoped lang="scss">
.data-backup {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.db-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.db-hint {
  font-size: 12px;
  color: var(--zp-secondary);
  line-height: 1.6;
  max-width: 720px;
}

.db-readonly {
  font-size: 12px;
  color: #d03f0a;
}

.db-modal {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.db-section-title {
  font-weight: 600;
  margin-top: 4px;
}

.db-drop {
  border: 1px dashed var(--zp-secondary);
  border-radius: 8px;
  padding: 20px 16px;
  text-align: center;
  cursor: pointer;
  background: var(--zp-secondary-background);
  transition: border-color 0.2s;

  &:hover,
  &.is-loaded {
    border-color: var(--primary-color);
  }

  .db-drop-title {
    font-weight: 600;
    word-break: break-all;
  }

  .db-drop-sub {
    margin-top: 6px;
    font-size: 12px;
    color: var(--zp-secondary);
  }
}

.db-analyzing {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--zp-secondary);
}

.db-grid {
  display: grid;
  grid-template-columns: minmax(140px, auto) 1fr;
  gap: 6px 16px;
  font-size: 13px;

  & > span:nth-child(odd) {
    color: var(--zp-secondary);
  }
}

.db-alert {
  margin: 0;
}

.db-mode-desc {
  font-size: 12px;
  color: var(--zp-secondary);
  line-height: 1.6;
}

.db-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}
</style>
