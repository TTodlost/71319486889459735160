"""Durable public replay sync across interrupted and repeated invocations."""
from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import hashlib
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'reconstruction'))
from tools import fetch_replays, sync_replays
from tools.replay_catalog import Catalog


BASE_URL = 'https://2026a.battlecode.co.uk'
TEAM = 'ee83f5e5-bcad-4923-86b4-b79ea6083d2e'
OPPONENT = '8a859a54-6acb-4e46-9f54-59d7e955b06c'
GROUP_A = '64013d4f-257c-43c5-bf6a-6b0fd598a7f4'
GROUP_B = '8b46b3b6-f9c5-4083-8d7c-bb549b90f550'
GROUP_C = '2949f36c-4dd3-49df-ae26-28f4e567a099'
GROUP_D = 'd9e0597b-c9dd-4574-84e9-16c6db28bf5d'
MATCH_A = '187ad51c-b387-4db0-b9f5-157354de5398'
MATCH_B = '2bcacac1-ec95-4d14-a850-4be1761a4722'
MATCH_C = 'b2f6634a-6462-4d4f-a4cf-d2f64282b35c'
NEW = '2026-10-14T10:00:00.000Z'
OLD = '2026-10-14T09:00:00.000Z'
OLDER = '2026-10-14T08:00:00.000Z'
ROUTES = {
    GROUP_A: '/api/v1/match-groups/64013d4f-257c-43c5-bf6a-6b0fd598a7f4',
    GROUP_B: '/api/v1/match-groups/8b46b3b6-f9c5-4083-8d7c-bb549b90f550',
    GROUP_C: '/api/v1/match-groups/2949f36c-4dd3-49df-ae26-28f4e567a099',
}
REPLAY_URLS = {
    MATCH_A: '/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle',
    MATCH_B: '/api/v1/matches/2bcacac1-ec95-4d14-a850-4be1761a4722/bundle',
    MATCH_C: '/api/v1/matches/b2f6634a-6462-4d4f-a4cf-d2f64282b35c/bundle',
}
# The downloader validates container headers; simulation parsing has its own tests.
REPLAY_BYTES = (bytes.fromhex('6136327065720000000001000001')
                + b'w62pam\0\0' + bytes(range(1, 33)))


def later(timestamp, seconds):
    parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def group(group_id=GROUP_A, *, created=NEW, status='finished', kind='ranked', elo=1777.25):
    finished = status == 'finished'
    before = elo if finished and kind == 'ranked' else None
    return {
        'id': group_id, 'createdAt': created, 'status': status, 'kind': kind,
        'finishedAt': later(created, 25) if finished else None,
        'ratedAt': later(created, 30) if before is not None else None,
        'sides': [
            {'side': 'a', 'teamId': TEAM, 'teamName': 'Historic Team',
             'ratingBefore': before, 'ratingAfter': before + 12.5 if before is not None else None},
            {'side': 'b', 'teamId': OPPONENT, 'teamName': 'Other Team',
             'ratingBefore': 1100.5 if before is not None else None,
             'ratingAfter': 1088.0 if before is not None else None},
        ],
    }


def detail(row, match_id):
    finished = row['status'] == 'finished'
    return {
        'group': deepcopy(row),
        'matches': [{
            'id': match_id, 'map': 'collision', 'position': 1,
            'status': 'finished' if finished else 'running',
            'createdAt': row['createdAt'], 'startedAt': later(row['createdAt'], 10),
            'finishedAt': later(row['createdAt'], 20) if finished else None,
            'replayUrl': REPLAY_URLS[match_id] if finished else None,
        }],
    }


def page(*groups, more=False):
    return {'groups': list(groups), 'more': more}


