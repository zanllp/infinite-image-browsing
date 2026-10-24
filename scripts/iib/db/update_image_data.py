import os
import time
from contextlib import closing
from typing import Callable, Dict, List, Optional

from scripts.iib.db.datamodel import (
    Image as DbImg,
    Tag,
    ImageTag,
    DataBase,
    Folder,
    commit_with_retry,
)
from scripts.iib.tool import (
    is_valid_media_path,
    get_modified_date,
    get_formatted_date,
    get_video_type,
    is_dev,
    is_image_file,
    is_audio_file,
    case_insensitive_get,
    get_img_geninfo_txt_path,
    parse_generation_parameters,
)
from scripts.iib.parsers.model import ImageGenerationInfo, ImageGenerationParams
from scripts.iib.logger import logger
from scripts.iib.parsers.index import parse_image_info
from scripts.iib.plugin import plugin_inst_map
from scripts.iib.auto_tag import AutoTagMatcher
from scripts.iib.userdata import apply_pending_user_tags


# 定义一个函数来获取图片文件的EXIF数据
def get_exif_data(file_path):
    if get_video_type(file_path):
        # 对于视频文件，尝试读取对应的txt标签文件
        txt_path = get_img_geninfo_txt_path(file_path)
        if txt_path:
            try:
                with open(txt_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        # 复用现有解析逻辑，添加视频标识
                        params = parse_generation_parameters(content + "\nSource Identifier: Video Tags")
                        return ImageGenerationInfo(
                            content,
                            ImageGenerationParams(
                                meta=params["meta"],
                                pos_prompt=params["pos_prompt"],
                                extra=params,
                            ),
                        )
            except Exception as e:
                if is_dev:
                    logger.error("Failed to read video txt file %s: %s", txt_path, e)
        return ImageGenerationInfo()
    try:
        return parse_image_info(file_path)
    except Exception as e:
        if is_dev:
            logger.error("get_exif_data %s", e)
    return ImageGenerationInfo()


def _notify_new_image(on_new_image, file_path: str) -> None:
    """回调通知调用方（worker 入队缩略图预热）；回调异常不影响索引。"""
    if on_new_image is None:
        return
    try:
        on_new_image(file_path)
    except Exception as e:
        logger.error("on_new_image callback failed for %s: %s", file_path, e)


def _flush_tag_counts(conn, tag_incr_count_rec: Dict[int, int]) -> None:
    """在调用方事务内把待累计的标签计数原子写回（不 commit）。

    旧实现是 Tag.get + tag.count += n + tag.save（INSERT OR REPLACE 整行），
    在后台线程和其它写者并发时会覆盖别人刚写的 count；改成 SET count = count + ?。
    """
    if not tag_incr_count_rec:
        return
    for tag_id, increment in tag_incr_count_rec.items():
        Tag.add_count(conn, tag_id, increment)
    tag_incr_count_rec.clear()


def _refresh_file_meta_only(conn, img: DbImg, file_path: str) -> None:
    """exif_edited=1 的图被增量重扫：只更新文件 size/date，不重解析、不动标签。"""
    size = os.path.getsize(file_path)
    date = get_modified_date(file_path)
    with closing(conn.cursor()) as cur:
        cur.execute(
            "UPDATE image SET size = ?, date = ? WHERE id = ?", (size, date, img.id)
        )
    img.size = size
    img.date = date


def _remove_stale_auto_tags(conn, image_id: int) -> None:
    """文件内容变更后重新解析前，先清掉该图旧的自动标签（custom 标签保留）。"""
    with closing(conn.cursor()) as cur:
        cur.execute(
            """DELETE FROM image_tag WHERE image_id = ?
            AND tag_id IN (SELECT id FROM tag WHERE type <> 'custom')""",
            (image_id,),
        )


def build_single_img_idx(conn, file_path, is_rebuild, safe_save_img_tag, on_new_image=None):
    """为单个文件建立/更新索引。

    Returns:
        True: 该文件被新增或更新（调用方应计入 files_indexed）；
        False: 已有且未变化，跳过。
    """
    img = DbImg.get(conn, file_path)

    if img and is_rebuild and img.exif_edited:
        logger.info(f"Image {file_path} has been manually edited, skipping rebuild.")
        return False

    parsed_params = None
    if is_rebuild:
        info = get_exif_data(file_path)
        parsed_params = info.params
        if not img:
            img = DbImg(
                file_path,
                info.raw_info,
                os.path.getsize(file_path),
                get_modified_date(file_path),
            )
            img.save(conn)
            _notify_new_image(on_new_image, file_path)
    else:
        if img:
            if img.date == get_modified_date(img.path):
                return False
            if img.exif_edited:
                # 手动编辑过 exif 的图：不重解析、不动标签（custom/auto 全部保留）
                _refresh_file_meta_only(conn, img, file_path)
                _notify_new_image(on_new_image, file_path)
                return True
            # 文件被替换：清掉旧的自动标签，避免旧 model/sampler 等标签残留
            _remove_stale_auto_tags(conn, img.id)
        info = get_exif_data(file_path)
        parsed_params = info.params
        img = DbImg(
            file_path,
            info.raw_info,
            os.path.getsize(file_path),
            get_modified_date(file_path),
        )
        # upsert：已存在的行保留 id 和 exif_edited，所以 custom 标签引用不会断
        img.save(conn)
        _notify_new_image(on_new_image, file_path)

    if not parsed_params:
        return True
    meta = parsed_params.meta
    lora = parsed_params.extra.get("lora", [])
    lyco = parsed_params.extra.get("lyco", [])
    if "final_width" in meta and "final_height" in meta:
        size_str = str(meta["final_width"]) + " × " + str(meta["final_height"])
    else:
        size_str = "Unknown Size"
    pos = parsed_params.pos_prompt
    size_tag = Tag.get_or_create(
        conn,
        size_str,
        type="size",
    )
    if size_tag:
        safe_save_img_tag(ImageTag(img.id, size_tag.id))
    # 确定媒体类型：Image / Video / Audio / Unknown
    if is_image_file(file_path):
        media_type_name = "Image"
    elif is_audio_file(file_path):
        media_type_name = "Audio"
    elif get_video_type(file_path):
        media_type_name = "Video"
    else:
        media_type_name = "Unknown"
    media_type_tag = Tag.get_or_create(conn, media_type_name, 'Media Type')
    if media_type_tag:
        safe_save_img_tag(ImageTag(img.id, media_type_tag.id))
    keys = [
        "Model",
        "Sampler",
        "Source Identifier",
        "Postprocess upscale by",
        "Postprocess upscaler",
        "Size",
        "Refiner",
        "Hires upscaler"
    ]
    keys += get_extra_meta_keys_from_plugins(meta.get("Source Identifier", ""))
    for k in keys:
        v = case_insensitive_get(meta, k)
        if not v:
            continue

        tag = Tag.get_or_create(conn, str(v), k)
        if tag:
            safe_save_img_tag(ImageTag(img.id, tag.id))
            if "Hires upscaler" == k:
                tag = Tag.get_or_create(conn, 'Hires All', k)
                if tag:
                    safe_save_img_tag(ImageTag(img.id, tag.id))
            elif "Refiner" == k:
                tag = Tag.get_or_create(conn, 'Refiner All', k)
                if tag:
                    safe_save_img_tag(ImageTag(img.id, tag.id))
    for i in lora:
        tag = Tag.get_or_create(conn, i["name"], "lora")
        if tag:
            safe_save_img_tag(ImageTag(img.id, tag.id))
    for i in lyco:
        tag = Tag.get_or_create(conn, i["name"], "lyco")
        if tag:
            safe_save_img_tag(ImageTag(img.id, tag.id))
    for k in pos:
        tag = Tag.get_or_create(conn, k, "pos")
        if tag:
            safe_save_img_tag(ImageTag(img.id, tag.id))

    AutoTagMatcher.get_instance(conn).apply(img.id, parsed_params, conn=conn)
    return True


def _scan_folder(
    conn,
    folder_path: str,
    *,
    is_rebuild: bool = False,
    on_new_image: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    should_pause: Optional[Callable[[], bool]] = None,
    batch_commit_size: int = 50,
) -> dict:
    """单层扫描实现（scan_folder_incremental / update_image_data 共用）。"""
    stats = {
        "folders": 0,
        "files_indexed": 0,
        "files_skipped": 0,
        "files_failed": 0,
        "paused": False,
    }
    folder_path = os.path.normpath(folder_path)
    tag_incr_count_rec: Dict[int, int] = {}

    def is_paused() -> bool:
        if should_pause is None:
            return False
        try:
            return bool(should_pause())
        except Exception as e:
            logger.error("should_pause callback failed: %s", e)
            return False

    if is_paused():
        stats["paused"] = True
        return stats

    # 关键：listdir 之前记录 mtime 并作为扫描结束后的水位写入。
    # 若扫描期间目录里新增了文件，水位仍是旧 mtime，下一轮还会判定过期，
    # 修复"扫描期间新增的图被写进水位 → 永久漏图"的 bug。
    try:
        folder_stat = os.stat(folder_path)
        mtime_before = get_formatted_date(folder_stat.st_mtime)
    except OSError:
        # 目录不存在/不可读：不写水位
        return stats

    if not is_rebuild:
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT modified_date FROM folders WHERE path = ?", (folder_path,)
            )
            folder_record = cur.fetchone()
        if folder_record is not None and folder_record[0] == mtime_before:
            return stats

    try:
        filenames = os.listdir(folder_path)
    except OSError:
        return stats

    stats["folders"] = 1
    commit_interval_sec = 1.0
    batch_size = max(1, int(batch_commit_size))
    pending = 0
    last_commit_at = time.monotonic()

    def safe_save_img_tag(img_tag: ImageTag):
        tag_incr_count_rec[img_tag.tag_id] = (
            tag_incr_count_rec.get(img_tag.tag_id, 0) + 1
        )
        img_tag.save_or_ignore(conn)

    def flush_pending() -> None:
        nonlocal pending, last_commit_at
        _flush_tag_counts(conn, tag_incr_count_rec)
        commit_with_retry(conn)
        pending = 0
        last_commit_at = time.monotonic()
        if on_progress is not None:
            try:
                on_progress(dict(stats))
            except Exception as e:
                logger.error("on_progress callback failed: %s", e)

    for filename in filenames:
        if is_paused():
            # 不写水位就返回，下轮仍判定过期（已处理部分的 DB 变更先提交，避免丢工作；
            # 已提交的图依赖 mtime 未变在重扫时被跳过，不会被重复计数）
            stats["paused"] = True
            break
        file_path = os.path.normpath(os.path.join(folder_path, filename))
        try:
            if not is_valid_media_path(file_path):
                # 子目录 / 非媒体文件不归单层扫描器处理
                continue
            updated = build_single_img_idx(
                conn,
                file_path,
                is_rebuild,
                safe_save_img_tag,
                on_new_image=on_new_image,
            )
            if updated:
                stats["files_indexed"] += 1
                pending += 1
            else:
                stats["files_skipped"] += 1
        except Exception as e:
            # 单图异常只计入 files_failed，不中断整个目录
            stats["files_failed"] += 1
            logger.error(
                "Tag generation failed. Skipping this file. file:%s error: %s",
                file_path,
                e,
            )
        if pending >= batch_size or (
            pending > 0 and time.monotonic() - last_commit_at >= commit_interval_sec
        ):
            flush_pending()

    # 收尾：把剩余变更 + 标签计数提交（paused 时也提交已处理的图，但不写水位）
    if pending > 0 or tag_incr_count_rec:
        flush_pending()

    if not stats["paused"]:
        try:
            Folder.set_modified_date(conn, folder_path, mtime_before)
            commit_with_retry(conn)
        except Exception as e:
            logger.error("Failed to update folder watermark for %s: %s", folder_path, e)

    # 导入用户数据时路径还没被索引的标签，等文件出现在 image 表后自动补上
    apply_pending_user_tags(conn)

    return stats


