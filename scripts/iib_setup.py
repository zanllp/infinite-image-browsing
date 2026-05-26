from scripts.iib.api import infinite_image_browsing_api, send_img_path
from modules import script_callbacks, infotext_utils as send
from scripts.iib.tool import locale
from scripts.iib.tool import read_sd_webui_gen_info_from_image
from PIL import Image
from scripts.iib.logger import logger

from fastapi import FastAPI
import gradio as gr
from modules.shared import cmd_opts

"""
api函数声明和启动分离方便另外一边被外部调用
"""


def on_ui_tabs():
    
    with gr.Blocks(analytics_enabled=False) as view:
        with gr.Row():
            with gr.Column():
                gr.HTML("", elem_id="iib_top")
                gr.HTML("error", elem_id="infinite_image_browsing_container_wrapper")
                # 以下是使用2个组件模拟粘贴过程
                img = gr.Image(
                    type="pil",
                    elem_id="iib_hidden_img",
                )

                def on_img_change():
                    send_img_path["value"] = ""  # 真正收到图片改变才允许放行
                
                img.change(on_img_change)

                img_update_trigger = gr.Button(
                    "button", elem_id="iib_hidden_img_update_trigger"
                )

                # 修改文本和图像，等待修改完成后前端触发粘贴按钮
                # 有时在触发后收不到回调，可能是在解析params。txt时除了问题删除掉就行了
                def img_update_func():
                    try:
                        path = send_img_path.get("value")
                        # logger.info("img_update_func %s", path)
                        img = Image.open(path)
                        info = read_sd_webui_gen_info_from_image(img, path)
                        return img, info
                    except Exception as e:
                        logger.error("img_update_func err %s",e)

                img_file_info = gr.Textbox(elem_id="iib_hidden_img_file_info")
                img_update_trigger.click(img_update_func, outputs=[img, img_file_info])
                for tab in ["txt2img", "img2img", "inpaint", "extras"]:
                    if hasattr(send, 'register_paste_params_button'):
                        btn = gr.Button(f"Send to {tab}", elem_id=f"iib_hidden_tab_{tab}")
                        # 注册粘贴
                        send.register_paste_params_button(
                            send.ParamBinding(
                                paste_button=btn,
                                tabname=tab,
                                source_image_component=img,
                                source_text_component=img_file_info,
                            )
                        )

        return (
            (
                view,
                "无边图像浏览" if locale == "zh" else "Infinite image browsing",
                "infinite-image-browsing",
            ),
        )

def on_app_started(_: gr.Blocks, app: FastAPI) -> None:
    # 第一个参数是SD-WebUI传进来的gr.Blocks，但是不需要使用
    DEFAULT_BASE = "/infinite_image_browsing"
    fe_public_path = None
    subpath = vars(cmd_opts).get("subpath")

    if subpath:
        base = "/" + subpath + "/" + DEFAULT_BASE
        fe_public_path = base.replace('//', '/')
    infinite_image_browsing_api(app, fe_public_path = fe_public_path)


script_callbacks.on_ui_tabs(on_ui_tabs)
script_callbacks.on_app_started(on_app_started)
