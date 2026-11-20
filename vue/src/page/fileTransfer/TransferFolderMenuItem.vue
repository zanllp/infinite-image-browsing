<script setup lang="ts">
import { FolderOpenOutlined } from '@/icon'
import type { MenuInfo } from 'ant-design-vue/lib/menu/src/interface'

const props = defineProps<{ mode: 'copy' | 'move', folder: string, label: string }>()
const emit = defineEmits<{ (event: 'browse', info: MenuInfo & { folder: string }): void }>()
function browse(domEvent: MouseEvent) {
  const key = props.mode === 'move' ? 'moveToOtherFolder' : 'copyToOtherFolder'
  emit('browse', { key, folder: props.folder, domEvent } as MenuInfo & { folder: string })
}
</script>

<template>
  <a-menu-item :key="`${mode}-to-${folder}`">
    <span class="folder-row">
      <span>{{ label }}</span>
      <a-tooltip :title="$t('chooseSubfolder')" :trigger="['hover', 'focus']">
        <button type="button" :aria-label="`${$t('chooseSubfolder')}: ${label}`"
          @click.stop="browse" @keydown.enter.stop @keydown.space.stop>
          <FolderOpenOutlined />
        </button>
      </a-tooltip>
    </span>
  </a-menu-item>
</template>

<style scoped>
.folder-row { display: flex; align-items: center; justify-content: space-between; }
button { display: grid; place-items: center; width: 26px; height: 26px; margin-left: 12px; border: 0; border-radius: 4px; background: transparent; color: inherit; cursor: pointer; }
button:hover, button:focus-visible { background: var(--primary-color-3); color: var(--primary-color); }
</style>
