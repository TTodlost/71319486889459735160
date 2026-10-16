from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import collector


class RefreshRecoveryTests(unittest.TestCase):
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
