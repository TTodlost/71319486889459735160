import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import collector


class RefreshRecoveryTests(unittest.TestCase):
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

            class FakeClient:
                def __init__(self, **kwargs):
                    pass

                def get_json(self, endpoint, params):
                    # The public API requires a timestamp_UUID cursor, even
                    # for the first history request. A bare timestamp is400.
                    assert params['before'].endswith('_ffffffff-ffff-ffff-ffff-ffffffffffff')
                    calls.append(dict(params))
                    return json.loads(json.dumps(source))

            with patch.object(collector, 'Client', FakeClient), \
                 patch.object(collector, 'iter_ladder', return_value=[{'teamId': 'a'}]):
                histories = collector.fetch_histories(archive, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z', lambda _: None)
                self.assertEqual(len(calls), 2)
                with self.assertRaisesRegex(ValueError, 'Pending or unresolved'):
                    collector.RatingIndex(histories, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z')
                # Same archive/request bounds; source evidence becomes newer.
                group['ratedAt'] = '2026-01-01T10:05:00Z'
                group['sides'][0].update(ratingBefore=1500, ratingAfter=1520)
                group['sides'][1].update(ratingBefore=1500, ratingAfter=1480)
                collector.fetch_histories(archive, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z', lambda _: None)
                self.assertEqual(len(calls), 4)
                index = collector.RatingIndex(histories, '2026-01-01T09:30:00Z', '2026-01-01T11:00:00Z')
                self.assertEqual(index.at('2026-01-01T10:30:00Z')['a']['elo'], 1520)


if __name__ == '__main__':
    unittest.main()