def scan_folder_incremental(
    conn,
    folder_path: str,
    *,
    on_new_image: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    should_pause: Optional[Callable[[], bool]] = None,
    batch_commit_size: int = 50,
) -> dict:
    """增量扫描一个目录的直接子项（不递归），分片提交并回调进度。

    供后台 worker 使用：worker 负责找出过期目录并逐个传入，本函数不递归。

    Args:
        conn: 调用方线程自己的数据库连接（不要跨线程共享）。
        folder_path: 要扫描的目录。
        on_new_image: 每成功新增/更新一张图后回调其绝对路径。
        on_progress: 每次 batch commit 后回调 stats 快照。
        should_pause: 每张图处理前调用；返回 True 则立即停止（不写目录水位）。
        batch_commit_size: 每 N 张图或约 1s commit 一次。

    Returns:
        dict: {"folders", "files_indexed", "files_skipped", "files_failed", "paused"}
    """
    return _scan_folder(
        conn,
        folder_path,
        on_new_image=on_new_image,
        on_progress=on_progress,
        should_pause=should_pause,
        batch_commit_size=batch_commit_size,
    )


def _update_folder_recursive(conn, folder_path: str, is_rebuild: bool) -> None:
    folder_path = os.path.normpath(folder_path)
    if not Folder.check_need_update(conn, folder_path):
        return
    print(f"Processing folder: {folder_path}")
    _scan_folder(conn, folder_path, is_rebuild=is_rebuild)
    # 目录水位已由 _scan_folder 写入；子目录仍需递归处理
    try:
        filenames = os.listdir(folder_path)
    except OSError:
        return
    for filename in filenames:
        child_path = os.path.normpath(os.path.join(folder_path, filename))
        try:
            if os.path.isdir(child_path):
                _update_folder_recursive(conn, child_path, is_rebuild)
        except Exception as e:
            logger.error(
                "Tag generation failed. Skipping this folder. folder:%s error: %s",
                child_path,
                e,
            )


