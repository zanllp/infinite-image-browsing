"""
Trend statistics and contribution heatmap data.
Read-only aggregation from existing tables; no schema changes required.
"""

import json
import os
from typing import List, Optional
from pydantic import BaseModel
from fastapi import Depends, FastAPI

from scripts.iib.db.datamodel import DataBase, GlobalSetting
from scripts.iib.logger import logger

TREND_CACHE_KEY = "trend_stats_v1"
TREND_CACHE_VERSION = 1
_TREND_MAX_TAGS = int(os.getenv("IIB_TREND_MAX_TAGS", "20") or "20")


class DailyContribution(BaseModel):
    date: str
    count: int


class MonthlyTrend(BaseModel):
    month: str
    count: int


class TagStat(BaseModel):
    name: str
    count: int


class TrendStatsResp(BaseModel):
    total_images: int
    total_disk_usage: int
    daily_contributions: List[DailyContribution]
    monthly_trends: List[MonthlyTrend]
    top_models: List[TagStat]
    top_samplers: List[TagStat]
    top_source: List[TagStat]
    top_lora: List[TagStat]
    cached_at: Optional[str] = None


def mount_trend_routes(
    app: FastAPI,
    db_api_base: str,
    verify_secret,
):
    @app.get(
        db_api_base + "/stats/trend",
        dependencies=[Depends(verify_secret)],
    )
    async def get_trend_stats():
        from scripts.iib.db.datamodel import Image

        conn = DataBase.get_conn()

        # Check cache — invalidate on version bump or image count change
        cached = GlobalSetting.get_setting(conn, TREND_CACHE_KEY)
        if cached and isinstance(cached, dict):
            if cached.get("cache_version") == TREND_CACHE_VERSION and cached.get("total_images") == Image.count(conn):
                return cached

        limit = _TREND_MAX_TAGS

        # Daily contributions (heatmap data)
        daily_rows = conn.execute(
            """
            SELECT DATE(date) as day, COUNT(*) as count
            FROM image
            WHERE date IS NOT NULL AND date != ''
            GROUP BY DATE(date)
            ORDER BY day ASC
            """
        ).fetchall()
        daily_contributions = [
            DailyContribution(date=row[0], count=row[1]) for row in daily_rows
        ]

        # Monthly trends
        monthly_rows = conn.execute(
            """
            SELECT substr(date, 1, 7) as month, COUNT(*) as count
            FROM image
            WHERE date IS NOT NULL AND date != ''
            GROUP BY substr(date, 1, 7)
            ORDER BY month ASC
            """
        ).fetchall()
        monthly_trends = [
            MonthlyTrend(month=row[0], count=row[1]) for row in monthly_rows
        ]

        # Top models, samplers, lora from pre-computed tag.count
        def _fetch_tag_stats(tag_type: str, limit: int) -> List[TagStat]:
            rows = conn.execute(
                "SELECT name, count FROM tag WHERE type = ? ORDER BY count DESC LIMIT ?",
                (tag_type, limit),
            ).fetchall()
            return [TagStat(name=row[0], count=row[1]) for row in rows]

        top_models = _fetch_tag_stats("Model", limit)
        top_samplers = _fetch_tag_stats("Sampler", limit)
        top_lora = _fetch_tag_stats("lora", limit)
        top_source = _fetch_tag_stats("Source Identifier", limit)

        # Total disk usage (SUM scan fast enough for typical libraries)
        disk_row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM image").fetchone()
        total_disk_usage = int(disk_row[0]) if disk_row else 0

        total_images = sum(c.count for c in daily_contributions)

        result = {
            "total_images": total_images,
            "total_disk_usage": total_disk_usage,
            "daily_contributions": [c.model_dump() for c in daily_contributions],
            "monthly_trends": [m.model_dump() for m in monthly_trends],
            "top_models": [m.model_dump() for m in top_models],
            "top_samplers": [s.model_dump() for s in top_samplers],
            "top_lora": [l.model_dump() for l in top_lora],
            "top_source": [s.model_dump() for s in top_source],
            "cache_version": TREND_CACHE_VERSION,
            "cached_at": None,
        }

        # Save cache
        try:
            from datetime import datetime

            result["cached_at"] = datetime.now().isoformat()
            GlobalSetting.save_setting(
                conn, TREND_CACHE_KEY, json.dumps(result, ensure_ascii=False)
            )
        except Exception as e:
            logger.warning(f"Failed to cache trend stats: {e}")

        return result
