"""离线单元测试：不联网，覆盖特征工程与启发式打分的核心逻辑.

运行: python -m pytest tests/ -q   （或 python tests/test_offline.py）
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ncm_scorer.api import (  # noqa: E402
    https_play_url, is_vip_fee, parse_artist_payload, parse_play_payload,
    parse_song_payload,
)
from ncm_scorer.features import build_features, title_flags          # noqa: E402
from ncm_scorer.model import build_labels                            # noqa: E402
from ncm_scorer.pipeline import fill_artist_scale, neighbor_candidates  # noqa: E402
from ncm_scorer.scoring import LIVE_PENALTY, heuristic_score         # noqa: E402
from ncm_scorer.storage import Store                                 # noqa: E402


def make_store() -> Store:
    return Store(os.path.join(tempfile.mkdtemp(), "t.db"))


def seed_song(store: Store, song_id: int, comments: list, pop: float = 60.0,
              publish_days_ago: float = 3.0):
    now = time.time()
    store.upsert_song({
        "song_id": song_id, "name": f"song-{song_id}", "artists": "tester",
        "artist_ids": [1], "album": "al",
        "publish_time": int((now - publish_days_ago * 86400) * 1000),
        "duration_ms": 200000, "pop": pop,
        "artist_album_size": 5, "artist_music_size": 20,
    })
    for i, c in enumerate(comments):
        store.add_snapshot(song_id, ts=int(now - (len(comments) - i) * 86400),
                           comments_total=c, pop=pop)


class TestFeatures(unittest.TestCase):
    def test_velocity_two_snapshots(self):
        store = make_store()
        seed_song(store, 1, comments=[1000, 1240])
        f = build_features(store, 1)
        self.assertAlmostEqual(f["comment_velocity_log"] > 0, True)
        self.assertEqual(f["has_velocity"], 1.0)
        self.assertGreater(f["early_density_log"], 0)

    def test_single_snapshot_no_velocity(self):
        store = make_store()
        seed_song(store, 2, comments=[500])
        f = build_features(store, 2)
        self.assertEqual(f["has_velocity"], 0.0)
        self.assertEqual(f["comment_velocity_log"], 0.0)

    def test_artist_chart_days_feature(self):
        import json as _json
        store = make_store()
        now = time.time()
        # 歌手 7：两首歌分别在 3 天和 5 天前上榜（不同日期 => days=2）
        for sid, days_ago in ((31, 3), (32, 5)):
            store.upsert_song({
                "song_id": sid, "name": f"s{sid}", "artists": "artist7",
                "artist_ids": [7], "publish_time": int((now - 10 * 86400) * 1000),
            })
            store.record_chart(3779629, sid, 1, ts=int(now - days_ago * 86400))
        # 待测歌：同歌手 7
        seed_song(store, 30, comments=[100])
        store.conn.execute("UPDATE songs SET artist_ids=? WHERE song_id=30", ('[7]',))
        store.conn.commit()
        # 对照歌：无上榜歌手 99
        seed_song(store, 33, comments=[100])
        store.conn.execute("UPDATE songs SET artist_ids=? WHERE song_id=33", ('[99]',))
        store.conn.commit()

        f_hot = build_features(store, 30)
        f_cold = build_features(store, 33)
        import math
        self.assertAlmostEqual(f_hot["artist_chart_days_log"], math.log1p(2))
        self.assertEqual(f_cold["artist_chart_days_log"], 0.0)
        # 防泄漏：as_of 早于所有榜单记录时活跃度为 0
        f_early = build_features(store, 30, now=now - 90 * 86400)
        self.assertEqual(f_early["artist_chart_days_log"], 0.0)
        # 同日上榜不计入（挡住专辑多首互相抬分）
        store.record_chart(3779629, 34, 1, ts=int(now - 3600))
        store.upsert_song({
            "song_id": 34, "name": "same-day", "artists": "artist7",
            "artist_ids": [7], "publish_time": int((now - 2 * 86400) * 1000),
        })
        f_same_day = build_features(store, 30)
        self.assertAlmostEqual(f_same_day["artist_chart_days_log"], math.log1p(2))


class TestTitleFlags(unittest.TestCase):
    def test_live_and_cover(self):
        self.assertTrue(title_flags("交个朋友 (Live)")["is_live"])
        self.assertTrue(title_flags("锈 (Live版)")["is_live"])
        self.assertTrue(title_flags("旧梦翻唱")["is_live"])
        self.assertFalse(title_flags("隐藏相册")["is_live"])
        self.assertFalse(title_flags("Olive")["is_live"])


class TestHeuristic(unittest.TestCase):
    def test_hot_beats_cold(self):
        store = make_store()
        # 热歌：高讨论 + 高热度 + 有增速；冷歌：全低
        seed_song(store, 10, comments=[1000, 1500], pop=90.0, publish_days_ago=2)
        seed_song(store, 11, comments=[3], pop=5.0, publish_days_ago=10)
        hot, _ = heuristic_score(build_features(store, 10))
        cold, _ = heuristic_score(build_features(store, 11))
        self.assertGreater(hot, cold)
        self.assertGreaterEqual(hot, 0.0)
        self.assertLessEqual(cold, 100.0)

    def test_score_range_and_detail(self):
        store = make_store()
        seed_song(store, 12, comments=[100, 200], pop=70.0)
        score, detail = heuristic_score(build_features(store, 12))
        self.assertTrue(0.0 <= score <= 100.0)
        self.assertIn("density", detail)
        self.assertIn("artist_heat", detail)
        self.assertIn("weights_used", detail)
        # 单快照场景权重再分配后总和仍为 1
        w = detail["weights_used"]
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_live_penalty_lowers_score(self):
        store = make_store()
        seed_song(store, 40, comments=[800, 1000], pop=80.0, publish_days_ago=2)
        feats = build_features(store, 40)
        studio, _ = heuristic_score(feats)
        live, detail = heuristic_score({**feats, "is_live": 1.0})
        self.assertLess(live, studio)
        self.assertAlmostEqual(live / studio, LIVE_PENALTY, delta=0.02)
        self.assertTrue(detail["is_live"])
        self.assertEqual(detail["live_penalty"], LIVE_PENALTY)


class TestLabels(unittest.TestCase):
    def test_rank_cutoff_not_chart_membership(self):
        store = make_store()
        now = time.time()
        seed_song(store, 51, comments=[10])
        seed_song(store, 52, comments=[10])
        seed_song(store, 53, comments=[10])  # 未上榜 = 负样本
        store.record_chart(3779629, 51, 3, ts=int(now))
        store.record_chart(3779629, 52, 40, ts=int(now))
        labels = build_labels(store, rank_cutoff=20)
        self.assertEqual(labels[51], 1)
        self.assertEqual(labels[52], 0)
        self.assertEqual(labels[53], 0)


class TestParsePayloads(unittest.TestCase):
    def test_song_detail_zero_scale_is_unknown(self):
        row = parse_song_payload({
            "id": 1, "name": "x", "popularity": 80,
            "artists": [{"id": 9, "name": "A", "albumSize": 0, "musicSize": 0}],
            "album": {"name": "al", "publishTime": 1},
            "duration": 1000,
        })
        self.assertIsNone(row["artist_album_size"])
        self.assertIsNone(row["artist_music_size"])
        self.assertEqual(row["pop"], 80.0)
        self.assertIsNone(row["fee"])

    def test_song_detail_keeps_fee(self):
        row = parse_song_payload({
            "id": 2, "name": "vip-song", "popularity": 90, "fee": 1,
            "artists": [{"id": 1, "name": "A"}],
            "album": {"name": "al"},
        })
        self.assertEqual(row["fee"], 1)
        self.assertTrue(is_vip_fee(1))
        self.assertTrue(is_vip_fee(4))
        self.assertFalse(is_vip_fee(0))
        self.assertFalse(is_vip_fee(8))
        self.assertFalse(is_vip_fee(None))

    def test_artist_profile_reads_scale(self):
        row = parse_artist_payload(
            {"artist": {"id": 9, "name": "A", "albumSize": 123, "musicSize": 159}},
            9,
        )
        self.assertEqual(row["album_size"], 123)
        self.assertEqual(row["music_size"], 159)

    def test_https_play_url_upgrades_http(self):
        self.assertEqual(
            https_play_url("http://m801.music.126.net/a.mp3"),
            "https://m801.music.126.net/a.mp3",
        )
        self.assertIsNone(https_play_url(None))
        self.assertIsNone(https_play_url(""))

    def test_parse_play_payload(self):
        got = parse_play_payload({
            "data": [{"url": "http://m801.music.126.net/a.mp3", "br": 128000, "fee": 8}],
        })
        self.assertEqual(got["url"], "https://m801.music.126.net/a.mp3")
        self.assertIsNone(parse_play_payload({"data": [{"url": None}]}))

    def test_fill_artist_scale(self):
        details = [{
            "song_id": 1, "artist_ids": [9],
            "artist_album_size": None, "artist_music_size": None,
        }]
        fill_artist_scale(details, {9: {"album_size": 10, "music_size": 40}})
        self.assertEqual(details[0]["artist_music_size"], 40)


class TestNeighbors(unittest.TestCase):
    def test_neighbor_candidates_filters_old_and_known(self):
        now = time.time()
        raw = [
            {"song_id": 1, "publish_time": int((now - 3 * 86400) * 1000)},
            {"song_id": 2, "publish_time": int((now - 80 * 86400) * 1000)},
            {"song_id": 3, "publish_time": int((now - 2 * 86400) * 1000)},
            {"song_id": 4, "publish_time": None},
        ]
        got = neighbor_candidates(raw, known_ids={1}, window_days=45, now=now)
        self.assertEqual([s["song_id"] for s in got], [3])


class TestStorage(unittest.TestCase):
    def test_upsert_and_chart_roundtrip(self):
        store = make_store()
        self.assertTrue(store.upsert_song({"song_id": 99, "name": "a"}))
        self.assertFalse(store.upsert_song({"song_id": 99, "name": "b"}))
        self.assertEqual(store.get_song(99)["name"], "b")
        store.record_chart(3779629, 99, 3)
        self.assertEqual(len(store.chart_hits(99)), 1)

    def test_upsert_preserves_artist_scale(self):
        store = make_store()
        store.upsert_song({
            "song_id": 88, "name": "a",
            "artist_album_size": 5, "artist_music_size": 20, "pop": 70,
        })
        # 榜单回写不带资历/热度字段时不得把已有值清零
        store.upsert_song({"song_id": 88, "name": "a2"})
        song = store.get_song(88)
        self.assertEqual(song["name"], "a2")
        self.assertEqual(song["artist_album_size"], 5)
        self.assertEqual(song["artist_music_size"], 20)
        self.assertEqual(song["pop"], 70)

    def test_window_filters_by_publish_time(self):
        store = make_store()
        now = time.time()
        # 三首歌入库时间相同（现在），发布时间不同
        store.upsert_song({  # 发布 10 天前：在 45 天窗口内
            "song_id": 21, "name": "fresh",
            "publish_time": int((now - 10 * 86400) * 1000)})
        store.upsert_song({  # 发布 70 天前：超窗，应被剔除
            "song_id": 22, "name": "old",
            "publish_time": int((now - 70 * 86400) * 1000)})
        store.upsert_song({  # 缺发布时间：回退按入库时间（刚入库 => 在窗口内）
            "song_id": 23, "name": "unknown-pt"})
        ids = store.tracked_song_ids(max_age_days=45)
        self.assertIn(21, ids)
        self.assertNotIn(22, ids)
        self.assertIn(23, ids)
        # by_publish=False 时退回旧的入库时间语义：三首都在
        ids2 = store.tracked_song_ids(max_age_days=45, by_publish=False)
        self.assertEqual(set(ids2), {21, 22, 23})


class TestSiteBuilder(unittest.TestCase):
    def test_filters_and_live_badge(self):
        scripts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from build_site import build
        from ncm_scorer.pipeline import score_all

        db = os.path.join(tempfile.mkdtemp(), "t.db")
        store = Store(db)
        seed_song(store, 61, comments=[400], pop=70.0, publish_days_ago=2)
        store.conn.execute("UPDATE songs SET name=?, fee=? WHERE song_id=61", ("原创新歌", 8))
        seed_song(store, 62, comments=[400], pop=70.0, publish_days_ago=2)
        store.conn.execute(
            "UPDATE songs SET name=?, fee=? WHERE song_id=62", ("旧曲 (Live)", 1)
        )
        store.conn.commit()
        score_all(store, [61, 62])
        store.close()
        out = os.path.join(tempfile.mkdtemp(), "index.html")
        build(db, out, top_n=10)
        from test_quality import RankingDocument
        from pathlib import Path
        page = RankingDocument(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(len(page.articles), 2)
        self.assertEqual([r["data-live"] for r in page.articles], ["0", "1"])
        self.assertEqual(page.config["top"], 10)
        self.assertGreater(page.config["dataAt"], 0)



if __name__ == "__main__":
    unittest.main()
