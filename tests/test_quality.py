"""Data and generated-document regressions. No network or production database writes."""
import json
import math
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from ncm_scorer.storage import Store
from ncm_scorer.api import NcmClient
from ncm_scorer.features import build_features
from ncm_scorer.model import _first_day_features
from ncm_scorer.pipeline import fetch_chart_and_discover, take_snapshots
from scripts.build_site import build, _pick_rows


class RankingDocument(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.articles = []
        self.scripts = []
        self.config = None
        self.script = None
        self.attrs = {}
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.attrs[attrs['id']] = attrs
        if tag == 'article':
            self.articles.append(attrs)
        if tag == 'script':
            self.script = {'attrs': attrs, 'text': ''}
            self.scripts.append(self.script)

    def handle_data(self, value):
        if self.script is not None:
            self.script['text'] += value

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            if self.script['attrs'].get('id') == 'ranking-config':
                self.config = json.loads(self.script['text'])
            self.script = None


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'data.db')
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.now = int(time.time())

    def song(self, sid, age, score, version='heuristic-v2', ts=None, **extra):
        self.store.upsert_song({'song_id': sid, 'name': f'song-{sid}', 'artists': 'artist',
                                'publish_time': int((self.now - age * 86400) * 1000), **extra})
        self.store.add_snapshot(sid, ts=ts or self.now, comments_total=100, pop=40)
        self.store.add_score(sid, score, version, ts=ts or self.now)

    def page(self, top=50, **kw):
        out = str(Path(self.tmp.name) / 'index.html')
        build(self.db, out, top_n=top, **kw)
        return RankingDocument(Path(out).read_text(encoding='utf-8'))

    def test_window_excludes_old_future_unknown_before_limit(self):
        self.song(1, 60, 100)
        self.song(2, 2, 70)
        self.song(3, -1, 90)
        self.song(4, 5, 80, publish_time=None)
        rows = self.store.latest_scores('heuristic-v2', limit=1, max_age_days=30, now=self.now)
        self.assertEqual([r['song_id'] for r in rows], [2])

    def test_all_candidates_kept_for_filter_specific_top(self):
        for sid in range(1, 8):
            self.song(sid, sid, 100 - sid)
        page = self.page(top=2)
        self.assertEqual(len(page.articles), 7)
        self.assertEqual(page.config['top'], 2)
        self.assertEqual(sum('hidden' not in r['class'].split() for r in page.articles), 2)

    def test_incomplete_and_stale_ml_falls_back_to_current_heuristic(self):
        self.song(1, 4, 70)
        self.song(2, 5, 60)
        self.store.add_score(1, 99, 'gbc-v1', ts=self.now)
        rows, label, _ = _pick_rows(self.store, 50, now=self.now)
        self.assertEqual([r['score'] for r in rows], [70, 60])
        self.store.add_score(2, 98, 'gbc-v1', ts=self.now - 86400)
        # One recent ML row must not make another stale prediction look current.
        rows, _, _ = _pick_rows(self.store, 50, now=self.now)
        self.assertEqual([r['score'] for r in rows], [70, 60])

    def test_stale_collection_not_refreshed_by_rebuilding_or_rescoring(self):
        self.song(1, 8, 70, ts=self.now - 3 * 86400)
        self.store.add_score(1, 71, 'heuristic-v2', ts=self.now)
        page = self.page()
        self.assertEqual(page.config['dataAt'], self.now - 3 * 86400)
        self.assertNotIn('hidden', page.attrs['freshness'])

    def test_empty_window_renders_explicit_empty_state(self):
        self.song(1, 80, 90)
        page = self.page()
        self.assertEqual(page.articles, [])
        self.assertNotIn('hidden', page.attrs['empty-state'])
        self.assertEqual(page.config['dataAt'], 0)

    def test_json_script_boundaries_survive_untrusted_song_and_api_strings(self):
        attack = '</script><script>window.injected=true</script>'
        self.song(1, 1, 80, name=attack)
        page = self.page(play_api=attack)
        self.assertEqual(len(page.scripts), 3)
        self.assertEqual(page.config['playApi'], attack)
        metadata = json.loads(page.scripts[-1]['text'])
        self.assertEqual(metadata['top1'], attack)

    def test_partial_song_update_keeps_artist_identity(self):
        self.song(1, 1, 60, artist_ids=[7], album='original', duration_ms=120000)
        self.store.upsert_song({'song_id': 1, 'name': 'renamed'})
        row = self.store.get_song(1)
        self.assertEqual(json.loads(row['artist_ids']), [7])
        self.assertEqual((row['artists'], row['album'], row['duration_ms']), ('artist', 'original', 120000))

    def test_first_day_features_ignore_future_snapshots_and_use_day_density(self):
        self.song(1, 3, 60, ts=self.now - 2 * 86400)
        self.store.add_snapshot(1, ts=self.now, comments_total=10000, pop=99)
        first = _first_day_features(self.store, 1)
        self.assertEqual(first['has_velocity'], 0)
        self.assertEqual(first['pop'], 40)
        self.assertAlmostEqual(first['comments_total_log'], math.log1p(100))
        self.assertAlmostEqual(first['early_density_log'], math.log1p(100))
        self.assertEqual(first, build_features(self.store, 1, now=self.now - 2 * 86400))

    def test_failed_comment_request_never_creates_fresh_snapshot_from_cached_pop(self):
        self.song(1, 3, 60, ts=self.now - 86400, pop=99)
        client = Mock()
        client.comments_total.return_value = None
        result = take_snapshots(client, self.store, [1])
        self.assertEqual(result, {'ok': 0, 'failed': 1})
        self.assertEqual(self.store.last_snapshot(1)['ts'], self.now - 86400)

    def test_single_request_failure_does_not_drop_other_snapshots(self):
        self.song(1, 3, 60, ts=self.now - 86400)
        self.song(2, 2, 50, ts=self.now - 86400)
        client = Mock()
        client.comments_total.side_effect = [RuntimeError('timeout'), 0]
        self.assertEqual(take_snapshots(client, self.store, [1, 2]), {'ok': 1, 'failed': 1})
        self.assertEqual(self.store.last_snapshot(2)['comments_total'], 0)

    def test_empty_chart_stops_collection(self):
        client = Mock()
        client.chart_tracks.return_value = []
        with self.assertRaises(RuntimeError):
            fetch_chart_and_discover(client, self.store)

    def test_invalid_total_is_not_a_zero_comment_observation(self):
        client = NcmClient()
        self.addCleanup(client.session.close)
        for payload in [{}, {'total': None}, {'total': -1}, {'total': True}, {'total': 'not a number'}]:
            client._get = Mock(return_value=payload)
            self.assertIsNone(client.comments_total(123))
        client._get = Mock(return_value={'total': 0})
        self.assertEqual(client.comments_total(123), 0)

    def test_missing_database_does_not_silently_create_empty_store(self):
        missing = str(Path(self.tmp.name) / 'missing.db')
        with self.assertRaises(ValueError):
            build(missing, str(Path(self.tmp.name) / 'index.html'))
        self.assertFalse(Path(missing).exists())
