<template>
  <a-modal
    v-model:visible="globalStore.showSmartOrganizeConfig"
    :title="t('smartOrganizeConfig')"
    width="70vw"
    @ok="handleOk"
    @cancel="handleCancel"
    :confirmLoading="loading"
  >
    <!-- Info panel -->
    <div class="info-panel">
      <div class="info-row">
        <span class="info-label">{{ t('organizeSourceFolder') }}:</span>
        <code class="info-path">{{ globalStore.smartOrganizeConfigPath }}</code>
      </div>
      <div class="info-tips">
        <span>{{ t('smartOrganizeNotice') }}</span>
        <span class="separator">|</span>
        <span>{{ t('topicSearchRequirementsOpenai') }}</span>
        <span class="separator">|</span>
        <span>{{ t('topicSearchRequirementsDepsPython') }}</span>
      </div>
    </div>

    <a-form :label-col="{ span: 8 }" :wrapper-col="{ span: 16 }">
      <!-- Target folder display -->
      <a-form-item :label="t('organizeTargetFolder')">
        <a-input v-model:value="config.destFolder" :placeholder="globalStore.smartOrganizeConfigPath" :disabled="loading" />
        <div class="form-item-hint">{{ t('organizeTargetFolderDesc') }}</div>
      </a-form-item>

      <!-- Recursive option -->
      <a-form-item :label="t('organizeRecursive')">
        <a-switch v-model:checked="config.recursive" :disabled="loading" />
        <div class="form-item-hint">{{ t('organizeRecursiveDesc') }}</div>
      </a-form-item>

      <!-- Min cluster size -->
      <a-form-item :label="t('organizeMinClusterSize')">
        <a-input-number v-model:value="config.minClusterSize" :min="2" :max="20" style="width: 100px" :disabled="loading" />
        <div class="form-item-hint">{{ t('organizeMinClusterSizeDesc') }}</div>
      </a-form-item>

      <!-- Action type -->
      <a-form-item :label="t('organizeAction')">
        <a-radio-group v-model:value="config.action" :disabled="loading">
          <a-radio value="move">{{ t('organizeActionMove') }}</a-radio>
          <a-radio value="copy">{{ t('organizeActionCopy') }}</a-radio>
        </a-radio-group>
      </a-form-item>
    </a-form>

    <!-- Loading status -->
    <div v-if="loading" class="loading-status">
      <a-spin size="small" />
      <span style="margin-left: 8px">{{ loadingText }}</span>
    </div>

    <template #footer>
      <a-button @click="handleCancel" :disabled="loading">{{ t('organizeCancel') }}</a-button>
      <a-button type="primary" @click="handleOk" :loading="loading">{{ t('organizeStartTask') }}</a-button>
    </template>
  </a-modal>
</template>

<script setup lang="ts">
import { reactive, watch, ref } from 'vue'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { startSmartOrganizeWithConfig, type SmartOrganizeConfig } from '@/util/smartOrganize'
import { buildIibOutputEmbeddings } from '@/api/db'

const globalStore = useGlobalStore()

const loading = ref(false)
const loadingText = ref('')

const config = reactive<SmartOrganizeConfig>({
  destFolder: '',
  recursive: false,
  minClusterSize: 3,
  action: 'move'
})

// Reset config when modal opens
watch(() => globalStore.showSmartOrganizeConfig, (val) => {
  if (val) {
    config.destFolder = ''
    config.recursive = false
    config.minClusterSize = 3
    config.action = 'move'
    loading.value = false
    loadingText.value = ''
  }
})

const handleOk = async () => {
  const folderPath = globalStore.smartOrganizeConfigPath

  try {
    loading.value = true

    // Step 1: Update index (use config.recursive to match organize scope)
    loadingText.value = t('updatingIndex')
    await buildIibOutputEmbeddings({ folder: folderPath, recursive: config.recursive })

    // Step 2: Start organize job
    loadingText.value = t('startingOrganizeJob')
    await startSmartOrganizeWithConfig([folderPath], {
      ...config,
      destFolder: config.destFolder || folderPath
    })

    globalStore.showSmartOrganizeConfig = false
  } catch (e: any) {
    console.error('Smart organize failed:', e)
  } finally {
    loading.value = false
    loadingText.value = ''
  }
}

const handleCancel = () => {
  if (!loading.value) {
    globalStore.showSmartOrganizeConfig = false
  }
}
</script>

<style scoped lang="scss">
.info-panel {
  margin-bottom: 16px;
  padding: 10px 12px;
  background: var(--zp-secondary-background);
  border-radius: 4px;
  font-size: 12px;

  .info-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .info-label {
    color: var(--zp-secondary-text);
    flex-shrink: 0;
  }

  .info-path {
    background: var(--zp-primary-background);
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    word-break: break-all;
  }

  .info-tips {
    color: var(--zp-tertiary-text);
    font-size: 11px;
    line-height: 1.5;

    .separator {
      margin: 0 6px;
      opacity: 0.5;
    }
  }
}

.form-item-hint {
  font-size: 12px;
  color: var(--zp-secondary-text);
  margin-top: 4px;
}

.loading-status {
  display: flex;
  align-items: center;
  padding: 12px;
  background: var(--zp-secondary-background);
  border-radius: 4px;
  margin-top: 8px;
}
</style>
