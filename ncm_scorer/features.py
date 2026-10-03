"""特征工程：把歌曲元数据 + 快照时间序列转成打分/训练用的特征向量.

特征设计原则：
- 全部来自歌曲发布早期可观测的数据（发布后数天内），避免标签泄漏；
- 对量纲敏感的计数特征一律 log1p 压缩；
- 评论增速依赖至少两次快照（每日运行 daily.py 自然累积）；
- 歌手上榜热忽略 24h 内记录，避免同日专辑多首互相抬分。
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Dict, List, Optional

from .storage import Store

FEATURE_NAMES = [
    "pop",                    # 平台热度值 0-100
    "artist_scale",           # log1p(专辑数 + 单曲数)，主歌手行业资历代理
    "artist_chart_days_log",  # log1p(主歌手近90天上榜天数)，流量歌手效应
    "age_hours",              # 距发行小时数（截断到 [1, 24*90]）
    "comments_total_log",     # log1p(最新评论总数)
    "comment_velocity_log",   # log1p(每小时评论增速)，两次快照差分
    "early_density_log",      # log1p(评论总数/发行天数)，早期讨论密度
    "has_velocity",           # 增速特征是否可用（不足两次快照时为 0）
    "is_live",                # 歌名判定为 Live/现场/翻唱（综艺回放，非原创新歌）
]

# 综艺/演唱会回放、翻唱：新歌榜常见噪音，不是「原创新歌爆款」
_LIVE_RE = re.compile(
    r"(\blive\b|live版|现场版|现场\b|演唱会|\bcover\b|翻唱)",
    re.IGNORECASE,
)


def title_flags(name: Optional[str]) -> Dict[str, bool]:
    """从歌名提取展示/打分用的标记。"""
    text = name or ""
    return {"is_live": bool(_LIVE_RE.search(text))}

MS = 1000.0
HOUR = 3600.0


def _age_hours(publish_time_ms: Optional[int], now: Optional[float] = None) -> float:
    if not publish_time_ms:
        return 24.0 * 30  # 缺发行时间时按 30 天兜底，中性偏保守
    dt = (now or time.time()) - publish_time_ms / MS
    return max(min(dt / HOUR, 24.0 * 90), 1.0)


def build_features(store: Store, song_id: int,
                   now: Optional[float] = None) -> Dict[str, float]:
    song = store.get_song(song_id)
    if song is None:
        raise KeyError(f"song {song_id} not in store")
    historical = now is not None
    now = time.time() if now is None else now
    snaps = [s for s in store.snapshots(song_id) if s['ts'] <= now]

    age_h = _age_hours(song.get("publish_time"), now)
    latest = snaps[-1] if snaps else None
    comments_total = (latest or {}).get("comments_total") or 0

    velocity = 0.0
    has_velocity = 0.0
    if len(snaps) >= 2:
        prev, last = snaps[-2], snaps[-1]
        d_hours = max((last["ts"] - prev["ts"]) / HOUR, 1.0)
        c0, c1 = prev.get("comments_total"), last.get("comments_total")
        if c0 is not None and c1 is not None:
            velocity = max(c1 - c0, 0) / d_hours
            has_velocity = 1.0

    # 主歌手近 90 天**其他歌曲**的上榜天数（截至 now，训练时传首日快照时刻防泄漏；
    # 排除自身，且忽略 24h 内的榜单记录，避免同日专辑多首互相抬分）
    try:
        lead_artist = (json.loads(song.get("artist_ids") or "[]") or [None])[0]
    except (TypeError, ValueError):
        lead_artist = None
    chart_days = 0
    if lead_artist is not None:
        activity = store.artist_chart_activity(
            as_of_ts=int(now), exclude_song_id=song_id, min_age_seconds=86400
        )
        chart_days = (activity.get(lead_artist) or {}).get("days") or 0

    flags = title_flags(song.get("name"))
    return {
        "pop": float(((latest or {}).get("pop") if historical else song.get("pop")) or 0.0),
        "artist_scale": math.log1p(
            int(song.get("artist_album_size") or 0) + int(song.get("artist_music_size") or 0)
        ),
        "artist_chart_days_log": math.log1p(chart_days),
        "age_hours": age_h,
        "comments_total_log": math.log1p(comments_total),
        "comment_velocity_log": math.log1p(velocity),
        "early_density_log": math.log1p(comments_total / max(age_h / 24.0, 0.5)),
        "has_velocity": has_velocity,
        "is_live": 1.0 if flags["is_live"] else 0.0,
    }


def build_dataset(store: Store, song_ids: Optional[List[int]] = None
                  ) -> List[Dict[str, Any]]:
    """批量构建特征数据集，附带歌曲名等展示字段."""
    ids = song_ids if song_ids is not None else store.tracked_song_ids()
    rows: List[Dict[str, Any]] = []
    for sid in ids:
        song = store.get_song(sid)
        if song is None or store.snapshots_count(sid) == 0:
            continue
        feats = build_features(store, sid)
        feats["song_id"] = sid
        feats["name"] = song.get("name")
        feats["artists"] = song.get("artists")
        rows.append(feats)
    return rows
