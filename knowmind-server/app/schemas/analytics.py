from __future__ import annotations

from pydantic import BaseModel


class AnalyticsOverviewOut(BaseModel):
    days: int
    chat_turns: int
    search_hits: int
    rag_cites: int
    unique_users: int
    total_events: int


class TopItemOut(BaseModel):
    item_id: str
    title: str
    count: int
    search_hits: int = 0
    rag_cites: int = 0


class TopItemsOut(BaseModel):
    items: list[TopItemOut]


class TrendPointOut(BaseModel):
    date: str
    search_hit: int = 0
    rag_cite: int = 0
    chat_turn: int = 0
    total: int = 0


class AnalyticsTrendOut(BaseModel):
    days: int
    points: list[TrendPointOut]
