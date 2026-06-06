import { FileNodeInfo, getTargetFolderFiles, batchGetFilesInfo } from '@/api/files'
import { first, isEqual } from 'lodash-es'
import { SortMethod, sortFiles } from './fileSort'
import { isMediaFile } from '@/util'
import { ref, Ref } from 'vue'

interface TreeNode {
  children: TreeNode[]
  info: FileNodeInfo
}

export class Walker {
  root: TreeNode
  execQueue: { fn: () => Promise<TreeNode>; info: FileNodeInfo }[] = []
  walkerInitPromsie: Promise<void>
  constructor(private entryPath: string, private sortMethod = SortMethod.CREATED_TIME_DESC) {
    this.root = {
      children: [],
      info: {
        name: this.entryPath,
        size: '-',
        bytes: 0,
        created_time: '',
        is_under_scanned_path: true,
        date: '',
        type: 'dir',
        fullpath: this.entryPath
      }
    }
    this.walkerInitPromsie = new Promise((resolve) => {
      batchGetFilesInfo([this.entryPath]).then(async (res) => {
        this.root.info = res[this.entryPath] ?? this.root.info
        await this.fetchChildren(this.root)
        resolve()
      })
    })

  }


  reset () {
    this.root.children = []
    return this.fetchChildren(this.root)
  }

  get images () {
    const getImg = (node: TreeNode): FileNodeInfo[] => {
      return node.children
        .map((child) => {
          if (child.info.type === 'dir') {
            return getImg(child)
          }
          if (isMediaFile(child.info.name)) {
            return child.info
          }
        })
        .filter((v) => v)
        .flat(1) as FileNodeInfo[]
    }
    return getImg(this.root)
  }

  get isCompleted () {
    return this.execQueue.length === 0
  }

  private async fetchChildren (par: TreeNode): Promise<TreeNode> {
    // console.log('fetch', par.info.fullpath)
    const { files } = await getTargetFolderFiles(par.info.fullpath)
    par.children = sortFiles(files, this.sortMethod).map((v) => ({
      info: v,
      children: []
    }))
    this.execQueue.shift()
    this.execQueue.unshift(
      ...par.children
        .filter((v) => v.info.type === 'dir')
        .map((v) => ({
          fn: () => this.fetchChildren(v),
          ...v
        }))
    ) // 用队列来实现dfs
    return par
  }
  async next () {
    await this.walkerInitPromsie
    const pkg = first(this.execQueue)
    if (!pkg) {
      return null
    }
    const res = await pkg.fn() // 这边调用时vue响应式没工作
    this.execQueue = this.execQueue.slice()
    this.root = { ...this.root }
    return res
  }

  /**
   * 暂不使用batchGetFilesInfo检测所有节点是否过期，然后并行打补丁的方式感觉替换完了后对于发现新的节点处理很麻烦
   */

  /**
   * 
   * @returns 是否过期
   */
  async isExpired () {
    const Alldirs = [this.root.info]
    const getDirs = (node: TreeNode) => {
      for (const child of node.children) {
        if (child.info.type === 'dir') {
          Alldirs.push(child.info)
          getDirs(child)
        }
      }
    }
    getDirs(this.root)

    const currNodesinfo = await batchGetFilesInfo(Alldirs.map((v) => v.fullpath))

    for (const node of Alldirs) {
      if (!isEqual(node, currNodesinfo[node.fullpath])) {
        return true
      }
    }
    return false
  }


  /**
   * 丝滑更新, 需要在在面接受新的walker
   * currPos: 当前浏览到的位置， 如果太多可能导致加载太慢，需要避免
   */
  async seamlessRefresh (currPos: number, cannelled: Ref<boolean> = ref(false)) {
    const startTime = performance.now();
    const newWalker = new Walker(this.entryPath, this.sortMethod)
    await newWalker.walkerInitPromsie
    while (!newWalker.isCompleted && newWalker.images.length < currPos) {
      if (cannelled.value) {
        throw new Error('canceled')
        
      }
      await newWalker.next()
    }
    const endTime = performance.now();
    console.log('seamlessRefresh currPos:', currPos, 'Time taken:', (endTime - startTime).toFixed(0), 'ms');
    return newWalker
  }

}
