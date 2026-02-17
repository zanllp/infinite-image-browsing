/* eslint-disable no-undef */
Promise.resolve().then(async () => {
  /**
   * This is a file generated using `yarn build`.
   * If you want to make changes, please modify `index.tpl.js` and run the command to generate it again.
   */
  const html = `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
    <meta http-equiv="Expires" content="0" />
    <meta charset="UTF-8" />
    <link rel="icon" href="/favicon.ico" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Infinite Image Browsing</title>
    <script type="module" crossorigin src="/infinite_image_browsing/fe-static/assets/index-840caa07.js"></script>
    <link rel="stylesheet" href="/infinite_image_browsing/fe-static/assets/index-6d9e7c10.css">
  </head>

  <body>
    <div id="zanllp_dev_gradio_fe">
      It seems to have failed to load. You can try refreshing the page. <br> If that doesn't work, click on <a href="https://github.com/zanllp/sd-webui-infinite-image-browsing/issues/90" target="_blank" >FAQ</a> for help</div>
    </div>
    
  </body>
</html>
`.replace(/\/infinite_image_browsing/g, (window.location.pathname + '/infinite_image_browsing').replace(/\/\//g, '/'))
  let containerSelector = '#infinite_image_browsing_container_wrapper'
  let shouldMaximize = localStorage.getItem('iib://disable_maximize') !== 'true'

  try {
    containerSelector = __iib_root_container__
    shouldMaximize = __iib_should_maximize__
  } catch (e) { /* empty */ }

  const delay = (timeout = 0) => new Promise((resolve) => setTimeout(resolve, timeout))
  const asyncCheck = async (getter, checkSize = 100, timeout = 1000) => {
    let target = getter()
    let num = 0
    while (checkSize * num < timeout && (target === undefined || target === null)) {
      await delay(checkSize)
      target = getter()
      num++
    }
    return target
  }
  
  const getTabIdxById = (id) => {
    const tabList = gradioApp().querySelectorAll('#tabs > .tabitem[id^=tab_]')
    return Array.from(tabList).findIndex((v) => v.id.includes(id))
  }

  const switch2targetTab = (idx) => {
    try {
      gradioApp().querySelector('#tabs').querySelectorAll('button')[idx].click()
    } catch (error) {
      console.error(error)
    }
  }

  const isLobe = () => {
    try {
      return !!gradioApp().querySelector('[alt*="lobehub"]')
    } catch (error) {
      return false
    }
  }

  /**
   * @type {HTMLDivElement}
   */
  const wrap = await asyncCheck(() => gradioApp().querySelector(containerSelector), 500, Infinity)
  wrap.childNodes.forEach((v) => wrap.removeChild(v))
  const iframe = document.createElement('iframe')
  iframe.srcdoc = html
  iframe.style = 'width: 100%;height:100vh'
  wrap.appendChild(iframe)

  if (shouldMaximize) {
    onUiTabChange(() => {
      const el = get_uiCurrentTabContent()
      if (el?.id.includes('infinite-image-browsing')) {
        try {
          const iibTop = gradioApp().querySelector('#iib_top')
          if (!iibTop) {
            throw new Error('element \'#iib_top\' is not found')
          }
          const topRect = iibTop.getBoundingClientRect()
          wrap.style = `
            top:${Math.max(isLobe() ? 32 : 128, topRect.top) - 10}px;
            position: fixed;
            left: 10px;
            right: 10px;
            z-index: 100;
            width: unset;
            bottom: 10px;`
          iframe.style = 'width: 100%;height:100%'
        } catch (error) {
          console.error('Error mounting IIB. Running fallback.', error)
          wrap.style = ''
          iframe.style = 'width: 100%;height:100vh'
        }
      }
    })
  }

  const IIB_container_id = [Date.now(), Math.random()].join()
  window.IIB_container_id = IIB_container_id
  const imgTransferBus = new BroadcastChannel('iib-image-transfer-bus')
  imgTransferBus.addEventListener('message', async (ev) => {
    const data = ev.data
    if (
      typeof data !== 'object' ||
      (typeof data.IIB_container_id === 'string' && data.IIB_container_id !== IIB_container_id)
    ) {
      return
    }
    console.log('iib-message:', data)
    const appDoc = gradioApp()
    switch (data.event) {
      case 'click_hidden_button': {
        const btn = gradioApp().querySelector(`#${data.btnEleId}`)
        btn.click()
        break
      }
      case 'send_to_control_net': {
        data.type === 'img2img' ? window.switch_to_img2img() : window.switch_to_txt2img()
        await delay(100)
        const cn = appDoc.querySelector(`#${data.type}_controlnet`)
        const wrap = cn.querySelector('.label-wrap')
        if (!wrap.className.includes('open')) {
          wrap.click()
          await delay(100)
        }
        wrap.scrollIntoView()
        wrap.dispatchEvent(await createPasteEvent(data.url))
        break
      }
      case 'send_to_outpaint': {
        switch2targetTab(getTabIdxById('openOutpaint'))
        await delay(100)
        const iframe = appDoc.querySelector('#openoutpaint-iframe')
        openoutpaint_send_image(await imgUrl2DataUrl(data.url))
        iframe.contentWindow.postMessage({
          key: appDoc.querySelector('#openoutpaint-key').value,
          type: 'openoutpaint/set-prompt',
          prompt: data.prompt,
          negPrompt: data.negPrompt
        })
        break
      }
    }

    function imgUrl2DataUrl(imgUrl) {
      return new Promise((resolve, reject) => {
        fetch(imgUrl)
          .then((response) => response.blob())
          .then((blob) => {
            const reader = new FileReader()
            reader.readAsDataURL(blob)
            reader.onloadend = function () {
              const dataURL = reader.result
              resolve(dataURL)
            }
          })
          .catch((error) => reject(error))
      })
    }

    async function createPasteEvent(imgUrl) {
      const response = await fetch(imgUrl)
      const imageBlob = await response.blob()
      const imageFile = new File([imageBlob], 'image.jpg', {
        type: imageBlob.type,
        lastModified: Date.now()
      })
      const dataTransfer = new DataTransfer()
      dataTransfer.items.add(imageFile)
      const pasteEvent = new ClipboardEvent('paste', {
        clipboardData: dataTransfer,
        bubbles: true
      })
      return pasteEvent
    }
  })
})
