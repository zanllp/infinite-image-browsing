import logging
import sys
from pathlib import Path

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web/comfyui"

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

try:
    from server import PromptServer  # type: ignore

    COMFYUI_BASE = "/iib"
    LEGACY_BASE = "/infinite_image_browsing"

    def _get_comfyui_output_dir() -> Path:
        try:
            import folder_paths  # type: ignore

            output_directory = getattr(folder_paths, "output_directory", None)
            if output_directory:
                return Path(output_directory)
        except Exception as exc:
            logger.warning("[Infinite Image Browsing] Unable to read ComfyUI output_directory: %s", exc)
        return Path.cwd() / "output"

    def _get_comfyui_input_dir() -> Path:
        try:
            import folder_paths  # type: ignore

            input_directory = getattr(folder_paths, "input_directory", None)
            if input_directory:
                return Path(input_directory)
        except Exception as exc:
            logger.warning("[Infinite Image Browsing] Unable to read ComfyUI input_directory: %s", exc)
        return Path.cwd() / "input"

    def _register_routes() -> None:
        app = PromptServer.instance.app

        # Mount Infinite Image Browsing routes on the ComfyUI aiohttp server.
        # ComfyUI uses aiohttp, while IIB was originally written for FastAPI.
        # Use the local bridge because aiohttp-asgi can produce invalid responses
        # in some ComfyUI/yarl packaged environments when Host includes a port.
        from scripts.iib.comfyui_asgi import ComfyUIASGIMount

        # Avoid double registration if ComfyUI reloads custom nodes in-process.
        registered = {getattr(route.resource, "canonical", None) for route in app.router.routes()}
        if COMFYUI_BASE in registered and LEGACY_BASE in registered:
            return

        comfy_output_dir = _get_comfyui_output_dir()
        comfy_input_dir = _get_comfyui_input_dir()
        if not comfy_output_dir.exists():
            logger.warning("[Infinite Image Browsing] ComfyUI output directory does not exist: %s", comfy_output_dir)

        from scripts.iib.comfyui_api import create_comfyui_lite_app

        iib_app = create_comfyui_lite_app(
            output_dir=str(comfy_output_dir),
            input_dir=str(comfy_input_dir),
            base=COMFYUI_BASE,
        )

        if COMFYUI_BASE not in registered:
            ComfyUIASGIMount(iib_app, COMFYUI_BASE).register(app)
        # Compatibility route for the already-built Vue bundle. Some generated
        # chunks still contain /infinite_image_browsing/fe-static and the API
        # client defaults to /infinite_image_browsing.
        if LEGACY_BASE not in registered:
            ComfyUIASGIMount(iib_app, LEGACY_BASE).register(app)
        logger.info("[Infinite Image Browsing] Mounted at %s and %s", COMFYUI_BASE, LEGACY_BASE)

    _register_routes()

except Exception as exc:  # pragma: no cover - keeps ComfyUI startup diagnostics visible
    logger.exception("[Infinite Image Browsing] Failed to register ComfyUI routes: %s", exc)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
