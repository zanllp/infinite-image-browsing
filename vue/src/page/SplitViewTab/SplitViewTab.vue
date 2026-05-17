<script lang="ts" setup>
// @ts-ignore
import { Splitpanes, Pane } from 'splitpanes'
import 'splitpanes/dist/splitpanes.css'
import { useGlobalStore, type TabPane } from '@/store/useGlobalStore'
import { defineAsyncComponent, watch, ref, nextTick } from 'vue'
import { globalEvents, asyncCheck, useGlobalEventListen } from '@/util'
import { debounce, uniqueId } from 'lodash-es'
import edgeTrigger from './edgeTrigger.vue'
import { t } from '@/i18n'
import { tryOnMounted, useDocumentVisibility, type Fn } from '@vueuse/core'
import ImgSliDrawer from '../ImgSli/ImgSliDrawer.vue'



const global = useGlobalStore()
const compMap: Record<TabPane['type'], ReturnType<typeof defineAsyncComponent>> = {
  local: defineAsyncComponent(() => import('@/page/fileTransfer/stackView.vue')),
  empty: defineAsyncComponent(() => import('./emptyStartup.vue')),
  'global-setting': defineAsyncComponent(() => import('@/page/globalSetting/globalSetting.vue')),
  'tag-search-matched-image-grid': defineAsyncComponent(
    () => import('@/page/TagSearch/MatchedImageGrid.vue')
  ),
  'topic-search-matched-image-grid': defineAsyncComponent(
    () => import('@/page/TopicSearch/MatchedImageGrid.vue')
  ),
  'tag-search': defineAsyncComponent(() => import('@/page/TagSearch/TagSearch.vue')),
  'fuzzy-search': defineAsyncComponent(() => import('@/page/TagSearch/SubstrSearch.vue')),
  'topic-search': defineAsyncComponent(() => import('@/page/TopicSearch/TopicSearch.vue')),
  'img-sli': defineAsyncComponent(() => import('@/page/ImgSli/ImgSliPagePane.vue')),
  'batch-download': defineAsyncComponent(() => import('@/page/batchDownload/batchDownload.vue')),
  'grid-view': defineAsyncComponent(() => import('@/page/gridView/gridView.vue')),
  'workspace-snapshot': defineAsyncComponent(() => import('@/page/WorkspeaceSnapshot/index.vue')),
  'random-image': defineAsyncComponent(() => import('@/page/randomImage/randomImage.vue')),
  'trend': defineAsyncComponent(() => import('@/page/Trend/TrendPanel.vue')),
}
const onEdit = (idx: number, targetKey: any, action: string) => {
  const tab = global.tabList[idx]
  if (action === 'add') {
    const empty: TabPane = { type: 'empty', key: uniqueId(), name: t('emptyStartPage') }
    tab.panes.push(empty)
    tab.key = empty.key
  } else {
    const paneIdx = tab.panes.findIndex((v) => v.key === targetKey)
    if (tab.key === targetKey) {
      // 只有在前台时才跳过去
      tab.key = tab.panes[paneIdx - 1]?.key ?? tab.panes[1]?.key
    }
    tab.panes.splice(paneIdx, 1)
    if (tab.panes.length === 0) {
      global.tabList.splice(idx, 1)
    }
    if (global.tabList.length === 0) {
      const pane = global.createEmptyPane()
      global.tabList.push({ panes: [pane], key: pane.key, id: uniqueId() })
    }
  }
}

useGlobalEventListen('closeTabPane', (tabIdx, key) => onEdit(tabIdx, key, 'del'))
const container = ref<HTMLDivElement>()
watch(
  () => global.tabList,
  async () => {
    await nextTick()
    global.saveRecord()
    Array.from(container.value?.querySelectorAll('.splitpanes__pane') ?? []).forEach(
      (tabEl, tabIdx) => {
        Array.from(tabEl.querySelectorAll('.ant-tabs-tab') ?? []).forEach((paneEl, paneIdx) => {
          const el = paneEl as HTMLDivElement
          el.setAttribute('draggable', 'true')
          el.setAttribute('tabIdx', tabIdx.toString())
          el.setAttribute('paneIdx', paneIdx.toString())
          el.ondragend = () => {
            global.dragingTab = undefined
          }
          el.ondragstart = (e) => {
            global.dragingTab = { tabIdx, paneIdx }
            e.dataTransfer!.setData(
              'text/plain',
              JSON.stringify({ tabIdx, paneIdx, from: 'tab-drag' })
            )
          }
        })
      }
    )
  },
  { immediate: true, deep: true }
)

const emitReturnToIIB = debounce(() => globalEvents.emit('returnToIIB'), 100)

tryOnMounted(async () => {
  const par = window.parent as Window & { get_uiCurrentTabContent (): undefined | HTMLButtonElement, onUiTabChange (cb: Fn): void }
  if (!await asyncCheck(() => par?.onUiTabChange, 200, 30_000)) {
    console.log('watch tab change failed')
    return
  }
  par.onUiTabChange(() => {
    const el = par.get_uiCurrentTabContent()
    if (el?.id.includes('infinite-image-browsing')) {
      emitReturnToIIB()
    }
  })
})
watch(useDocumentVisibility(), v => v && emitReturnToIIB())

</script>
<template>
  <div ref="container">
    <splitpanes class="default-theme">
      <pane v-for="(tab, tabIdx) in global.tabList" :key="tab.id">
        <edge-trigger :tabIdx="tabIdx">
          <a-tabs type="editable-card" v-model:activeKey="tab.key" @edit="(key:any, act:any) => onEdit(tabIdx, key, act)">
            <a-tab-pane v-for="(pane, paneIdx) in tab.panes" :key="pane.key" :tab="pane.name" class="pane">
              <component :is="compMap[pane.type]" :tabIdx="tabIdx" :paneKey="pane.key" :paneIdx="paneIdx" v-bind="pane" />
            </a-tab-pane>
          </a-tabs>
        </edge-trigger>
      </pane>
    </splitpanes>
    <img-sli-drawer />
  </div>
</template>
<style scoped lang="scss">
:deep() .splitpanes {
  .splitpanes__splitter {
    background: var(--zp-primary-background);
  }

  .splitpanes__pane {
    background: var(--zp-primary-background);
    height: 100vh;
  }
}

.pane {
  height: calc(100vh - 40px);
  --pane-max-height: calc(100vh - 40px);
}
</style>
