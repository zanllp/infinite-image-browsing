import { checkPathExists, type getGlobalSetting } from '@/api'
import type { ExtraPathType, ExtraPathModel } from '@/api/db'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { pick, type ReturnTypeAsync } from '@/util'
import { normalizeRelativePathToAbsolute } from '@/util/path'
import { uniqBy } from 'lodash-es'
import { delay } from 'vue3-ts-util'

export type QuickMovePath = {
  key: string
  zh: string
  dir: string
  can_delete: boolean
  types: (ExtraPathType | 'preset')[]
}

export const getQuickMovePaths = async ({
  global_setting,
  sd_cwd,
  home,
  extra_paths,
  cwd,
  launch_mode
}: ReturnTypeAsync<typeof getGlobalSetting>): Promise<QuickMovePath[]> => {
  if (launch_mode === 'comfyui') {
    const g = useGlobalStore() as any
    g.extraPathAliasMap = {}
    await delay(0)
    const outputPath = extra_paths[0]
    if (!outputPath) return []
    return [{
      key: 'comfyui_output',
      zh: '输出文件夹',
      dir: outputPath.path,
      can_delete: false,
      types: ['walk', 'cli_access_only']
    }]
  }
  
  const picked = pick(
    global_setting,
    'outdir_grids',
    'outdir_extras_samples',
    'outdir_img2img_grids',
    'outdir_img2img_samples',
    'outdir_grids',
    'outdir_extras_samples',
    'outdir_samples',
    'outdir_txt2img_grids',
    'outdir_txt2img_samples',
    'outdir_save',
  )
  picked.outdir_extras_samples ??= 'outputs/extras-images'
  picked.outdir_img2img_grids ??= 'outputs/img2img-grids'
  picked.outdir_img2img_samples ??= 'outputs/img2img-images'
  picked.outdir_save ??= 'log/images'
  picked.outdir_txt2img_grids ??= 'outputs/txt2img-grids'
  picked.outdir_txt2img_samples ??= 'outputs/txt2img-images'
  const pathMap = {
    ...picked,
    cwd: sd_cwd,
    home,
    desktop: `${home}/Desktop`
  }
  Object.keys(pathMap).forEach((_k) => {
    const k = _k as keyof typeof pathMap
    if (pathMap[k]) {
      try {
        pathMap[k] = normalizeRelativePathToAbsolute(pathMap[k], sd_cwd)
      } catch (error) {
        console.error(error)
      }
    }
  })
  const exists = await checkPathExists(Object.values(pathMap).filter((v) => v))
  type Keys = keyof typeof pathMap
  const cnMap: Record<Keys, string> = {
    outdir_txt2img_samples: t('t2i'),
    outdir_img2img_samples: t('i2i'),
    outdir_save: t('saveButtonSavesTo'),
    outdir_extras_samples: t('extra'),
    outdir_grids: t('gridImage'),
    outdir_img2img_grids: t('i2i-grid'),
    outdir_samples: t('image'),
    outdir_txt2img_grids: t('t2i-grid'),
    cwd: t('workingFolder'),
    home: 'home',
    desktop: t('desktop')
  }
  const g = useGlobalStore() as any
  g.extraPathAliasMap = {
    home: home,
    [t('desktop')]: pathMap.desktop,
    [t('workingFolder')]: cwd,
    [t('t2i')]: pathMap.outdir_txt2img_samples,
    [t('i2i')]: pathMap.outdir_img2img_samples,
    ...extra_paths.filter((v: ExtraPathModel) => v.alias).reduce((acc, v: ExtraPathModel) => {
      acc[v.alias!] = normalizeRelativePathToAbsolute(v.path, sd_cwd)
      return acc
    }, {} as Record<string, string>)
  }
  await delay(0)
  const res: QuickMovePath[] = Object.keys(cnMap)
    .filter((k) => exists[pathMap[k as keyof typeof pathMap] as string])
    .map((k) => {
      const key = k as Keys
      return {
        key,
        zh: cnMap[key],
        dir: pathMap[key],
        can_delete: false,
        types: ['preset' as const]
      }
    })
  res.push(...extra_paths.map((v: ExtraPathModel): QuickMovePath => ({ key: v.path, zh: v.alias || g.getShortPath(v.path), dir: v.path, can_delete: true, types: v.types })))
  return uniqBy(res, (v: QuickMovePath) => v.key + v.types.join())
}
