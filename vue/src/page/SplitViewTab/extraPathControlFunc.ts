
import { ExtraPathType, addExtraPath, aliasExtraPath, removeExtraPath } from '@/api/db'
import { globalEvents } from '@/util'
import { Input, Modal, RadioButton, RadioGroup, message, Button } from 'ant-design-vue'
import { open } from '@tauri-apps/api/dialog'
import { checkPathExists } from '@/api'
import { h, ref } from 'vue'
import { t } from '@/i18n'
import { useGlobalStore } from '@/store/useGlobalStore'
import { isTauri } from '@/util/env'
import { EyeOutlined } from '@/icon'



export const addToExtraPath = async (initType: ExtraPathType, initPath?: string) => {
  const g = useGlobalStore()
  const path = ref(initPath ?? '')

  const type = ref(initType)
  const openToSelectPath = async () => {
    const ret = await open({ directory: true, defaultPath: initPath })
    if (typeof ret === 'string') {
      path.value = ret
    } else {
      return
    }
  }
  path.value = await new Promise<string>((resolve) => {
    Modal.confirm({
      title: t('inputTargetFolderPath'),
      width: '800px',
      content: () => {
        return h('div', [
          g.conf?.enable_access_control ? h('a', {
            style: {
              'word-break': 'break-all',
              'margin-bottom': '4px',
              display: 'block'
            },
            target: '_blank',
            href: 'https://github.com/zanllp/sd-webui-infinite-image-browsing/issues/518'
          }, 'Please open this link first (Access Control mode only)') : '',
          isTauri ? h(Button, { onClick: openToSelectPath, style: {  margin: '4px 0' } } , t('selectFolder') ): '',
          h(Input, {
            value: path.value,
            'onUpdate:value': (v: string) => (path.value = v)
          }),
          h('div', [
            h('span', t('type')+': '),
            h(RadioGroup, {
              value: type.value,
              'onUpdate:value': (v: ExtraPathType) => (type.value = v),
              buttonStyle: 'solid',
              style: { margin: '16px 0 32px' }
            }, [
              h(RadioButton, { value: 'walk' }, 'Walk'),
              h(RadioButton, { value: 'scanned' }, 'Normal'),
              h(RadioButton, { value: 'scanned-fixed' }, 'Fixed')
            ])
          ]),
          h('p', 'Walk: '+ t('walkModeDoc')),
          h('p', 'Normal: '+ t('normalModelDoc')),
          h('p', 'Fixed: '+ t('fixedModeDoc')),
          h('div', {
            style: {
              marginTop: '24px',
              padding: '16px 18px',
              border: '1px solid var(--primary-color)',
              borderRadius: '10px',
              background: 'linear-gradient(135deg, var(--primary-color-1), transparent)',
              boxShadow: '0 8px 24px rgba(208, 63, 10, 0.12)'
            }
          }, [
            h('div', {
              style: {
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                marginBottom: '6px',
                color: 'var(--primary-color)',
                fontSize: '16px',
                fontWeight: 600
              }
            }, [h(EyeOutlined), t('imageViewPromoTitle')]),
            h('div', {
              style: {
                color: 'var(--zp-secondary)',
                fontSize: '13px',
                lineHeight: 1.7
              }
            }, t('imageViewPromoDesc')),
            h('div', {
              style: {
                marginTop: '8px',
                color: 'var(--primary-color)',
                fontSize: '12px'
              }
            }, t('imageViewPromoHint'))
          ])
        ])
      },
      async onOk () {
        if (!path.value) {
          message.error(t('pathIsEmpty'))
          throw new Error('pathIsEmpty')
        }
        const res = await checkPathExists([path.value])
        if (res[path.value]) {
          resolve(path.value)
        } else {
          message.error(t('pathDoesNotExist'))
        }
      }
    })
  })
  Modal.confirm({
    content: t('confirmToAddToExtraPath'),
    async onOk () {
      await addExtraPath({ types: [type.value], path: path.value })
      message.success(t('addCompleted'))
      globalEvents.emit('searchIndexExpired')
      globalEvents.emit('updateGlobalSetting')
    }
  })
}

export const onRemoveExtraPathClick = (path: string, type: ExtraPathType) => {
  Modal.confirm({
    content: t('confirmDelete'),
    closable: true,
    async onOk () {
      await removeExtraPath({ types: [type], path })
      message.success(t('removeCompleted'))
      globalEvents.emit('searchIndexExpired')
      globalEvents.emit('updateGlobalSetting')
    }
  })
}

export const onAliasExtraPathClick = (path: string) => {
  const alias = ref('')
  Modal.confirm({
    title: t('inputAlias'),
    content: () => {
      return h('div', [
        h('div', {
          style: {
            'word-break': 'break-all',
            'margin-bottom': '4px'
          }
        }, 'Path: ' + path),
        h(Input, {
          value: alias.value,
          'onUpdate:value': (v: string) => (alias.value = v)
        })]
      )
    },
    async onOk () {
      await aliasExtraPath({ alias: alias.value, path })
      message.success(t('addAliasCompleted'))
      globalEvents.emit('updateGlobalSetting')
    }
  })
}