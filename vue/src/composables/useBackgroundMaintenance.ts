import { computed, reactive, ref } from 'vue'
import { getGlobalSettingRaw, setAppFeSettingForce, waitForServerReady } from '@/api'
import { getIndexStatus, runIndexNow, type IndexStatusResp } from '@/api/db'
import { globalEvents } from '@/util'

/**
 * 后台静默维护（实验性功能）：静默更新索引 / 静默预热缩略图。
 *
 * - 配置持久化在 DB `global_setting` 表，key 为 `background_maintenance`；
 * - 后端单线程 worker 实际执行，前端只负责 配置读写 + 状态轮询 + 完成事件；
 * - 这里是模块级单例：分屏多个 pane、多个组件同时使用时共享同一份状态，
 *   轮询与请求都是单飞（single-flight），不会因为组件多而叠加请求。
 */

/** GlobalSetting / app_fe_setting 中持久化本功能配置的 key */
export const BACKGROUND_MAINTENANCE_SETTING_KEY = 'background_maintenance'

export interface BackgroundMaintenanceSettings {
  /** 静默更新索引（总开关） */
  index: boolean
  /** 静默预热缩略图（依赖 index；index 关闭时强制 false） */
  thumbnail: boolean
  thumbnailSize: number
  /** 后端维护：缩略图预热从何时开始（只读，前端不主动修改） */
  thumbnailSince: string | null
  cpuLimit: number
  /** 启动页 banner 是否「不再提示」 */
  bannerDismissed: boolean
}

export const DEFAULT_BACKGROUND_MAINTENANCE_SETTINGS: Readonly<BackgroundMaintenanceSettings> = {
  index: false,
  thumbnail: false,
  thumbnailSize: 512,
  thumbnailSince: null,
  cpuLimit: 20,
  bannerDismissed: false
}

const POLL_INTERVAL_MS = 3000
const SAVE_DEBOUNCE_MS = 300

const clampNumber = (value: unknown, min: number, max: number, fallback: number) => {
  const num = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(num)) {
    return fallback
  }
  return Math.min(max, Math.max(min, num))
}

/**
 * 把服务端/本地拿到的原始配置规范化成前端使用的形状。
 * 规则与后端保持一致：index 关闭 ⇒ thumbnail 强制 false。
 */
export const normalizeBackgroundMaintenanceSettings = (raw: unknown): BackgroundMaintenanceSettings => {
  const src = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const index = !!src.index
  return {
    index,
    thumbnail: index && !!src.thumbnail,
    thumbnailSize: clampNumber(src.thumbnailSize, 256, 1024, 512),
    thumbnailSince:
      typeof src.thumbnailSince === 'string' && src.thumbnailSince ? src.thumbnailSince : null,
    cpuLimit: clampNumber(src.cpuLimit, 5, 50, 20),
    bannerDismissed: !!src.bannerDismissed
  }
}

// ---- 模块级单例状态 ----
const settings = reactive<BackgroundMaintenanceSettings>({ ...DEFAULT_BACKGROUND_MAINTENANCE_SETTINGS })
const status = ref<IndexStatusResp | null>(null)
const settingsLoaded = ref(false)
const modalOpen = ref(false)
const saving = ref(false)
const runningNow = ref(false)
const lastVersion = ref<number | null>(null)

let settingsLoadPromise: Promise<void> | null = null
let statusPollPromise: Promise<void> | null = null
let initPromise: Promise<void> | null = null
let runningNowPromise: Promise<boolean> | null = null
let pollTimer: ReturnType<typeof setInterval> | null = null
let saveTimer: ReturnType<typeof setTimeout> | null = null
let visibilityListenerBound = false
/** 用户是否已经改过设置：改过之后不再被 loadSettings 覆盖 */
let settingsDirty = false

const isPageVisible = () =>
  typeof document === 'undefined' || document.visibilityState !== 'hidden'

const currentPayload = (): BackgroundMaintenanceSettings => ({
  index: settings.index,
  thumbnail: settings.index ? settings.thumbnail : false,
  thumbnailSize: settings.thumbnailSize,
  thumbnailSince: settings.thumbnailSince,
  cpuLimit: settings.cpuLimit,
  bannerDismissed: settings.bannerDismissed
})

/**
 * 从 DB 读取配置（单飞）。默认只读一次，force=true 可强制重读。
 * 失败时抛给调用方（轮询/初始化处自行静默）。
 */
const loadSettings = (force = false): Promise<void> => {
  if (settingsLoaded.value && !force) {
    return Promise.resolve()
  }
  if (settingsLoadPromise) {
    return settingsLoadPromise
  }
  settingsLoadPromise = (async () => {
    try {
      // 桌面版后端 sidecar 可能比 webview 晚就绪：启动页 banner 在 onMounted 里就会
      // 调到这里，不先等端口能响应的话，冷启动必然发出一个 ECONNREFUSED 的
      // /global_setting（拦截器再去 JSON.parse 一个不存在的响应体，控制台报
      // "undefined" is not valid JSON，还会弹"发生了个错误"）。
      await waitForServerReady()
      const conf = await getGlobalSettingRaw()
      const raw = (conf?.app_fe_setting as Record<string, unknown> | undefined)?.[
        BACKGROUND_MAINTENANCE_SETTING_KEY
      ]
      if (settingsDirty) {
        // 用户已经改过：以本地为准，仅标记已加载
        settingsLoaded.value = true
        return
      }
      Object.assign(settings, normalizeBackgroundMaintenanceSettings(raw))
      settingsLoaded.value = true
    } finally {
      settingsLoadPromise = null
    }
  })()
  return settingsLoadPromise
}

