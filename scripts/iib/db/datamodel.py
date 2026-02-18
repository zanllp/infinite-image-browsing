from datetime import datetime
import json
import random
from sqlite3 import Connection, connect
from enum import Enum
import sqlite3
from typing import Dict, List, Optional, TypedDict, Union
from scripts.iib.tool import (
    cwd,
    get_modified_date,
    human_readable_size,
    tags_translate,
    is_dev,
    find,
    unique_by,
)
from contextlib import closing
import os
import threading
import re
import hashlib


class FileInfoDict(TypedDict):
    type: str
    date: float
    size: int
    name: str
    bytes: bytes
    created_time: float
    fullpath: str


class Cursor:
    def __init__(self, has_next=True, next=""):
        self.has_next = has_next
        self.next = next


class DataBase:
    local = threading.local()

    _initing = False

    num = 0

    path = os.getenv("IIB_DB_PATH", "iib.db")

    @classmethod
    def get_conn(clz) -> Connection:
        # for : sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread
        if hasattr(clz.local, "conn"):
            return clz.local.conn
        else:
            conn = clz.init()
            clz.local.conn = conn

            return conn
        
    @classmethod
    def get_db_file_path(clz):
        return clz.path if os.path.isabs(clz.path) else os.path.join(cwd, clz.path)

    @classmethod
    def init(clz):
        # 创建连接并打开数据库
        conn = connect(clz.get_db_file_path())

        # # 禁用 WAL 模式，使用传统的 DELETE 日志模式
        # conn.execute("PRAGMA journal_mode=DELETE")

        def regexp(expr, item):
            if not isinstance(item, str):
                return False
            reg = re.compile(expr, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
            return reg.search(item) is not None

        conn.create_function("regexp", 2, regexp)
        try:
            Folder.create_table(conn)
            ImageTag.create_table(conn)
            Tag.create_table(conn)
            Image.create_table(conn)
            ExtraPath.create_table(conn)
            DirCoverCache.create_table(conn)
            GlobalSetting.create_table(conn)
            ImageEmbedding.create_table(conn)
            ImageEmbeddingFail.create_table(conn)
            TopicTitleCache.create_table(conn)
            TopicClusterCache.create_table(conn)
        finally:
            conn.commit()
        clz.num += 1
        if is_dev:
            print(f"当前连接数{clz.num}")
        return conn


class Image:
    def __init__(self, path, exif=None, size=0, date="", id=None):
        self.path = path
        self.exif = exif
        self.id = id
        self.size = size
        self.date = date

    def to_file_info(self) -> FileInfoDict:
        return {
            "type": "file",
            "id": self.id,
            "date": self.date,
            "created_date": self.date,
            "size": human_readable_size(self.size),
            "is_under_scanned_path": True,
            "bytes": self.size,
            "name": os.path.basename(self.path),
            "fullpath": self.path,
        }

    def save(self, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE  INTO image (path, exif, size, date) VALUES (?, ?, ?, ?)",
                (self.path, self.exif, self.size, self.date),
            )
            self.id = cur.lastrowid

    def update_path(self, conn: Connection, new_path: str, force=False):
        self.path = os.path.normpath(new_path)
        with closing(conn.cursor()) as cur:
            if force: # force update path
                cur.execute("DELETE FROM image WHERE path = ?", (self.path,))
            cur.execute("UPDATE image SET path = ? WHERE id = ?", (self.path, self.id))

    @classmethod
    def get(cls, conn: Connection, id_or_path):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT * FROM image WHERE id = ? OR path = ?", (id_or_path, id_or_path)
            )
            row = cur.fetchone()
            if row is None:
                return None
            else:
                return cls.from_row(row)

    @classmethod
    def get_by_ids(cls, conn: Connection, ids: List[int]) -> List["Image"]:
        if not ids:
            return []

        query = """
            SELECT * FROM image
            WHERE id IN ({})
        """.format(
            ",".join("?" * len(ids))
        )

        with closing(conn.cursor()) as cur:
            cur.execute(query, ids)
            rows = cur.fetchall()

        images = []
        for row in rows:
            images.append(cls.from_row(row))
        return images

    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS image (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            path TEXT UNIQUE,
                            exif TEXT,
                            size INTEGER,
                            date TEXT
                        )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS image_idx_path ON image(path)")

    @classmethod
    def count(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM image")
            count = cur.fetchone()[0]
            return count

    @classmethod
    def from_row(cls, row: tuple):
        image = cls(path=row[1], exif=row[2], size=row[3], date=row[4])
        image.id = row[0]
        return image

    @classmethod
    def remove(cls, conn: Connection, image_id: int) -> None:
        with closing(conn.cursor()) as cur:
            # Manual cascade delete to avoid leaving orphan rows in related tables.
            # NOTE: SQLite foreign key constraints are often disabled by default unless
            # PRAGMA foreign_keys=ON is set. We still delete related rows explicitly
            # so deletion works regardless of FK settings and keeps DB clean.
            cur.execute("DELETE FROM image_embedding WHERE image_id = ?", (int(image_id),))
            cur.execute("DELETE FROM image_embedding_fail WHERE image_id = ?", (int(image_id),))
            cur.execute("DELETE FROM image_tag WHERE image_id = ?", (int(image_id),))
            cur.execute("DELETE FROM image WHERE id = ?", (image_id,))
            conn.commit()

    @classmethod
    def safe_batch_remove(cls, conn: Connection, image_ids: List[int]) -> None:
        if not (image_ids):
            return
        with closing(conn.cursor()) as cur:
            try:
                placeholders = ",".join("?" * len(image_ids))
                # Manual cascade delete for related tables.
                # Keep this in sync with tables referencing image.id.
                cur.execute(
                    f"DELETE FROM image_embedding WHERE image_id IN ({placeholders})",
                    image_ids,
                )
                cur.execute(
                    f"DELETE FROM image_embedding_fail WHERE image_id IN ({placeholders})",
                    image_ids,
                )
                cur.execute(
                    f"DELETE FROM image_tag WHERE image_id IN ({placeholders})",
                    image_ids,
                )
                cur.execute(
                    f"DELETE FROM image WHERE id IN ({placeholders})", image_ids
                )
            except BaseException as e:
                print(e)
            finally:
                conn.commit()

    @classmethod
    def find_by_substring(
        cls, conn: Connection, substring: str, limit: int = 500, cursor="", regexp="", path_only=False,
        folder_paths: List[str] = [], media_type: str = None
    ) -> tuple[List["Image"], Cursor]:
        api_cur = Cursor()
        with closing(conn.cursor()) as cur:
            params = []
            where_clauses = []
            if regexp:
                if path_only:
                    where_clauses.append("(path REGEXP ?)")
                    params.append(regexp)
                else:
                    where_clauses.append("((exif REGEXP ?) OR (path REGEXP ?))")
                    params.extend((regexp, regexp))
            else:
                if path_only:
                    where_clauses.append("(path LIKE ?)")
                    params.append(f"%{substring}%")
                else:
                    where_clauses.append("(path LIKE ? OR exif LIKE ?)")
                    params.extend((f"%{substring}%", f"%{substring}%"))
            if cursor:
                where_clauses.append("(date < ?)")
                params.append(cursor)
            if folder_paths:
                folder_clauses = []
                for folder_path in folder_paths:
                    folder_clauses.append("(image.path LIKE ?)")
                    params.append(os.path.join(folder_path, "%"))
                where_clauses.append("(" + " OR ".join(folder_clauses) + ")")
            
            # 构建SQL查询
            if media_type and media_type.lower() != "all":
                # 需要JOIN到image_tag和tag表来过滤媒体类型
                sql = """SELECT DISTINCT image.* FROM image 
                        INNER JOIN image_tag ON image.id = image_tag.image_id 
                        INNER JOIN tag ON image_tag.tag_id = tag.id"""
                # 添加媒体类型过滤条件
                media_type_name = "Image" if media_type.lower() == "image" else "Video"
                where_clauses.append("(tag.type = 'Media Type' AND tag.name = ?)")
                params.append(media_type_name)
            else:
                sql = "SELECT * FROM image"
            
            if where_clauses:
                sql += " WHERE "
                sql += " AND ".join(where_clauses)
            sql += " ORDER BY date DESC LIMIT ? "
            params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()

        api_cur.has_next = len(rows) >= limit
        images = []
        deleted_ids = []
        for row in rows:
            img = cls.from_row(row)
            if os.path.exists(img.path):
                images.append(img)
            else:
                deleted_ids.append(img.id)
        cls.safe_batch_remove(conn, deleted_ids)
        if images:
            api_cur.next = str(images[-1].date)
        return images, api_cur
    
    @classmethod
    def get_random_images(cls, conn: Connection, size: int) -> List["Image"]:
        if size <= 0:
            return []
        
        images = []
        deleted_ids = []
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM image ORDER BY RANDOM() LIMIT ?", (size,))
            rows = cur.fetchall()
            
            for row in rows:
                img = cls.from_row(row)
                if os.path.exists(img.path):
                    images.append(img)
                else:
                    deleted_ids.append(img.id)
        
        if deleted_ids:
            cls.safe_batch_remove(conn, deleted_ids)
        
        return images


class ImageEmbedding:
    """
    Store embeddings for image prompt text.

    Notes:
    - vec is stored as float32 bytes (little-endian), compatible with Python's array('f').
    - text_hash is used to skip recomputation when prompt text doesn't change.
    """

    @classmethod
    def create_table(cls, conn: Connection):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS image_embedding (
                    image_id INTEGER PRIMARY KEY,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    vec BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (image_id) REFERENCES image(id)
                )"""
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS image_embedding_idx_model_hash ON image_embedding(model, text_hash)"
            )

    @staticmethod
    def compute_text_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @classmethod
    def get_by_image_ids(cls, conn: Connection, image_ids: List[int]):
        if not image_ids:
            return {}
        placeholders = ",".join("?" * len(image_ids))
        query = f"SELECT image_id, model, dim, text_hash, vec, updated_at FROM image_embedding WHERE image_id IN ({placeholders})"
        with closing(conn.cursor()) as cur:
            cur.execute(query, image_ids)
            rows = cur.fetchall()
        res = {}
        for row in rows:
            res[row[0]] = {
                "image_id": row[0],
                "model": row[1],
                "dim": row[2],
                "text_hash": row[3],
                "vec": row[4],
                "updated_at": row[5],
            }
        return res

    @classmethod
    def upsert(
        cls,
        conn: Connection,
        image_id: int,
        model: str,
        dim: int,
        text_hash: str,
        vec_blob: bytes,
        updated_at: Optional[str] = None,
    ):
        updated_at = updated_at or datetime.now().isoformat()
        with closing(conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO image_embedding (image_id, model, dim, text_hash, vec, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(image_id) DO UPDATE SET
                    model = excluded.model,
                    dim = excluded.dim,
                    text_hash = excluded.text_hash,
                    vec = excluded.vec,
                    updated_at = excluded.updated_at
                """,
                (image_id, model, dim, text_hash, vec_blob, updated_at),
            )