class SyncTests(unittest.TestCase):
    def setUp(self):
        clock_patch = patch.object(sync_replays, 'datetime', wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = datetime.fromisoformat('2026-10-15T00:00:00+00:00')
        self.addCleanup(clock_patch.stop)
        directory = tempfile.TemporaryDirectory(prefix='sync_replay_tests_')
        self.addCleanup(directory.cleanup)
        self.outdir = Path(directory.name)
        self.catalog = Catalog(self.outdir, BASE_URL)
        self.addCleanup(lambda: self.catalog.close())

    def reopen(self):
        self.catalog.close()
        self.catalog = Catalog(self.outdir, BASE_URL)

    def client(self, listings, details=None, *, fail_downloads=()):
        """Separate listing order from detail scheduling; forbid unrelated APIs."""
        client = fetch_replays.Client(BASE_URL, delay=0, retries=0)
        client.listings = list(listings)
        detail_by_path = {ROUTES[group_id]: payload for group_id, payload in (details or {}).items()}
        failures = list(fail_downloads)

        def get_json(path, params=None):
            params = dict(params or {})
            if path == '/api/v1/match-groups':
                if not client.listings:
                    raise AssertionError(f'Unexpected discovery page: {params}')
                before, payload = client.listings.pop(0)
                self.assertEqual(params.get('scope'), 'all')
                self.assertEqual(params.get('before'), before)
                self.assertNotIn('team', params)
                self.assertNotIn('q', params)
                if isinstance(payload, Exception):
                    raise payload
                return deepcopy(payload)
            if path not in detail_by_path:
                raise AssertionError(f'Unexpected API request: {path}, {params}')
            self.assertFalse(params)
            payload = detail_by_path[path]
            if isinstance(payload, Exception):
                raise payload
            return deepcopy(payload)

        def download(path, destination):
            self.assertIn(path, REPLAY_URLS.values())
            destination = Path(destination)
            if path in failures:
                failures.remove(path)
                destination.write_bytes(b'incomplete')
                raise fetch_replays.FetchError('interrupted public replay download')
            destination.write_bytes(REPLAY_BYTES)

        client.get_json = Mock(side_effect=get_json)
        client.download = Mock(side_effect=download)
        return client

    def run_sync(self, client, **kwargs):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = sync_replays.sync_once(client, self.catalog, **kwargs)
        self.assertIsInstance(result, dict)
        for key in ('discovered', 'details', 'downloaded', 'existing', 'errors'):
            self.assertIn(key, result)
        self.assertFalse(client.listings, 'An expected discovery page was not consumed')
        return result

    def replay_path(self, group_id=GROUP_A, match_id=MATCH_A):
        return self.outdir / group_id / f'{match_id}.repl26a'

    def files(self):
        return sorted(self.outdir.glob('*/*.repl26a'))

    def detail_calls(self, client):
        return [call for call in client.get_json.call_args_list
                if call.args[0] != '/api/v1/match-groups']

    def seed_two(self):
        # The earliest ranked snapshot begins at the website's initial 1500,
        # and its settled after-rating is the next game's start-time rating.
        first, second = group(elo=1512.5), group(GROUP_B, created=OLD, elo=1500.0)
        client = self.client([(None, page(first, second))],
                             {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        self.run_sync(client)
        self.assertEqual(len(self.files()), 2)
        return first, second

    def test_global_sync_downloads_all_public_groups_without_ladder_requests(self):
        self.seed_two()
        self.assertTrue(self.catalog.get_meta('history_complete'))
        records = list(self.catalog.replay_records())
        self.assertEqual({row['match_id'] for row in records}, {MATCH_A, MATCH_B})
        self.assertTrue(all(row['downloaded'] for row in records))
        for path in self.files():
            self.assertEqual(path.read_bytes(), REPLAY_BYTES)

    def test_cursor_continues_after_current_head_refresh_and_process_restart(self):
        first = group()
        first_client = self.client([(None, page(first, more=True))], {GROUP_A: detail(first, MATCH_A)})
        self.run_sync(first_client, max_pages=1)
        self.assertEqual(self.catalog.get_meta('scan_state')['before'],
                         '2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4')
        self.assertFalse(self.catalog.get_meta('history_complete'))
        self.reopen()
        second = group(GROUP_B, created=OLD)
        second_client = self.client([
            (None, page(first, more=True)),
            ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4', page(second)),
        ], {GROUP_B: detail(second, MATCH_B)})
        self.run_sync(second_client, max_pages=2)
        self.assertEqual(len(self.files()), 2)
        self.assertTrue(self.catalog.get_meta('history_complete'))
        self.assertFalse(self.catalog.get_meta('scan_state'))

    def test_failed_discovery_page_preserves_committed_cursor_for_next_run(self):
        first = group()
        failing = self.client([
            (None, page(first, more=True)),
            ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4',
             fetch_replays.FetchError('temporary listing failure')),
        ], {GROUP_A: detail(first, MATCH_A)})
        self.assertTrue(self.run_sync(failing)['errors'])
        self.reopen()
        second = group(GROUP_B, created=OLD)
        resumed = self.client([
            (None, page(first, more=True)),
            ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4', page(second)),
        ], {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        self.run_sync(resumed)
        self.assertEqual(len(self.files()), 2)

    def test_resumed_head_and_old_queues_share_page_detail_and_download_budgets(self):
        first, second = group(), group(GROUP_B, created=OLD)
        fresh = group(GROUP_C, created='2026-10-14T12:00:00.000Z')
        oldest = group(GROUP_D, created=OLDER)
        self.catalog.store_detail(detail(first, MATCH_A))
        self.catalog.upsert_group(second)
        self.catalog.order_attempts('detail', [GROUP_B])
        self.catalog.order_attempts('download', [MATCH_A])
        state = {'before': NEW + '_' + GROUP_A, 'newest': {'created_at': NEW, 'id': GROUP_A},
                 'stop_at': None, 'observed_at': '2026-10-14T11:00:00+00:00'}
        self.catalog.set_meta('scan_state', state)
        client = self.client([(None, page(fresh, more=True)),
                              (NEW + '_' + GROUP_A, page(oldest, more=True))],
                             {GROUP_B: detail(second, MATCH_B), GROUP_C: detail(fresh, MATCH_C)})
        result = self.run_sync(client, max_pages=2, max_details=2, max_downloads=2)
        self.assertEqual(result['errors'], 0)
        listings = [call for call in client.get_json.call_args_list if call.args[0] == '/api/v1/match-groups']
        self.assertEqual(len(listings), 2)
        self.assertEqual([call.args[0] for call in self.detail_calls(client)], [ROUTES[GROUP_C], ROUTES[GROUP_B]])
        self.assertEqual([call.args[0] for call in client.download.call_args_list],
                         [REPLAY_URLS[MATCH_C], REPLAY_URLS[MATCH_A]])
        self.assertTrue(self.replay_path(GROUP_C, MATCH_C).is_file())
        self.assertTrue(self.replay_path(GROUP_A, MATCH_A).is_file())
        self.assertFalse(self.replay_path(GROUP_B, MATCH_B).exists())
        expected_state = dict(state, before=OLDER + '_' + GROUP_D)
        self.assertEqual(self.catalog.get_meta('scan_state'), expected_state)
        self.assertIsNone(self.catalog.get_meta('history_through'))
        for record in self.catalog.replay_records():
            self.assertTrue(all(side['elo_at_time'] is None for side in record['teams']))

    def test_single_page_budget_alternates_head_and_backfill_across_restart(self):
        first, second = group(), group(GROUP_B, created=OLD)
        fresh = group(GROUP_C, created='2026-10-14T12:00:00.000Z')
        initial = self.client([(None, page(first, more=True))])
        self.run_sync(initial, max_pages=1, max_details=0, max_downloads=0)
        saved = self.catalog.get_meta('scan_state')
        self.reopen()
        head = self.client([(None, page(fresh, more=True))], {GROUP_C: detail(fresh, MATCH_C)})
        self.run_sync(head, max_pages=1, max_details=1, max_downloads=1)
        self.assertEqual(self.catalog.get_meta('scan_state'), saved)
        self.assertTrue(self.replay_path(GROUP_C, MATCH_C).is_file())
        self.reopen()
        backfill = self.client([(NEW + '_' + GROUP_A, page(second, more=True))],
                               {GROUP_A: detail(first, MATCH_A)})
        self.run_sync(backfill, max_pages=1, max_details=1, max_downloads=1)
        self.assertEqual(self.catalog.get_meta('scan_state')['before'], OLD + '_' + GROUP_B)
        self.assertTrue(self.replay_path(GROUP_A, MATCH_A).is_file())
        self.assertEqual(len(self.detail_calls(head)), 1)
        self.assertEqual(len(self.detail_calls(backfill)), 1)

    def test_current_head_failure_does_not_block_backfill_or_queued_downloads(self):
        first, second = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(first, more=True))], {GROUP_A: detail(first, MATCH_A)})
        self.run_sync(initial, max_pages=1, max_downloads=0)
        self.reopen()
        resumed = self.client([(None, fetch_replays.FetchError('head temporarily unavailable')),
                               (NEW + '_' + GROUP_A, page(second))])
        result = self.run_sync(resumed, max_pages=2, max_details=0, max_downloads=1)
        self.assertEqual(result['errors'], 1)
        self.assertIsNone(self.catalog.get_meta('scan_state'))
        self.assertEqual(resumed.download.call_count, 1)
        self.assertTrue(self.replay_path().is_file())

    def test_single_detail_slot_alternates_failed_head_work_with_old_queue(self):
        first, second = group(), group(GROUP_B, created=OLD)
        fresh = group(GROUP_C, created='2026-10-14T12:00:00.000Z')
        oldest = group(GROUP_D, created=OLDER)
        self.catalog.upsert_group(second)
        self.catalog.order_attempts('detail', [GROUP_B])
        self.catalog.set_meta('scan_state', {'before': NEW + '_' + GROUP_A,
            'newest': {'created_at': NEW, 'id': GROUP_A}, 'stop_at': None})
        first_pass = self.client([(None, page(fresh, more=True)),
                                  (NEW + '_' + GROUP_A, page(oldest, more=True))],
                                 {GROUP_C: fetch_replays.FetchError('fresh detail unavailable')})
        self.assertEqual(self.run_sync(first_pass, max_pages=2, max_details=1, max_downloads=0)['errors'], 1)
        self.assertEqual([c.args[0] for c in self.detail_calls(first_pass)], [ROUTES[GROUP_C]])
        self.reopen()
        second_pass = self.client([(None, page(fresh, more=True)), (OLDER + '_' + GROUP_D, page())],
                                  {GROUP_B: detail(second, MATCH_B)})
        self.run_sync(second_pass, max_pages=2, max_details=1, max_downloads=1)
        self.assertEqual([c.args[0] for c in self.detail_calls(second_pass)], [ROUTES[GROUP_B]])
        self.assertTrue(self.replay_path(GROUP_B, MATCH_B).is_file())

    def test_reserved_work_interleaves_old_attempts_before_second_fresh_attempt(self):
        self.catalog.order_attempts('detail', [GROUP_A, GROUP_B])
        ordered = sync_replays.work_order(self.catalog, 'detail',
            [GROUP_A, GROUP_B, GROUP_C, GROUP_D], {GROUP_C, GROUP_D}, 4)
        self.assertEqual(ordered, [GROUP_C, GROUP_A, GROUP_D, GROUP_B])

    def test_cursor_does_not_advance_when_group_cache_write_fails(self):
        first, second = group(), group(GROUP_B, created=OLD)
        client = self.client([(None, page(first, second, more=True))],
                             {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        original = self.catalog.upsert_group
        calls = 0

        def store(row):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('simulated catalog write failure')
            return original(row)

        with patch.object(self.catalog, 'upsert_group', side_effect=store):
            self.assertTrue(self.run_sync(client)['errors'])
        scan = self.catalog.get_meta('scan_state') or {}
        self.assertFalse(scan.get('before'))
        self.reopen()
        restarted = self.client([(None, page(first, second))],
                                {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        self.run_sync(restarted)
        self.assertEqual(len(self.files()), 2)

    def test_failed_first_incremental_request_persists_active_scan_before_http(self):
        first, second = self.seed_two()
        self.assertIsNone(self.catalog.get_meta('scan_state'))
        previous_frontier = self.catalog.get_meta('discovery_frontier')
        failing = self.client([(None, fetch_replays.FetchError('first request unavailable'))])
        original_request = failing.get_json.side_effect

        def request_with_checkpoint(path, params=None):
            self.assertIsNotNone(self.catalog.get_meta('scan_state'),
                                 'Discovery must checkpoint before the first network request')
            return original_request(path, params)

        failing.get_json.side_effect = request_with_checkpoint
        self.assertTrue(self.run_sync(failing)['errors'])
        scan = self.catalog.get_meta('scan_state')
        self.assertIsInstance(scan, dict)
        self.assertIsNone(scan['before'])
        self.assertEqual(scan['stop_at'], previous_frontier)
        self.reopen()
        self.assertIsNotNone(self.catalog.get_meta('scan_state'))
        for record in self.catalog.replay_records():
            self.assertTrue(all(side['elo_at_time'] is None for side in record['teams']))
        recovered = self.client([(None, page(first, second))])
        self.run_sync(recovered)
        self.assertIsNone(self.catalog.get_meta('scan_state'))
        records = list(self.catalog.replay_records(team=TEAM, min_elo=1510))
        self.assertEqual([record['match_id'] for record in records], [MATCH_A])

    def test_partial_incremental_page_hides_derived_elo_after_committed_group_write(self):
        self.seed_two()
        self.assertTrue(self.catalog.get_meta('history_complete'))
        self.assertIsNone(self.catalog.get_meta('scan_state'))
        before = list(self.catalog.replay_records(team=TEAM, min_elo=1510))
        self.assertEqual(before[0]['teams'][0]['elo_at_time'], 1512.5)
        newest = group(GROUP_C, created='2026-10-14T12:00:00.000Z', elo=1537.5)
        missing = group(GROUP_D, created='2026-10-14T11:00:00.000Z', elo=1525.0)
        client = self.client([(None, page(newest, missing, more=True))])
        original = self.catalog.upsert_group

        def store(row):
            if row['id'] == GROUP_D:
                raise OSError('second incremental cache write failed')
            return original(row)

        with patch.object(self.catalog, 'upsert_group', side_effect=store):
            self.assertTrue(self.run_sync(client, max_details=0, max_downloads=0)['errors'])
        self.assertIn(GROUP_C, self.catalog.known_group_ids())
        self.assertNotIn(GROUP_D, self.catalog.known_group_ids())
        scan = self.catalog.get_meta('scan_state')
        self.assertIsInstance(scan, dict)
        self.assertIsNone(scan['before'])
        self.reopen()
        self.assertIsNotNone(self.catalog.get_meta('scan_state'))
        records = list(self.catalog.replay_records())
        self.assertEqual({record['match_id'] for record in records}, {MATCH_A, MATCH_B})
        for record in records:
            self.assertTrue(all(side['elo_at_time'] is None for side in record['teams']))
        self.assertFalse(list(self.catalog.replay_records(team=TEAM, min_elo=1510)))

    def test_unchanged_terminal_groups_need_no_detail_or_download_on_rerun(self):
        first, second = self.seed_two()
        original = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                    for path in self.files()}
        self.reopen()
        client = self.client([(None, page(first, second, more=True))])
        self.run_sync(client)
        self.assertFalse(self.detail_calls(client))
        client.download.assert_not_called()
        self.assertEqual(original,
                         {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                          for path in self.files()})

    def test_pending_group_older_than_frontier_is_rechecked_when_replay_finishes(self):
        first, pending = group(), group(GROUP_B, created=OLD, status='running')
        old_boundary = group(GROUP_C, created=OLDER)
        initial = self.client([(None, page(first, pending, old_boundary))],
                              {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(pending, MATCH_B),
                               GROUP_C: detail(old_boundary, MATCH_C)})
        self.run_sync(initial)
        self.assertEqual(len(self.files()), 2)
        self.reopen()
        finished = group(GROUP_B, created=OLD)
        client = self.client([(None, page(first, old_boundary, more=True))],
                             {GROUP_B: detail(finished, MATCH_B)})
        self.run_sync(client)
        self.assertEqual([call.args[0] for call in self.detail_calls(client)], [ROUTES[GROUP_B]])
        self.assertTrue(self.replay_path(GROUP_B, MATCH_B).exists())

    def test_incremental_scan_reads_all_groups_at_frontier_timestamp(self):
        first, second = self.seed_two()
        self.reopen()
        same_time = group(GROUP_C, created=NEW)
        client = self.client([
            (None, page(first, more=True)),
            ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4',
             page(same_time, second, more=True)),
        ], {GROUP_C: detail(same_time, MATCH_C)})
        self.run_sync(client)
        self.assertTrue(self.replay_path(GROUP_C, MATCH_C).exists())
        self.assertEqual(client.download.call_count, 1)
        self.assertFalse(self.catalog.get_meta('scan_state'))

    def test_detail_limit_preserves_unprocessed_groups_across_restart(self):
        first, second, third = group(), group(GROUP_B, created=OLD), group(GROUP_C, created=OLDER)
        details = {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B),
                   GROUP_C: detail(third, MATCH_C)}
        for expected_files in (1, 2, 3):
            if expected_files != 1:
                self.reopen()
            client = self.client([(None, page(first, second, third))], details)
            self.run_sync(client, max_details=1)
            self.assertLessEqual(len(self.detail_calls(client)), 1)
            self.assertEqual(len(self.files()), expected_files)

    def test_zero_limits_discover_history_without_details_or_downloads(self):
        first, second = group(), group(GROUP_B, created=OLD)
        client = self.client([(None, page(first, second))])
        self.run_sync(client, max_details=0, max_downloads=0)
        self.assertFalse(self.detail_calls(client))
        client.download.assert_not_called()
        self.assertEqual(self.catalog.known_group_ids(), {GROUP_A, GROUP_B})
        self.assertEqual(set(self.catalog.group_ids_needing_detail()), {GROUP_A, GROUP_B})
        self.assertTrue(self.catalog.get_meta('history_complete'))
        self.assertFalse(self.files())

    def test_failed_group_detail_is_retried_after_restart(self):
        first = group()
        failed = self.client([(None, page(first))],
                             {GROUP_A: fetch_replays.FetchError('temporary detail failure')})
        self.assertTrue(self.run_sync(failed)['errors'])
        failed.download.assert_not_called()
        self.assertEqual(set(self.catalog.group_ids_needing_detail()), {GROUP_A})
        self.reopen()
        retried = self.client([(None, page(first))], {GROUP_A: detail(first, MATCH_A)})
        self.assertFalse(self.run_sync(retried)['errors'])
        self.assertEqual(len(self.detail_calls(retried)), 1)
        self.assertEqual(self.replay_path().read_bytes(), REPLAY_BYTES)

    def assert_detail_budget_is_fair(self, *, running):
        newest = group(status='running' if running else 'finished')
        older = group(GROUP_B, created=OLD)
        newest_detail = (detail(newest, MATCH_A) if running else
                         fetch_replays.FetchError('newest detail keeps failing'))
        details = {GROUP_A: newest_detail, GROUP_B: detail(older, MATCH_B)}
        initial = self.client([(None, page(newest, older))], details)
        self.run_sync(initial, max_details=1)
        self.assertEqual([call.args[0] for call in self.detail_calls(initial)], [ROUTES[GROUP_A]])
        self.assertFalse(self.files())
        self.reopen()
        next_pass = self.client([(None, page(newest, older))], details)
        self.run_sync(next_pass, max_details=1)
        self.assertEqual([call.args[0] for call in self.detail_calls(next_pass)], [ROUTES[GROUP_B]])
        self.assertEqual(self.replay_path(GROUP_B, MATCH_B).read_bytes(), REPLAY_BYTES)
        self.assertIn(GROUP_A, set(self.catalog.group_ids_needing_detail()))
        # Rotation continues to retry pending work once older work was served.
        self.reopen()
        retried = self.client([(None, page(newest, older))], details)
        self.run_sync(retried, max_details=1)
        self.assertEqual([call.args[0] for call in self.detail_calls(retried)], [ROUTES[GROUP_A]])
        retried.download.assert_not_called()

    def test_detail_budget_running_newest_does_not_starve_older_group(self):
        self.assert_detail_budget_is_fair(running=True)

    def test_detail_budget_failing_newest_does_not_starve_older_group(self):
        self.assert_detail_budget_is_fair(running=False)

    def test_download_limit_preserves_queue_across_restart(self):
        first, second, third = group(), group(GROUP_B, created=OLD), group(GROUP_C, created=OLDER)
        details = {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B),
                   GROUP_C: detail(third, MATCH_C)}
        for expected_files in (1, 2, 3):
            if expected_files != 1:
                self.reopen()
            client = self.client([(None, page(first, second, third))], details)
            self.run_sync(client, max_downloads=1)
            self.assertLessEqual(client.download.call_count, 1)
            self.assertEqual(len(self.files()), expected_files)
            if expected_files != 1:
                self.assertFalse(self.detail_calls(client))

    def test_rate_limited_discovery_stops_before_queued_downloads(self):
        first, second = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(first, second))],
                              {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        self.run_sync(initial, max_downloads=0)
        limited = self.client([(None, fetch_replays.RateLimitError('Rate limited; retry after 120 seconds', 120))])
        with self.assertRaises(fetch_replays.RateLimitError):
            self.run_sync(limited)
        limited.download.assert_not_called()
        self.assertEqual(len(list(self.catalog.replay_records(pending_only=True))), 2)
        self.assertIsNotNone(self.catalog.get_meta('scan_state'))
        self.reopen()
        resumed = self.client([(None, page(first, second))])
        self.assertEqual(self.run_sync(resumed)['new_downloads'], 2)

    def test_rate_limited_head_does_not_request_backfill(self):
        first = group()
        initial = self.client([(None, page(first, more=True))])
        self.run_sync(initial, max_pages=1, max_details=0, max_downloads=0)
        checkpoint = self.catalog.get_meta('scan_state')
        limited = self.client([(None, fetch_replays.RateLimitError('Rate limited; retry after 5 seconds', 5))])
        with self.assertRaises(fetch_replays.RateLimitError):
            self.run_sync(limited, max_pages=2)
        self.assertEqual(limited.get_json.call_count, 1)
        limited.download.assert_not_called()
        self.assertEqual(self.catalog.get_meta('scan_state'), checkpoint)

    def test_rate_limited_detail_stops_before_other_groups(self):
        first, second = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(first, second))])
        self.run_sync(initial, max_details=0, max_downloads=0)
        limited = self.client([(None, page(first, second))],
                              {GROUP_A: fetch_replays.RateLimitError('Rate limited; retry after 5 seconds', 5),
                               GROUP_B: detail(second, MATCH_B)})
        with self.assertRaises(fetch_replays.RateLimitError):
            self.run_sync(limited)
        self.assertEqual(len(self.detail_calls(limited)), 1)
        limited.download.assert_not_called()
        self.assertEqual(len(list(self.catalog.group_ids_needing_detail())), 2)

    def test_rate_limited_download_preserves_files_and_pending_work_for_restart(self):
        first, second, third = group(), group(GROUP_B, created=OLD), group(GROUP_C, created=OLDER)
        initial = self.client([(None, page(first, second, third))],
                              {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B),
                               GROUP_C: detail(third, MATCH_C)})
        self.run_sync(initial, max_downloads=1)
        existing = self.replay_path().read_bytes()
        metadata = self.replay_path().with_suffix('.json').read_bytes()
        limited = self.client([(None, page(first, second, third))])
        limited.download.side_effect = fetch_replays.RateLimitError('Rate limited; retry after 5 seconds', 5)
        with self.assertRaises(fetch_replays.RateLimitError):
            self.run_sync(limited)
        self.assertEqual(limited.download.call_count, 1)
        self.assertEqual(self.replay_path().read_bytes(), existing)
        self.assertEqual(self.replay_path().with_suffix('.json').read_bytes(), metadata)
        self.assertEqual(list(self.outdir.glob('*/*.part')), [])
        self.reopen()
        self.assertEqual(len(list(self.catalog.replay_records(pending_only=True))), 2)
        resumed = self.client([(None, page(first, second, third))])
        self.assertEqual(self.run_sync(resumed)['new_downloads'], 2)
        self.assertEqual(len(self.files()), 3)

    def test_failed_download_retries_from_catalog_after_restart(self):
        first, second = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(first, second))],
                              {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)},
                              fail_downloads=[REPLAY_URLS[MATCH_A]])
        self.assertTrue(self.run_sync(initial)['errors'])
        self.assertFalse(self.replay_path().exists())
        self.assertTrue(self.replay_path(GROUP_B, MATCH_B).exists())
        self.assertEqual(list(self.outdir.glob('*/*.part')), [])
        self.reopen()
        retry = self.client([(None, page(first, second))])
        result = self.run_sync(retry)
        self.assertFalse(result['errors'])
        self.assertFalse(self.detail_calls(retry))
        self.assertEqual(retry.download.call_count, 1)
        self.assertTrue(self.replay_path().exists())

    def test_queued_download_continues_when_discovery_request_fails(self):
        newest, older = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(newest, older))],
                              {GROUP_A: detail(newest, MATCH_A), GROUP_B: detail(older, MATCH_B)})
        self.run_sync(initial, max_downloads=0)
        self.assertFalse(self.files())
        self.assertEqual(len(list(self.catalog.replay_records(pending_only=True))), 2)
        self.reopen()
        failing_discovery = self.client([(None, fetch_replays.FetchError('listing unavailable'))])
        result = self.run_sync(failing_discovery, max_downloads=1)
        self.assertTrue(result['errors'])
        self.assertEqual(failing_discovery.download.call_count, 1)
        self.assertEqual(self.replay_path().read_bytes(), REPLAY_BYTES)
        self.assertFalse(self.replay_path(GROUP_B, MATCH_B).exists())
        self.assertIsNotNone(self.catalog.get_meta('scan_state'))
        sidecar = json.loads(self.replay_path().with_suffix('.json').read_text(encoding='utf-8'))
        self.assertTrue(all(side['elo_at_time'] is None for side in sidecar['teams']))

    def test_download_budget_repeated_newest_failure_does_not_starve_older_replay(self):
        newest, older = group(), group(GROUP_B, created=OLD)
        initial = self.client([(None, page(newest, older))],
                              {GROUP_A: detail(newest, MATCH_A), GROUP_B: detail(older, MATCH_B)},
                              fail_downloads=[REPLAY_URLS[MATCH_A]])
        self.assertTrue(self.run_sync(initial, max_downloads=1)['errors'])
        self.assertEqual([call.args[0] for call in initial.download.call_args_list], [REPLAY_URLS[MATCH_A]])
        self.assertFalse(self.files())
        self.reopen()
        next_pass = self.client([(None, page(newest, older))], fail_downloads=[REPLAY_URLS[MATCH_A]])
        self.run_sync(next_pass, max_downloads=1)
        self.assertEqual([call.args[0] for call in next_pass.download.call_args_list], [REPLAY_URLS[MATCH_B]])
        self.assertEqual(self.replay_path(GROUP_B, MATCH_B).read_bytes(), REPLAY_BYTES)
        self.assertFalse(self.replay_path().exists())
        self.assertFalse(self.detail_calls(next_pass))
        self.reopen()
        retried = self.client([(None, page(newest, older))], fail_downloads=[REPLAY_URLS[MATCH_A]])
        self.assertTrue(self.run_sync(retried, max_downloads=1)['errors'])
        self.assertEqual([call.args[0] for call in retried.download.call_args_list], [REPLAY_URLS[MATCH_A]])
        self.assertFalse(self.replay_path().exists())
        self.assertEqual(len(self.files()), 1)

    def test_missing_downloaded_file_is_requeued_without_refetching_detail(self):
        first, second = self.seed_two()
        self.replay_path().unlink()
        self.reopen()
        client = self.client([(None, page(first, second))])
        self.run_sync(client)
        self.assertFalse(self.detail_calls(client))
        self.assertEqual(client.download.call_count, 1)
        self.assertEqual(self.replay_path().read_bytes(), REPLAY_BYTES)

    def test_full_rescan_restarts_discovery_without_redownloading_files(self):
        first, second = self.seed_two()
        self.reopen()
        client = self.client([(None, page(first, more=True)),
                              ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4', page(second))],
                             {GROUP_A: detail(first, MATCH_A), GROUP_B: detail(second, MATCH_B)})
        self.run_sync(client, full=True)
        client.download.assert_not_called()
        self.assertEqual(len(self.files()), 2)
        self.assertTrue(self.catalog.get_meta('history_complete'))

    def test_existing_fetcher_file_is_imported_and_old_sidecar_is_enriched(self):
        path = self.replay_path()
        path.parent.mkdir()
        path.write_bytes(REPLAY_BYTES)
        old = {'group_id': GROUP_A, 'match_id': MATCH_A,
               'fetched_at': '2026-10-14T07:00:00Z', 'user_note': 'keep this field',
               'teams': [{'id': TEAM, 'name': 'Historic Team'}]}
        path.with_suffix('.json').write_text(json.dumps(old), encoding='utf-8')
        first = group()
        client = self.client([(None, page(first))], {GROUP_A: detail(first, MATCH_A)})
        self.run_sync(client)
        client.download.assert_not_called()
        self.assertEqual(path.read_bytes(), REPLAY_BYTES)
        records = list(self.catalog.replay_records())
        self.assertEqual([row['match_id'] for row in records], [MATCH_A])
        self.assertTrue(records[0]['downloaded'])
        sidecar = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        self.assertEqual(sidecar['fetched_at'], old['fetched_at'])
        self.assertEqual(sidecar['user_note'], old['user_note'])
        self.assertEqual(sidecar['teams'][0]['elo_before'], 1777.25)

    def test_listing_uses_historical_group_ratings_without_current_ladder(self):
        self.seed_two()
        records = list(self.catalog.replay_records(team=TEAM, kind='ranked', min_elo=1510))
        self.assertEqual([row['match_id'] for row in records], [MATCH_A])
        self.assertEqual(records[0]['teams'][0]['elo_before'], 1512.5)
        self.assertEqual(records[0]['teams'][0]['elo_after'], 1525.0)
        self.assertEqual(records[0]['teams'][0]['elo_at_time'], 1512.5)
        self.assertEqual(records[0]['teams'][0]['elo_source'], 'rated_event')

    def test_changed_local_file_reports_failure_without_conflicting_checksums(self):
        first, second = self.seed_two()
        path = self.replay_path()
        sidecar_before = path.with_suffix('.json').read_bytes()
        recorded_hash = next(row for row in self.catalog.replay_records()
                             if row['match_id'] == MATCH_A)['sha256']
        with path.open('ab') as replay:
            replay.write(b'changed')
        client = self.client([(None, page(first, second))])
        stats = self.run_sync(client)
        self.assertGreater(stats['errors'], 0)
        changed = next(row for row in self.catalog.replay_records() if row['match_id'] == MATCH_A)
        self.assertFalse(changed['downloaded'])
        self.assertEqual(changed['sha256'], recorded_hash)
        self.assertEqual(path.with_suffix('.json').read_bytes(), sidecar_before)
        client.download.assert_not_called()

    def test_resumed_backfill_keeps_original_snapshot_cutoff_for_late_games(self):
        self.clock.now.return_value = datetime.fromisoformat('2026-10-14T11:00:00+00:00')
        newest = group(GROUP_A, created=NEW, kind='unranked')
        older = group(GROUP_B, created=OLD, elo=1500)
        client = self.client([(None, page(newest, more=True))])
        self.run_sync(client, max_pages=1, max_details=0, max_downloads=0)
        self.reopen()
        self.clock.now.return_value = datetime.fromisoformat('2026-10-14T14:00:00+00:00')
        late_detail = detail(newest, MATCH_A)
        late_detail['matches'][0]['startedAt'] = '2026-10-14T13:00:00Z'
        client = self.client([(None, page(newest, more=True)),
                              ('2026-10-14T10:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4', page(older))],
                             {GROUP_A: late_detail, GROUP_B: detail(older, MATCH_B)})
        self.run_sync(client)
        late_record = next(row for row in self.catalog.replay_records() if row['match_id'] == MATCH_A)
        self.assertIsNone(late_record['teams'][0]['elo_at_time'])
        self.assertEqual(late_record['teams'][0]['elo_source'], 'after_snapshot')
        self.assertEqual(self.catalog.get_meta('history_through'), '2026-10-14T11:00:00+00:00')
        self.run_sync(self.client([(None, page(newest, older))]))
        refreshed = next(row for row in self.catalog.replay_records() if row['match_id'] == MATCH_A)
        self.assertEqual(refreshed['teams'][0]['elo_at_time'], 1512.5)

    def test_front_scan_cannot_claim_elo_while_old_ranked_group_awaits_refresh(self):
        self.clock.now.return_value = datetime.fromisoformat('2026-10-14T14:00:00+00:00')
        anchor = group(GROUP_C, created=OLD, elo=1500)
        anchor['ratedAt'] = '2026-10-14T12:00:00Z'
        pending = group(GROUP_D, created=OLDER, status='running')
        target = group(GROUP_B, created=NEW, kind='unranked')
        target_detail = detail(target, MATCH_B)
        target_detail['matches'][0]['startedAt'] = '2026-10-14T13:30:00Z'
        self.catalog.upsert_group(anchor)
        self.catalog.upsert_group(pending)
        self.catalog.store_detail(target_detail)
        self.catalog.set_meta('history_complete', True)
        self.catalog.set_meta('discovery_frontier', {'created_at': '2026-10-14T13:00:00Z', 'id': GROUP_A})
        newest = group(GROUP_A, created='2026-10-14T13:00:00Z', kind='unranked')
        self.run_sync(self.client([(None, page(newest, target, anchor, more=True))]),
                      max_details=0, max_downloads=0)
        record = next(row for row in self.catalog.replay_records() if row['match_id'] == MATCH_B)
        self.assertIsNone(record['teams'][0]['elo_at_time'])
        self.assertEqual(record['teams'][0]['elo_source'], 'pending_rating')
        self.assertIsNone(self.catalog.get_meta('scan_state'))
        self.assertEqual(self.catalog.get_meta('history_through'), '2026-10-14T14:00:00+00:00')

    def test_list_command_filters_local_historical_elo_without_network(self):
        self.seed_two()
        output = StringIO()
        with patch.object(fetch_replays.Client, '_request', side_effect=AssertionError('List attempted a network request')):
            with redirect_stdout(output), redirect_stderr(StringIO()):
                result = sync_replays.main(['--outdir', str(self.outdir), '--list', '--team', TEAM,
                                            '--kind', 'ranked', '--min-elo', '1510', '--limit', '1', '--json'])
        self.assertEqual(result, 0)
        records = json.loads(output.getvalue())
        self.assertEqual([record['match_id'] for record in records], [MATCH_A])
        self.assertEqual(records[0]['teams'][0]['elo_at_time'], 1512.5)

    def test_sync_rejects_client_from_another_origin_before_any_request(self):
        client = fetch_replays.Client('https://2027a.battlecode.co.uk', delay=0)
        client.get_json = Mock()
        client.download = Mock()
        with self.assertRaisesRegex(fetch_replays.FetchError, 'origin'):
            sync_replays.sync_once(client, self.catalog)
        client.get_json.assert_not_called()
        client.download.assert_not_called()

    def test_catalog_cannot_reopen_same_storage_for_another_event_origin(self):
        self.seed_two()
        self.catalog.close()
        with self.assertRaises((fetch_replays.FetchError, RuntimeError, ValueError)):
            Catalog(self.outdir, 'https://2027a.battlecode.co.uk')
        self.catalog = Catalog(self.outdir, BASE_URL)
        self.assertEqual(len(list(self.catalog.replay_records())), 2)


if __name__ == '__main__':
    unittest.main()
