"""网易云音乐明文 API 直连客户端.

所有端点均经实测（2026-08-14，详见 docs/API_ENDPOINTS.md）：
- GET /api/playlist/detail?id=3779629        新歌榜 100 首
- GET /api/song/detail/?id={id}&ids=[{id}]   歌曲详情（热度 pop、专辑发行时间）
- GET /api/artist/{id}                       歌手档案（albumSize/musicSize，资历真值）
- GET /api/v1/resource/comments/R_SO_4_{id}  评论总数
- GET /api/artist/top/song?id={artist_id}    歌手热门曲（邻域负样本）
- GET /api/song/enhance/player/url           官方 128k 播放地址（匿名可听档）

不需要 weapi 加密、不需要登录；注意控制请求频率（默认限速 1 req/s 量级），
仅用于个人研究。
"""

from __future__ import annotations

import time
import json
import logging
from typing import Any, Dict, List, Optional
import requests

log = logging.getLogger(__name__)

BASE = "https://music.163.com/api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://music.163.com",
    # 官网匿名态也会带的设备标记，不是用户登录 Cookie；128k 播放地址靠它
    "Cookie": "os=pc; appver=8.10.35",
}


class NcmApiError(RuntimeError):
    pass


def parse_song_payload(s: Dict[str, Any]) -> Dict[str, Any]:
    """把歌曲详情 JSON 收成内部字段。song/detail 里 albumSize/musicSize 常为 0 占位，0 视为未知。"""
    artists = s.get("artists") or s.get("ar") or []
    lead = artists[0] if artists else {}
    album = s.get("album") or s.get("al") or {}
    album_size = int(lead.get("albumSize") or 0)
    music_size = int(lead.get("musicSize") or 0)
    pop = s.get("popularity")
    if pop is None:
        pop = s.get("pop") or 0.0
    return {
        "song_id": s["id"],
        "name": s.get("name", ""),
        "artists": "/".join(a.get("name", "") for a in artists),
        "artist_ids": [a["id"] for a in artists if a.get("id")],
        "album": album.get("name", ""),
        "publish_time": album.get("publishTime") or s.get("publishTime"),
        "duration_ms": s.get("duration") or s.get("dt"),
        "pop": float(pop or 0.0),
        "artist_album_size": album_size or None,
        "artist_music_size": music_size or None,
        "fee": int(s["fee"]) if s.get("fee") is not None else None,
    }


def is_vip_fee(fee) -> bool:
    """只有会员曲/数字专辑才标 VIP。0 免费，8 低音质免费可听，都不能算 VIP。"""
    try:
        return int(fee) in (1, 4)
    except (TypeError, ValueError):
        return False


