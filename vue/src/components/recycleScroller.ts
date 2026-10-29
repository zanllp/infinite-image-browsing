/**
 * RecycleScroller 统一出口。
 *
 * 全项目只在这里直接依赖 vue-virtual-scroller：
 * - 顺带把库的 CSS 引进来，各页面不用重复 import；
 * - vue-virtual-scroller 3.x 用「组件泛型」声明插槽类型（item: TItem），
 *   而当前 vue-tsc(volar 1.4) 还不支持解析组件泛型，TItem 会被推断成 unknown，
 *   导致所有 v-slot 里的 item 都要补 as。这里统一退化成 any，
 *   与迁移前 @ts-ignore 掉类型的行为保持一致。
 */
import { RecycleScroller as RecycleScrollerImpl } from 'vue-virtual-scroller'
import 'vue-virtual-scroller/dist/vue-virtual-scroller.css'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const RecycleScroller = RecycleScrollerImpl as any
