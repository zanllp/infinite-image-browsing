<script setup lang="ts">
import { computed, ref } from 'vue'
import { toImageUrl } from '@/util/file'
import type { FileNodeInfo } from '@/api/files'
import { DragOutlined } from '@/icon'

interface Props {
  file: FileNodeInfo
  title?: string
  size?: string
}

const props = withDefaults(defineProps<Props>(), {
  title: 'Drag to transfer image'
})

const imageUrl = computed(() => {
  if (!props.file) return ''
  return toImageUrl(props.file)
})

const showImage = ref(false)

function toggleImage() {
  showImage.value = !showImage.value
}
const closeImage = () => {
  showImage.value=false
}
</script>

<template>
  <div class="draggable-image-wrapper"
        @mouseleave="closeImage">
    <div class="trigger-container" :title="title" @click="toggleImage" >
      <slot class="trigger-slot">
        <div class="default-trigger">
          <DragOutlined class="trigger-icon" />
          <span class="trigger-text">{{ $t('dragImageToTransfer') }}</span>
        </div>
      </slot>
      <img
        v-if="showImage"
        :src="imageUrl"
        :alt="file.name"
        draggable="true"
        class="hover-image"
        :style="{ width: size||'256px', height: size ||  '256px' }"
        @mouseleave="closeImage"
        @error="(e) => (e.target as HTMLImageElement).style.display = 'none'"
        @click.stop
      />
    </div>
  </div>
</template>

<style scoped lang="scss">
.draggable-image-wrapper {
  padding: 4px 0;
  margin-top: 8px;
  display: flex;
  cursor: pointer;
  justify-content: center;
  align-items: center;

  .trigger-container {
    display: inline-flex;
    align-items: center;
    position: relative;
    user-select: none;

    :deep(.trigger-slot) {
      display: inline-flex;
      align-items: center;
      cursor: pointer;
      transition: all 0.3s ease;
    }

    &:active :deep(.trigger-slot) {
      transform: scale(0.95);
    }

    :deep(.custom-drag-trigger) {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 6px 24px;
      border-radius: 8px;
      background: var(--zp-primary-background);
      color: var(--zp-primary);
      transition: all 0.3s ease;
      position: relative;
      overflow: hidden;

      // 静态多彩渐变边框
      &::before {
        content: '';
        position: absolute;
        inset: 0;
        border-radius: 8px;
        padding: 2px;
        background: linear-gradient(
          135deg,
          #ff6b6b 0%,
          #feca57 16.67%,
          #48dbfb 33.33%,
          #ff9ff3 50%,
          #54a0ff 66.67%,
          #5f27cd 83.33%,
          #ff6b6b 100%
        );
        -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        opacity: 0.9;
      }

      &:hover {
        background: linear-gradient(
          135deg,
          rgba(255, 107, 107, 0.1),
          rgba(72, 219, 251, 0.1)
        );
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
        transform: translateY(-1px);
      }

      &:active {
        transform: translateY(0);
      }

      .trigger-icon {
        font-size: 18px;
        transition: transform 0.3s ease;
        position: relative;
        z-index: 1;
      }

      .trigger-text {
        font-size: 12px;
        white-space: nowrap;
        position: relative;
        z-index: 1;
        letter-spacing: 0.5px;
      }

      &:hover .trigger-icon {
        transform: scale(1.1) rotate(5deg);
      }
    }

    .default-trigger {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 6px 24px;
      border-radius: 8px;
      background: var(--zp-primary-background);
      color: var(--zp-primary);
      transition: all 0.3s ease;
      position: relative;
      overflow: hidden;

      // 静态多彩渐变边框
      &::before {
        content: '';
        position: absolute;
        inset: 0;
        border-radius: 8px;
        padding: 2px;
        background: linear-gradient(
          135deg,
          #ff6b6b 0%,
          #feca57 16.67%,
          #48dbfb 33.33%,
          #ff9ff3 50%,
          #54a0ff 66.67%,
          #5f27cd 83.33%,
          #ff6b6b 100%
        );
        -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
        -webkit-mask-composite: xor;
        mask-composite: exclude;
        opacity: 0.9;
      }

      &:hover {
        background: linear-gradient(
          135deg,
          rgba(255, 107, 107, 0.1),
          rgba(72, 219, 251, 0.1)
        );
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
        transform: translateY(-1px);
      }

      &:active {
        transform: translateY(0);
      }

      .trigger-icon {
        font-size: 18px;
        transition: transform 0.3s ease;
        position: relative;
        z-index: 1;
      }

      .trigger-text {
        font-size: 14px;
        font-weight: 500;
        white-space: nowrap;
        position: relative;
        z-index: 1;
        letter-spacing: 0.5px;
      }

      &:hover .trigger-icon {
        transform: scale(1.1) rotate(5deg);
      }
    }

    .hover-image {
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%) scale(1);
      object-fit: contain;
      border-radius: 8px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
      background: var(--zp-primary-background);
      opacity: 1;
      pointer-events: auto;
      animation: scaleIn 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      z-index: 10000;
      cursor: grab;

      @keyframes scaleIn {
        from {
          transform: translate(-50%, -50%) scale(0.8);
          opacity: 0;
        }
        to {
          transform: translate(-50%, -50%) scale(1);
          opacity: 1;
        }
      }

      &:active {
        cursor: grabbing;
      }
    }
  }
}
</style>