def https_play_url(url: Optional[str]) -> Optional[str]:
    """官方接口常返回 http CDN，GitHub Pages 是 https，必须升协议否则浏览器拦截。"""
    if not url:
        return None
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def parse_play_payload(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析 /api/song/enhance/player/url 的第一条。"""
    item = (data.get("data") or [None])[0] or {}
    url = https_play_url(item.get("url"))
    if not url:
        return None
    return {
        "url": url,
        "br": item.get("br"),
        "fee": item.get("fee"),
        "size": item.get("size"),
    }


def parse_artist_payload(data: Dict[str, Any], artist_id: int) -> Dict[str, Any]:
    """从 /api/artist/{id} 取资历真值。"""
    a = data.get("artist") or {}
    return {
        "artist_id": int(a.get("id") or artist_id),
        "name": a.get("name") or "",
        "album_size": int(a.get("albumSize") or 0),
        "music_size": int(a.get("musicSize") or 0),
    }


class NcmClient:
    """轻量直连客户端，内置限速与单次重试."""

    def __init__(self, min_interval: float = 1.0, timeout: float = 15.0):
        self.min_interval = min_interval
        self.timeout = timeout
        self._last_ts = 0.0
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    # ------------------------------------------------------------- internals
    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last_ts)
        if wait > 0:
            time.sleep(wait)
        self._last_ts = time.time()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             retries: int = 1) -> Dict[str, Any]:
        url = f"{BASE}{path}"
        for attempt in range(retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                if resp.status_code != 200:
                    raise NcmApiError(f"HTTP {resp.status_code} for {url}")
                data = resp.json()
                if data.get("code") not in (200, None):
                    raise NcmApiError(f"API code={data.get('code')} for {url}: {str(data)[:200]}")
                return data
            except (requests.RequestException, json.JSONDecodeError) as e:
                if attempt == retries:
                    raise NcmApiError(f"request failed for {url}: {e}") from e
                log.warning("retry %s: %s", url, e)
                time.sleep(2)
        raise NcmApiError("unreachable")

    # ---------------------------------------------------------------- public
    def chart_tracks(self, chart_id: int) -> List[Dict[str, Any]]:
        """拉取榜单曲目，返回 [{song_id, rank, name, artists, album, publish_time}]."""
        data = self._get("/playlist/detail", {"id": chart_id})
        result = data.get("result") or {}
        playlist = result.get("playlist") or result
        tracks = playlist.get("tracks") or []
        if not tracks:
            return []
        parsed: List[Dict[str, Any]] = []
        for rank, t in enumerate(tracks, start=1):
            parsed.append({
                "song_id": t["id"],
                "rank": rank,
                "name": t.get("name", ""),
                "artists": "/".join(a.get("name", "") for a in t.get("artists", [])),
                "artist_ids": [a["id"] for a in t.get("artists", []) if a.get("id")],
                "album": (t.get("album") or {}).get("name", ""),
                "publish_time": (t.get("album") or {}).get("publishTime"),
                "duration_ms": t.get("duration"),
            })
        return parsed

    def song_detail(self, song_id: int) -> Dict[str, Any]:
        """单首歌详情: 热度 pop(0-100)、发行时间、主歌手规模."""
        return self.song_details([song_id])[0]

    def song_details(self, song_ids: List[int]) -> List[Dict[str, Any]]:
        """批量歌曲详情（ids 接口一次最多建议 100 首）."""
        if not song_ids:
            return []
        data = self._get(
            "/song/detail/",
            {"id": song_ids[0], "ids": json.dumps(list(song_ids))},
        )
        songs = data.get("songs") or []
        return [parse_song_payload(s) for s in songs]

    def artist_profile(self, artist_id: int) -> Dict[str, Any]:
        """歌手档案：albumSize / musicSize 以该端点为准（song/detail 里经常是 0）。"""
        data = self._get(f"/artist/{artist_id}")
        return parse_artist_payload(data, artist_id)

    def artist_top_songs(self, artist_id: int) -> List[Dict[str, Any]]:
        """歌手热门曲目（明文接口，见 docs/API_ENDPOINTS.md §4）."""
        data = self._get("/artist/top/song", {"id": artist_id})
        songs = data.get("songs") or data.get("hotSongs") or []
        parsed: List[Dict[str, Any]] = []
        for s in songs:
            artists = s.get("artists") or s.get("ar") or []
            album = s.get("album") or s.get("al") or {}
            parsed.append({
                "song_id": s["id"],
                "name": s.get("name", ""),
                "artists": "/".join(a.get("name", "") for a in artists),
                "artist_ids": [a["id"] for a in artists if a.get("id")],
                "album": album.get("name", ""),
                "publish_time": album.get("publishTime") or album.get("publish_time"),
                "duration_ms": s.get("duration") or s.get("dt"),
            })
        return parsed

    def song_play_url(self, song_id: int) -> Optional[Dict[str, Any]]:
        """官方匿名 128k 播放地址。无地址（真·无试听权）返回 None。"""
        data = self._get(
            "/song/enhance/player/url",
            {"id": song_id, "ids": json.dumps([song_id]), "br": 128000},
        )
        return parse_play_payload(data)

    def comments_total(self, song_id: int) -> Optional[int]:
        """评论总数；拉取失败返回 None（不中断流程）."""
        try:
            data = self._get(
                f"/v1/resource/comments/R_SO_4_{song_id}", {"limit": 1, "offset": 0}
            )
            total = data.get('total')
            # Missing/invalid totals are failures, not real zero-comment snapshots.
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                log.warning('comments_total(%s): missing or invalid total', song_id)
                return None
            return total
        except NcmApiError as e:
            log.warning("comments_total(%s) failed: %s", song_id, e)
            return None
