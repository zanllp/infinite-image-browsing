import { ref, watch } from 'vue'
import { debounce } from 'lodash-es'
import { setAppFeSetting } from '@/api'
import { useGlobalStore } from '@/store/useGlobalStore'

/** 启动页上支持自定义的列表 */
export type StartupBlockId = 'walkMode' | 'normalFixed' | 'launch'

export interface StartupListLayout {
  /** 被隐藏的条目 id（主要是内置预设） */
  hidden: string[]
  /** 自定义顺序，没列出来的按默认顺序排在其后 */
  order: string[]
}

export type StartupLayout = Record<StartupBlockId, StartupListLayout>

export const startupBlockIds: StartupBlockId[] = ['walkMode', 'normalFixed', 'launch']

const emptyLayout = (): StartupLayout => ({
  walkMode: { hidden: [], order: [] },
  normalFixed: { hidden: [], order: [] },
  launch: { hidden: [], order: [] }
})

const layout = ref<StartupLayout>(emptyLayout())

/** 本次会话用户是否已经改过：改过之后不再被服务端返回的状态覆盖 */
const dirty = ref(false)
let watching = false
let save: (() => void) | null = null

const parseLayout = (saved: any): StartupLayout => {
  const base = emptyLayout()
  startupBlockIds.forEach((id) => {
    const conf = saved?.[id]
    if (conf) {
      base[id] = { hidden: conf.hidden ?? [], order: conf.order ?? [] }
    }
  })
  return base
}

const markDirty = () => {
  dirty.value = true
  save?.()
}

/** 把某个列表按保存的顺序/隐藏状态整理好 */
export const applyStartupLayout = <T>(block: StartupBlockId, items: T[], getId: (item: T) => string): T[] => {
  const conf = layout.value[block]
  const visible = items.filter((item) => !conf.hidden.includes(getId(item)))
  if (!conf.order.length) {
    return visible
  }
  const rank = new Map(conf.order.map((id, idx) => [id, idx]))
  return visible
    .map((item, idx) => ({ item, idx, rank: rank.get(getId(item)) ?? Number.MAX_SAFE_INTEGER }))
    .sort((a, b) => (a.rank === b.rank ? a.idx - b.idx : a.rank - b.rank))
    .map((v) => v.item)
}

export const useStartupLayout = () => {
  const g = useGlobalStore()
  if (!watching) {
    watching = true
    save = debounce(() => {
      // 跟 fullscreen_layout 用同一个后端 KV，跟随"同步设置"开关
      setAppFeSetting('startup_page_layout', layout.value).catch((e) =>
        console.error('failed to save startup page layout', e)
      )
    }, 400)
    // conf 在 App.vue 里是 await getGlobalSetting() 之后才赋值的，晚于本组件 mount，
    // 所以这里必须 watch 等它到达，不能只在 setup 时读一次
    watch(
      () => g.conf?.app_fe_setting?.startup_page_layout,
      (saved) => {
        if (saved && !dirty.value) {
          layout.value = parseLayout(saved)
        }
      },
      { immediate: true }
    )
  }

  const isHidden = (block: StartupBlockId, id: string) => layout.value[block].hidden.includes(id)

  const hiddenIds = (block: StartupBlockId) => layout.value[block].hidden

  const toggleHidden = (block: StartupBlockId, id: string) => {
    const conf = layout.value[block]
    conf.hidden = isHidden(block, id) ? conf.hidden.filter((v) => v !== id) : [...conf.hidden, id]
    markDirty()
  }

  /** 批量显示/隐藏（用于"隐藏全部预设"） */
  const setHidden = (block: StartupBlockId, ids: string[], hidden: boolean) => {
    const conf = layout.value[block]
    const next = new Set(conf.hidden)
    ids.forEach((id) => (hidden ? next.add(id) : next.delete(id)))
    conf.hidden = [...next]
    markDirty()
  }

  /** visibleIds 是当前列表里实际显示出来的顺序 */
  const moveItem = (block: StartupBlockId, visibleIds: string[], id: string, delta: number) => {
    const ids = [...visibleIds]
    const from = ids.indexOf(id)
    const to = from + delta
    if (from < 0 || to < 0 || to >= ids.length) {
      return
    }
    ;[ids[from], ids[to]] = [ids[to], ids[from]]
    layout.value[block].order = [...ids, ...layout.value[block].hidden]
    markDirty()
  }

  const resetBlock = (block: StartupBlockId) => {
    layout.value[block] = { hidden: [], order: [] }
    markDirty()
  }

  return {
    layout,
    isHidden,
    hiddenIds,
    toggleHidden,
    setHidden,
    moveItem,
    resetBlock
  }
}
