"""采集编排：拉新歌榜 -> 入库新歌 -> 对已跟踪歌曲拍快照 -> 启发式打分.

这是 daily.py 与 CLI 各子命令共用的业务层。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from . import CHART_NEW_SONG
from .api import NcmClient
from .features import build_features
from .scoring import MODEL_VERSION, heuristic_score
from .storage import Store

log = logging.getLogger(__name__)

ML_MODEL_VERSION = "gbc-v1"

# 新歌定义：按歌曲发布时间计，发行超过该天数即停止跟踪（榜单上发布较久的歌会提前退出）
DEFAULT_NEW_SONG_WINDOW_DAYS = 45
# 单次快照最多跟踪多少首歌（控制每日请求量：约 2 req/song -> 限速 1s 约 10 分钟）
DEFAULT_MAX_TRACKED = 200
# 歌手邻域负样本：每日最多拉多少位歌手、入库多少首未上榜近作
DEFAULT_NEIGHBOR_ARTISTS = 12
DEFAULT_NEIGHBOR_NEW = 20
# song/detail 的资历字段常为 0，改走 /api/artist/{id}；每日上限控制请求量
DEFAULT_MAX_ARTIST_PROFILES = 50
# 同人多首（专辑倾销）名次衰减：第 2 首 ×0.82，第 3 首 ×0.82² …
ARTIST_DUMP_DECAY = 0.82


def fetch_chart_and_discover(client: NcmClient, store: Store,
                             chart_id: int = CHART_NEW_SONG) -> Dict[str, int]:
    """拉榜单 -> 记录榜单快照 -> 新面孔入库. 返回 {charted, new_songs}."""
    tracks = client.chart_tracks(chart_id)
    if not tracks:
        raise RuntimeError('新歌榜返回空数据，本次停止采集并保留上次发布结果。')
    new_count = 0
    for t in tracks:
        store.record_chart(chart_id, t["song_id"], t["rank"])
        is_new = store.upsert_song({
            "song_id": t["song_id"],
            "name": t["name"],
            "artists": t["artists"],
            "artist_ids": t.get("artist_ids") or [],
            "album": t["album"],
            "publish_time": t.get("publish_time"),
            "duration_ms": t.get("duration_ms"),
        })
        if is_new:
            new_count += 1
    log.info("chart %s: %d tracks, %d new songs", chart_id, len(tracks), new_count)
    return {"charted": len(tracks), "new_songs": new_count}


def neighbor_candidates(raw_songs: List[Dict[str, Any]], known_ids: set,
                        window_days: float = DEFAULT_NEW_SONG_WINDOW_DAYS,
                        now: Optional[float] = None) -> List[Dict[str, Any]]:
    """从歌手热门曲里筛出窗口内、尚未跟踪的近作（纯函数，供离线测试）."""
    now = now if now is not None else time.time()
    cutoff_ms = int((now - window_days * 86400) * 1000)
    out: List[Dict[str, Any]] = []
    seen = set(known_ids)
    for s in raw_songs:
        sid = s.get("song_id")
        if sid is None or sid in seen:
            continue
        pt = s.get("publish_time")
        if not pt or int(pt) < cutoff_ms:
            continue
        seen.add(sid)
        out.append(s)
    return out


def discover_artist_neighbors(client: NcmClient, store: Store,
                              max_artists: int = DEFAULT_NEIGHBOR_ARTISTS,
                              max_new: int = DEFAULT_NEIGHBOR_NEW) -> Dict[str, int]:
    """用上榜歌手的热门近作补未上榜负样本，供后续 ML 训练。

    每日请求上限约 max_artists 次（另加后续快照），保持个人研究量级。
    """
    artist_ids = store.recent_lead_artist_ids(limit=max_artists)
    known = set(store.tracked_song_ids())
    added = 0
    fetched = 0
    for aid in artist_ids:
        if added >= max_new:
            break
        try:
            songs = client.artist_top_songs(aid)
        except Exception as e:  # noqa: BLE001
            log.warning("artist_top_songs(%s) failed: %s", aid, e)
            continue
        fetched += 1
        for s in neighbor_candidates(songs, known):
            if added >= max_new:
                break
            store.upsert_song({
                "song_id": s["song_id"],
                "name": s["name"],
                "artists": s["artists"],
                "artist_ids": s.get("artist_ids") or [],
                "album": s.get("album"),
                "publish_time": s.get("publish_time"),
                "duration_ms": s.get("duration_ms"),
            })
            known.add(s["song_id"])
            added += 1
    log.info("neighbors: artists=%d fetched=%d new=%d", len(artist_ids), fetched, added)
    return {"neighbor_artists": fetched, "neighbor_new": added}


def fill_artist_scale(details: List[Dict[str, Any]],
                      profiles: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """用歌手档案回填 song/detail 里缺失的资历（纯函数，供离线测试）."""
    for d in details:
        if d.get("artist_music_size"):
            continue
        aids = d.get("artist_ids") or []
        if not aids:
            continue
        prof = profiles.get(int(aids[0]))
        if not prof:
            continue
        d["artist_album_size"] = prof.get("album_size") or None
        d["artist_music_size"] = prof.get("music_size") or None
    return details


def enrich_songs(client: NcmClient, store: Store, song_ids: List[int],
                 max_artist_profiles: int = DEFAULT_MAX_ARTIST_PROFILES) -> Dict[str, int]:
    """批量补全歌曲详情；资历走歌手档案，不信 song/detail 里的 0 占位."""
    profiles: Dict[int, Dict[str, Any]] = {}
    fetched = 0
    updated = 0
    for i in range(0, len(song_ids), 100):
        batch = song_ids[i:i + 100]
        try:
            details = client.song_details(batch)
        except Exception as e:  # noqa: BLE001
            log.warning("enrich batch failed: %s", e)
            continue
        need_aids = []
        for d in details:
            if d.get("artist_music_size"):
                continue
            aids = d.get("artist_ids") or []
            if aids and int(aids[0]) not in profiles and int(aids[0]) not in need_aids:
                need_aids.append(int(aids[0]))
        for aid in need_aids:
            if fetched >= max_artist_profiles:
                break
            try:
                profiles[aid] = client.artist_profile(aid)
                fetched += 1
            except Exception as e:  # noqa: BLE001
                log.warning("artist_profile(%s) failed: %s", aid, e)
        fill_artist_scale(details, profiles)
        for d in details:
            store.upsert_song({
                "song_id": d["song_id"], "name": d["name"],
                "artists": d["artists"], "artist_ids": d["artist_ids"],
                "album": d["album"], "publish_time": d["publish_time"],
                "duration_ms": d["duration_ms"], "pop": d["pop"],
                "artist_album_size": d.get("artist_album_size"),
                "artist_music_size": d.get("artist_music_size"),
                "fee": d.get("fee"),
            })
            updated += 1
    log.info("enrich: songs=%d artist_profiles=%d", updated, fetched)
    return {"enriched": updated, "artist_profiles": fetched}


def take_snapshots(client: NcmClient, store: Store,
                   song_ids: List[int]) -> Dict[str, int]:
    """对指定歌曲拍快照（评论总数 + 最新 pop）."""
    ok = fail = 0
    for sid in song_ids:
        try:
            total = client.comments_total(sid)
        except Exception as exc:
            log.warning('comments(%s) failed: %s', sid, exc)
            fail += 1
            continue
        song = store.get_song(sid)
        pop = (song or {}).get("pop")
        if total is None:
            fail += 1
            continue
        store.add_snapshot(sid, comments_total=total, pop=pop)
        ok += 1
    log.info("snapshots: %d ok, %d failed", ok, fail)
    return {"ok": ok, "failed": fail}


def _lead_artist_key(song: Dict[str, Any]) -> str:
    try:
        ids = json.loads(song.get("artist_ids") or "[]")
        if ids:
            return f"id:{ids[0]}"
    except (TypeError, ValueError):
        pass
    return f"name:{song.get('artists') or ''}"


def apply_artist_diversity(results: List[Dict[str, Any]],
                           decay: float = ARTIST_DUMP_DECAY
                           ) -> List[Dict[str, Any]]:
    """同人第 2 首起衰减，避免一张专辑占满 Top。最高分那首不罚。"""
    ranked = sorted(results, key=lambda r: r["score"], reverse=True)
    seen: Dict[str, int] = {}
    for r in ranked:
        key = r.get("lead_artist") or ""
        n = seen.get(key, 0)
        seen[key] = n + 1
        if n < 1 or not key:
            continue
        factor = decay ** n
        raw = r["score"]
        r["score"] = round(raw * factor, 1)
        detail = dict(r.get("detail") or {})
        detail["raw_score"] = raw
        detail["dump_penalty"] = round(factor, 3)
        r["detail"] = detail
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked


def score_all(store: Store, song_ids: List[int],
              model_path: str = "model.pkl") -> List[Dict[str, Any]]:
    """对指定歌曲计算启发式分数并落库；若存在已训练模型则追加 ML 概率分."""
    ml_predict = None
    if os.path.exists(model_path):
        try:
            from .model import load_model
            clf = load_model(model_path)
            from .features import FEATURE_NAMES

            def ml_predict(feats: Dict[str, Any]) -> float:
                x = [[feats[k] for k in FEATURE_NAMES]]
                return float(clf.predict_proba(x)[0][1])
        except Exception as e:  # noqa: BLE001
            log.warning("model %s load failed, skip ML scores: %s", model_path, e)
            ml_predict = None

    results: List[Dict[str, Any]] = []
    for sid in song_ids:
        song = store.get_song(sid)
        if song is None or store.snapshots_count(sid) == 0:
            continue
        feats = build_features(store, sid)
        score, detail = heuristic_score(feats)
        store.add_score(sid, score, MODEL_VERSION, detail=detail)
        row = {
            "song_id": sid, "name": song.get("name"),
            "artists": song.get("artists"), "score": score, "detail": detail,
        }
        if ml_predict is not None:
            try:
                prob = ml_predict(feats)
                store.add_score(sid, round(prob * 100, 1), ML_MODEL_VERSION,
                                detail={"prob": round(prob, 4)})
                row["ml_score"] = round(prob * 100, 1)
            except Exception as e:  # noqa: BLE001
                log.warning("ml predict failed for %s: %s", sid, e)
        results.append(row)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def run_daily(client: NcmClient, store: Store,
              max_tracked: int = DEFAULT_MAX_TRACKED) -> Dict[str, Any]:
    """每日例行：发现新歌 -> 补全详情 -> 快照 -> 打分."""
    stats: Dict[str, Any] = {}
    stats.update(fetch_chart_and_discover(client, store))
    stats.update(discover_artist_neighbors(client, store))
    # 缺 pop，或歌手资历被旧版榜单回写清零的歌曲，补一次详情
    need_enrich = []
    for sid in store.tracked_song_ids():
        song = store.get_song(sid)
        if song is None:
            continue
        if song.get("pop") is None or not song.get("artist_music_size"):
            need_enrich.append(sid)
    if need_enrich:
        stats.update(enrich_songs(client, store, need_enrich[:max_tracked]))
    ids = store.tracked_song_ids(max_age_days=DEFAULT_NEW_SONG_WINDOW_DAYS)[:max_tracked]
    snapshot_start = int(time.time())
    stats.update(take_snapshots(client, store, ids))
    if not stats['ok']:
        raise RuntimeError('本次未采集到有效评论快照，停止打分和发布，保留上次结果。')
    current_ids = [sid for sid in ids if (store.last_snapshot(sid) or {}).get('ts', 0) >= snapshot_start]
    scored = score_all(store, current_ids)
    stats["scored"] = len(scored)
    stats["top5"] = [
        {"name": r["name"], "artists": r["artists"], "score": r["score"]}
        for r in scored[:5]
    ]
    return stats
