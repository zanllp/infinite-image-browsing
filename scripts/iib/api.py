import base64
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
import shutil
import sqlite3

from scripts.iib.dir_cover_cache import get_top_4_media_info
from scripts.iib.tool import (
    get_created_date_by_stat,
    get_video_type,
    human_readable_size,
    is_valid_media_path,
    is_media_file,
    get_cache_dir,
    get_formatted_date,
    is_win,
    cwd,
    locale,
    enable_access_control,
    get_windows_drives,
    get_sd_webui_conf,
    get_valid_img_dirs,
    open_folder,
    get_img_geninfo_txt_path,
    unique_by,
    create_zip_file,
    normalize_paths,
    to_abs_path,
    is_secret_key_required,
    open_file_with_default_app,
    is_exe_ver,
    backup_db_file,
    get_current_commit_hash,
    get_current_tag,
    get_file_info_by_path,
    get_data_file_path
)
from fastapi import FastAPI, HTTPException, Header, Response
from fastapi.staticfiles import StaticFiles
import asyncio
from typing import List, Optional
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from PIL import Image
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import hashlib
from contextlib import closing
from scripts.iib.db.datamodel import (
    DataBase,
    ExtraPathType,
    Image as DbImg,
    Tag,
    Folder,
    ImageTag,
    ExtraPath,
    FileInfoDict,
    Cursor, 
    GlobalSetting,
)
from scripts.iib.db.update_image_data import update_image_data, rebuild_image_index, add_image_data_single
from scripts.iib.topic_cluster import mount_topic_cluster_routes
from scripts.iib.tag_graph import mount_tag_graph_routes
from scripts.iib.organize_files import mount_organize_routes
from scripts.iib.logger import logger
from scripts.iib.seq import seq
import urllib.parse
from scripts.iib.fastapi_video import range_requests_response, close_video_file_reader
from scripts.iib.parsers.index import parse_image_info
import scripts.iib.plugin

try:
    import pillow_avif
except Exception as e:
    logger.error(e)

import requests
import dotenv



# 加载环境变量
dotenv.load_dotenv()

index_html_path = get_data_file_path("vue/dist/index.html") if is_exe_ver else os.path.join(cwd, "vue/dist/index.html")  # 在app.py也被使用


send_img_path = {"value": ""}
mem = {"secret_key_hash": None, "extra_paths": [], "all_scanned_paths": []}
secret_key = os.getenv("IIB_SECRET_KEY")
if secret_key:
    print("Secret key loaded successfully. ")

# AI 配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

print(f"AI Model: {AI_MODEL or 'Not configured'}")
print(f"OpenAI Base URL: {OPENAI_BASE_URL}")
print(f"OpenAI API Key: {'Configured' if OPENAI_API_KEY else 'Not configured'}")
print(f"Embedding Model: {EMBEDDING_MODEL or 'Not configured'}")

WRITEABLE_PERMISSIONS = ["read-write", "write-only"]

is_api_writeable = not (os.getenv("IIB_ACCESS_CONTROL_PERMISSION")) or (
    os.getenv("IIB_ACCESS_CONTROL_PERMISSION") in WRITEABLE_PERMISSIONS
)
IIB_DEBUG=False


async def write_permission_required():
    if not is_api_writeable:
        error_msg = (
            "User is not authorized to perform this action. Required permission: "
            + ", ".join(WRITEABLE_PERMISSIONS)
        )
        raise HTTPException(status_code=403, detail=error_msg)


async def verify_secret(request: Request):
    if not secret_key:
        if is_secret_key_required:
            raise HTTPException(status_code=400, detail={"type": "secret_key_required"})
        return
    token = request.cookies.get("IIB_S")
    if not token:
        raise HTTPException(status_code=401, detail={"type": "secret_verification_failed"})
    if not mem["secret_key_hash"]:
        mem["secret_key_hash"] = hashlib.sha256(
            (secret_key + "_ciallo").encode("utf-8")
        ).hexdigest()
    if mem["secret_key_hash"] != token:
        raise HTTPException(status_code=401, detail={"type": "secret_verification_failed"})

