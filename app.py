from scripts.iib.timeline import tlog

tlog("python_start")

import codecs
from typing import List
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse
import uvicorn
import os
from scripts.iib.api import infinite_image_browsing_api, index_html_path, DEFAULT_BASE
from scripts.iib.tool import (
    get_sd_webui_conf,
    get_valid_img_dirs,
    sd_img_dirs,
    normalize_paths,
)
from scripts.iib.db.datamodel import DataBase, Image, ExtraPath
from scripts.iib.db.update_image_data import update_image_data
import argparse
from typing import Optional, Coroutine
import json

tag = "\033[31m[warn]\033[0m"

default_port = 8000
default_host = "127.0.0.1"

def get_all_img_dirs(sd_webui_config: str, relative_to_config: bool):
    dirs = get_valid_img_dirs(
        get_sd_webui_conf(
            sd_webui_config=sd_webui_config,
            sd_webui_path_relative_to_config=relative_to_config,
        )
    )
    dirs += list(map(lambda x: x.path, ExtraPath.get_extra_paths(DataBase.get_conn())))
    return dirs


def sd_webui_paths_check(sd_webui_config: str, relative_to_config: bool):
    conf = {}
    with codecs.open(sd_webui_config, "r", "utf-8") as f:
        conf = json.loads(f.read())
    if relative_to_config:
        for dir in sd_img_dirs:
            if not os.path.isabs(conf[dir]):
                conf[dir] = os.path.normpath(
                    os.path.join(sd_webui_config, "../", conf[dir])
                )
    paths = [conf.get(key) for key in sd_img_dirs]
    paths_check(paths)


def paths_check(paths):
    for path in paths:
        if not path or len(path.strip()) == 0:
            continue
        if os.path.isabs(path):
            abs_path = path
        else:
            abs_path = os.path.normpath(os.path.join(os.getcwd(), path))
        if not os.path.exists(abs_path):
            print(f"{tag} The path '{abs_path}' will be ignored (value: {path}).")


def do_update_image_index(sd_webui_config: str, relative_to_config=False):
    dirs = get_all_img_dirs(sd_webui_config, relative_to_config)
    if not len(dirs):
        return print(f"{tag} no valid image directories, skipped")
    conn = DataBase.get_conn()
    update_image_data(dirs)
    if Image.count(conn=conn) == 0:
        return print(f"{tag} it appears that there is some issue")
    print("update image index completed. ✨")


class AppUtils:
    def __init__(
        self,
        sd_webui_config: Optional[str] = None,
        update_image_index: bool = False,
        extra_paths: List[str] = [],
        sd_webui_path_relative_to_config=False,
        allow_cors=False,
        enable_shutdown=False,
        sd_webui_dir: Optional[str] = None,
        base: Optional[str] = None,
        export_fe_fn=False,
        **args: dict,
    ):
        """
        Parameter definitions can be found by running the `python app.py -h `command or by examining the setup_parser() function.
        """
        self.sd_webui_config = sd_webui_config
        self.update_image_index = update_image_index
        self.extra_paths = extra_paths
        self.sd_webui_path_relative_to_config = sd_webui_path_relative_to_config
        self.allow_cors = allow_cors
        self.enable_shutdown = enable_shutdown
        self.sd_webui_dir = sd_webui_dir
        if base and not base.startswith("/"):
            base = "/" + base
        self.base = base
        self.export_fe_fn = export_fe_fn
        if sd_webui_dir:
            DataBase.path = os.path.join(
                sd_webui_dir, "extensions/sd-webui-infinite-image-browsing/iib.db"
            )
            self.sd_webui_config = os.path.join(sd_webui_dir, "config.json")
            self.sd_webui_path_relative_to_config = True

    def set_params(self, *args, **kwargs) -> None:
        """改变参数，与__init__的行为一致"""
        self.__init__(*args, **kwargs)

    @staticmethod
    def async_run(
        app: FastAPI, port: int = default_port, host=default_host
    ) -> Coroutine:
        """
        用于从异步运行的 FastAPI，在 Jupyter Notebook 环境中非常有用
        """
        # 不建议改成 async def，并且用 await 替换 return，
        # 因为这样会失去对 server.serve() 的控制。
        config = uvicorn.Config(app, host=host, port=port)
        server = uvicorn.Server(config)
        return server.serve()

    def wrap_app(self, app: FastAPI) -> None:
        """
        为传递的app挂载上infinite_image_browsing后端
        """
        sd_webui_config = self.sd_webui_config
        update_image_index = self.update_image_index
        extra_paths = self.extra_paths

        if sd_webui_config:
            sd_webui_paths_check(sd_webui_config, self.sd_webui_path_relative_to_config)
            if update_image_index:
                do_update_image_index(
                    sd_webui_config, self.sd_webui_path_relative_to_config
                )
        paths_check(extra_paths)

        infinite_image_browsing_api(
            app,
            sd_webui_config=sd_webui_config,
            extra_paths_cli=normalize_paths(extra_paths, os.getcwd()),
            sd_webui_path_relative_to_config=self.sd_webui_path_relative_to_config,
            allow_cors=self.allow_cors,
            enable_shutdown=self.enable_shutdown,
            launch_mode="server",
            base=self.base,
            export_fe_fn=self.export_fe_fn,
        )

    def get_root_browser_app(self) -> FastAPI:
        """
        获取首页挂载在"/"上的infinite_image_browsing FastAPI实例
        """
        app = FastAPI()

        # 用于在首页显示
        @app.get("/")
        def index():
            if isinstance(self.base, str):
                with open(index_html_path, "r", encoding="utf-8") as file:
                    content = file.read().replace(DEFAULT_BASE, self.base)
                    return Response(content=content, media_type="text/html")
            return FileResponse(index_html_path)

        self.wrap_app(app)
        return app


def setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A fast and powerful image/video browser with infinite scrolling and advanced search capabilities. It also supports parsing/viewing image information generated by multiple AI software."
    )
    parser.add_argument(
        "--host", type=str, default=default_host, help="The host to use"
    )
    parser.add_argument(
        "--port", type=int, help="The port to use", default=default_port
    )
    parser.add_argument(
        "--parent_pid",
        type=int,
        default=None,
        help="PID of the desktop shell that started this process. The server exits as soon as that process is gone, so it can never be left behind as an orphan.",
    )
    parser.add_argument(
        "--sd_webui_config", type=str, default=None, help="The path to the config file"
    )
    parser.add_argument(
        "--update_image_index", action="store_true", help="Update the image index"
    )
    parser.add_argument(
        "--generate_video_cover",
        action="store_true",
        help="Pre-generate video cover images to speed up browsing.",
    )
    parser.add_argument(
        "--generate_image_cache",
        action="store_true",
        help="Pre-generate image cache to speed up browsing. By default, only the extra paths added by the user are processed, not the paths in sd_webui_config. If you need to process paths in sd_webui_config, you must use the --sd_webui_config and --sd_webui_path_relative_to_config parameters.",
    )
    parser.add_argument(
        "--generate_image_cache_size",
        type=str,
        default="512x512",
        help="The size of the image cache to generate. Default is 512x512",
    )
    parser.add_argument(
        "--gen_cache_verbose",
        action="store_true",
        help="Verbose mode for cache generation.",
    )
    parser.add_argument(
        "--extra_paths",
        nargs="+",
        help="Extra paths to use, these paths will be added to the homepage but the images in these paths will not be indexed. They are only used for browsing. If you need to index them, please add them via the '+ Add' button on the homepage.",
        default=[],
    )
    parser.add_argument(
        "--sd_webui_path_relative_to_config",
        action="store_true",
        help="Use the file path of the sd_webui_config file as the base for all relative paths provided within the sd_webui_config file.",
    )
    parser.add_argument(
        "--allow_cors",
        action="store_true",
        help="Allow Cross-Origin Resource Sharing (CORS) for the API.",
    )
    parser.add_argument(
        "--enable_shutdown",
        action="store_true",
        help="Enable the shutdown endpoint.",
    )
    parser.add_argument(
        "--sd_webui_dir",
        type=str,
        default=None,
        help="The path to the sd_webui folder. When specified, the sd_webui's configuration will be used and the extension must be installed within the sd_webui. Data will be shared between the two.",
    )
    parser.add_argument(
        "--export_fe_fn",
        default=True,
        action="store_true",
        help="Export front-end functions to enable external access through iframe.",
    )
    parser.add_argument("--base", type=str, help="The base URL for the IIB Api.")
    return parser


def launch_app(
    port: int = default_port, host: str = default_host, *args, **kwargs: dict
) -> None:
    """
    Launches the application on the specified port.

    Args:
        **kwargs (dict): Optional keyword arguments that can be used to configure the application.
            These can be viewed by running 'python app.py -h' or by checking the setup_parser() function.
    """
    app_utils = AppUtils(*args, **kwargs)
    app = app_utils.get_root_browser_app()
    uvicorn.run(app, host=host, port=port)


async def async_launch_app(
    port: int = default_port, host: str = default_host, *args, **kwargs: dict
) -> None:
    """
    Asynchronously launches the application on the specified port.

    Args:
        **kwargs (dict): Optional keyword arguments that can be used to configure the application.
            These can be viewed by running 'python app.py -h' or by checking the setup_parser() function.
    """
    app_utils = AppUtils(*args, **kwargs)
    app = app_utils.get_root_browser_app()
    await app_utils.async_run(app, host=host, port=port)


if __name__ == "__main__":
    parser = setup_parser()
    args = parser.parse_args()
    args_dict = vars(args)

    if args_dict.get("generate_video_cover"):
        from scripts.iib.video_cover_gen import generate_video_covers

        conn = DataBase.get_conn()
        generate_video_covers(
            dirs = map(lambda x: x.path, ExtraPath.get_extra_paths(conn)),
            verbose=args.gen_cache_verbose,
        )
        exit(0)
    if args_dict.get("generate_image_cache"):
        from scripts.iib.img_cache_gen import generate_image_cache
        generate_image_cache(
            dirs = get_all_img_dirs(args.sd_webui_config, args.sd_webui_path_relative_to_config), 
            size = args.generate_image_cache_size,
            verbose = args.gen_cache_verbose
        )
        exit(0)

    tlog("launch_app", port=args.port, host=args.host)
    if args_dict.get("parent_pid"):
        from scripts.iib.parent_watchdog import watch_parent

        watch_parent(args_dict["parent_pid"])
    launch_app(**vars(args))