class ImageEmbeddingFail:
    """
    Cache embedding failures per image+model+text_hash to avoid repeatedly hitting the API
    for known-failing inputs. This helps keep clustering/search usable by skipping bad items.
    """

    @classmethod
    def create_table(cls, conn: Connection):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS image_embedding_fail (
                    image_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    error TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(image_id, model, text_hash)
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS image_embedding_fail_idx_model ON image_embedding_fail(model)")

    @classmethod
    def get_by_image_ids(cls, conn: Connection, image_ids: List[int], model: str) -> Dict[int, Dict]:
        if not image_ids:
            return {}
        ids = [int(x) for x in image_ids]
        placeholders = ",".join(["?"] * len(ids))
        with closing(conn.cursor()) as cur:
            cur.execute(
                f"SELECT image_id, text_hash, error, updated_at FROM image_embedding_fail WHERE model = ? AND image_id IN ({placeholders})",
                (str(model), *ids),
            )
            rows = cur.fetchall()
        out: Dict[int, Dict] = {}
        for image_id, text_hash, error, updated_at in rows or []:
            out[int(image_id)] = {
                "text_hash": str(text_hash or ""),
                "error": str(error or ""),
                "updated_at": str(updated_at or ""),
            }
        return out

    @classmethod
    def upsert(
        cls,
        conn: Connection,
        *,
        image_id: int,
        model: str,
        text_hash: str,
        error: str,
        updated_at: Optional[str] = None,
    ):
        updated_at = updated_at or datetime.now().isoformat()
        with closing(conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO image_embedding_fail (image_id, model, text_hash, error, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(image_id, model, text_hash) DO UPDATE SET
                      error = excluded.error,
                      updated_at = excluded.updated_at
                """,
                (int(image_id), str(model), str(text_hash), str(error or "")[:600], str(updated_at)),
            )

    @classmethod
    def delete(cls, conn: Connection, *, image_id: int, model: str):
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM image_embedding_fail WHERE image_id = ? AND model = ?", (int(image_id), str(model)))


class TopicTitleCache:
    """
    Cache cluster titles/keywords to avoid repeated LLM calls.
    """

    @classmethod
    def create_table(cls, conn: Connection):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS topic_title_cache (
                    cluster_hash TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    model TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS topic_title_cache_idx_model ON topic_title_cache(model)"
            )

    @classmethod
    def get(cls, conn: Connection, cluster_hash: str):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT title, keywords, model, updated_at FROM topic_title_cache WHERE cluster_hash = ?",
                (cluster_hash,),
            )
            row = cur.fetchone()
        if not row:
            return None
        title, keywords, model, updated_at = row
        try:
            kw = json.loads(keywords) if isinstance(keywords, str) else []
        except Exception:
            kw = []
        if not isinstance(kw, list):
            kw = []
        return {"title": title, "keywords": kw, "model": model, "updated_at": updated_at}

    @classmethod
    def get_all_keywords_frequency(cls, conn: Connection, model: Optional[str] = None) -> Dict[str, int]:
        """
        Get keyword frequency from all cached clusters.
        Optionally filter by model.
        Returns a dictionary mapping keyword -> frequency.
        """
        with closing(conn.cursor()) as cur:
            if model:
                cur.execute(
                    "SELECT keywords FROM topic_title_cache WHERE model = ?",
                    (model,),
                )
            else:
                cur.execute(
                    "SELECT keywords FROM topic_title_cache",
                )
            rows = cur.fetchall()
        
        keyword_frequency: Dict[str, int] = {}
        for row in rows:
            keywords_str = row[0] if row else None
            try:
                keywords = json.loads(keywords_str) if isinstance(keywords_str, str) else []
            except Exception:
                keywords = []
            if isinstance(keywords, list):
                for kw in keywords:
                    if isinstance(kw, str) and kw.strip():
                        keyword_frequency[kw] = keyword_frequency.get(kw, 0) + 1
        return keyword_frequency

    @classmethod
    def upsert(
        cls,
        conn: Connection,
        cluster_hash: str,
        title: str,
        keywords: List[str],
        model: str,
        updated_at: Optional[str] = None,
    ):
        updated_at = updated_at or datetime.now().isoformat()
        kw = json.dumps([str(x) for x in (keywords or [])], ensure_ascii=False)
        with closing(conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO topic_title_cache (cluster_hash, title, keywords, model, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cluster_hash) DO UPDATE SET
                    title = excluded.title,
                    keywords = excluded.keywords,
                    model = excluded.model,
                    updated_at = excluded.updated_at
                """,
                (cluster_hash, title, kw, model, updated_at),
            )

    @classmethod
    def update_keywords(
        cls,
        conn: Connection,
        cluster_hash: str,
        keywords: List[str],
        updated_at: Optional[str] = None,
    ):
        """
        Update only the keywords for an existing cluster cache entry.
        """
        updated_at = updated_at or datetime.now().isoformat()
        kw = json.dumps([str(x) for x in (keywords or [])], ensure_ascii=False)
        with closing(conn.cursor()) as cur:
            cur.execute(
                "UPDATE topic_title_cache SET keywords = ?, updated_at = ? WHERE cluster_hash = ?",
                (kw, updated_at, cluster_hash),
            )


class TopicClusterCache:
    """
    Persist the final clustering result (clusters/noise) to avoid re-clustering when:
    - embeddings haven't changed (by max(updated_at) & count), and
    - clustering parameters are unchanged.

    This is intentionally lightweight:
    - result is stored as JSON text
    - caller defines cache_key (sha1 over params + folders + normalize version + lang, etc.)
    """

    @classmethod
    def create_table(cls, conn: Connection):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS topic_cluster_cache (
                            cache_key TEXT PRIMARY KEY,
                            folders TEXT NOT NULL,
                            model TEXT NOT NULL,
                            params TEXT NOT NULL,
                            embeddings_count INTEGER NOT NULL,
                            embeddings_max_updated_at TEXT NOT NULL,
                            result TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS topic_cluster_cache_idx_model ON topic_cluster_cache(model)")

    @classmethod
    def get(cls, conn: Connection, cache_key: str):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT folders, model, params, embeddings_count, embeddings_max_updated_at, result, updated_at FROM topic_cluster_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
        if not row:
            return None
        folders, model, params, embeddings_count, embeddings_max_updated_at, result, updated_at = row
        try:
            folders_obj = json.loads(folders) if isinstance(folders, str) else []
        except Exception:
            folders_obj = []
        try:
            params_obj = json.loads(params) if isinstance(params, str) else {}
        except Exception:
            params_obj = {}
        try:
            result_obj = json.loads(result) if isinstance(result, str) else None
        except Exception:
            result_obj = None
        return {
            "cache_key": cache_key,
            "folders": folders_obj if isinstance(folders_obj, list) else [],
            "model": str(model),
            "params": params_obj if isinstance(params_obj, dict) else {},
            "embeddings_count": int(embeddings_count or 0),
            "embeddings_max_updated_at": str(embeddings_max_updated_at or ""),
            "result": result_obj,
            "updated_at": str(updated_at or ""),
        }

    @classmethod
    def upsert(
        cls,
        conn: Connection,
        *,
        cache_key: str,
        folders: List[str],
        model: str,
        params: Dict,
        embeddings_count: int,
        embeddings_max_updated_at: str,
        result: Dict,
        updated_at: Optional[str] = None,
    ):
        updated_at = updated_at or datetime.now().isoformat()
        folders_s = json.dumps([str(x) for x in (folders or [])], ensure_ascii=False)
        params_s = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
        result_s = json.dumps(result or {}, ensure_ascii=False)
        with closing(conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO topic_cluster_cache
                   (cache_key, folders, model, params, embeddings_count, embeddings_max_updated_at, result, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     folders = excluded.folders,
                     model = excluded.model,
                     params = excluded.params,
                     embeddings_count = excluded.embeddings_count,
                     embeddings_max_updated_at = excluded.embeddings_max_updated_at,
                     result = excluded.result,
                     updated_at = excluded.updated_at
                """,
                (
                    cache_key,
                    folders_s,
                    str(model),
                    params_s,
                    int(embeddings_count or 0),
                    str(embeddings_max_updated_at or ""),
                    result_s,
                    updated_at,
                ),
            )

class Tag:
    def __init__(self, name: str, score: int, type: str, count=0, color = ""):
        self.name = name
        self.score = score
        self.type = type
        self.count = count
        self.id = None
        self.color = color
        self.display_name = tags_translate.get(name)

    @staticmethod
    def validate_tag_name(name: str):
        if not name:
            return None
        
        # Check if name starts with Chinese characters
        if len(name) > 0 and '\u4e00' <= name[0] <= '\u9fff':
            # Chinese starts: max 12 characters
            if len(name) > 12:
                return "INVALID_TAG_NAME_TOO_LONG"
        else:
            # Other languages: max 8 words and 40 characters
            words = name.split()
            if len(words) > 8 and len(name) > 40:
                return "INVALID_TAG_TOO_MANY_WORDS"
        
        return None

    def save(self, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE INTO tag (id, name, score, type, count, color) VALUES (?, ?, ?, ?, ?, ?)",
                (self.id, self.name, self.score, self.type, self.count, self.color),
            )
            self.id = cur.lastrowid

    @classmethod
    def remove(cls, conn, tag_id):
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM tag WHERE id = ?", (tag_id,))
            conn.commit()

    @classmethod
    def get(cls, conn: Connection, id):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM tag WHERE id = ?", (id,))
            row = cur.fetchone()
            if row is None:
                return None
            else:
                return cls.from_row(row)

    @classmethod
    def get_all_custom_tag(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM tag where type = 'custom'")
            rows = cur.fetchall()
            tags: list[Tag] = []
            for row in rows:
                tags.append(cls.from_row(row))
            return tags

    @classmethod
    def get_all(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT COUNT(*) FROM tag")
            total_count = cur.fetchone()[0]
            
            tags: list[Tag] = []
            
            if total_count > 4096:
                # Get all non-pos tags
                cur.execute("SELECT * FROM tag WHERE type != 'pos'")
                rows = cur.fetchall()
                for row in rows:
                    tags.append(cls.from_row(row))
                
                # Get top 4096 pos tags ordered by count (descending)
                cur.execute("SELECT * FROM tag WHERE type = 'pos' ORDER BY count DESC LIMIT 4096")
                pos_rows = cur.fetchall()
                for row in pos_rows:
                    tags.append(cls.from_row(row))
            else:
                # Get all tags normally
                cur.execute("SELECT * FROM tag")
                rows = cur.fetchall()
                for row in rows:
                    tags.append(cls.from_row(row))
            
            print(f"tag: loaded {len(tags)} tags (total: {total_count})")
            return tags

    @classmethod
    def get_or_create(cls, conn: Connection, name: str, type: str):
        assert name and type
        
        # Validate tag name
        error_name = cls.validate_tag_name(name)
        if error_name:
            # Return None for invalid tag names
            return None
        
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT tag.* FROM tag WHERE name = ? and type = ?", (name, type)
            )
            row = cur.fetchone()
            if row is None:
                tag = cls(name=name, score=0, type=type)
                tag.save(conn)
                return tag
            else:
                return cls.from_row(row)

    @classmethod
    def from_row(cls, row: tuple):
        tag = cls(name=row[1], score=row[2], type=row[3], count=row[4], color=row[5])
        tag.id = row[0]
        return tag

    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS tag (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            score INTEGER,
            type TEXT,
            count INTEGER,
            UNIQUE(name, type) ON CONFLICT REPLACE
            );
            """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS tag_idx_name ON tag(name)")
            cur.execute(
                """INSERT OR IGNORE INTO tag(name, score, type, count)
                VALUES ("like", 0, "custom", 0);
                """
            )
            try:
                cur.execute(
                    """ALTER TABLE tag
                    ADD COLUMN color TEXT DEFAULT ''"""
                )
            except sqlite3.OperationalError as e:
                pass


class ImageTag:
    def __init__(self, image_id: int, tag_id: int):
        assert tag_id and image_id
        self.image_id = image_id
        self.tag_id = tag_id

    def save(self, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO image_tag (image_id, tag_id, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (self.image_id, self.tag_id),
            )

    def save_or_ignore(self, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO image_tag (image_id, tag_id, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (self.image_id, self.tag_id),
            )

    @classmethod
    def get_tags_for_image(
        cls,
        conn: Connection,
        image_id: int,
        tag_id: Optional[int] = None,
        type: Optional[str] = None,
    ):
        with closing(conn.cursor()) as cur:
            query = "SELECT tag.* FROM tag INNER JOIN image_tag ON tag.id = image_tag.tag_id WHERE image_tag.image_id = ?"
            params = [image_id]
            if tag_id:
                query += " AND image_tag.tag_id = ?"
                params.append(tag_id)
            if type:
                query += " AND tag.type = ?"
                params.append(type)
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            return [Tag.from_row(x) for x in rows]

    @classmethod
    def get_images_for_tag(cls, conn: Connection, tag_id):
        with closing(conn.cursor()) as cur:
            cur.execute(
                "SELECT image.* FROM image INNER JOIN image_tag ON image.id = image_tag.image_id WHERE image_tag.tag_id = ?",
                (tag_id,),
            )
            rows = cur.fetchall()
            images = []
            for row in rows:
                images.append(Image.from_row(row))
            return images

    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS image_tag (
                            image_id INTEGER,
                            tag_id INTEGER,
                            FOREIGN KEY (image_id) REFERENCES image(id),
                            FOREIGN KEY (tag_id) REFERENCES tag(id),
                            PRIMARY KEY (image_id, tag_id)
                        )"""
            )
            try:
                cur.execute(
                    """ALTER TABLE image_tag
                    ADD COLUMN created_at TIMESTAMP"""
                )
                
                cur.execute(
                    """UPDATE image_tag
                    SET created_at = CURRENT_TIMESTAMP
                    WHERE created_at IS NULL"""
                )
            except sqlite3.OperationalError as e:
                pass
            
    @classmethod
    def get_images_by_tags(
        cls,
        conn: Connection,
        tag_dict: Dict[str, List[int]],
        limit: int = 500,
        cursor="",
        folder_paths: List[str] = None,
        random_sort: bool = False,
    ) -> tuple[List[Image], Cursor]:
        query = """
            SELECT image.id, image.path, image.size,image.date
            FROM image
            INNER JOIN image_tag ON image.id = image_tag.image_id
        """

        where_clauses = []
        params = []

        for operator, tag_ids in tag_dict.items():
            if operator == "and" and tag_dict["and"]:
                where_clauses.append(
                    "tag_id IN ({})".format(",".join("?" * len(tag_ids)))
                )
                params.extend(tag_ids)
            elif operator == "not" and tag_dict["not"]:
                where_clauses.append(
                    """(image_id NOT IN (
  SELECT image_id
  FROM image_tag
  WHERE tag_id IN ({})
))""".format(
                        ",".join("?" * len(tag_ids))
                    )
                )
                params.extend(tag_ids)
            elif operator == "or" and tag_dict["or"]:
                where_clauses.append(
                    """(image_id IN (
  SELECT image_id
  FROM image_tag
  WHERE tag_id IN ({})
  GROUP BY image_id
  HAVING COUNT(DISTINCT tag_id) >= 1
)
)""".format(
                        ",".join("?" * len(tag_ids))
                    )
                )
                params.extend(tag_ids)    

        if folder_paths:
            folder_clauses = []
            for folder_path in folder_paths:
                folder_clauses.append("(image.path LIKE ?)")
                params.append(os.path.join(folder_path, "%"))
                print(folder_path)
            where_clauses.append("(" + " OR ".join(folder_clauses) + ")")

        if cursor and not random_sort:
            where_clauses.append("(image.date < ?)")
            params.append(cursor)
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += " GROUP BY image.id"
        if "and" in tag_dict and tag_dict['and']:
            query += " HAVING COUNT(DISTINCT tag_id) = ?"
            params.append(len(tag_dict["and"]))

        if random_sort:
            query += " ORDER BY RANDOM() LIMIT ?"
            # For random sort, use offset-based pagination
            if cursor:
                try:
                    offset = int(cursor)
                    query = query.replace("LIMIT ?", f"LIMIT ? OFFSET {offset}")
                except (ValueError, TypeError):
                    pass  # Invalid cursor, start from beginning
        else:
            query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        api_cur = Cursor()
        with closing(conn.cursor()) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            images = []
            deleted_ids = []
            for row in rows:
                img = Image(id=row[0], path=row[1], size=row[2], date=row[3])
                if os.path.exists(img.path):
                    images.append(img)
                else:
                    deleted_ids.append(img.id)
            Image.safe_batch_remove(conn, deleted_ids)
            api_cur.has_next = len(rows) >= limit
            if images:
                if random_sort:
                    # For random sort, use offset-based cursor
                    current_offset = int(cursor) if cursor else 0
                    api_cur.next = str(current_offset + len(images))
                else:
                    # For date sort, use date-based cursor
                    api_cur.next = str(images[-1].date)
            return images, api_cur

    @classmethod
    def batch_get_tags_by_path(
        cls, conn: Connection, paths: List[str], type="custom"
    ) -> Dict[str, List[Tag]]:
        if not paths:
            return {}
        tag_dict = {}
        with closing(conn.cursor()) as cur:
            placeholders = ",".join("?" * len(paths))
            query = f"""
                SELECT image.path, tag.* FROM image_tag
                INNER JOIN image ON image_tag.image_id = image.id
                INNER JOIN tag ON image_tag.tag_id = tag.id
                WHERE tag.type = '{type}' AND image.path IN ({placeholders})
            """
            cur.execute(query, paths)
            rows = cur.fetchall()
            for row in rows:
                path = row[0]
                tag = Tag.from_row(row[1:])
                if path in tag_dict:
                    tag_dict[path].append(tag)
                else:
                    tag_dict[path] = [tag]
        return tag_dict

    @classmethod
    def remove(
        cls,
        conn: Connection,
        image_id: Optional[int] = None,
        tag_id: Optional[int] = None,
    ) -> None:
        assert image_id or tag_id
        with closing(conn.cursor()) as cur:
            if tag_id and image_id:
                cur.execute(
                    "DELETE FROM image_tag WHERE image_id = ? and tag_id = ?",
                    (image_id, tag_id),
                )
            elif tag_id:
                cur.execute("DELETE FROM image_tag WHERE tag_id = ?", (tag_id,))
            else:
                cur.execute("DELETE FROM image_tag WHERE image_id = ?", (image_id,))
            conn.commit()


class Folder:
    def __init__(self, id: int, path: str, modified_date: str):
        self.id = id
        self.path = path
        self.modified_date = modified_date

    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS folders
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        path TEXT,
                        modified_date TEXT)"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS folders_idx_path ON folders(path)")

    @classmethod
    def check_need_update(cls, conn: Connection, folder_path: str):
        folder_path = os.path.normpath(folder_path)
        with closing(conn.cursor()) as cur:
            if not os.path.exists(folder_path):
                return False
            cur.execute("SELECT * FROM folders WHERE path=?", (folder_path,))
            folder_record = cur.fetchone()  # 如果这个文件夹没有记录，或者修改时间与数据库不同，则需要修改
            return not folder_record or (
                folder_record[2] != get_modified_date(folder_path)
            )

    @classmethod
    def update_modified_date_or_create(cls, conn: Connection, folder_path: str):
        folder_path = os.path.normpath(folder_path)
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM folders WHERE path = ?", (folder_path,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE folders SET modified_date = ? WHERE path = ?",
                    (get_modified_date(folder_path), folder_path),
                )
            else:
                cur.execute(
                    "INSERT INTO folders (path, modified_date) VALUES (?, ?)",
                    (folder_path, get_modified_date(folder_path)),
                )

    @classmethod
    def get_expired_dirs(cls, conn: Connection):
        dirs: List[str] = []
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM folders")
            result_set = cur.fetchall()
            extra_paths = ExtraPath.get_extra_paths(conn)
            for ep in extra_paths:
                if not find(result_set, lambda x: x[1] == ep.path):
                    dirs.append(ep.path)
            for row in result_set:
                folder_path = row[1]
                if (
                    os.path.exists(folder_path)
                    and get_modified_date(folder_path) != row[2]
                ):
                    dirs.append(folder_path)
            return unique_by(dirs, os.path.normpath)

    @classmethod
    def remove_folder(cls, conn: Connection, folder_path: str):
        folder_path = os.path.normpath(folder_path)
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM folders WHERE path = ?", (folder_path,))

    @classmethod
    def remove_all(cls, conn: Connection):
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM folders")
            conn.commit()


class ExtraPathType(Enum):
    scanned = "scanned"
    scanned_fixed = "scanned-fixed"
    walk = "walk"
    cli_only = "cli_access_only"


class ExtraPath:
    def __init__(self, path: str, types: Union[str, List[str]], alias = ''):
        self.path = os.path.normpath(path)
        self.types = types.split('+') if isinstance(types, str) else types
        self.alias = alias

    def save(self, conn):
        type_str = '+'.join(self.types)
        for type in self.types:
            assert type in [ExtraPathType.walk.value, ExtraPathType.scanned.value, ExtraPathType.scanned_fixed.value]
        with closing(conn.cursor()) as cur:
            cur.execute(
                "INSERT INTO extra_path (path, type, alias) VALUES (?, ?, ?) "
                "ON CONFLICT (path) DO UPDATE SET type = excluded.type, alias = excluded.alias",
                (self.path, type_str, self.alias),
            )

    @classmethod
    def get_target_path(cls, conn, path) -> Optional['ExtraPath']:
        path = os.path.normpath(path)
        query = f"SELECT * FROM extra_path where path = ?"
        params = (path,)
        with closing(conn.cursor()) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            paths: List[ExtraPath] = []
            for row in rows:
                path = row[0]
                if os.path.exists(path):
                    paths.append(ExtraPath(*row))
                else:                    
                    sql = "DELETE FROM extra_path WHERE path = ?"
                    cur.execute(sql, (path,))
                    conn.commit()
            return paths[0] if paths else None

    @classmethod
    def get_extra_paths(cls, conn) -> List["ExtraPath"]:
        query = "SELECT * FROM extra_path"
        with closing(conn.cursor()) as cur:
            cur.execute(query)
            rows = cur.fetchall()
            paths: List[ExtraPath] = []
            for row in rows:
                path = row[0]
                if os.path.exists(path):
                    paths.append(ExtraPath(*row))
                else:
                    cls.remove(conn, path)
            return paths

    @classmethod
    def remove(
        cls,
        conn,
        path: str,
        types: List[str] = None,
        img_search_dirs: Optional[List[str]] = [],
        all_scanned_paths: Optional[List[str]] = [],
    ):
        with closing(conn.cursor()) as cur:
            path = os.path.normpath(path)

            target = cls.get_target_path(conn, path)
            if not target:
                return
            new_types = []
            for type in target.types:
                if type not in types:
                    new_types.append(type)
            if new_types:
                target.types = new_types
                target.save(conn)
            else:
                sql = "DELETE FROM extra_path WHERE path = ?"
                cur.execute(sql, (path,))

            if path not in img_search_dirs:
                Folder.remove_folder(conn, path)
            conn.commit()

            # Clean up orphaned images that are no longer under any scanned path
            if all_scanned_paths:
                remaining_paths = [
                    os.path.normpath(p) for p in all_scanned_paths
                    if os.path.normpath(p) != path
                ]
                cls._cleanup_orphaned_images(conn, path, remaining_paths)

    @classmethod
    def _cleanup_orphaned_images(
        cls,
        conn,
        removed_path: str,
        remaining_paths: List[str],
    ):
        """
        Clean up images under removed_path that are not covered by any remaining_paths.
        An image is orphaned if it's under removed_path but not under any of the remaining paths.
        """
        with closing(conn.cursor()) as cur:
            # Find all images under the removed path
            cur.execute(
                "SELECT id, path FROM image WHERE path LIKE ?",
                (removed_path + os.sep + "%",)
            )
            rows = cur.fetchall()

            if not rows:
                return

            orphaned_ids = []
            for row in rows:
                img_id, img_path = row[0], row[1]
                img_path_normalized = os.path.normpath(img_path)

                # Check if this image is still covered by any remaining path
                is_still_owned = False
                for remaining_path in remaining_paths:
                    # Image is owned if its path starts with the remaining path
                    if img_path_normalized.startswith(remaining_path + os.sep) or img_path_normalized == remaining_path:
                        is_still_owned = True
                        break

                if not is_still_owned:
                    orphaned_ids.append(img_id)

            # Batch remove orphaned images
            if orphaned_ids:
                Image.safe_batch_remove(conn, orphaned_ids)

    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS extra_path (
                            path TEXT PRIMARY KEY,
                            type TEXT NOT NULL,
                            alias TEXT DEFAULT ''
                        )"""
            )
            try:
                cur.execute(
                    """ALTER TABLE extra_path
                    ADD COLUMN alias TEXT DEFAULT ''"""
                )
            except sqlite3.OperationalError:
                pass

class DirCoverCache:
    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS dir_cover_cache (
                    folder_path TEXT PRIMARY KEY,
                    modified_time TEXT,
                    media_files TEXT
                )
            """)

    @classmethod
    def is_cache_expired(cls, conn, folder_path):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT modified_time FROM dir_cover_cache WHERE folder_path = ?", (folder_path,))
            result = cur.fetchone()

        if not result:
            return True

        cached_time = datetime.fromisoformat(result[0])
        folder_modified_time = os.path.getmtime(folder_path)
        return datetime.fromtimestamp(folder_modified_time) > cached_time

    @classmethod
    def cache_media_files(cls, conn, folder_path, media_files):
        media_files_json = json.dumps(media_files)
        with closing(conn.cursor()) as cur:
            cur.execute("""
                INSERT INTO dir_cover_cache (folder_path, modified_time, media_files)
                VALUES (?, ?, ?)
                ON CONFLICT(folder_path) DO UPDATE SET modified_time = excluded.modified_time, media_files = excluded.media_files
            """, (folder_path, datetime.now().isoformat(), media_files_json))
            conn.commit()

    @classmethod
    def get_cached_media_files(cls, conn, folder_path):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT media_files FROM dir_cover_cache WHERE folder_path = ?", (folder_path,))
            result = cur.fetchone()

        if result:
            media_files_json = result[0]
            return json.loads(media_files_json)
        else:
            return []
        
# Global settings storage, also use as key-value store
class GlobalSetting:
    @classmethod
    def create_table(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS global_setting (
                            setting_json TEXT,
                            name TEXT PRIMARY KEY,
                            created_time TEXT,
                            modified_time TEXT
                        )"""
            )

    @classmethod
    def get_setting(cls, conn, name):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT setting_json FROM global_setting WHERE name = ?", (name,))
            result = cur.fetchone()
            if result:
                return json.loads(result[0])
            else:
                return None

    @classmethod
    def save_setting(cls, conn, name: str, setting: str):
        json.loads(setting) # check if it is valid json
        with closing(conn.cursor()) as cur:
            cur.execute(
                """INSERT INTO global_setting (setting_json, name, created_time, modified_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET setting_json = excluded.setting_json, modified_time = excluded.modified_time
                """,
                (setting, name, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            conn.commit()


    @classmethod
    def remove_setting(cls, conn, name: str):
        with closing(conn.cursor()) as cur:
            cur.execute("DELETE FROM global_setting WHERE name = ?", (name,))
            conn.commit()

    @classmethod
    def get_all_settings(cls, conn):
        with closing(conn.cursor()) as cur:
            cur.execute("SELECT * FROM global_setting")
            rows = cur.fetchall()
            settings = {}
            for row in rows:
                settings[row[1]] = json.loads(row[0])
            return settings