DEFAULT_BASE = "/infinite_image_browsing"
def infinite_image_browsing_api(app: FastAPI, **kwargs):
    backup_db_file(DataBase.get_db_file_path())
    api_base = kwargs.get("base") if isinstance(kwargs.get("base"), str) else DEFAULT_BASE
    fe_public_path = kwargs.get("fe_public_path") if isinstance(kwargs.get("fe_public_path"), str) else api_base
    cache_base_dir = get_cache_dir()

    # print(f"IIB api_base:{api_base} fe_public_path:{fe_public_path}")
    if IIB_DEBUG or is_exe_ver:
        @app.exception_handler(Exception)
        async def exception_handler(request: Request, exc: Exception):
            error_msg = f"An exception occurred while processing {request.method} {request.url}: {exc}"
            logger.error(error_msg)

            return JSONResponse(
                status_code=500, content={"message": "Internal Server Error"}
            )
        @app.middleware("http")
        async def log_requests(request: Request, call_next):
            path = request.url.path
            if (
                path.find("infinite_image_browsing/image-thumbnail") == -1
                and path.find("infinite_image_browsing/file") == -1
                and path.find("infinite_image_browsing/fe-static") == -1
            ):
                logger.info(f"Received request: {request.method} {request.url}")
                if request.query_params:
                    logger.debug(f"Query Params: {request.query_params}")
                if request.path_params:
                    logger.debug(f"Path Params: {request.path_params}")

            try:
                return await call_next(request)
            except HTTPException as http_exc:
                logger.warning(
                    f"HTTPException occurred while processing {request.method} {request.url}: {http_exc}"
                )
                raise http_exc
            except Exception as exc:
                logger.error(
                    f"An exception occurred while processing {request.method} {request.url}: {exc}"
                )

    
    if kwargs.get("allow_cors"):
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=r"^[\w./:-]+$",
            allow_methods=["*"],
            allow_headers=["*"],
            allow_credentials=True,
        )

    def get_img_search_dirs():
        try:
            return get_valid_img_dirs(get_sd_webui_conf(**kwargs))
        except Exception as e:
            print(e)
            return []

    def update_all_scanned_paths():
        allowed_paths = os.getenv("IIB_ACCESS_CONTROL_ALLOWED_PATHS")
        if allowed_paths:
            sd_webui_conf = get_sd_webui_conf(**kwargs)
            path_config_key_map = {
                "save": "outdir_save",
                "extra": "outdir_extras_samples",
                "txt2img": "outdir_txt2img_samples",
                "img2img": "outdir_img2img_samples",
            }

            def path_map(path: str):
                path = path.strip()
                if path in path_config_key_map:
                    return sd_webui_conf.get(path_config_key_map.get(path))
                return path

            paths = normalize_paths(
                seq(allowed_paths.split(","))
                .map(path_map)
                .filter(lambda x: x)
                .to_list(),
                os.getcwd()
            )
        else:
            paths = (
                get_img_search_dirs()
                + mem["extra_paths"]
                + kwargs.get("extra_paths_cli", [])
            )
        mem["all_scanned_paths"] = unique_by(paths)

    update_all_scanned_paths()

    def update_extra_paths(conn: sqlite3.Connection):
        r = ExtraPath.get_extra_paths(conn)
        mem["extra_paths"] = [x.path for x in r]
        update_all_scanned_paths()

    def safe_commonpath(seq):
        try:
            return os.path.commonpath(seq)
        except Exception as e:
            # logger.error(e)
            return ""

    def is_path_under_parents(path, parent_paths: List[str] = []):
        """
        Check if the given path is under one of the specified parent paths.
        :param path: The path to check.
        :param parent_paths: By default, all scanned paths are included in the list of parent paths
        :return: True if the path is under one of the parent paths, False otherwise.
        """
        try:
            if not parent_paths:
                parent_paths = mem["all_scanned_paths"]
            path = to_abs_path(path)
            for parent_path in parent_paths:
                if safe_commonpath([path, parent_path]) == parent_path:
                    return True
        except Exception as e:
            logger.error(e)
        return False

    def is_path_trusted(path: str):
        if not enable_access_control:
            return True
        try:
            parent_paths = mem["all_scanned_paths"]
            path = to_abs_path(path)
            for parent_path in parent_paths:
                if len(path) <= len(parent_path):
                    if parent_path.startswith(path):
                        return True
                else:
                    if path.startswith(parent_path):
                        return True
        except:
            pass
        return False

    def check_path_trust(path: str):
        if not is_path_trusted(path):
            raise HTTPException(status_code=403)

    def filter_allowed_files(files: List[FileInfoDict]):
        return [x for x in files if is_path_trusted(x["fullpath"])]



    class PathsReq(BaseModel):
        paths: List[str]

    @app.get(f"{api_base}/hello")
    async def greeting():
        return "hello"

    @app.get(f"{api_base}/global_setting", dependencies=[Depends(verify_secret)])
    async def global_setting():
        all_custom_tags = []

        extra_paths = []
        app_fe_setting = {}
        try:
            conn = DataBase.get_conn()
            all_custom_tags = Tag.get_all_custom_tag(conn)
            extra_paths = ExtraPath.get_extra_paths(conn) + [
                ExtraPath(path, ExtraPathType.cli_only.value)
                for path in kwargs.get("extra_paths_cli", [])
            ]
            update_extra_paths(conn)
            app_fe_setting = GlobalSetting.get_all_settings(conn)
        except Exception as e:
            print(e)
        return {
            "global_setting": get_sd_webui_conf(**kwargs),
            "cwd": cwd,
            "is_win": is_win,
            "home": os.environ.get("USERPROFILE") if is_win else os.environ.get("HOME"),
            "sd_cwd": os.getcwd(),
            "all_custom_tags": all_custom_tags,
            "extra_paths": extra_paths,
            "enable_access_control": enable_access_control,
            "launch_mode": kwargs.get("launch_mode", "sd"),
            "export_fe_fn": bool(kwargs.get("export_fe_fn")),
            "app_fe_setting": app_fe_setting,
            "is_readonly": not is_api_writeable,
        }
    
    
    class AppFeSettingReq(BaseModel):
        name: str
        value: str
    
    @app.post(f"{api_base}/app_fe_setting", dependencies=[Depends(verify_secret), Depends(write_permission_required)])
    async def app_fe_setting(req: AppFeSettingReq):
        conn = DataBase.get_conn()
        GlobalSetting.save_setting(conn, req.name, req.value)
        # 如果更新的是自动标签规则，重新加载
        if req.name == "auto_tag_rules":
            from scripts.iib.auto_tag import AutoTagMatcher
            AutoTagMatcher.reload_rules(conn)

    class AppFeSettingDelReq(BaseModel):
        name: str

    @app.delete(f"{api_base}/app_fe_setting", dependencies=[Depends(verify_secret), Depends(write_permission_required)])
    async def remove_app_fe_setting(req: AppFeSettingDelReq):
        conn = DataBase.get_conn()
        GlobalSetting.remove_setting(conn, req.name)
    
    @app.get(f"{api_base}/version", dependencies=[Depends(verify_secret)])
    async def get_version():
        import sys
        import platform

        def _get_dist_version(dist_name: str = None, module_name: str = None):
            # Try importlib.metadata first (distribution metadata), then fallback to module __version__
            try:
                if dist_name:
                    try:
                        from importlib.metadata import version

                        return version(dist_name)
                    except Exception:
                        pass
                if module_name:
                    mod = __import__(module_name)
                    return getattr(mod, "__version__", None)
                if dist_name:
                    # try importing by normalized name
                    mod = __import__(dist_name.replace("-", "_"))
                    return getattr(mod, "__version__", None)
            except Exception as e:
                logger.debug("Version probe failed for %s/%s: %s", dist_name, module_name, e)
            return None

        versions = {
            "python_version": sys.version.splitlines()[0],
            "platform": platform.platform(),
            "hash": get_current_commit_hash(),
            "tag": get_current_tag(),
            "av": _get_dist_version("av", "av"),
            "imageio": _get_dist_version("imageio", "imageio"),
            "pillow": _get_dist_version("Pillow", "PIL"),
            "imageio_ffmpeg": _get_dist_version("imageio-ffmpeg", "imageio_ffmpeg"),
            "pillow_avif_plugin": _get_dist_version("pillow-avif-plugin", "pillow_avif"),
            "requests": _get_dist_version("requests", "requests"),
            "numpy": _get_dist_version("numpy", "numpy"),
            "hnswlib": _get_dist_version("hnswlib", "hnswlib"),
        }

        logger.info("Version info requested: %s", {k: v for k, v in versions.items() if v})
        return versions

    class DeleteFilesReq(BaseModel):
        file_paths: List[str]

    @app.post(
        api_base + "/delete_files",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def delete_files(req: DeleteFilesReq):
        conn = DataBase.get_conn()

        for path in req.file_paths:
            check_path_trust(path)
            try:
                if os.path.isdir(path):
                    if len(os.listdir(path)):
                        error_msg = (
                            "When a folder is not empty, it is not allowed to be deleted."
                            if locale == "en"
                            else "文件夹不为空时不允许删除。"
                        )
                        raise HTTPException(400, detail=error_msg)
                    shutil.rmtree(path)
                else:
                    close_video_file_reader(path)
                    txt_path = get_img_geninfo_txt_path(path)

                    os.remove(path)
                    if txt_path:
                        os.remove(txt_path)

                    img = DbImg.get(conn, os.path.normpath(path))
                    if img:
                        logger.info("delete file: %s", path)
                        ImageTag.remove(conn, img.id)
                        DbImg.remove(conn, img.id)
            except OSError as e:
                # 处理删除失败的情况
                logger.error("delete failed")
                error_msg = (
                    f"Error deleting file {path}: {e}"
                    if locale == "en"
                    else f"删除文件 {path} 时出错：{e}"
                )
                raise HTTPException(400, detail=error_msg)

        return {"ok": True}

    class CreateFoldersReq(BaseModel):
        dest_folder: str

    @app.post(
        api_base + "/mkdirs",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def create_folders(req: CreateFoldersReq):
        if enable_access_control:
            if not is_path_under_parents(req.dest_folder):
                raise HTTPException(status_code=403)
        os.makedirs(req.dest_folder, exist_ok=True)

    class MoveFilesReq(BaseModel):
        file_paths: List[str]
        dest: str
        create_dest_folder: Optional[bool] = False
        continue_on_error: Optional[bool] = False

    @app.post(
        api_base + "/copy_files",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def copy_files(req: MoveFilesReq):
        errors = []
        for path in req.file_paths:
            try:
                check_path_trust(path)
                shutil.copy(path, req.dest)
                txt_path = get_img_geninfo_txt_path(path)
                if txt_path:
                    shutil.copy(txt_path, req.dest)
            except OSError as e:
                error_msg = (
                    f"Error copying file {path} to {req.dest}: {e}"
                    if locale == "en"
                    else f"复制文件 {path} 到 {req.dest} 时出错：{e}"
                )
                if req.continue_on_error:
                    errors.append(error_msg)
                    continue
                raise HTTPException(400, detail=error_msg)
        return {"errors": errors}

    @app.post(
        api_base + "/move_files",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def move_files(req: MoveFilesReq):
        if req.create_dest_folder:
            os.makedirs(req.dest, exist_ok=True)
        elif not os.path.isdir(req.dest):
            error_msg = (
                f"Destination folder {req.dest} does not exist."
                if locale == "en"
                else f"目标文件夹 {req.dest} 不存在。"
            )
            raise HTTPException(400, detail=error_msg)

        conn = DataBase.get_conn()        
        errors = []

        def move_file_with_geninfo(path: str, dest: str):
            path = os.path.normpath(path)
            txt_path = get_img_geninfo_txt_path(path)
            if txt_path:
                shutil.move(txt_path, dest)
            img = DbImg.get(conn, path)
            new_path = os.path.normpath(os.path.join(dest, os.path.basename(path)))
            if img:
                logger.info(f"update file path: {path} -> {new_path} in db")
                img.update_path(conn, new_path, force=True)

        for path in req.file_paths:
            try:
                check_path_trust(path)
                path = os.path.normpath(path)
                base_dir = os.path.dirname(path)
                files = list(os.walk(path))
                is_dir = os.path.isdir(path)
                shutil.move(path, req.dest)
                if is_dir:
                    for root, _, files in files:
                        relative_path = root[len(base_dir) + 1 :]
                        dest = os.path.join(req.dest, relative_path)
                        for file in files:
                            is_valid = is_media_file(file)
                            if is_valid:
                                move_file_with_geninfo(os.path.join(root, file), dest)
                else:
                    move_file_with_geninfo(path, req.dest)
                            
                conn.commit()
            except OSError as e:
                
                conn.rollback()
                error_msg = (
                    f"Error moving file {path} to {req.dest}: {e}"
                    if locale == "en"
                    else f"移动文件 {path} 到 {req.dest} 时出错：{e}"
                )
                if req.continue_on_error:
                    errors.append(error_msg)
                    continue
                raise HTTPException(400, detail=error_msg)
        return {"errors": errors}

    @app.get(api_base + "/files", dependencies=[Depends(verify_secret)])
    async def get_target_folder_files(folder_path: str):
        files: List[FileInfoDict] = []
        try:
            if is_win and folder_path == "/":
                for item in get_windows_drives():
                    files.append(
                        {"type": "dir", "size": "-", "name": item, "fullpath": item}
                    )
            else:
                if not os.path.exists(folder_path):
                    return {"files": []}
                folder_path = to_abs_path(folder_path)
                check_path_trust(folder_path)
                folder_listing: List[os.DirEntry] = os.scandir(folder_path)
                is_under_scanned_path = is_path_under_parents(folder_path)
                for item in folder_listing:
                    if not os.path.exists(item.path):
                        continue
                    fullpath = os.path.normpath(item.path)
                    name = os.path.basename(item.path)
                    stat = item.stat()
                    date = get_formatted_date(stat.st_mtime)
                    created_time = get_created_date_by_stat(stat)
                    if item.is_file():
                        bytes = stat.st_size
                        size = human_readable_size(bytes)
                        files.append(
                            {
                                "type": "file",
                                "date": date,
                                "size": size,
                                "name": name,
                                "bytes": bytes,
                                "created_time": created_time,
                                "fullpath": fullpath,
                                "is_under_scanned_path": is_under_scanned_path,
                            }
                        )
                    elif item.is_dir():
                        files.append(
                            {
                                "type": "dir",
                                "date": date,
                                "created_time": created_time,
                                "size": "-",
                                "name": name,
                                "is_under_scanned_path": is_under_scanned_path,
                                "fullpath": fullpath,
                            }
                        )
        except Exception as e:
            # logger.error(e)
            raise HTTPException(status_code=400, detail=str(e))

        return {"files": filter_allowed_files(files)}
    

    @app.post(api_base + "/batch_get_files_info", dependencies=[Depends(verify_secret)])
    async def batch_get_files_info(req: PathsReq):
        res = {}
        for path in req.paths:
            check_path_trust(path)
            res[path] = get_file_info_by_path(path)
        return res

    @app.get(api_base + "/image-thumbnail", dependencies=[Depends(verify_secret)])
    async def thumbnail(path: str, t: str, size: str = "256x256"):
        check_path_trust(path)
        if not cache_base_dir:
            return
        # 生成缓存文件的路径
        hash_dir = hashlib.md5((path + t).encode("utf-8")).hexdigest()
        hash = hash_dir + size
        cache_dir = os.path.join(cache_base_dir, "iib_cache", hash_dir)
        cache_path = os.path.join(cache_dir, f"{size}.webp")

        # 如果缓存文件存在，则直接返回该文件
        if os.path.exists(cache_path):
            return FileResponse(
                cache_path,
                media_type="image/webp",
                headers={"Cache-Control": "max-age=31536000", "ETag": hash},
            )

                
        # 如果小于64KB，直接返回原图
        if os.path.getsize(path) < 64 * 1024:
            return FileResponse(
                path,
                media_type="image/" + path.split(".")[-1],
                headers={"Cache-Control": "max-age=31536000", "ETag": hash},
            )
        

        # 如果缓存文件不存在，则生成缩略图并保存
        with Image.open(path) as img:
            w, h = size.split("x")
            img.thumbnail((int(w), int(h)))
            os.makedirs(cache_dir, exist_ok=True)
            img.save(cache_path, "webp")
            # print(f"Image cache generated: {path}")

        # 返回缓存文件
        return FileResponse(
            cache_path,
            media_type="image/webp",
            headers={"Cache-Control": "max-age=31536000", "ETag": hash},
        )

    @app.get(api_base + "/file", dependencies=[Depends(verify_secret)])
    async def get_file(path: str, t: str, disposition: Optional[str] = None):
        filename = path
        import mimetypes

        check_path_trust(path)
        if not os.path.exists(filename):
            raise HTTPException(status_code=404)
        if not os.path.isfile(filename):
            raise HTTPException(status_code=400, detail=f"{filename} is not a file")
        # 根据文件后缀名获取媒体类型
        media_type, _ = mimetypes.guess_type(filename)
        headers = {}
        if disposition:
            encoded_filename = urllib.parse.quote(disposition.encode('utf-8'))
            headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{encoded_filename}"

        if is_path_under_parents(filename) and is_valid_media_path(
            filename
        ):  # 认为永远不变,不要协商缓存了试试
            headers[
                "Cache-Control"
            ] = "public, max-age=31536000"  # 针对同样名字文件但实际上不同内容的文件要求必须传入创建时间来避免浏览器缓存
            headers["Expires"] = (datetime.now() + timedelta(days=365)).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )

        return FileResponse(
            filename,
            media_type=media_type,
            headers=headers,
        )
    
    @app.get(api_base + "/stream_video", dependencies=[Depends(verify_secret)])
    async def stream_video(path: str, request: Request):      
        check_path_trust(path)
        import mimetypes
        media_type, _ = mimetypes.guess_type(path)
        return range_requests_response(
            request, file_path=path, content_type=media_type
        )

    @app.get(api_base + "/video_cover", dependencies=[Depends(verify_secret)])
    async def video_cover(path: str, mt: str):        
        check_path_trust(path)
        if not cache_base_dir:
            return
        
        if not os.path.exists(path):
            raise HTTPException(status_code=404)
        if not os.path.isfile(path) and get_video_type(path):
            raise HTTPException(status_code=400, detail=f"{path} is not a video file")
        # 生成缓存文件的路径
        hash_dir = hashlib.md5((path + mt).encode("utf-8")).hexdigest()
        hash = hash_dir
        cache_dir = os.path.join(cache_base_dir, "iib_cache", "video_cover", hash_dir)
        cache_path = os.path.join(cache_dir, "cover.webp")
        # 如果缓存文件存在，则直接返回该文件
        if os.path.exists(cache_path):
            return FileResponse(
                cache_path,
                media_type="image/webp",
                headers={
                    "Cache-Control": "no-store",
                },
            )
        if not is_media_file(path):
            raise HTTPException(status_code=400, detail=f"{path} is not a video file")
        # 如果缓存文件不存在，则生成缩略图并保存
        try:
            import imageio.v3 as iio
            logger.info(
                "Generating video cover thumbnail: path=%s, mt=%s, cache_path=%s",
                path,
                mt,
                cache_path,
            )
            frame = iio.imread(
                path,
                index=16,
                plugin="pyav",
            )

            os.makedirs(cache_dir, exist_ok=True)
            iio.imwrite(cache_path, frame, extension=".webp")
            logger.info("Saved video cover thumbnail: %s", cache_path)
        except Exception as e:
            # record full stack trace and contextual info in English
            logger.exception(
                "Failed to generate video cover for path=%s mt=%s cache_dir=%s: %s",
                path,
                mt,
                cache_dir,
                e,
            )
            # return a clear HTTP error (detail contains exception message)
            raise HTTPException(status_code=500, detail=f"Failed to generate video cover: {e}")

        # 返回缓存文件
        return FileResponse(
            cache_path,
            media_type="image/webp",
            headers={
                "Cache-Control": "no-store",
            },
        )
    
    class SetTargetFrameAsCoverReq(BaseModel):
        base64_img: str
        path: str
        updated_time: str

    def save_base64_image(base64_str, file_path):
        if base64_str.startswith('data:image'):
            base64_str = base64_str.split(',')[1]
        image_data = base64.b64decode(base64_str)
        with open(file_path, 'wb') as file:
            file.write(image_data)

    @app.post(api_base+ "/set_target_frame_as_video_cover", dependencies=[Depends(verify_secret), Depends(write_permission_required)])
    async def set_target_frame_as_video_cover(req: SetTargetFrameAsCoverReq):
        hash_dir = hashlib.md5((req.path + req.updated_time).encode("utf-8")).hexdigest()
        hash = hash_dir
        cache_dir = os.path.join(cache_base_dir, "iib_cache", "video_cover", hash_dir)
        cache_path = os.path.join(cache_dir, "cover.webp")

        os.makedirs(cache_dir, exist_ok=True)
        
        save_base64_image(req.base64_img, cache_path)
        return FileResponse(
            cache_path,
            media_type="image/webp",
            headers={"ETag": hash},
        )

    @app.post(api_base + "/send_img_path", dependencies=[Depends(verify_secret)])
    async def api_set_send_img_path(path: str):
        send_img_path["value"] = path

    # 等待图片信息生成完成
    @app.get(api_base + "/gen_info_completed", dependencies=[Depends(verify_secret)])
    async def api_set_send_img_path():
        for _ in range(30):  # timeout 3s
            if send_img_path["value"] == "":  # 等待setup里面生成完成
                return True
            v = send_img_path["value"]
            # is_dev and logger.info("gen_info_completed %s %s", _, v)
            await asyncio.sleep(0.1)
        return send_img_path["value"] == ""

    @app.get(api_base + "/image_geninfo", dependencies=[Depends(verify_secret)])
    async def image_geninfo(path: str):
        # 使用 get_exif_data 函数，它已经支持视频文件
        from scripts.iib.db.update_image_data import get_exif_data
        try:
            result = get_exif_data(path)
            return result.raw_info or ""
        except Exception as e:
            logger.error(f"Failed to get geninfo for {path}: {e}")
            return ""

    class GeninfoBatchReq(BaseModel):
        paths: List[str]

    @app.post(api_base + "/image_geninfo_batch", dependencies=[Depends(verify_secret)])
    async def image_geninfo_batch(req: GeninfoBatchReq):
        from scripts.iib.db.update_image_data import get_exif_data
        res = {}
        conn = DataBase.get_conn()
        for path in req.paths:
            try:
                img = DbImg.get(conn, path)
                if img:
                    res[path] = img.exif
                else:
                    result = get_exif_data(path)
                    res[path] = result.raw_info or ""
            except Exception as e:
                logger.error(f"Failed to get geninfo for {path}: {e}", stack_info=True)
                res[path] = ""
        return res

    @app.get(api_base + "/image_exif", dependencies=[Depends(verify_secret)])
    async def image_exif(path: str):
        try:
            if get_video_type(path):
                return {}
            with Image.open(path) as img:
                exif_data = {}
                try:
                    exif_dict = img._getexif()
                    if exif_dict:
                        exif_data = {str(k): str(v) for k, v in exif_dict.items()}
                except AttributeError:
                    pass
                
                info_data = {k: str(v) for k, v in img.info.items() if not k.startswith('exif')}
                exif_data.update(info_data)
                
                return exif_data
        except Exception as e:
            logger.error(f"Failed to get exif for {path}: {e}")
            return {}


    class CheckPathExistsReq(BaseModel):
        paths: List[str]

    @app.post(api_base + "/check_path_exists", dependencies=[Depends(verify_secret)])
    async def check_path_exists(req: CheckPathExistsReq):
        update_all_scanned_paths()
        res = {}
        for path in req.paths:
            res[path] = os.path.exists(path) and is_path_trusted(path)
        return res

    @app.get(api_base)
    def index_bd():
        if fe_public_path:
            with open(index_html_path, "r", encoding="utf-8") as file:
                content = file.read().replace(DEFAULT_BASE, fe_public_path)
                return Response(content=content, media_type="text/html")
        return FileResponse(index_html_path)
    
    static_dir = get_data_file_path("vue/dist") if is_exe_ver else f"{cwd}/vue/dist" 
    @app.get(api_base + "/fe-static/{file_path:path}")
    async def serve_static_file(file_path: str):
        file_full_path = f"{static_dir}/{file_path}"
        if file_path.endswith(".js"):
            with open(file_full_path, "r", encoding="utf-8") as file:
                content = file.read().replace(DEFAULT_BASE, fe_public_path)
            return Response(content=content, media_type="text/javascript")
        else:
            return FileResponse(file_full_path)

    class OpenFolderReq(BaseModel):
        path: str

    @app.post(
        api_base + "/open_folder",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    def open_folder_using_explore(req: OpenFolderReq):
        if not is_path_trusted(req.path):
            raise HTTPException(status_code=403)
        open_folder(*os.path.split(req.path))

    @app.post(api_base + "/shutdown")
    async def shutdown_app():
        # This API endpoint is mainly used as a sidecar in Tauri applications to shut down the application
        if not kwargs.get("enable_shutdown"):
            raise HTTPException(status_code=403, detail="Shutdown is disabled.")
        os.kill(os.getpid(), 9)
        return {"message": "Application is shutting down."}


    class PackReq(BaseModel):
        paths: List[str]
        compress: bool
        pack_only: bool


    @app.post(
        api_base + "/zip",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    def zip_files(req: PackReq):
        for path in req.paths:
            check_path_trust(path)
            if not os.path.isfile(path):
                   raise HTTPException(400, "The corresponding path must be a file.")
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
        zip_temp_dir = os.path.join(cwd, "zip_temp")
        os.makedirs(zip_temp_dir, exist_ok=True)
        file_path = os.path.join(zip_temp_dir, f"iib_batch_download_{timestamp}.zip")
        create_zip_file(req.paths, file_path, req.compress)
        if not req.pack_only:
            return FileResponse(file_path, media_type="application/zip")
    
    @app.post(
        api_base + "/open_with_default_app",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    def open_target_file_withDefault_app(req: OpenFolderReq):
        check_path_trust(req.path)
        open_file_with_default_app(req.path)

    # ========== Flatten Folder API ==========

    class FlattenFolderReq(BaseModel):
        folder_path: str
        dry_run: bool = True  # If True, only check for conflicts without moving

    class FlattenFolderResp(BaseModel):
        success: bool
        total_files: int
        conflicts: List[str]  # List of duplicate file names
        moved_files: int = 0
        errors: List[str] = []

    @app.post(
        api_base + "/flatten_folder",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def flatten_folder(req: FlattenFolderReq):
        """
        Flatten a folder by moving all files from subfolders to the root folder.
        Two phases:
        1. dry_run=True: Scan and check for filename conflicts
        2. dry_run=False: Actually move the files
        """
        check_path_trust(req.folder_path)
        folder_path = os.path.normpath(req.folder_path)

        if not os.path.isdir(folder_path):
            raise HTTPException(400, detail=f"Not a folder: {folder_path}")

        # Collect all files recursively
        all_files = []  # List of (full_path, filename)
        for root, dirs, files in os.walk(folder_path):
            # Skip the root folder itself
            if root == folder_path:
                continue
            for f in files:
                if is_media_file(f):
                    full_path = os.path.join(root, f)
                    all_files.append((full_path, f))

        # Check for filename conflicts
        filename_count = {}
        for _, filename in all_files:
            filename_count[filename] = filename_count.get(filename, 0) + 1

        conflicts = [name for name, count in filename_count.items() if count > 1]

        if req.dry_run:
            return FlattenFolderResp(
                success=len(conflicts) == 0,
                total_files=len(all_files),
                conflicts=conflicts
            )

        # If not dry_run, check for conflicts first
        if conflicts:
            raise HTTPException(
                400,
                detail=f"Cannot flatten: {len(conflicts)} filename conflicts found"
            )

        # Actually move files
        conn = DataBase.get_conn()
        moved_count = 0
        errors = []

        for full_path, filename in all_files:
            try:
                dest_path = os.path.join(folder_path, filename)

                # Move the file
                shutil.move(full_path, dest_path)

                # Update database
                img = DbImg.get(conn, full_path)
                if img:
                    img.update_path(conn, dest_path, force=True)

                # Move associated txt file if exists
                txt_path = get_img_geninfo_txt_path(full_path)
                if txt_path and os.path.exists(txt_path):
                    txt_dest = os.path.join(folder_path, os.path.basename(txt_path))
                    shutil.move(txt_path, txt_dest)

                moved_count += 1
            except Exception as e:
                errors.append(f"{full_path}: {str(e)}")

        # Clean up empty directories
        for root, dirs, files in os.walk(folder_path, topdown=False):
            if root != folder_path:
                try:
                    if not os.listdir(root):  # Directory is empty
                        os.rmdir(root)
                except Exception:
                    pass

        return FlattenFolderResp(
            success=len(errors) == 0,
            total_files=len(all_files),
            conflicts=[],
            moved_files=moved_count,
            errors=errors
        )

    @app.post(
        api_base + "/batch_top_4_media_info",
        dependencies=[Depends(verify_secret)],
    )
    def batch_get_top_4_media_cover_info(req: PathsReq):
        for path in req.paths:
            check_path_trust(path)
        res = {}
        for path in req.paths:
            res[path] = get_top_4_media_info(path)
        return res

    db_api_base = api_base + "/db"

    @app.get(db_api_base + "/basic_info", dependencies=[Depends(verify_secret)])
    async def get_db_basic_info():
        conn = DataBase.get_conn()
        img_count = DbImg.count(conn)
        tags = Tag.get_all(conn)
        expired_dirs = Folder.get_expired_dirs(conn)
        return {
            "img_count": img_count,
            "tags": tags,
            "expired": len(expired_dirs) != 0,
            "expired_dirs": expired_dirs,
        }
    
    
    

    @app.get(db_api_base + "/random_images", dependencies=[Depends(verify_secret)])
    async def random_image():
        conn = DataBase.get_conn()
        imgs = DbImg.get_random_images(conn, 128)
        return filter_allowed_files([x.to_file_info() for x in imgs])

    @app.get(db_api_base + "/expired_dirs", dependencies=[Depends(verify_secret)])
    async def get_db_expired():
        conn = DataBase.get_conn()
        expired_dirs = Folder.get_expired_dirs(conn)
        return {
            "expired": len(expired_dirs) != 0,
            "expired_dirs": expired_dirs,
        }

    @app.post(
        db_api_base + "/update_image_data",
        dependencies=[Depends(verify_secret)],
    )
    async def update_image_db_data():
        try:
            DataBase._initing = True
            conn = DataBase.get_conn()
            img_count = DbImg.count(conn)
            update_extra_paths(conn)
            dirs = (
                get_img_search_dirs()
                if img_count == 0
                else Folder.get_expired_dirs(conn)
            ) + mem["extra_paths"]

            update_image_data(dirs)
        finally:
            DataBase._initing = False

    class SearchBySubstrReq(BaseModel):
        surstr: str
        cursor: Optional[str] = ""
        regexp: Optional[str] = ""
        folder_paths: List[str] = None
        size: Optional[int] = 200
        path_only: Optional[bool] = False
        media_type: Optional[str] = None  # "all", "image", "video"

    @app.post(db_api_base + "/search_by_substr", dependencies=[Depends(verify_secret)])
    async def search_by_substr(req: SearchBySubstrReq):
        if IIB_DEBUG:
            logger.info(req)
        conn = DataBase.get_conn()
        folder_paths=normalize_paths(req.folder_paths or [], os.getcwd())
        if(not folder_paths and req.folder_paths):
            return { "files": [], "cursor": Cursor(has_next=False) }
        imgs, next_cursor = DbImg.find_by_substring(
            conn=conn, 
            substring=req.surstr, 
            cursor=req.cursor, 
            limit=req.size,
            regexp=req.regexp,
            folder_paths=folder_paths,
            path_only=req.path_only,
            media_type=req.media_type
        )
        return {
            "files": filter_allowed_files([x.to_file_info() for x in imgs]),
            "cursor": next_cursor
        }
    
    class MatchImagesByTagsReq(BaseModel):
        and_tags: Optional[List[int]] = []
        or_tags: Optional[List[int]] = []
        not_tags: Optional[List[int]] = []
        cursor: Optional[str] = ""
        folder_paths: List[str] = None
        size: Optional[int] = 200
        random_sort: Optional[bool] = False

    @app.post(db_api_base + "/match_images_by_tags", dependencies=[Depends(verify_secret)])
    async def match_image_by_tags(req: MatchImagesByTagsReq):
        if IIB_DEBUG:
            logger.info(req)
        conn = DataBase.get_conn()
        folder_paths=normalize_paths(req.folder_paths or [], os.getcwd())
        if(not folder_paths and req.folder_paths):
            return { "files": [], "cursor": Cursor(has_next=False) }
        imgs, next_cursor = ImageTag.get_images_by_tags(
            conn=conn,
            tag_dict={"and": req.and_tags, "or": req.or_tags, "not": req.not_tags},
            cursor=req.cursor,
            folder_paths=folder_paths,
            limit=req.size,
            random_sort=req.random_sort
        )
        return {
            "files": filter_allowed_files([x.to_file_info() for x in imgs]),
            "cursor": next_cursor
        }

    @app.get(db_api_base + "/img_selected_custom_tag", dependencies=[Depends(verify_secret)])
    async def get_img_selected_custom_tag(path: str):
        path = os.path.normpath(path)
        if not is_valid_media_path(path):
            return []
        conn = DataBase.get_conn()
        update_extra_paths(conn)
        if not is_path_under_parents(path):
            return []
        img = DbImg.get(conn, path)
        if not img:
            if DbImg.count(conn) == 0:
                return []
            update_image_data([os.path.dirname(path)])
            img = DbImg.get(conn, path)
        assert img
        # tags = Tag.get_all_custom_tag()
        return ImageTag.get_tags_for_image(conn, img.id, type="custom")

    @app.post(db_api_base + "/get_image_tags", dependencies=[Depends(verify_secret)])
    async def get_img_tags(req: PathsReq):
        conn = DataBase.get_conn()
        return ImageTag.batch_get_tags_by_path(conn, req.paths)
    

    # update tag
    class UpdateTagReq(BaseModel):
        id: int
        color: str

    @app.post(
        db_api_base + "/update_tag",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def update_tag(req: UpdateTagReq):
        conn = DataBase.get_conn()
        tag = Tag.get(conn, req.id)
        if tag:
            tag.color = req.color
            tag.save(conn)
        conn.commit()


    class ToggleCustomTagToImgReq(BaseModel):
        img_path: str
        tag_id: int

    @app.post(
        db_api_base + "/toggle_custom_tag_to_img",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def toggle_custom_tag_to_img(req: ToggleCustomTagToImgReq):
        conn = DataBase.get_conn()
        path = os.path.normpath(req.img_path)
        update_extra_paths(conn)
        if not is_path_under_parents(path):
            raise HTTPException(
                400,
                '当前文件不在搜索路径内，你可以将它添加到扫描路径再尝试。在右上角的"更多"里面'
                if locale == "zh"
                else 'The current file is not within the scan path. You can add it to the scan path and try again. In the top right corner, click on "More".',
            )
        img = DbImg.get(conn, path)
        if not img:
            if DbImg.count(conn):
                # update_image_data([os.path.dirname(path)])
                add_image_data_single(path)
                img = DbImg.get(conn, path)
            else:
                raise HTTPException(
                    400,
                    "你需要先通过图像搜索页生成索引"
                    if locale == "zh"
                    else "You need to generate an index through the image search page first.",
                )
        tags = ImageTag.get_tags_for_image(
            conn=conn, image_id=img.id, type="custom", tag_id=req.tag_id
        )
        is_remove = len(tags)
        if is_remove:
            ImageTag.remove(conn, img.id, tags[0].id)
        else:
            ImageTag(img.id, req.tag_id).save(conn)
        conn.commit()
        return {"is_remove": is_remove}

    class BatchUpdateImageReq(BaseModel):
        img_paths: List[str]
        action: str
        tag_id: int

    @app.post(
        db_api_base + "/batch_update_image_tag",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def batch_update_image_tag(req: BatchUpdateImageReq):
        assert req.action in ["add", "remove"]
        conn = DataBase.get_conn()
        paths: List[str] = seq(req.img_paths).map(os.path.normpath).to_list()
        update_extra_paths(conn)
        for path in paths:
            if not is_path_under_parents(path):
                raise HTTPException(
                    400,
                    '当前文件不在搜索路径内，你可以将它添加到扫描路径再尝试。在右上角的"更多"里面'
                    if locale == "zh"
                    else 'The current file is not within the scan path. You can add it to the scan path and try again. In the top right corner, click on "More".',
                )
            img = DbImg.get(conn, path)
            if not img:
                if DbImg.count(conn):
                    add_image_data_single(path)
                    img = DbImg.get(conn, path)
                else: 
                    raise HTTPException(
                        400,
                        "你需要先通过图像搜索页生成索引"
                        if locale == "zh"
                        else "You need to generate an index through the image search page first.",
                    )
        try:            
            for path in paths:
                img = DbImg.get(conn, path)
                if req.action == "add":
                    ImageTag(img.id, req.tag_id).save_or_ignore(conn)
                else:
                    ImageTag.remove(conn, img.id, req.tag_id)
        finally:
            conn.commit()

    class AddCustomTagReq(BaseModel):
        tag_name: str

    @app.post(
        db_api_base + "/add_custom_tag",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def add_custom_tag(req: AddCustomTagReq):
        conn = DataBase.get_conn()
        tag = Tag.get_or_create(conn, name=req.tag_name, type="custom")
        conn.commit()
        if tag is None:
            raise HTTPException(400, "Invalid tag name")
        return tag
    
    class RenameFileReq(BaseModel):
        path: str
        name: str

    @app.post(
        db_api_base + "/rename",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def rename_file(req: RenameFileReq):
        conn = DataBase.get_conn()
        try:
            # Normalize the paths

            path = os.path.normpath(req.path)
            new_path = os.path.join(os.path.dirname(path), req.name)

            # Check if the file exists
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail="File not found")

            # Check if a file with the new name already exists
            if os.path.exists(new_path):
                raise HTTPException(status_code=400, detail="A file with the new name already exists")
            close_video_file_reader(path)
            img = DbImg.get(conn, path)
            if img:
                img.update_path(conn, new_path)
                conn.commit()

            # Perform the file rename operation
            os.rename(path, new_path)


            return {"detail": "File renamed successfully", "new_path": new_path}

        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    class RemoveCustomTagReq(BaseModel):
        tag_id: int

    @app.post(
        db_api_base + "/remove_custom_tag",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def remove_custom_tag(req: RemoveCustomTagReq):
        conn = DataBase.get_conn()
        ImageTag.remove(conn, tag_id=req.tag_id)
        Tag.remove(conn, req.tag_id)

    class RemoveCustomTagFromReq(BaseModel):
        img_id: int
        tag_id: str

    @app.post(
        db_api_base + "/remove_custom_tag_from_img",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def remove_custom_tag_from_img(req: RemoveCustomTagFromReq):
        conn = DataBase.get_conn()
        ImageTag.remove(conn, image_id=req.img_id, tag_id=req.tag_id)


    # ===== 主题聚类 / Embedding（拆分到独立模块，减少 api.py 体积）=====
    topic_cluster_funcs = mount_topic_cluster_routes(
        app=app,
        db_api_base=db_api_base,
        verify_secret=verify_secret,
        write_permission_required=write_permission_required,
        openai_base_url=OPENAI_BASE_URL,
        openai_api_key=OPENAI_API_KEY,
        embedding_model=EMBEDDING_MODEL,
        ai_model=AI_MODEL,
    )

    # ===== Tag 关系图 =====
    mount_tag_graph_routes(
        app=app,
        db_api_base=db_api_base,
        verify_secret=verify_secret,
        embedding_model=EMBEDDING_MODEL,
        ai_model=AI_MODEL,
        openai_base_url=OPENAI_BASE_URL,
        openai_api_key=OPENAI_API_KEY,
    )

    # ===== 智能文件整理 =====
    mount_organize_routes(
        app=app,
        db_api_base=db_api_base,
        verify_secret=verify_secret,
        write_permission_required=write_permission_required,
        start_cluster_job_func=topic_cluster_funcs["start_cluster_job"],
        get_cluster_job_status_func=topic_cluster_funcs["get_cluster_job_status"],
    )


    class ExtraPathModel(BaseModel):
        path: str
        types: List[str]

    @app.post(
        f"{db_api_base}/extra_paths",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def create_extra_path(extra_path: ExtraPathModel):
        if enable_access_control:
            if not is_path_under_parents(extra_path.path):
                raise HTTPException(status_code=403)
        conn = DataBase.get_conn()
        path = ExtraPath.get_target_path(conn, extra_path.path)
        if path:
            for t in extra_path.types:
                path.types.append(t)
            path.types = unique_by(path.types)
        else:
            path = ExtraPath(extra_path.path, extra_path.types)
        try:
            path.save(conn)
        finally:
            conn.commit()

    class ExtraPathAliasModel(BaseModel):
        path: str
        alias: str


    @app.post(
        f"{db_api_base}/alias_extra_path",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def alias_extra_path(req: ExtraPathAliasModel):
        conn = DataBase.get_conn()
        path = ExtraPath.get_target_path(conn, req.path)
        if not path:
            raise HTTPException(400)
        path.alias = req.alias
        try:
            path.save(conn)
        finally:
            conn.commit()
        return path
        

    @app.get(
        f"{db_api_base}/extra_paths",
        dependencies=[Depends(verify_secret)],
    )
    async def read_extra_paths():
        conn = DataBase.get_conn()
        return ExtraPath.get_extra_paths(conn)
    


    @app.delete(
        f"{db_api_base}/extra_paths",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def delete_extra_path(extra_path: ExtraPathModel):
        path = to_abs_path(extra_path.path)
        conn = DataBase.get_conn()
        ExtraPath.remove(
            conn,
            path,
            extra_path.types,
            img_search_dirs=get_img_search_dirs(),
            all_scanned_paths=mem["all_scanned_paths"],
        )
        update_extra_paths(conn)

    
    @app.post(
        f"{db_api_base}/rebuild_index",
        dependencies=[Depends(verify_secret), Depends(write_permission_required)],
    )
    async def rebuild_index():
        update_extra_paths(conn = DataBase.get_conn())
        rebuild_image_index(search_dirs = get_img_search_dirs() + mem["extra_paths"])


    # AI 相关路由
    class AIChatRequest(BaseModel):
        messages: List[dict]
        temperature: Optional[float] = 0.7
        max_tokens: Optional[int] = None
        stream: Optional[bool] = False

    @app.post(f"{api_base}/ai-chat", dependencies=[Depends(verify_secret), Depends(write_permission_required)])
    async def ai_chat(req: AIChatRequest):
        """通用AI聊天接口，转发到OpenAI兼容API"""
        if not OPENAI_API_KEY:
            raise HTTPException(status_code=500, detail="OpenAI API Key not configured")

        try:
            payload = {
                "model": AI_MODEL,
                "messages": req.messages,
                "temperature": req.temperature,
                "stream": req.stream
            }
            if req.max_tokens:
                payload["max_tokens"] = req.max_tokens

            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }

            response = requests.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=60
            )

            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail=response.text)

            return response.json()

        except requests.RequestException as e:
            logger.error(f"AI API request failed: {e}")
            raise HTTPException(status_code=500, detail=f"AI API request failed: {str(e)}")

