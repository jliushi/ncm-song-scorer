"""SQLite 存储层：歌曲、每日快照、榜单记录、打分历史."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    song_id       INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    artists       TEXT,
    artist_ids    TEXT,
    album         TEXT,
    publish_time  INTEGER,          -- ms epoch, 可能为 NULL
    duration_ms   INTEGER,
    pop           REAL,
    artist_album_size INTEGER DEFAULT 0,
    artist_music_size INTEGER DEFAULT 0,
    first_seen    INTEGER NOT NULL,  -- s epoch, 首次入库时间
    fee           INTEGER            -- 0 免费，1 会员曲，4 数字专辑，8 低音质免费
);

CREATE TABLE IF NOT EXISTS snapshots (
    song_id        INTEGER NOT NULL,
    ts             INTEGER NOT NULL, -- s epoch
    comments_total INTEGER,
    pop            REAL,
    PRIMARY KEY (song_id, ts)
);

CREATE TABLE IF NOT EXISTS charts (
    chart_id   INTEGER NOT NULL,
    song_id    INTEGER NOT NULL,
    rank       INTEGER,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (chart_id, song_id, ts)
);

CREATE TABLE IF NOT EXISTS scores (
    song_id        INTEGER NOT NULL,
    ts             INTEGER NOT NULL,
    score          REAL NOT NULL,
    model_version  TEXT NOT NULL,
    detail         TEXT,
    PRIMARY KEY (song_id, ts, model_version)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_song_ts ON snapshots(song_id, ts);
CREATE INDEX IF NOT EXISTS idx_charts_song_ts ON charts(song_id, ts);
"""


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: str = "ncm_scorer.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(songs)")}
        if "fee" not in cols:
            self.conn.execute("ALTER TABLE songs ADD COLUMN fee INTEGER")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- songs
    def upsert_song(self, row: Dict[str, Any]) -> bool:
        """入库/更新歌曲；返回是否为首次入库（新发现的歌曲）."""
        cur = self.conn.execute(
            "SELECT 1 FROM songs WHERE song_id=?", (row["song_id"],)
        )
        exists = cur.fetchone() is not None
        if exists:
            self.conn.execute(
                """UPDATE songs SET name=COALESCE(?, name), artists=COALESCE(?, artists),
                       artist_ids=COALESCE(?, artist_ids), album=COALESCE(?, album),
                       publish_time=COALESCE(?, publish_time), duration_ms=COALESCE(?, duration_ms),
                       pop=COALESCE(?, pop),
                       artist_album_size=COALESCE(?, artist_album_size),
                       artist_music_size=COALESCE(?, artist_music_size),
                       fee=COALESCE(?, fee)
                   WHERE song_id=?""",
                (row.get("name"), row.get("artists"),
                 json.dumps(row["artist_ids"]) if row.get("artist_ids") is not None else None, row.get("album"),
                 row.get("publish_time"), row.get("duration_ms"),
                 row.get("pop"), row.get("artist_album_size"),
                 row.get("artist_music_size"), row.get("fee"),
                 row["song_id"]),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            """INSERT INTO songs (song_id, name, artists, artist_ids, album,
                  publish_time, duration_ms, pop, artist_album_size,
                  artist_music_size, first_seen, fee)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["song_id"], row.get("name", ""), row.get("artists"),
             json.dumps(row.get("artist_ids") or []), row.get("album"),
             row.get("publish_time"), row.get("duration_ms"),
             row.get("pop"), row.get("artist_album_size") or 0,
             row.get("artist_music_size") or 0, _now(), row.get("fee")),
        )
        self.conn.commit()
        return True

    def get_song(self, song_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM songs WHERE song_id=?", (song_id,))
        r = cur.fetchone()
        return dict(r) if r else None

    def tracked_song_ids(self, max_age_days: float = float("inf"),
                         by_publish: bool = True) -> List[int]:
        """窗口内的已跟踪歌曲.

        by_publish=True 时窗口按歌曲发布时间（publish_time）计算，缺失发布
        时间的歌回退用 first_seen；False 则按入库时间过滤。
        """
        if max_age_days == float("inf"):
            cutoff = 0
        else:
            cutoff = _now() - int(max_age_days * 86400)
        if by_publish:
            # publish_time 为 ms，first_seen 为 s；COALESCE 统一成秒再比较
            cond = "(COALESCE(publish_time, first_seen * 1000) / 1000) >= ?"
        else:
            cond = "first_seen >= ?"
        cur = self.conn.execute(
            f"SELECT song_id FROM songs WHERE {cond} ORDER BY song_id", (cutoff,)
        )
        return [r["song_id"] for r in cur.fetchall()]

    # --------------------------------------------------------- snapshots
    def add_snapshot(self, song_id: int, ts: Optional[int] = None,
                     comments_total: Optional[int] = None,
                     pop: Optional[float] = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots (song_id, ts, comments_total, pop) "
            "VALUES (?,?,?,?)",
            (song_id, ts or _now(), comments_total, pop),
        )
        self.conn.commit()

    def snapshots(self, song_id: int) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM snapshots WHERE song_id=? ORDER BY ts", (song_id,)
        )
        return [dict(r) for r in cur.fetchall()]

    def last_snapshot(self, song_id: int) -> Optional[Dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT * FROM snapshots WHERE song_id=? ORDER BY ts DESC LIMIT 1",
            (song_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None

    def snapshots_count(self, song_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n FROM snapshots WHERE song_id=?", (song_id,)
        )
        return int(cur.fetchone()["n"])

    # ------------------------------------------------------------- charts
    def record_chart(self, chart_id: int, song_id: int, rank: int,
                     ts: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO charts (chart_id, song_id, rank, ts) VALUES (?,?,?,?)",
            (chart_id, song_id, rank, ts or _now()),
        )
        self.conn.commit()

    def chart_hits(self, song_id: int, since_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM charts WHERE song_id=?"
        args: Sequence[Any] = (song_id,)
        if since_ts is not None:
            q += " AND ts >= ?"
            args = (song_id, since_ts)
        cur = self.conn.execute(q, args)
        return [dict(r) for r in cur.fetchall()]

    def artist_chart_activity(self, as_of_ts: Optional[int] = None,
                              window_days: int = 90,
                              exclude_song_id: Optional[int] = None,
                              min_age_seconds: int = 0
                              ) -> Dict[int, Dict[str, int]]:
        """近 window_days 内各主歌手的上榜活跃度: {artist_id: {songs, days}}.

        as_of_ts 用于防标签泄漏：训练特征取首日快照时刻的活跃度，
        只统计 ts <= as_of - min_age_seconds 的榜单记录。exclude_song_id
        排除当前歌曲自身的上榜记录（歌手势能应来自其**其他**歌曲）。
        min_age_seconds=86400 可挡住同日专辑多首互相抬分。
        """
        as_of = as_of_ts or _now()
        until = as_of - int(min_age_seconds)
        since = as_of - window_days * 86400
        cur = self.conn.execute(
            """
            SELECT json_extract(s.artist_ids, '$[0]') AS artist_id,
                   COUNT(DISTINCT c.song_id) AS songs,
                   COUNT(DISTINCT date(c.ts, 'unixepoch')) AS days
            FROM charts c JOIN songs s ON s.song_id = c.song_id
            WHERE c.ts <= ? AND c.ts >= ?
              AND json_extract(s.artist_ids, '$[0]') IS NOT NULL
              AND (? IS NULL OR c.song_id != ?)
            GROUP BY 1
            """,
            (until, since, exclude_song_id, exclude_song_id),
        )
        return {r["artist_id"]: {"songs": r["songs"], "days": r["days"]}
                for r in cur.fetchall()}

    def best_chart_rank(self, song_id: int,
                        chart_id: Optional[int] = None) -> Optional[int]:
        """该歌曲在榜单上的历史最佳名次；从未上榜返回 None."""
        if chart_id is None:
            cur = self.conn.execute(
                "SELECT MIN(rank) AS r FROM charts WHERE song_id=?", (song_id,)
            )
        else:
            cur = self.conn.execute(
                "SELECT MIN(rank) AS r FROM charts WHERE song_id=? AND chart_id=?",
                (song_id, chart_id),
            )
        r = cur.fetchone()
        return None if r is None or r["r"] is None else int(r["r"])

    def recent_lead_artist_ids(self, since_ts: Optional[int] = None,
                               limit: int = 15) -> List[int]:
        """近期上榜歌曲的主歌手 id，按上榜曲目数降序."""
        since = since_ts if since_ts is not None else _now() - 2 * 86400
        cur = self.conn.execute(
            """
            SELECT json_extract(s.artist_ids, '$[0]') AS artist_id,
                   COUNT(DISTINCT c.song_id) AS n
            FROM charts c JOIN songs s ON s.song_id = c.song_id
            WHERE c.ts >= ?
              AND json_extract(s.artist_ids, '$[0]') IS NOT NULL
            GROUP BY 1
            ORDER BY n DESC
            LIMIT ?
            """,
            (since, limit),
        )
        return [int(r["artist_id"]) for r in cur.fetchall() if r["artist_id"] is not None]

    # ------------------------------------------------------------- scores
    def add_score(self, song_id: int, score: float, model_version: str,
                  detail: Optional[Dict[str, Any]] = None,
                  ts: Optional[int] = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scores (song_id, ts, score, model_version, detail) "
            "VALUES (?,?,?,?,?)",
            (song_id, ts or _now(), float(score), model_version,
             json.dumps(detail, ensure_ascii=False) if detail else None),
        )
        self.conn.commit()

    def latest_scores(self, model_version: Optional[str] = None,
                      limit: Optional[int] = 50, max_age_days: Optional[float] = None,
                      now: Optional[float] = None) -> List[Dict[str, Any]]:
        as_of = time.time() if now is None else now
        filters = ""
        filter_args = []
        if max_age_days is not None:
            filters = "WHERE g.publish_time BETWEEN ? AND ? "
            filter_args = [int((as_of - max_age_days * 86400) * 1000), int(as_of * 1000)]
        q = (
            "SELECT s.song_id, s.score, s.ts, s.detail, g.name, g.artists, "
            "g.publish_time, g.fee, (SELECT MAX(p.ts) FROM snapshots p "
            "WHERE p.song_id=s.song_id AND p.comments_total IS NOT NULL AND p.ts<=s.ts) AS data_ts "
            "FROM scores s JOIN (SELECT song_id, MAX(ts) AS mts FROM scores "
            "{mv} GROUP BY song_id) x ON s.song_id=x.song_id AND s.ts=x.mts "
            "{mvo} JOIN songs g ON g.song_id=s.song_id "
            "{filters} ORDER BY s.score DESC, s.song_id ASC LIMIT ?"
        )
        if model_version:
            mv = "WHERE model_version=?"
            mvo = "AND s.model_version=?"
            args: Iterable[Any] = (model_version, model_version, *filter_args, -1 if limit is None else limit)
        else:
            mv = ""
            mvo = ""
            args = (*filter_args, -1 if limit is None else limit)
        cur = self.conn.execute(q.format(mv=mv, mvo=mvo, filters=filters), tuple(args))
        return [dict(r) for r in cur.fetchall()]
