import { invoke } from '@tauri-apps/api'
import { ref } from 'vue'
export const tauriConf = ref<{ port: number; updater: boolean }>()
export const refreshTauriConf = async () => {
  console.log(import.meta.env);
  
  if (!import.meta.env.TAURI_ARCH) {
    return
  }
  try {
    tauriConf.value = await invoke('get_tauri_conf')
  } catch (error) {
    console.error(error)
  }
}