const persistSettings = async () => {
  saving.value = true
  try {
    await setAppFeSettingForce(BACKGROUND_MAINTENANCE_SETTING_KEY, currentPayload())
  } finally {
    saving.value = false
  }
}

/**
 * 保存配置：默认防抖 300ms 合并多次修改；immediate=true 立即写入（如「不再提示」）。
 */
const saveSettings = (
  patch: Partial<BackgroundMaintenanceSettings>,
  options: { immediate?: boolean } = {}
): Promise<void> => {
  settingsDirty = true
  Object.assign(settings, normalizeBackgroundMaintenanceSettings({ ...currentPayload(), ...patch }))
  settingsLoaded.value = true
  if (saveTimer) {
    clearTimeout(saveTimer)
    saveTimer = null
  }
  if (options.immediate) {
    return persistSettings().catch(() => undefined)
  }
  saveTimer = setTimeout(() => {
    saveTimer = null
    void persistSettings().catch(() => undefined)
  }, SAVE_DEBOUNCE_MS)
  return Promise.resolve()
}

/** 连续轮询失败次数：避免后端重启时 indicator 永远停在「运行中」的旧状态 */
let statusPollFailures = 0

/**
 * 轮询后台状态（单飞，页面 hidden 时跳过）。
 * status.version 变化 ⇒ 后台完成一轮索引更新 ⇒ 发全局事件让标签列表等刷新。
 */
const pollStatus = (): Promise<void> => {
  if (statusPollPromise) {
    return statusPollPromise
  }
  if (!isPageVisible()) {
    return Promise.resolve()
  }
  statusPollPromise = (async () => {
    try {
      const next = await getIndexStatus()
      if (!next) {
        statusPollFailures += 1
        if (statusPollFailures >= 3) {
          // 连续失败（后端重启 / 端点暂不可用）时清掉旧状态，避免胶囊卡住
          status.value = null
        }
        return
      }
      statusPollFailures = 0
      status.value = next
      const prevVersion = lastVersion.value
      if (
        prevVersion !== null &&
        typeof next.version === 'number' &&
        next.version !== prevVersion
      ) {
        globalEvents.emit('backgroundMaintenanceUpdated')
      }
      if (typeof next.version === 'number') {
        lastVersion.value = next.version
      }
    } finally {
      statusPollPromise = null
    }
  })()
  return statusPollPromise
}

const stopPollTimer = () => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/**
 * 启动状态轮询（幂等）：页面可见时 3s 一次；切到后台暂停，切回前台恢复并立即拉一次。
 */
const startPolling = () => {
  if (typeof window === 'undefined') {
    return
  }
  if (!visibilityListenerBound && typeof document !== 'undefined') {
    visibilityListenerBound = true
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        startPolling()
      } else {
        stopPollTimer()
      }
    })
  }
  if (pollTimer || !isPageVisible()) {
    return
  }
  void pollStatus()
  pollTimer = setInterval(() => {
    if (isPageVisible()) {
      void pollStatus()
    }
  }, POLL_INTERVAL_MS)
}

/** 立即请求后台跑一次索引扫描，返回是否成功触发。 */
const runNow = (): Promise<boolean> => {
  if (runningNowPromise) {
    return runningNowPromise
  }
  runningNow.value = true
  runningNowPromise = (async () => {
    try {
      const resp = await runIndexNow()
      const ok = !!resp?.ok
      if (ok) {
        await pollStatus()
      }
      return ok
    } finally {
      runningNow.value = false
      runningNowPromise = null
    }
  })()
  return runningNowPromise
}

const openModal = () => {
  modalOpen.value = true
  void loadSettings().catch(() => undefined)
}

const closeModal = () => {
  modalOpen.value = false
}

/**
 * 全局初始化（App.vue 调用一次）：等后端就绪 → 读配置 → 启动轮询。
 * 失败静默：后端旧版本 / 暂时不可用时不影响主流程。
 */
export const initBackgroundMaintenance = (): Promise<void> => {
  if (initPromise) {
    return initPromise
  }
  initPromise = (async () => {
    try {
      await waitForServerReady()
    } catch (error) {
      // 后端可能尚未就绪：后续请求失败会静默处理
    }
    await loadSettings().catch(() => undefined)
    startPolling()
  })()
  return initPromise
}

/**
 * 统一入口：组件里 `const bg = useBackgroundMaintenance()` 后使用。
 * 注意：这里不直接调用 pinia store，避免在非 setup 环境下触发 pinia 初始化问题。
 */
export const useBackgroundMaintenance = () => {
  /** 实验功能总开关是否已开启 */
  const enabled = computed(() => settings.index)
  /** 启动页 banner 是否展示 */
  const bannerVisible = computed(
    () => settingsLoaded.value && !settings.index && !settings.bannerDismissed
  )
  /** 索引是否正在后台更新 */
  const indexRunning = computed(() => !!status.value?.index.running)
  /** 缩略图是否正在后台预热 */
  const thumbRunning = computed(() => !!status.value?.thumb.running)
  /** 是否有后台任务在跑（决定 indicator 是否出现） */
  const active = computed(() => indexRunning.value || thumbRunning.value)
  /** 后台任务是否出错（出错时允许回退显示旧的黄色过期提示） */
  const backgroundFailed = computed(() => !!status.value?.index.error)

  return {
    // 状态
    settings,
    status,
    settingsLoaded,
    modalOpen,
    saving,
    runningNow,
    enabled,
    bannerVisible,
    indexRunning,
    thumbRunning,
    active,
    backgroundFailed,
    // 动作
    loadSettings,
    saveSettings,
    pollStatus,
    runNow,
    openModal,
    closeModal,
    initBackgroundMaintenance
  }
}
