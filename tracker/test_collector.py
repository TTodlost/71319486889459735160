from concurrent.futures import Future
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import collector


class RefreshRecoveryTests(unittest.TestCase):
    def test_public_replay_bridge_passes_raw_bytes_for_raw_and_zip_inputs(self):
        replay = b'a62per\0\0' + bytes(range(32))
        match = {'id': 'recent-map', 'rounds': 10}
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            for zipped in (False, True):
                with self.subTest(zipped=zipped):
                    path = Path(directory) / ('bundle.zip' if zipped else 'raw.repl26a')
                    if zipped:
                        with zipfile.ZipFile(path, 'w') as archive:
                            archive.writestr('replay.repl26a', replay)
                            archive.writestr('team-a.stdout', b'not replay data')
                    else:
                        path.write_bytes(replay)
                    process = collector.subprocess.CompletedProcess([], 0, json.dumps(stats), '')
                    with patch.object(collector.subprocess, 'run', return_value=process) as run:
                        self.assertEqual(collector.replay_statistics(path, match), stats)
                    command = run.call_args.args[0]
                    self.assertEqual(command, ['node', str(collector.ROOT / 'replay_summary.mjs')])
                    options = run.call_args.kwargs
                    payload = json.loads(options['input'])
                    self.assertEqual(base64.b64decode(payload['replayBase64']), replay)
                    self.assertEqual(payload['match'], match)
                    self.assertTrue(options['capture_output'])
                    self.assertTrue(options['text'])
                    self.assertEqual(options['timeout'], 120)

    def test_public_replay_bridge_rejects_engine_failure_and_invalid_statistics(self):
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'raw.repl26a'
            path.write_bytes(b'replay bytes')
            match = {'id': 'recent-map'}
            process = collector.subprocess.CompletedProcess([], 1, json.dumps(stats), 'engine failed')
            with patch.object(collector.subprocess, 'run', return_value=process):
                with self.assertRaisesRegex(ValueError, 'Replay summary failed.*recent-map.*engine failed'):
                    collector.replay_statistics(path, match)
            for payload in (None, [], {}, dict(stats, outcome='unknown'), dict(stats, round=True),
                            dict(stats, round=0), dict(stats, champions={}), dict(stats, champions=[])):
                with self.subTest(payload=payload):
                    process = collector.subprocess.CompletedProcess([], 0, json.dumps(payload), '')
                    with patch.object(collector.subprocess, 'run', return_value=process):
                        with self.assertRaisesRegex(ValueError, 'Invalid reconstructed replay summary'):
                            collector.replay_statistics(path, match)
            process = collector.subprocess.CompletedProcess([], 0, '{truncated', '')
            with patch.object(collector.subprocess, 'run', return_value=process):
                with self.assertRaises(ValueError):
                    collector.replay_statistics(path, match)
            with patch.object(collector.subprocess, 'run',
                              side_effect=collector.subprocess.TimeoutExpired('node', 120)):
                with self.assertRaises(collector.subprocess.TimeoutExpired):
                    collector.replay_statistics(path, match)

    def cached_matches(self, cache, count=1, advertised=True):
        team = '1890540d-1ffc-4847-adbf-d0d08772f268'
        opponent = 'a4350547-b7a1-47b1-9031-4f7f084ad0d8'
        gid = '67c8e148-50d5-4393-a568-5a8d4361cb94'
        mids = ['2154025b-1c95-4cd1-a120-b3b64232a19a',
                '3254025b-1c95-4cd1-a120-b3b64232a19a'][:count]
        group = {'id': gid, 'kind': 'ranked', 'status': 'finished',
                 'createdAt': '2026-10-16T09:50:03.000Z',
                 'finishedAt': '2026-10-16T09:50:08.000Z',
                 'ratedAt': '2026-10-16T09:50:08.000Z',
                 'sides': [{'teamId': team, 'teamName': 'Leader', 'side': 'a'},
                           {'teamId': opponent, 'teamName': 'Opponent', 'side': 'b'}]}
        matches = [{'id': mid, 'status': 'finished', 'startedAt': '2026-10-16T09:50:04.000Z',
                    'map': 'rushbait_v2', 'rounds': 10, 'replayUrl': '/bundle/' + mid,
                    'summaryAvailable': advertised,
                    'sides': [{'teamId': team, 'side': 'a', 'result': 'win', 'score': 5},
                              {'teamId': opponent, 'side': 'b', 'result': 'loss', 'score': 2}]}
                   for mid in mids]
        archive = cache / 'current/archive'
        paths = {}
        for mid in mids:
            path = archive / gid / (mid + '.repl26a')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'existing verified replay fixture')
            paths[mid] = path
        with collector.Catalog(archive, 'https://2026a.battlecode.co.uk') as catalog:
            catalog.store_detail({'group': group, 'matches': matches})
            self.assertEqual(catalog.db.execute('select detail_needed from groups').fetchone()[0], 0)
            ordered_ids = [record['match_id'] for record in catalog.replay_records()]
        leader = {'teamId': team, 'name': 'Leader', 'rank': 1, 'rating': 1600}
        return archive, group, leader, paths, ordered_ids

    def test_old_complete_cache_is_repaired_without_deleting_existing_replays(self):
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        for advertised in (True, False):
            with self.subTest(cached_summary_available=advertised), TemporaryDirectory() as directory:
                cache = Path(directory)
                archive, group, leader, paths, mids = self.cached_matches(cache, advertised=advertised)
                mid = mids[0]
                client = MagicMock(base_url='https://2026a.battlecode.co.uk')
                client.get_json.side_effect = AssertionError('Complete cached detail requires no request.')
                with patch.object(collector, 'CACHE', cache), \
                     patch.object(collector, 'Client', return_value=client), \
                     patch.object(collector, 'iter_ladder', return_value=[leader]), \
                     patch.object(collector, 'recent_groups', return_value=[group]), \
                     patch.object(collector, 'download_replay', return_value=(paths[mid], False)), \
                     patch.object(collector, 'write_metadata', return_value={'sha256': 'a' * 64, 'bytes': 31}), \
                     patch.object(collector, 'replay_statistics', return_value=stats) as engine:
                    for _ in range(2):
                        result = collector.collect_current(lambda _: None,
                            datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
                        self.assertEqual(result[1], {mid})
                client.get_json.assert_not_called()
                engine.assert_called_once()
                self.assertEqual(engine.call_args.args[0], paths[mid])
                self.assertEqual(paths[mid].read_bytes(), b'existing verified replay fixture')
                with collector.Catalog(archive, client.base_url) as catalog:
                    saved = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                    self.assertEqual(saved['matches'][0]['statistics'], stats)
                    self.assertEqual(saved['matches'][0]['statisticsSource'], 'official-replay-engine')
                    self.assertEqual(catalog.db.execute('select downloaded from replays').fetchone()[0], 1)

    def test_cached_missing_public_result_is_refreshed_before_reconstruction(self):
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            archive, group, leader, paths, mids = self.cached_matches(cache)
            mid = mids[0]
            client = MagicMock(base_url='https://2026a.battlecode.co.uk')
            with collector.Catalog(archive, client.base_url) as catalog:
                fresh = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                stale = json.loads(json.dumps(fresh))
                for side in stale['matches'][0]['sides']:
                    side.pop('score')
                    side.pop('result')
                catalog.store_detail(stale)
                self.assertEqual(catalog.db.execute('select detail_needed from groups').fetchone()[0], 0)
            client.get_json.return_value = fresh

            def reconstruct(path, match):
                client.get_json.assert_called_once_with('/api/v1/match-groups/' + group['id'])
                self.assertEqual(match['sides'], fresh['matches'][0]['sides'])
                return stats

            with patch.object(collector, 'CACHE', cache), \
                 patch.object(collector, 'Client', return_value=client), \
                 patch.object(collector, 'iter_ladder', return_value=[leader]), \
                 patch.object(collector, 'recent_groups', return_value=[group]), \
                 patch.object(collector, 'download_replay', return_value=(paths[mid], False)), \
                 patch.object(collector, 'write_metadata', return_value={'sha256': 'a' * 64, 'bytes': 31}), \
                 patch.object(collector, 'replay_statistics', side_effect=reconstruct) as engine:
                for _ in range(2):
                    result = collector.collect_current(lambda _: None,
                        datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
                    self.assertEqual(result[1], {mid})
            client.get_json.assert_called_once_with('/api/v1/match-groups/' + group['id'])
            engine.assert_called_once()
            self.assertEqual(paths[mid].read_bytes(), b'existing verified replay fixture')

    def test_refresh_preserves_only_unchanged_reconstructed_statistics(self):
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        match = {'id': 'map-a', 'map': 'rushbait_v2', 'patchId': 'patch-2',
                 'startedAt': '2026-10-16T09:50:04.000Z', 'rounds': 10, 'status': 'finished',
                 'sides': [{'side': 'a', 'result': 'win', 'score': 5},
                           {'side': 'b', 'result': 'loss', 'score': 2}]}
        previous = {'matches': [dict(match, statistics=stats,
                                     statisticsSource='official-replay-engine')]}
        refreshed = {'matches': [json.loads(json.dumps(match))]}
        collector.preserve_reconstructed_statistics(previous, refreshed)
        self.assertEqual(refreshed['matches'][0]['statistics'], stats)
        self.assertEqual(refreshed['matches'][0]['statisticsSource'], 'official-replay-engine')

        changed_winner = json.loads(json.dumps(match['sides']))
        changed_winner[0]['result'], changed_winner[1]['result'] = 'loss', 'win'
        for change in ({'rounds': 11}, {'sides': changed_winner}, {'patchId': 'another-patch'}):
            with self.subTest(change=change):
                refreshed = {'matches': [dict(match, **change)]}
                collector.preserve_reconstructed_statistics(previous, refreshed)
                self.assertNotIn('statistics', refreshed['matches'][0])

        newer_stats = dict(stats, outcome='teamB')
        refreshed = {'matches': [dict(match, statistics=newer_stats)]}
        collector.preserve_reconstructed_statistics(previous, refreshed)
        self.assertEqual(refreshed['matches'][0]['statistics'], newer_stats)
        self.assertNotIn('statisticsSource', refreshed['matches'][0])
        unverified = {'matches': [dict(match, statistics=stats)]}
        refreshed = {'matches': [dict(match)]}
        collector.preserve_reconstructed_statistics(unverified, refreshed)
        self.assertNotIn('statistics', refreshed['matches'][0])

    def test_partial_cache_repair_survives_later_engine_failure(self):
        class InlinePool:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, function, *args):
                task = Future()
                try:
                    task.set_result(function(*args))
                except Exception as error:
                    task.set_exception(error)
                return task

        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            archive, group, leader, paths, mids = self.cached_matches(cache, count=2)
            first, failed = mids
            client = MagicMock(base_url='https://2026a.battlecode.co.uk')
            client.get_json.side_effect = AssertionError('Cached detail requires no request.')

            def downloaded(client, record, archive):
                return paths[record['match_id']], False

            def reconstructed(path, match):
                if match['id'] == failed:
                    raise ValueError('engine failed for later map')
                self.assertEqual(match['id'], first)
                return stats

            with patch.object(collector, 'CACHE', cache), \
                 patch.object(collector, 'Client', return_value=client), \
                 patch.object(collector, 'iter_ladder', return_value=[leader]), \
                 patch.object(collector, 'recent_groups', return_value=[group]), \
                 patch.object(collector, 'download_replay', side_effect=downloaded), \
                 patch.object(collector, 'write_metadata', return_value={'sha256': 'a' * 64, 'bytes': 31}), \
                 patch.object(collector, 'replay_statistics', side_effect=reconstructed), \
                 patch.object(collector, 'ThreadPoolExecutor', InlinePool), \
                 patch.object(collector, 'as_completed', side_effect=lambda tasks: iter(tasks)):
                with self.assertRaisesRegex(ValueError, 'engine failed for later map'):
                    collector.collect_current(lambda _: None,
                        datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
            with collector.Catalog(archive, client.base_url) as catalog:
                saved = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                matches = {match['id']: match for match in saved['matches']}
                self.assertEqual(matches[first]['statistics'], stats)
                self.assertEqual(matches[first]['statisticsSource'], 'official-replay-engine')
                self.assertNotIn('statistics', matches[failed])
                downloaded = dict(catalog.db.execute('select match_id,downloaded from replays'))
                self.assertEqual(downloaded, {first: 1, failed: 0})
            for path in paths.values():
                self.assertEqual(path.read_bytes(), b'existing verified replay fixture')

    def test_reconstructed_draw_is_cached_and_excluded_without_repeated_engine_work(self):
        stats = {'outcome': 'teamA', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            archive, group, leader, paths, mids = self.cached_matches(cache, count=2)
            drawn, decisive = mids
            client = MagicMock(base_url='https://2026a.battlecode.co.uk')
            client.get_json.side_effect = AssertionError('Complete cached detail requires no request.')
            with collector.Catalog(archive, client.base_url) as catalog:
                detail = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                for match in detail['matches']:
                    if match['id'] == drawn:
                        for side in match['sides']:
                            side.update(result='draw', score=2)
                catalog.store_detail(detail)

            def downloaded(client, record, archive):
                return paths[record['match_id']], False

            def reconstruct(path, match):
                return dict(stats, outcome='draw') if match['id'] == drawn else dict(stats)

            with patch.object(collector, 'CACHE', cache), \
                 patch.object(collector, 'Client', return_value=client), \
                 patch.object(collector, 'iter_ladder', return_value=[leader]), \
                 patch.object(collector, 'recent_groups', return_value=[group]), \
                 patch.object(collector, 'download_replay', side_effect=downloaded), \
                 patch.object(collector, 'write_metadata', return_value={'sha256': 'a' * 64, 'bytes': 31}), \
                 patch.object(collector, 'replay_statistics', side_effect=reconstruct) as engine:
                for _ in range(2):
                    result = collector.collect_current(lambda _: None,
                        datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
                    self.assertEqual(result[1], {decisive})
            self.assertEqual(engine.call_count, 2)
            self.assertEqual({call.args[1]['id'] for call in engine.call_args_list}, set(mids))
            client.get_json.assert_not_called()
            with collector.Catalog(archive, client.base_url) as catalog:
                detail = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                matches = {match['id']: match for match in detail['matches']}
                self.assertEqual(matches[drawn]['statistics']['outcome'], 'draw')
                self.assertEqual(matches[drawn]['statisticsSource'], 'official-replay-engine')
                self.assertEqual(matches[decisive]['statistics']['outcome'], 'teamA')
                self.assertEqual(dict(catalog.db.execute('select match_id,downloaded from replays')),
                                 {drawn: 1, decisive: 1})
            for path in paths.values():
                self.assertEqual(path.read_bytes(), b'existing verified replay fixture')

    def test_all_draw_sample_stops_after_saving_reconstruction(self):
        stats = {'outcome': 'draw', 'round': 10, 'champions': [{'champion': 0}]}
        with TemporaryDirectory() as directory:
            cache = Path(directory)
            archive, group, leader, paths, mids = self.cached_matches(cache)
            mid = mids[0]
            client = MagicMock(base_url='https://2026a.battlecode.co.uk')
            client.get_json.side_effect = AssertionError('Complete cached detail requires no request.')
            with collector.Catalog(archive, client.base_url) as catalog:
                detail = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                for side in detail['matches'][0]['sides']:
                    side.update(result='draw', score=2)
                catalog.store_detail(detail)
            with patch.object(collector, 'CACHE', cache), \
                 patch.object(collector, 'Client', return_value=client), \
                 patch.object(collector, 'iter_ladder', return_value=[leader]), \
                 patch.object(collector, 'recent_groups', return_value=[group]), \
                 patch.object(collector, 'download_replay', return_value=(paths[mid], False)), \
                 patch.object(collector, 'write_metadata', return_value={'sha256': 'a' * 64, 'bytes': 31}), \
                 patch.object(collector, 'replay_statistics', return_value=stats) as engine:
                with self.assertRaisesRegex(ValueError, 'No completed replays with decisive outcomes'):
                    collector.collect_current(lambda _: None,
                        datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
                with self.assertRaisesRegex(ValueError, 'No complete recent replay records'):
                    collector.collect_current(lambda _: None,
                        datetime(2026, 10, 16, 10, tzinfo=timezone.utc))
            engine.assert_called_once()
            with collector.Catalog(archive, client.base_url) as catalog:
                detail = json.loads(catalog.db.execute('select detail_json from groups').fetchone()[0])
                self.assertEqual(detail['matches'][0]['statistics']['outcome'], 'draw')
                self.assertEqual(catalog.db.execute('select downloaded from replays').fetchone()[0], 1)

    def test_current_sample_pages_from_head_and_caps_completed_series(self):
        def group(name, created, status='finished', rated=True):
            return {'id': name, 'createdAt': created, 'kind': 'ranked', 'status': status,
                    'ratedAt': created if rated else None}

        pages = [
            {'groups': [group('future', '2026-10-15T13:00:00.000Z'),
                        group('pending', '2026-10-15T11:59:00.000Z', rated=False),
                        group('a', '2026-10-15T11:50:00.000Z')], 'more': True},
            {'groups': [group('a', '2026-10-15T11:50:00.000Z'),
                        group('b', '2026-10-15T11:40:00.000Z'),
                        group('c', '2026-10-15T11:30:00.000Z')], 'more': True}]
        calls = []

        class FakeClient:
            def get_json(self, endpoint, params):
                calls.append(dict(params))
                return pages.pop(0)

        result = collector.recent_groups(FakeClient(), 'team', '2026-10-14T12:00:00.000Z',
                                         '2026-10-15T12:00:00.000Z', limit=2)
        self.assertEqual([g['id'] for g in result], ['a', 'b'])
        self.assertEqual(calls[0]['before'], '2026-10-15T12:00:00.000Z_ffffffff-ffff-ffff-ffff-ffffffffffff')
        self.assertEqual(calls[1]['before'], '2026-10-15T11:50:00.000Z_a')
        self.assertEqual(calls[0]['team'], 'team')

    def test_current_sample_stops_at_window_and_rejects_broken_pagination(self):
        class FakeClient:
            def get_json(self, endpoint, params):
                return {'groups': [{'id': 'old', 'createdAt': '2026-10-13T12:00:00.000Z',
                                    'kind': 'ranked', 'status': 'finished', 'ratedAt': 'old'}], 'more': True}

        self.assertEqual(collector.recent_groups(FakeClient(), 'team', '2026-10-14T12:00:00.000Z',
                                                '2026-10-15T12:00:00.000Z'), [])
        with patch.object(FakeClient, 'get_json', return_value={'groups': [], 'more': True}):
            with self.assertRaisesRegex(ValueError, 'no progress'):
                collector.recent_groups(FakeClient(), 'team', 'start', 'end')

    def test_every_refresh_fetches_current_leaders_independent_of_artifact(self):
        sample = (Path('recent'), {'new-map'}, [{'teamId': 'now-first', 'rank': 1}],
                  {'leaderboardUpdatedAt': '2026-10-15T12:00:00Z'})
        with patch.object(collector, 'collect_current', return_value=sample) as current, \
             patch.object(collector, 'latest_snapshot', side_effect=AssertionError('old archive must not gate freshness')), \
             patch.object(collector, 'build_data', return_value={'fresh': True}) as build:
            self.assertEqual(collector.refresh(lambda _: None), {'fresh': True})
            collector.refresh(lambda _: None)
        self.assertEqual(current.call_count, 2)
        self.assertIsNone(build.call_args.args[1])
        self.assertEqual(build.call_args.kwargs['match_ids'], {'new-map'})
        self.assertEqual(build.call_args.kwargs['leaderboard'], sample[2])

    def test_current_replay_workers_share_metadata_request_pacing(self):
        leader = {'teamId': 'leader', 'name': 'Leader', 'rank': 1, 'rating': 1600}
        group = {'id': 'series'}
        matches = [{'id': mid, 'startedAt': '2026-10-15T11:00:00.000Z',
                    'statistics': {'outcome': 'teamA'}, 'replayUrl': '/bundle/' + mid}
                   for mid in ('map-a', 'map-b')]
        detail = {'group': group, 'matches': matches}
        records = [{'match_id': m['id'], 'sha256': 'previous-checksum'} for m in matches]
        catalog = MagicMock()
        catalog.__enter__.return_value = catalog
        catalog.db.execute.return_value.fetchone.return_value = {
            'detail_needed': False, 'detail_json': json.dumps(detail)}
        catalog.replay_records.return_value = records
        clients = []

        class FakeClient:
            base_url = 'https://2026a.battlecode.co.uk'

            def __init__(self, **kwargs):
                self.options = kwargs
                clients.append(self)

            def get_json(self, *args):
                raise AssertionError('Cached series detail should be reused.')

        def downloaded(client, record, archive):
            return archive / (record['match_id'] + '.repl26a'), False

        pacer = object()
        with TemporaryDirectory() as directory, \
             patch.object(collector, 'CACHE', Path(directory)), \
             patch.object(collector, 'RequestPacer', return_value=pacer) as make_pacer, \
             patch.object(collector, 'Client', FakeClient), \
             patch.object(collector, 'iter_ladder', return_value=[leader]), \
             patch.object(collector, 'recent_groups', return_value=[group]), \
             patch.object(collector, 'Catalog', return_value=catalog), \
             patch.object(collector, 'download_replay', side_effect=downloaded), \
             patch.object(collector, 'write_metadata', return_value={'sha256': 'verified', 'bytes': 12}), \
             patch.object(collector, 'ThreadPoolExecutor', wraps=collector.ThreadPoolExecutor) as pool:
            archive, match_ids, ladder, meta = collector.collect_current(
                lambda _: None, datetime(2026, 10, 15, 12, tzinfo=timezone.utc))

        make_pacer.assert_called_once_with()
        pool.assert_called_once_with(max_workers=2)
        self.assertEqual(len(clients), 3)
        self.assertTrue(all(c.options['pacer'] is pacer for c in clients))
        self.assertTrue(all(c.options['retries'] == 3 for c in clients))
        self.assertEqual(match_ids, {'map-a', 'map-b'})
        self.assertEqual(ladder, [leader])
        self.assertEqual(meta['collection']['series'], 1)
        self.assertEqual({call.args[0] for call in catalog.mark_downloaded.call_args_list}, match_ids)
        catalog.store_detail.assert_not_called()

    def test_interrupted_restore_is_retried_in_a_fresh_directory(self):
        artifact = {'id': 123, 'created_at': '2026-01-01T12:00:00Z'}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            attempts = []

            class FakeApi:
                def __init__(self, token):
                    pass

                def download_artifact(self, repository, selected, destination):
                    destination.write_bytes(b'verified download fixture')

            def restore(snapshot, archive):
                archive.mkdir()
                attempts.append(archive)
                if len(attempts) == 1:
                    raise ValueError('interrupted restore')
                (archive / 'catalog.sqlite3').write_bytes(b'catalog fixture')

            with patch.object(collector, 'CACHE', root), patch.object(collector, 'STATE', root / 'state.json'), \
                 patch.object(collector, 'DATA', root / 'data.json'), patch.object(collector, 'GitHubApi', FakeApi), \
                 patch.object(collector, 'github_token', return_value='test-only'), \
                 patch.object(collector, 'trusted_artifacts', return_value=[artifact]), \
                 patch.object(collector, 'restore_snapshot', side_effect=restore):
                with self.assertRaisesRegex(ValueError, 'interrupted'):
                    collector.latest_snapshot(lambda _: None)
                archive, _ = collector.latest_snapshot(lambda _: None)
                self.assertEqual(len(attempts), 2)
                self.assertNotEqual(attempts[0], attempts[1])
                self.assertEqual(archive, attempts[1])
                self.assertTrue((archive / 'catalog.sqlite3').exists())
                collector.latest_snapshot(lambda _: None)
                self.assertEqual(len(attempts), 2)

    def test_pending_history_is_refetched_after_source_resolves(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'archive'
            archive.mkdir()
            db = collector.sqlite3.connect(archive / 'catalog.sqlite3')
            db.execute('create table rating_events(team_id text)')
            db.execute("insert into rating_events values('a')")
            db.commit()
            db.close()
            group = {'id': 'game', 'kind': 'ranked', 'status': 'finished',
                     'createdAt': '2026-01-01T10:00:00Z', 'ratedAt': None,
                     'sides': [{'teamId': 'a', 'ratingBefore': None, 'ratingAfter': None},
                               {'teamId': 'b', 'ratingBefore': None, 'ratingAfter': None}]}
            source = {'groups': [group], 'more': False}
            calls = []
            clients = []

            class FakeClient:
                def __init__(self, **kwargs):
                    clients.append(kwargs)

                def get_json(self, endpoint, params):
                    # The public API requires a timestamp_UUID cursor, even
                    # for the first history request. A bare timestamp is400.
                    assert params['before'].endswith('_ffffffff-ffff-ffff-ffff-ffffffffffff')
                    calls.append(dict(params))
                    return json.loads(json.dumps(source))

            pacers = [object(), object()]
            with patch.object(collector, 'Client', FakeClient), \
                 patch.object(collector, 'RequestPacer', side_effect=pacers), \
                 patch.object(collector, 'ThreadPoolExecutor', wraps=collector.ThreadPoolExecutor) as pool, \
                 patch.object(collector, 'iter_ladder', return_value=[{'teamId': 'a'}]):
                histories = collector.fetch_histories(archive, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z', lambda _: None)
                self.assertEqual(len(calls), 2)
                self.assertEqual(len(clients), 3)
                self.assertTrue(all(c['pacer'] is pacers[0] for c in clients))
                with self.assertRaisesRegex(ValueError, 'Pending or unresolved'):
                    collector.RatingIndex(histories, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z')
                # Same archive/request bounds; source evidence becomes newer.
                group['ratedAt'] = '2026-01-01T10:05:00Z'
                group['sides'][0].update(ratingBefore=1500, ratingAfter=1520)
                group['sides'][1].update(ratingBefore=1500, ratingAfter=1480)
                collector.fetch_histories(archive, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z', lambda _: None)
                self.assertEqual(len(calls), 4)
                self.assertEqual(len(clients), 6)
                self.assertTrue(all(c['pacer'] is pacers[1] for c in clients[3:]))
                self.assertTrue(all(c['retries'] == 3 for c in clients))
                self.assertTrue(all(call.kwargs == {'max_workers': 2} for call in pool.call_args_list))
                index = collector.RatingIndex(histories, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z')
                self.assertEqual(index.at('2026-01-01T10:30:00Z')['a']['elo'], 1520)


if __name__ == '__main__':
    unittest.main()
