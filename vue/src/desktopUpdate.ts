import { invoke } from '@tauri-apps/api/tauri'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { Modal } from 'ant-design-vue'
import { once } from 'lodash-es'
import { h, ref } from 'vue'
import { t } from './i18n'

export const releasesUrl = 'https://github.com/zanllp/infinite-image-browsing/releases'
export const desktopUpdateVersion = ref<string | null>(null)
let dialogOpen = false

const checkDesktopUpdate = async () => {
  try {
    desktopUpdateVersion.value = await invoke<string | null>('check_desktop_update')
  } catch (error) {
    desktopUpdateVersion.value = null
    console.debug('Desktop update check skipped:', error)
  }
}

export const checkDesktopUpdateOnStartup = once(checkDesktopUpdate)

export const offerDesktopUpdate = () => {
  const version = desktopUpdateVersion.value
  if (!version || dialogOpen) return
  dialogOpen = true
  const modal = Modal.confirm({
    title: t('desktopUpdate.title', { version }),
    content: t('desktopUpdate.available'),
    okText: t('desktopUpdate.install'),
    cancelText: t('cancel'),
    afterClose: () => { dialogOpen = false },
    onOk: async () => {
      modal.update({
        content: t('desktopUpdate.working'),
        cancelButtonProps: { disabled: true },
        keyboard: false
      })
      let unlisten: UnlistenFn | undefined
      try {
        let downloaded = 0
        let lastPercent = -1
        unlisten = await listen<{ chunkLength: number; contentLength?: number }>(
          'tauri://update-download-progress', ({ payload }) => {
            downloaded += payload.chunkLength
            if (!payload.contentLength) return
            const percent = Math.floor(100 * downloaded / payload.contentLength)
            if (percent !== lastPercent) {
              modal.update({ content: `${t('desktopUpdate.working')} ${percent}%` })
              lastPercent = percent
            }
          }
        )
        await invoke('install_desktop_update')
      } catch (error) {
        console.error('Desktop update failed:', error)
        Modal.error({
          title: t('desktopUpdate.failed'),
          content: h('a', { href: releasesUrl, target: '_blank', rel: 'noopener noreferrer' },
            t('desktopUpdate.download')),
          okText: t('close')
        })
        // Installation consumes the pending update, even on failure.
        desktopUpdateVersion.value = null
        void checkDesktopUpdate()
      } finally {
        unlisten?.()
      }
    }
  })
}
