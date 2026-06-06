from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import urllib.parse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel

from scripts.iib.fastapi_video import range_requests_response
from scripts.iib.logger import logger
from scripts.iib.parsers.comfyui import ComfyUIParser
from scripts.iib.tool import (
    get_cache_dir,
    get_created_date_by_stat,
    get_formatted_date,
    get_video_type,
    human_readable_size,
    is_audio_file,
    is_image_file,
    is_media_file,
    is_video_file,
)

try:
    import pillow_avif  # noqa: F401
except Exception as exc:  # pragma: no cover - optional codec
    logger.debug("pillow_avif is not available: %s", exc)


_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "/infinite_image_browsing"
index_html_path = _PACKAGE_ROOT / "vue" / "dist" / "index.html"


class PathsReq(BaseModel):
    paths: List[str]


class ComfyUILiteConfig:
    def __init__(
        self,
        output_dir: str | os.PathLike[str],
        input_dir: str | os.PathLike[str] | None = None,
        base: str = "/iib",
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.input_dir = Path(input_dir).resolve() if input_dir else None
        self.base = base if base.startswith("/") else f"/{base}"
        self.allowed_roots = [self.output_dir]
        if self.input_dir and self.input_dir.exists():
            self.allowed_roots.append(self.input_dir)


class _FolderCacheEntry:
    def __init__(self, mtime_ns: int, files: List[Dict[str, Any]]) -> None:
        self.mtime_ns = mtime_ns
        self.files = files


class ComfyUILiteApi:
    """Small read-only API surface for browsing ComfyUI output inside ComfyUI.

    This intentionally avoids DB/tag/search/desktop-wide features from the full IIB server.
    """

    def __init__(self, config: ComfyUILiteConfig) -> None:
        self.config = config
        self.cache_base_dir = get_cache_dir()
        self.folder_cache: Dict[str, _FolderCacheEntry] = {}

    def create_app(self) -> FastAPI:
        app = FastAPI()
        self.mount(app)
        return app

    def mount(self, app: FastAPI) -> None:
        base = self.config.base

        @app.get("/")
        def root_index():
            return self._index_response()

        @app.get(base)
        def index():
            return self._index_response()

        @app.get("/fe-static/{file_path:path}")
        async def serve_static_file(file_path: str):
            return self._serve_static_file(file_path)

        @app.get(f"{base}/fe-static/{{file_path:path}}")
        async def serve_static_file_with_base(file_path: str):
            return self._serve_static_file(file_path)

        @app.get(f"{base}/hello")
        async def hello():
            return "hello"

        @app.get(f"{base}/global_setting")
        async def global_setting():
            output = str(self.config.output_dir)
            return {
                "global_setting": {},
                "cwd": output,
                "is_win": os.name == "nt",
                "home": "",
                "sd_cwd": output,
                "all_custom_tags": [],
                "extra_paths": [
                    {
                        "path": output,
                        "type": "walk+cli_only",
                        "name": "输出文件夹",
                    }
                ],
                "enable_access_control": True,
                "launch_mode": "comfyui",
                "export_fe_fn": True,
                "app_fe_setting": {},
                "is_readonly": True,
            }

        @app.get(f"{base}/version")
        async def version():
            return {"hash": "comfyui-lite", "tag": "comfyui-lite"}

        @app.get(f"{base}/files")
        async def files(folder_path: str):
            folder = self._resolve_trusted_path(folder_path, allow_root_parent=True)
            if not folder.exists() or not folder.is_dir():
                raise HTTPException(status_code=404, detail="Folder does not exist")
            return {"files": self._list_folder(folder)}

        @app.post(f"{base}/batch_get_files_info")
        async def batch_get_files_info(req: PathsReq):
            res: Dict[str, Optional[Dict[str, Any]]] = {}
            for item in req.paths:
                try:
                    path = self._resolve_trusted_path(item, allow_root_parent=True)
                    res[item] = self._file_info(path) if path.exists() else None
                except HTTPException:
                    res[item] = None
            return res

        @app.get(f"{base}/image-thumbnail")
        async def image_thumbnail(path: str, t: str, size: str = "256x256"):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                logger.warning("Thumbnail requested for missing image: %r", str(target))
                raise HTTPException(status_code=404, detail=f"Image does not exist: {target}")
            if not is_image_file(str(target)):
                raise HTTPException(status_code=400, detail="Not an image file")
            return self._thumbnail_response(target, t, size)

        @app.get(f"{base}/img/{{filename}}")
        async def get_image(filename: str, path: str, t: str):
            target = self._resolve_trusted_path(path)
            decoded_filename = urllib.parse.unquote(filename)
            if target.name != decoded_filename:
                raise HTTPException(status_code=400, detail="Filename mismatch")
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            if not is_image_file(str(target)):
                raise HTTPException(status_code=400, detail="Not an image file")
            media_type, _ = mimetypes.guess_type(str(target))
            return FileResponse(
                str(target),
                media_type=media_type,
                headers=self._long_cache_headers(target.name),
            )

        @app.get(f"{base}/file")
        async def get_file(path: str, t: str, disposition: Optional[str] = None):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            media_type, _ = mimetypes.guess_type(str(target))
            headers = self._long_cache_headers(disposition)
            return FileResponse(str(target), media_type=media_type, headers=headers)

        @app.get(f"{base}/stream_video")
        async def stream_video(path: str, request: Request):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file():
                raise HTTPException(status_code=404)
            media_type, _ = mimetypes.guess_type(str(target))
            return range_requests_response(request, file_path=str(target), content_type=media_type)

        @app.get(f"{base}/image_geninfo")
        async def image_geninfo(path: str):
            target = self._resolve_trusted_path(path)
            return self._read_comfyui_geninfo(target)

        @app.post(f"{base}/image_geninfo_batch")
        async def image_geninfo_batch(req: PathsReq):
            res: Dict[str, str] = {}
            for item in req.paths:
                try:
                    target = self._resolve_trusted_path(item)
                    res[item] = self._read_comfyui_geninfo(target)
                except Exception:
                    res[item] = ""
            return res

        @app.post(f"{base}/db/get_image_tags")
        async def get_image_tags(req: PathsReq):
            # The existing FileItem flow asks for tags for visible files. The
            # ComfyUI-lite backend has no tag database, so return empty lists to
            # keep the old frontend contract without emitting 404s.
            return {path: [] for path in req.paths}

        @app.get(f"{base}/db/basic_info")
        async def get_db_basic_info():
            return {"img_count": 0, "tags": [], "expired": False, "expired_dirs": []}

        @app.get(f"{base}/image_exif")
        async def image_exif(path: str):
            target = self._resolve_trusted_path(path)
            if not target.exists() or not target.is_file() or not is_image_file(str(target)):
                return {}
            try:
                with Image.open(target) as img:
                    return {k: str(v) for k, v in img.info.items() if not k.lower().startswith("exif")}
            except Exception as exc:
                logger.error("Failed to get exif for %s: %s", target, exc)
                return {}

        @app.post(f"{base}/check_path_exists")
        async def check_path_exists(req: PathsReq):
            res: Dict[str, bool] = {}
            for item in req.paths:
                try:
                    res[item] = self._resolve_trusted_path(item, allow_root_parent=True).exists()
                except HTTPException:
                    res[item] = False
            return res

        @app.post(f"{base}/app_fe_setting")
        async def app_fe_setting():
            # Read-only ComfyUI build keeps UI state in browser storage.
            return {"success": True}

        @app.delete(f"{base}/app_fe_setting")
        async def remove_app_fe_setting():
            return {"success": True}

    def _index_response(self) -> Response:
        with open(index_html_path, "r", encoding="utf-8") as file:
            content = file.read().replace(DEFAULT_BASE, self.config.base)
        return Response(content=content, media_type="text/html")

    def _serve_static_file(self, file_path: str) -> FileResponse:
        static_dir = index_html_path.parent
        target = (static_dir / file_path).resolve()
        try:
            target.relative_to(static_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(target))

    def _resolve_trusted_path(self, raw_path: str, allow_root_parent: bool = False) -> Path:
        if raw_path in ("", "/"):
            return self.config.output_dir
        target = Path(raw_path).expanduser().resolve()
        roots: Iterable[Path] = self.config.allowed_roots
        for root in roots:
            try:
                target.relative_to(root)
                return target
            except ValueError:
                if allow_root_parent:
                    try:
                        root.relative_to(target)
                        return target
                    except ValueError:
                        pass
        raise HTTPException(status_code=403, detail="Path is outside ComfyUI output/input directories")

    def _file_info(self, path: Path) -> Dict[str, Any]:
        stat = path.stat()
        date = get_formatted_date(stat.st_mtime)
        created_time = get_created_date_by_stat(stat)
        if path.is_file():
            return {
                "type": "file",
                "date": date,
                "size": human_readable_size(stat.st_size),
                "name": path.name,
                "bytes": stat.st_size,
                "created_time": created_time,
                "fullpath": str(path),
                "is_under_scanned_path": True,
            }
        return {
            "type": "dir",
            "date": date,
            "created_time": created_time,
            "size": "-",
            "name": path.name,
            "bytes": 0,
            "is_under_scanned_path": True,
            "fullpath": str(path),
        }

    def _list_folder(self, folder: Path) -> List[Dict[str, Any]]:
        stat = folder.stat()
        cache_key = str(folder)
        cached = self.folder_cache.get(cache_key)
        if cached and cached.mtime_ns == stat.st_mtime_ns:
            return cached.files

        files: List[Dict[str, Any]] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        fullpath = Path(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            files.append(self._file_info(fullpath))
                        elif entry.is_file(follow_symlinks=False) and is_media_file(entry.path):
                            files.append(self._file_info(fullpath))
                    except OSError as exc:
                        logger.debug("Skip unreadable output item %s: %s", entry.path, exc)
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        self.folder_cache[cache_key] = _FolderCacheEntry(stat.st_mtime_ns, files)
        return files

    def _thumbnail_response(self, path: Path, t: str, size: str) -> FileResponse:
        if not self.cache_base_dir:
            raise HTTPException(status_code=500, detail="Cache directory is not available")
        try:
            w, h = size.split("x")
            max_size = (int(w), int(h))
            if max(max_size) > 1024 or min(max_size) <= 0:
                raise ValueError("invalid thumbnail size")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid thumbnail size")

        hash_dir = hashlib.md5((str(path) + t).encode("utf-8")).hexdigest()
        cache_dir = Path(self.cache_base_dir) / "iib_cache" / "comfyui_lite" / hash_dir
        cache_path = cache_dir / f"{size}.jpg"
        headers = {"Cache-Control": "max-age=31536000", "ETag": hash_dir + size}
        if cache_path.exists():
            return FileResponse(str(cache_path), media_type="image/jpeg", headers=headers)

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail(max_size)
                if img.mode in ("RGBA", "LA"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    alpha = img.getchannel("A") if "A" in img.getbands() else None
                    background.paste(img.convert("RGBA"), mask=alpha)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(cache_path, "jpeg", quality=82, optimize=False)
            return FileResponse(str(cache_path), media_type="image/jpeg", headers=headers)
        except Exception as exc:
            logger.error("Failed to generate image thumbnail. path=%s error=%s", path, exc)
            raise HTTPException(status_code=415, detail=f"Unable to generate image thumbnail: {exc}")

    def _read_comfyui_geninfo(self, path: Path) -> str:
        if not path.exists() or not path.is_file() or not is_image_file(str(path)):
            return ""
        try:
            with Image.open(path) as img:
                if ComfyUIParser.test(img, str(path)):
                    return ComfyUIParser.parse(img, str(path)).raw_info or ""
                return ""
        except Exception as exc:
            logger.debug("Failed to read ComfyUI geninfo for %s: %s", path, exc)
            return ""

    def _long_cache_headers(self, filename: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Cache-Control": "public, max-age=31536000",
            "Expires": (datetime.now() + timedelta(days=365)).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        }
        if filename:
            encoded = urllib.parse.quote(filename.encode("utf-8"))
            headers["Content-Disposition"] = f"inline; filename*=UTF-8''{encoded}"
        return headers


def create_comfyui_lite_app(output_dir: str | os.PathLike[str], input_dir: str | os.PathLike[str] | None = None, base: str = "/iib") -> FastAPI:
    return ComfyUILiteApi(ComfyUILiteConfig(output_dir=output_dir, input_dir=input_dir, base=base)).create_app()
