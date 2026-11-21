import { invoke } from '@tauri-apps/api/tauri'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { Modal, message } from 'ant-design-vue'
import { once } from 'lodash-es'
import { ref } from 'vue'
import { t } from './i18n'

export const desktopUpdateVersion = ref<string | null>(null)
let dialogOpen = false

export const offerDesktopUpdate = async () => {
  if (dialogOpen) return
  dialogOpen = true
  try {
    const version = await invoke<string | null>('check_desktop_update')
    desktopUpdateVersion.value = version
    if (!version) {
      dialogOpen = false
      return
    }
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
          message.error(t('desktopUpdate.failed'))
        } finally {
          unlisten?.()
        }
      }
    })
  } catch (error) {
    dialogOpen = false
    // Offline or no published feed yet: never interrupt startup.
    console.debug('Desktop update check skipped:', error)
  }
}

export const checkDesktopUpdateOnStartup = once(offerDesktopUpdate)
