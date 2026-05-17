"""
Trend statistics and contribution heatmap data.
Read-only aggregation from existing tables; no schema changes required.
"""

import os
from typing import List
from pydantic import BaseModel
from fastapi import Depends, FastAPI

from scripts.iib.db.datamodel import DataBase

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
        conn = DataBase.get_conn()
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

        disk_row = conn.execute("SELECT COALESCE(SUM(size), 0) FROM image").fetchone()
        total_disk_usage = int(disk_row[0]) if disk_row else 0

        total_images = sum(c.count for c in daily_contributions)

        return {
            "total_images": total_images,
            "total_disk_usage": total_disk_usage,
            "daily_contributions": [c.model_dump() for c in daily_contributions],
            "monthly_trends": [m.model_dump() for m in monthly_trends],
            "top_models": [m.model_dump() for m in top_models],
            "top_samplers": [s.model_dump() for s in top_samplers],
            "top_lora": [l.model_dump() for l in top_lora],
            "top_source": [s.model_dump() for s in top_source],
        }