def update_image_data(search_dirs: List[str], is_rebuild = False):
    """同步（递归）更新索引，保持旧的 CLI / API 语义；内部按目录复用增量扫描器。"""
    conn = DataBase.get_conn()
    if is_rebuild:
        Folder.remove_all(conn)

    for dir_path in search_dirs:
        _update_folder_recursive(conn, dir_path, is_rebuild)
    commit_with_retry(conn)
    apply_pending_user_tags(conn)


def add_image_data_single(file_path):
    conn = DataBase.get_conn()
    tag_incr_count_rec: Dict[int, int] = {}

    def safe_save_img_tag(img_tag):
        tag_incr_count_rec[img_tag.tag_id] = (
            tag_incr_count_rec.get(img_tag.tag_id, 0) + 1
        )
        img_tag.save_or_ignore(conn)

    file_path = os.path.normpath(file_path)
    try:
        if not is_valid_media_path(file_path):
            return
        build_single_img_idx(conn, file_path, False, safe_save_img_tag)
    except Exception as e:
        logger.error("Tag generation failed. Skipping this file. file:%s error: %s", file_path, e)
        conn.commit()
    finally:
        _flush_tag_counts(conn, tag_incr_count_rec)
        commit_with_retry(conn)
        apply_pending_user_tags(conn)


def rebuild_image_index(search_dirs: List[str]):
    conn = DataBase.get_conn()
    with closing(conn.cursor()) as cur:
        cur.execute(
            """DELETE FROM image_tag
            WHERE image_tag.tag_id IN (
                SELECT tag.id FROM tag WHERE tag.type <> 'custom'
            )
            """
        )
        cur.execute("""DELETE FROM tag WHERE tag.type <> 'custom'""")
        commit_with_retry(conn)
        update_image_data(search_dirs=search_dirs, is_rebuild=True)


def get_extra_meta_keys_from_plugins(source_identifier: str):
    try:
        plugin = plugin_inst_map.get(source_identifier)
        if plugin:
            return plugin.extra_convert_to_tag_meta_keys
    except Exception as e:
        logger.error("get_extra_meta_keys_from_plugins %s", e)
    return []
