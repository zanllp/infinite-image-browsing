<script lang="ts" setup>
import { message } from 'ant-design-vue'
import { ref, watch } from 'vue'
import { t } from '@/i18n'

const props = defineProps<{
  visible: boolean
  title: string
  defaultName?: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'confirm', value: string): void
}>()

const name = ref('')

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      name.value = props.defaultName ?? ''
    }
  },
  { immediate: true }
)

const close = () => emit('update:visible', false)

const confirm = () => {
  const value = name.value.trim()
  if (!value) {
    message.warning(t('imageViewNameRequired'))
    return
  }
  emit('confirm', value)
  close()
}
</script>

<template>
  <a-modal
    :visible="visible"
    :title="title"
    :ok-text="$t('saveButtonSavesTo')"
    destroy-on-close
    @cancel="close"
    @ok="confirm"
  >
    <a-input
      v-model:value="name"
      :placeholder="$t('imageViewName')"
      @press-enter="confirm"
    />
  </a-modal>
</template>
