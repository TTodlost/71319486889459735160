"""Public API traversal and safe replay downloads, without network fixtures."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'reconstruction'))
from tools import fetch_replays


TEAM = 'ee83f5e5-bcad-4923-86b4-b79ea6083d2e'
OTHER_TEAM = '8a859a54-6acb-4e46-9f54-59d7e955b06c'
GROUP = '64013d4f-257c-43c5-bf6a-6b0fd598a7f4'
OTHER_GROUP = '8b46b3b6-f9c5-4083-8d7c-bb549b90f550'
MATCH = '187ad51c-b387-4db0-b9f5-157354de5398'
OTHER_MATCH = '2bcacac1-ec95-4d14-a850-4be1761a4722'
PENDING_MATCH = 'b2f6634a-6462-4d4f-a4cf-d2f64282b35c'
# The replay prefix is magic + two three-byte versions, followed by map magic.
# This fixture checks transport validation only, not a complete engine replay.
REPLAY_HEADER = bytes.fromhex('6136327065720000000001000001')
REPLAY_BYTES = REPLAY_HEADER + b'w62pam\0\0' + bytes(range(1, 33))


def ladder(teams, *, page=1, pages=1):
    return {'teams': teams, 'unrated': [], 'page': page, 'pages': pages,
            'pageSize': 25, 'total': len(teams)}


def group(group_id=GROUP, *, created='2026-10-14T09:00:00.000Z', kind='ranked'):
    return {'id': group_id, 'createdAt': created, 'kind': kind, 'status': 'completed'}


def match(match_id=MATCH, *, replay_url=None):
    return {'id': match_id, 'map': 'collision', 'status': 'completed', 'position': 0,
            'replayUrl': replay_url or f'/api/v1/matches/{match_id}/bundle'}


def group_details(group_id=GROUP, *, matches=()):
    return {'group': dict(group(group_id), sides=[{'teamId': TEAM, 'teamName': 'Example'}]),
            'matches': list(matches)}


def archive_bytes(replay=REPLAY_BYTES, *, member='replay.repl26a'):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(member, replay)
        archive.writestr('result.json', '{"status":"completed"}')
    return buffer.getvalue()


def corrupt_archive_bytes():
    payload = bytearray(archive_bytes())
    name_length, extra_length = struct.unpack_from('<HH', payload, 26)
    data_offset = 30 + name_length + extra_length
    payload[data_offset + len(REPLAY_BYTES) - 1] ^= 0x01
    return bytes(payload)


class ScriptedClient:
    """Require the documented route and selected query fields for every call."""
    def __init__(self, steps=(), *, payload=REPLAY_BYTES, interrupted=False):
        self.steps = list(steps)
        self.calls = []
        self.downloads = []
        self.payload = payload
        self.interrupted = interrupted

    def get_json(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if not self.steps:
            raise AssertionError(f'Unexpected API request: {path}, {params}')
        expected_path, expected_params, result = self.steps.pop(0)
        if path != expected_path:
            raise AssertionError(f'Expected {expected_path}, got {path}')
        for key, value in expected_params.items():
            if params.get(key) != value:
                raise AssertionError(f'Expected query {key}={value!r}, got {params}')
        return result

    def download(self, path, destination):
        destination = Path(destination)
        self.downloads.append((path, destination))
        destination.write_bytes(self.payload)
        if self.interrupted:
            raise OSError('connection lost during replay download')


class Response(BytesIO):
    def __init__(self, payload, headers=None, *, status=200):
        super().__init__(payload)
        self.headers = dict(headers or {})
        self.status = status


POLL_URL = f'/api/v1/matches/{MATCH}/bundle?downloadJob=example-job'


def pending_response(*, retry_after=5, headers=None, **fields):
    data = {'status': 'pending', 'jobId': 'example-job', 'pollUrl': POLL_URL,
            'retryAfter': retry_after}
    data.update(fields)
    return Response(json.dumps(data).encode(), {'Content-Type': 'application/json',
                                              **(headers or {})}, status=202)


class Clock:
    def __init__(self):
        self.now = 100.0

    def sleep(self, seconds):
        self.now += seconds

    def monotonic(self):
        return self.now


class ClientTests(unittest.TestCase):
    def client(self, *, retries=0):
        client = fetch_replays.Client(delay=0, retries=retries)
        client.opener = Mock()
        return client

    def test_relative_and_same_origin_absolute_urls_resolve(self):
        client = self.client()
        expected = 'https://2026a.battlecode.co.uk/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle'
        self.assertEqual(client.url('/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle'), expected)
        self.assertEqual(client.url(expected), expected)

    def test_foreign_origin_or_fragment_is_rejected_before_request(self):
        client = self.client()
        for path in ('https://other.example/api/v1/matches/x/bundle',
                     '//other.example/api/v1/matches/x/bundle',
                     'https://user:password@2026a.battlecode.co.uk/api/v1/ladder',
                     '/api/v1/ladder#unexpected'):
            with self.subTest(path=path):
                with self.assertRaises(fetch_replays.FetchError):
                    client.get_json(path)
        client.opener.open.assert_not_called()

    def test_get_json_encodes_query_and_honors_timeout(self):
        client = self.client()
        client.timeout = 7
        client.opener.open.return_value = Response(b'{"teams":[]}')
        self.assertEqual(client.get_json('/api/v1/ladder', {'q': 'A & B', 'page': 2}), {'teams': []})
        args, kwargs = client.opener.open.call_args
        parsed = urlsplit(args[0].full_url)
        self.assertEqual(parsed.path, '/api/v1/ladder')
        self.assertEqual(parse_qs(parsed.query), {'q': ['A & B'], 'page': ['2']})
        self.assertEqual(kwargs['timeout'], 7)

    def test_json_html_and_non_object_are_rejected(self):
        for payload in (b'<html>bad gateway</html>', b'[1,2]', b'{unfinished'):
            with self.subTest(payload=payload):
                client = self.client()
                client.opener.open.return_value = Response(payload)
                with self.assertRaises(fetch_replays.FetchError):
                    client.get_json('/api/v1/ladder')

    def test_transient_http_failure_is_retried(self):
        client = self.client(retries=1)
        error = HTTPError('https://2026a.battlecode.co.uk/api/v1/ladder', 503,
                          'temporarily unavailable', {'Retry-After': '0'}, BytesIO(b''))
        client.opener.open.side_effect = [error, Response(b'{"teams":[]}')]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            self.assertEqual(client.get_json('/api/v1/ladder'), {'teams': []})
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_permanent_http_failure_is_not_retried(self):
        client = self.client(retries=3)
        client.opener.open.side_effect = HTTPError(
            'https://2026a.battlecode.co.uk/api/v1/ladder', 404, 'not found', {}, BytesIO(b''))
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, '404'):
                client.get_json('/api/v1/ladder')
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_long_retry_after_returns_clear_failure_without_waiting(self):
        client = self.client(retries=1)
        client.opener.open.side_effect = HTTPError(
            'https://2026a.battlecode.co.uk/api/v1/ladder', 429, 'rate limited',
            {'Retry-After': '120'}, BytesIO(b''))
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, '120'):
                client.get_json('/api/v1/ladder')
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_rate_limit_cooldown_blocks_requests_until_monotonic_expiry(self):
        client = self.client(retries=1)
        client.opener.open.side_effect = [HTTPError(
            'https://2026a.battlecode.co.uk/api/v1/ladder', 429, 'rate limited',
            {'Retry-After': '120'}, BytesIO(b'')), Response(b'{"teams":[]}')]
        with patch.object(fetch_replays.time, 'sleep') as sleep, \
                patch.object(fetch_replays.time, 'monotonic', return_value=100) as monotonic:
            with self.assertRaises(fetch_replays.RateLimitError) as first:
                client.get_json('/api/v1/ladder')
            self.assertEqual(first.exception.retry_after, 120)
            monotonic.return_value = 150
            with self.assertRaises(fetch_replays.RateLimitError) as second:
                client.get_json('/api/v1/match-groups')
            self.assertEqual(second.exception.retry_after, 70)
            self.assertEqual(client.opener.open.call_count, 1)
            monotonic.return_value = 221
            self.assertEqual(client.get_json('/api/v1/ladder'), {'teams': []})
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_not_called()

    def test_exhausted_rate_limit_preserves_cooldown_including_without_retries(self):
        for retries in (0, 1):
            with self.subTest(retries=retries):
                client = self.client(retries=retries)
                client.opener.open.side_effect = [HTTPError(
                    'https://2026a.battlecode.co.uk/api/v1/ladder', 429, 'rate limited',
                    {'Retry-After': '5'}, BytesIO(b'')) for _ in range(retries + 1)]
                clock = Clock()
                with patch.object(fetch_replays.time, 'sleep', side_effect=clock.sleep) as sleep, \
                        patch.object(fetch_replays.time, 'monotonic', side_effect=clock.monotonic):
                    with self.assertRaises(fetch_replays.RateLimitError) as error:
                        client.get_json('/api/v1/ladder')
                    self.assertEqual(error.exception.retry_after, 15 * 2 ** retries)
                    with self.assertRaises(fetch_replays.RateLimitError):
                        client.get_json('/api/v1/ladder')
                self.assertEqual(client.opener.open.call_count, retries + 1)
                self.assertEqual(sleep.call_args_list, [call(15)] * retries)

    def test_short_rate_limit_retries_honor_header_and_succeed(self):
        client = self.client(retries=1)
        client.opener.open.side_effect = [HTTPError(
            'https://2026a.battlecode.co.uk/api/v1/ladder', 429, 'rate limited',
            {'Retry-After': '5'}, BytesIO(b'')), Response(b'{"teams":[]}')]
        clock = Clock()
        with patch.object(fetch_replays.time, 'sleep', side_effect=clock.sleep) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=clock.monotonic):
            self.assertEqual(client.get_json('/api/v1/ladder'), {'teams': []})
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_called_once_with(15)

    def test_repeated_rate_limits_increase_backoff_despite_short_server_hint(self):
        client = self.client(retries=3)
        client.opener.open.side_effect = [HTTPError(
            client.url('/api/v1/ladder'), 429, 'rate limited', {'Retry-After': '5'}, BytesIO(b''))
            for _ in range(3)] + [Response(b'{"teams":[]}')]
        clock = Clock()
        with patch.object(fetch_replays.time, 'sleep', side_effect=clock.sleep) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=clock.monotonic):
            self.assertEqual(client.get_json('/api/v1/ladder'), {'teams': []})
        self.assertEqual(client.opener.open.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(15), call(30), call(60)])

    def test_rate_limit_backoff_honors_larger_server_hint_and_caps_own_exponent(self):
        client = self.client()
        for attempt, header, expected in ((0, '40', 40), (1, '40', 40),
                                          (8, '5', 120), (8, '300', 300)):
            with self.subTest(attempt=attempt, header=header):
                error = HTTPError(client.url('/api/v1/ladder'), 429, 'rate limited',
                                  {'Retry-After': header}, BytesIO(b''))
                self.assertEqual(client._retry_delay(error, attempt), expected)
                error.close()

    def test_http_date_rate_limit_and_malformed_headers(self):
        for header, expected in [('Thu, 01 Jan 1970 00:02:00 GMT', 120),
                                 ('not a date', 15), ('nan', 15), ('inf', 15), ('0', 15)]:
            with self.subTest(header=header):
                client = self.client()
                client.opener.open.side_effect = HTTPError(
                    'https://2026a.battlecode.co.uk/api/v1/ladder', 429, 'rate limited',
                    {'Retry-After': header}, BytesIO(b''))
                with patch.object(fetch_replays.time, 'time', return_value=0), \
                        patch.object(fetch_replays.time, 'sleep') as sleep:
                    with self.assertRaises(fetch_replays.RateLimitError) as error:
                        client.get_json('/api/v1/ladder')
                self.assertEqual(error.exception.retry_after, expected)
                sleep.assert_not_called()

    def test_invalid_download_response_retries_and_replaces_all_partial_bytes(self):
        for payload in (b'<html>Service temporarily unavailable</html>' * 10,
                        REPLAY_HEADER, archive_bytes()[:-10], corrupt_archive_bytes()):
            with self.subTest(payload=payload[:16]):
                client = self.client(retries=1)
                client.opener.open.side_effect = [Response(payload), Response(REPLAY_BYTES)]
                with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory:
                    destination = Path(directory) / 'replay.part'
                    with patch.object(fetch_replays.time, 'sleep') as sleep:
                        client.download('/api/v1/matches/x/bundle', destination)
                    self.assertEqual(destination.read_bytes(), REPLAY_BYTES)
                self.assertEqual(client.opener.open.call_count, 2)
                sleep.assert_called_once_with(1)

    def test_download_validation_and_http_failures_share_one_retry_budget(self):
        client = self.client(retries=2)
        valid_bundle = archive_bytes()
        client.opener.open.side_effect = [
            Response(b'<html>busy</html>'),
            HTTPError('https://2026a.battlecode.co.uk/api/v1/matches/x/bundle',
                      503, 'busy', {'Retry-After': '0'}, BytesIO(b'')),
            Response(valid_bundle),
        ]
        with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory, \
                patch.object(fetch_replays.time, 'sleep') as sleep:
            destination = Path(directory) / 'replay.part'
            client.download('/api/v1/matches/x/bundle', destination)
            self.assertEqual(destination.read_bytes(), valid_bundle)
        self.assertEqual(client.opener.open.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(0)])

    def test_invalid_response_reports_header_fingerprint_and_http_encoding(self):
        client = self.client()
        payload = b'not a replay'
        client.opener.open.return_value = Response(
            payload, {'Content-Type': 'text/html', 'Content-Encoding': 'gzip'})
        with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory:
            with self.assertRaises(fetch_replays.FetchError) as caught:
                client.download('/api/v1/matches/x/bundle', Path(directory) / 'replay.part')
        self.assertIn(f'header={payload.hex()}', str(caught.exception))
        self.assertIn('12 bytes', str(caught.exception))
        self.assertIn("content-type='text/html'", str(caught.exception))
        self.assertIn("content-encoding='gzip'", str(caught.exception))

    def test_download_os_errors_are_not_retried(self):
        client = self.client(retries=3)
        client.opener.open.return_value = Response(REPLAY_BYTES)
        with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory, \
                patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaises(OSError):
                client.download('/api/v1/matches/x/bundle', Path(directory) / 'missing' / 'replay.part')
        self.assertEqual(client.opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_incomplete_download_is_retried_and_replaces_partial_bytes(self):
        client = self.client(retries=1)
        client.opener.open.side_effect = [
            Response(b'partial', {'Content-Length': str(len(REPLAY_BYTES))}),
            Response(REPLAY_BYTES, {'Content-Length': str(len(REPLAY_BYTES))}),
        ]
        with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory:
            destination = Path(directory) / 'replay.part'
            with patch.object(fetch_replays.time, 'sleep') as sleep:
                client.download('/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle', destination)
            self.assertEqual(destination.read_bytes(), REPLAY_BYTES)
        self.assertEqual(client.opener.open.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_malformed_content_length_is_rejected_before_file_creation(self):
        with tempfile.TemporaryDirectory(prefix='fetch_client_tests_') as directory:
            destination = Path(directory) / 'replay.part'
            for content_length in ('invalid', '-1'):
                with self.subTest(content_length=content_length):
                    client = self.client(retries=3)
                    client.opener.open.return_value = Response(
                        REPLAY_BYTES, {'Content-Length': content_length})
                    with patch.object(fetch_replays.time, 'sleep') as sleep:
                        with self.assertRaises(fetch_replays.FetchError):
                            client.download('/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle', destination)
                    self.assertEqual(client.opener.open.call_count, 1)
                    sleep.assert_not_called()
                    self.assertFalse(destination.exists())

    def test_source_urls_select_ladder_team_and_match_group(self):
        client = self.client()
        cases = [
            ('https://2026a.battlecode.co.uk/game/ladder', ('ladder', None)),
            ('https://2026a.battlecode.co.uk/game/team/ee83f5e5-bcad-4923-86b4-b79ea6083d2e', ('team', TEAM)),
            ('https://2026a.battlecode.co.uk/game/match-groups/64013d4f-257c-43c5-bf6a-6b0fd598a7f4', ('group', GROUP)),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(fetch_replays.parse_source(client, source), expected)


class RequestPacerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.pacer = fetch_replays.RequestPacer(delay=1)
        self.enterContext(patch.object(fetch_replays.time, 'monotonic', side_effect=self.clock.monotonic))
        self.sleep = self.enterContext(patch.object(fetch_replays.time, 'sleep', side_effect=self.clock.sleep))

    def client(self, retries=0):
        client = fetch_replays.Client(pacer=self.pacer, retries=retries)
        client.opener = Mock()
        return client

    def test_every_attempt_is_paced_including_pending_polls_and_failure_retries(self):
        client = self.client(retries=1)
        times = []
        responses = [pending_response(retry_after=0), HTTPError(
            client.url(POLL_URL), 503, 'busy', {'Retry-After': '0'}, BytesIO(b'')),
            Response(REPLAY_BYTES)]

        def open_request(*args, **kwargs):
            times.append(self.clock.now)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client.opener.open.side_effect = open_request
        with tempfile.TemporaryDirectory() as directory:
            client.download('/api/v1/matches/x/bundle', Path(directory) / 'replay.part')
        self.assertEqual(times, [100, 101, 102])

    def test_waiting_peer_rechecks_cooldown_added_during_its_pacing_sleep(self):
        self.pacer.wait()
        client = self.client()
        times = []
        client.opener.open.side_effect = lambda *args, **kwargs: (
            times.append(self.clock.now) or Response(b'{}'))

        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.sleep.call_count == 1:
                self.pacer.cooldown(30)

        self.sleep.side_effect = sleep
        self.assertEqual(client.get_json('/api/v1/ladder'), {})
        self.assertEqual(times, [131])
        self.assertEqual(self.sleep.call_args_list, [call(1), call(30)])

    def test_nonterminal_rate_limit_cooldown_is_shared_with_another_client(self):
        owner, peer = self.client(retries=1), self.client()
        times = []
        owner.opener.open.side_effect = [HTTPError(
            owner.url('/api/v1/ladder'), 429, 'rate limited', {'Retry-After': '5'}, BytesIO(b'')),
            Response(b'{}')]
        peer.opener.open.side_effect = lambda *args, **kwargs: (
            times.append(self.clock.now) or Response(b'{}'))

        def sleep(seconds):
            until = self.clock.now + seconds
            if self.sleep.call_count == 1:
                self.assertEqual(peer.get_json('/api/v1/match-groups'), {})
            self.clock.now = max(self.clock.now, until)

        self.sleep.side_effect = sleep
        self.assertEqual(owner.get_json('/api/v1/ladder'), {})
        self.assertEqual(times, [115])
        self.assertEqual(self.clock.now, 116)

    def test_exhausted_rate_limit_blocks_other_clients_without_opening(self):
        owner, peer = self.client(), self.client()
        owner.opener.open.side_effect = HTTPError(
            owner.url('/api/v1/ladder'), 429, 'rate limited', {'Retry-After': '5'}, BytesIO(b''))
        with self.assertRaises(fetch_replays.RateLimitError):
            owner.get_json('/api/v1/ladder')
        with self.assertRaises(fetch_replays.RateLimitError) as caught:
            peer.get_json('/api/v1/match-groups')
        self.assertEqual(caught.exception.retry_after, 15)
        peer.opener.open.assert_not_called()
        self.sleep.assert_not_called()

    def test_shared_cooldown_and_spacing_cannot_extend_pending_deadline(self):
        self.pacer.cooldown(30)
        with self.assertRaises(fetch_replays.RateLimitError):
            self.pacer.wait(deadline=120)
        self.sleep.assert_not_called()
        self.clock.now = 131
        self.pacer.wait()
        with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending'):
            self.pacer.wait(deadline=132)
        self.sleep.assert_not_called()


class PendingReplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='pending_replay_tests_')
        self.addCleanup(directory.cleanup)
        self.outdir = Path(directory.name)
        self.destination = self.outdir / GROUP / f'{MATCH}.repl26a'
        self.client = fetch_replays.Client(delay=0, retries=0)
        self.client.opener = Mock()
        self.record = {'group_id': GROUP, 'match_id': MATCH,
                       'replay_url': f'/api/v1/matches/{MATCH}/bundle'}

    def download(self):
        return fetch_replays.download_replay(self.client, self.record, self.outdir)

    def requested_urls(self):
        return [args[0].full_url for args, _ in self.client.opener.open.call_args_list]

    def assert_no_files(self):
        self.assertEqual([path for path in self.outdir.rglob('*') if path.is_file()], [])

    def test_pending_job_polls_returned_url_and_publishes_only_replay(self):
        bundle = archive_bytes()
        self.client.opener.open.side_effect = [
            pending_response(retry_after=9, headers={'Retry-After': '2'}), Response(bundle)]

        def wait(seconds):
            self.assertEqual(seconds, 2)
            self.assertFalse(self.destination.exists())
            self.assertEqual([path.read_bytes() for path in self.outdir.rglob('*.part')], [b''])

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait) as sleep:
            destination, fresh = self.download()
        self.assertTrue(fresh)
        self.assertEqual(destination.read_bytes(), bundle)
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url']),
                                                self.client.url(POLL_URL)])
        sleep.assert_called_once_with(2)
        self.assertEqual(list(self.outdir.rglob('*.part')), [])

    def test_repeated_pending_responses_do_not_consume_failure_retries(self):
        self.client.opener.open.side_effect = [pending_response() for _ in range(3)] + [
            Response(REPLAY_BYTES)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            self.download()
        self.assertEqual(self.destination.read_bytes(), REPLAY_BYTES)
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url'])] +
                         [self.client.url(POLL_URL)] * 3)
        self.assertEqual(sleep.call_args_list, [call(5)] * 3)

    def test_transient_failures_before_and_during_polls_share_retry_budget(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [
            HTTPError(self.client.url(self.record['replay_url']), 503, 'busy',
                      {'Retry-After': '0'}, BytesIO(b'')),
            pending_response(retry_after=0),
            HTTPError(self.client.url(POLL_URL), 503, 'busy',
                      {'Retry-After': '0'}, BytesIO(b'')),
            Response(REPLAY_BYTES),
        ]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, '503'):
                self.download()
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url'])] * 2 +
                         [self.client.url(POLL_URL)])
        self.assertEqual(sleep.call_args_list, [call(0), call(0)])
        self.assert_no_files()

    def test_retry_on_poll_reuses_poll_url_instead_of_starting_another_job(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [pending_response(retry_after=0),
                                              Response(b'<html>busy</html>'),
                                              pending_response(retry_after=0), Response(REPLAY_BYTES)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            self.download()
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url'])] +
                         [self.client.url(POLL_URL)] * 3)
        self.assertEqual(sleep.call_args_list, [call(0), call(1), call(0)])
        self.assertEqual(self.destination.read_bytes(), REPLAY_BYTES)

    def test_invalid_or_failed_job_json_is_rejected_without_publication_or_retry(self):
        for payload in (b'{unfinished', b'[]', b'{"status":"failed","error":"job failed"}',
                        b'{"status":"pending"}',
                        b'{"status":"pending","jobId":"","pollUrl":"/poll"}',
                        b'{"status":"pending","jobId":42,"pollUrl":"/poll"}',
                        b'{"status":"pending","jobId":"job","pollUrl":null}'):
            with self.subTest(payload=payload):
                self.client.retries = 3
                self.client.opener.open.reset_mock()
                self.client.opener.open.side_effect = [Response(payload, status=202)]
                with patch.object(fetch_replays.time, 'sleep') as sleep:
                    with self.assertRaises(fetch_replays.FetchError):
                        self.download()
                self.assertEqual(self.client.opener.open.call_count, 1)
                sleep.assert_not_called()
                self.assert_no_files()

    def test_unsafe_poll_urls_are_rejected_before_another_request(self):
        for poll_url in ('https://other.example/poll', '//other.example/poll',
                         '/poll#fragment', 'https://user:pass@2026a.battlecode.co.uk/poll', ' '):
            with self.subTest(poll_url=poll_url):
                self.client.opener.open.reset_mock()
                self.client.opener.open.side_effect = [pending_response(pollUrl=poll_url)]
                with patch.object(fetch_replays.time, 'sleep') as sleep:
                    with self.assertRaises(fetch_replays.FetchError):
                        self.download()
                self.assertEqual(self.client.opener.open.call_count, 1)
                sleep.assert_not_called()
                self.assert_no_files()

    def test_poll_delay_uses_valid_header_then_body_then_five_second_fallback(self):
        for header, body, expected in [(None, 7, 7), ('3', 7, 3), ('invalid', 2, 2),
                                       (None, None, 5), ('nan', 'inf', 5),
                                       (None, 'invalid', 5), (None, True, 5),
                                       (None, {'seconds': 7}, 5), (None, [7], 5),
                                       ('Thu, 01 Jan 1970 00:00:04 GMT', 7, 4)]:
            with self.subTest(header=header, body=body):
                self.destination.unlink(missing_ok=True)
                self.client.opener.open.reset_mock()
                headers = {} if header is None else {'Retry-After': header}
                self.client.opener.open.side_effect = [pending_response(retry_after=body, headers=headers),
                                                      Response(REPLAY_BYTES)]
                with patch.object(fetch_replays.time, 'sleep') as sleep, \
                        patch.object(fetch_replays.time, 'time', return_value=0):
                    self.download()
                sleep.assert_called_once_with(expected)

    def test_zero_delay_pending_is_bounded_to_twelve_poll_requests(self):
        self.client.opener.open.side_effect = lambda *args, **kwargs: pending_response(retry_after=0)
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 13)
        self.assertEqual(sleep.call_args_list, [call(0)] * 12)
        self.assert_no_files()

    def test_long_pending_delay_defers_without_sleeping_or_another_request(self):
        self.client.opener.open.side_effect = [pending_response(retry_after=120)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 1)
        sleep.assert_not_called()
        self.assert_no_files()

    def test_pending_waits_share_a_sixty_second_deadline(self):
        self.client.opener.open.side_effect = [pending_response(retry_after=20),
                                              pending_response(retry_after=20),
                                              pending_response(retry_after=21)]
        now = 100

        def wait(seconds):
            nonlocal now
            now += seconds

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=lambda: now):
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(20), call(20)])
        self.assert_no_files()

    def test_poll_timeout_is_limited_to_remaining_pending_deadline(self):
        self.client.opener.open.side_effect = [pending_response(retry_after=55), Response(REPLAY_BYTES)]
        now = 100

        def wait(seconds):
            nonlocal now
            now += seconds

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait), \
                patch.object(fetch_replays.time, 'monotonic', side_effect=lambda: now):
            self.download()
        self.assertEqual([kwargs['timeout'] for _, kwargs in self.client.opener.open.call_args_list],
                         [30, 5])

    def test_poll_failure_backoff_cannot_exceed_pending_deadline(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [pending_response(retry_after=40), HTTPError(
            self.client.url(POLL_URL), 503, 'busy', {'Retry-After': '21'}, BytesIO(b''))]
        now = 100

        def wait(seconds):
            nonlocal now
            now += seconds

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=lambda: now):
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 2)
        sleep.assert_called_once_with(40)
        self.assert_no_files()

    def test_rate_limit_during_polling_preserves_typed_error_and_cleans_temp(self):
        self.client.opener.open.side_effect = [pending_response(retry_after=0), HTTPError(
            self.client.url(POLL_URL), 429, 'rate limited', {'Retry-After': '120'}, BytesIO(b''))]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaises(fetch_replays.RateLimitError) as caught:
                self.download()
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertEqual(self.client.opener.open.call_count, 2)
        sleep.assert_called_once_with(0)
        self.assert_no_files()

    def test_rate_limit_beyond_pending_deadline_preserves_cooldown_and_blocks_next_request(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [pending_response(retry_after=40), HTTPError(
            self.client.url(POLL_URL), 429, 'rate limited', {'Retry-After': '30'}, BytesIO(b''))]
        now = 100

        def wait(seconds):
            nonlocal now
            now += seconds

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=lambda: now):
            with self.assertRaises(fetch_replays.RateLimitError) as first:
                self.download()
            self.assertEqual(first.exception.retry_after, 30)
            now += 5
            with self.assertRaises(fetch_replays.RateLimitError) as second:
                self.client.get_json('/api/v1/ladder')
            self.assertEqual(second.exception.retry_after, 25)
        self.assertEqual(self.client.opener.open.call_count, 2)
        sleep.assert_called_once_with(40)
        self.assert_no_files()

    def test_missing_poll_job_rechecks_original_url_once_with_shared_retry_budget(self):
        self.client.retries = 1
        bundle = archive_bytes()
        self.client.opener.open.side_effect = [pending_response(retry_after=0), HTTPError(
            self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b'{"error":"Not found"}')),
            Response(bundle)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            self.download()
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url']),
                                                self.client.url(POLL_URL),
                                                self.client.url(self.record['replay_url'])])
        self.assertEqual(sleep.call_args_list, [call(0), call(1)])
        self.assertEqual(self.destination.read_bytes(), bundle)
        self.assertEqual(list(self.outdir.rglob('*.part')), [])

    def test_missing_poll_job_fallback_keeps_original_url_after_new_pending_job(self):
        self.client.retries = 3
        next_poll = POLL_URL.replace('example-job', 'next-job')
        self.client.opener.open.side_effect = [pending_response(retry_after=0), HTTPError(
            self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b'')),
            pending_response(retry_after=0, jobId='next-job', pollUrl=next_poll), HTTPError(
                self.client.url(self.record['replay_url']), 404, 'not found', {}, BytesIO(b'')),
            Response(REPLAY_BYTES)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, '404'):
                self.download()
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url']),
                                                self.client.url(POLL_URL),
                                                self.client.url(self.record['replay_url']),
                                                self.client.url(self.record['replay_url'])])
        self.assertEqual(sleep.call_args_list, [call(0), call(1), call(0)])
        self.assert_no_files()

    def test_missing_poll_job_fallback_polls_original_until_same_job_finishes(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [pending_response(), HTTPError(
            self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b'')),
            pending_response(), Response(REPLAY_BYTES)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            self.download()
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url']),
                                                self.client.url(POLL_URL)] +
                         [self.client.url(self.record['replay_url'])] * 2)
        self.assertEqual(sleep.call_args_list, [call(5), call(1), call(5)])
        self.assertEqual(self.destination.read_bytes(), REPLAY_BYTES)
        self.assertEqual(list(self.outdir.rglob('*.part')), [])

    def test_missing_poll_job_never_recovers_without_remaining_failure_retry(self):
        for previous_failure in (False, True):
            with self.subTest(previous_failure=previous_failure):
                self.client.retries = int(previous_failure)
                self.client.opener.open.reset_mock()
                responses = [HTTPError(self.client.url(self.record['replay_url']), 503, 'busy',
                                       {'Retry-After': '0'}, BytesIO(b''))] if previous_failure else []
                self.client.opener.open.side_effect = responses + [pending_response(retry_after=0),
                    HTTPError(self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b'')),
                    Response(REPLAY_BYTES)]
                with patch.object(fetch_replays.time, 'sleep'):
                    with self.assertRaisesRegex(fetch_replays.FetchError, '404'):
                        self.download()
                self.assertEqual(self.client.opener.open.call_count, 2 + int(previous_failure))
                self.assert_no_files()

    def test_missing_poll_job_fallback_preserves_pending_deadline(self):
        self.client.retries = 1
        self.client.opener.open.side_effect = [pending_response(retry_after=40), HTTPError(
            self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b'')), pending_response(retry_after=20)]
        now = 100

        def wait(seconds):
            nonlocal now
            now += seconds

        with patch.object(fetch_replays.time, 'sleep', side_effect=wait) as sleep, \
                patch.object(fetch_replays.time, 'monotonic', side_effect=lambda: now):
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(40), call(1)])
        self.assert_no_files()

    def test_missing_poll_job_fallback_preserves_poll_cap(self):
        self.client.retries = 1

        def response(*args, **kwargs):
            if self.client.opener.open.call_count == 2:
                raise HTTPError(self.client.url(POLL_URL), 404, 'not found', {}, BytesIO(b''))
            return pending_response(retry_after=0)

        self.client.opener.open.side_effect = response
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaisesRegex(fetch_replays.FetchError, 'still pending.*retry later'):
                self.download()
        self.assertEqual(self.client.opener.open.call_count, 14)
        self.assertEqual(self.requested_urls(), [self.client.url(self.record['replay_url']),
                                                self.client.url(POLL_URL)] +
                         [self.client.url(self.record['replay_url'])] * 12)
        self.assertEqual(sleep.call_args_list, [call(0), call(1)] + [call(0)] * 11)
        self.assert_no_files()


class LadderTests(unittest.TestCase):
    def test_pages_follow_api_page_numbers_and_query(self):
        first = {'teamId': TEAM, 'name': 'Example'}
        second = {'teamId': OTHER_TEAM, 'name': 'Other'}
        client = ScriptedClient([
            ('/api/v1/ladder', {'page': 1, 'q': 'Example'}, ladder([first], pages=2)),
            ('/api/v1/ladder', {'page': 2, 'q': 'Example'}, ladder([second], page=2, pages=2)),
        ])
        self.assertEqual(list(fetch_replays.iter_ladder(client, query='Example')), [first, second])
        self.assertFalse(client.steps)

    def test_top_stops_without_fetching_next_page(self):
        first = {'teamId': TEAM, 'name': 'Example'}
        client = ScriptedClient([
            ('/api/v1/ladder', {'page': 1}, ladder([first, {'teamId': OTHER_TEAM}], pages=4)),
        ])
        self.assertEqual(list(fetch_replays.iter_ladder(client, top=1)), [first])
        self.assertEqual(len(client.calls), 1)

    def test_repeating_page_is_an_error(self):
        client = ScriptedClient([
            ('/api/v1/ladder', {'page': 1}, ladder([{'teamId': TEAM}], pages=2)),
            ('/api/v1/ladder', {'page': 2}, ladder([{'teamId': OTHER_TEAM}], pages=2)),
        ])
        with self.assertRaises(fetch_replays.FetchError):
            list(fetch_replays.iter_ladder(client))

    def test_malformed_ladder_is_an_error(self):
        for payload in ({'teams': {}, 'page': 1, 'pages': 1},
                        {'teams': [], 'page': 1, 'pages': 'many'},
                        {'teams': [], 'page': 1, 'pages': 2}):
            with self.subTest(payload=payload):
                client = ScriptedClient([('/api/v1/ladder', {}, payload)])
                with self.assertRaises(fetch_replays.FetchError):
                    list(fetch_replays.iter_ladder(client))


class TeamGroupsTests(unittest.TestCase):
    def test_pagination_uses_created_time_and_group_id_cursor(self):
        first = group()
        second = group(OTHER_GROUP, created='2026-10-13T08:00:00.000Z')
        client = ScriptedClient([
            ('/api/v1/match-groups', {'team': TEAM, 'scope': 'all'},
             {'groups': [first], 'more': True}),
            ('/api/v1/match-groups',
             {'team': TEAM, 'scope': 'all',
              'before': '2026-10-14T09:00:00.000Z_64013d4f-257c-43c5-bf6a-6b0fd598a7f4'},
             {'groups': [second], 'more': False}),
        ])
        self.assertEqual(list(fetch_replays.iter_team_groups(client, TEAM)), [first, second])
        self.assertFalse(client.steps)

    def test_max_groups_stops_without_fetching_next_page(self):
        first = group()
        client = ScriptedClient([
            ('/api/v1/match-groups', {'team': TEAM},
             {'groups': [first, group(OTHER_GROUP)], 'more': True}),
        ])
        self.assertEqual(list(fetch_replays.iter_team_groups(client, TEAM, max_groups=1)), [first])
        self.assertEqual(len(client.calls), 1)

    def test_kind_filter_is_sent_to_the_api(self):
        first = group()
        client = ScriptedClient([
            ('/api/v1/match-groups', {'team': TEAM, 'kind': 'ranked'},
             {'groups': [first], 'more': False}),
        ])
        self.assertEqual(list(fetch_replays.iter_team_groups(client, TEAM, kind='ranked')), [first])

    def test_repeating_cursor_is_an_error(self):
        client = ScriptedClient([
            ('/api/v1/match-groups', {'team': TEAM}, {'groups': [group()], 'more': True}),
            ('/api/v1/match-groups', {'team': TEAM}, {'groups': [group()], 'more': True}),
        ])
        with self.assertRaises(fetch_replays.FetchError):
            list(fetch_replays.iter_team_groups(client, TEAM))
        self.assertEqual(len(client.calls), 2)

    def test_malformed_group_page_is_an_error(self):
        for payload in ({'groups': {}, 'more': False},
                        {'groups': [], 'more': True},
                        {'groups': [{'id': GROUP}], 'more': True}):
            with self.subTest(payload=payload):
                client = ScriptedClient([('/api/v1/match-groups', {'team': TEAM}, payload)])
                with self.assertRaises(fetch_replays.FetchError):
                    list(fetch_replays.iter_team_groups(client, TEAM))


class ReplayEnumerationTests(unittest.TestCase):
    def test_groups_and_matches_are_deduplicated_and_pending_skipped(self):
        pending = match(PENDING_MATCH)
        pending['status'] = 'pending'
        pending['replayUrl'] = None
        empty = match(OTHER_MATCH)
        empty['replayUrl'] = ''
        client = ScriptedClient([
            ('/api/v1/match-groups/64013d4f-257c-43c5-bf6a-6b0fd598a7f4', {},
             group_details(matches=[match(), match(), pending, empty])),
            ('/api/v1/match-groups/8b46b3b6-f9c5-4083-8d7c-bb549b90f550', {},
             group_details(OTHER_GROUP, matches=[match(), match(OTHER_MATCH)])),
        ])
        records = list(fetch_replays.iter_replays(client, [GROUP, GROUP, OTHER_GROUP]))
        self.assertEqual([record['match_id'] for record in records], [MATCH, OTHER_MATCH])
        self.assertEqual([record['group_id'] for record in records], [GROUP, OTHER_GROUP])
        self.assertEqual(records[0]['replay_url'],
                         '/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle')
        self.assertEqual(records[0]['map'], 'collision')
        self.assertEqual(len(client.calls), 2)

    def test_malformed_match_list_is_an_error(self):
        client = ScriptedClient([
            ('/api/v1/match-groups/64013d4f-257c-43c5-bf6a-6b0fd598a7f4', {},
             {'group': group(), 'matches': {}}),
        ])
        with self.assertRaises(fetch_replays.FetchError):
            list(fetch_replays.iter_replays(client, [GROUP]))


class ReplayFileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='fetch_replay_tests_')
        self.addCleanup(directory.cleanup)
        self.outdir = Path(directory.name)

    def file(self, payload):
        path = self.outdir / 'fixture.repl26a'
        path.write_bytes(payload)
        return path

    def record(self):
        return {'group_id': GROUP, 'match_id': MATCH, 'map': 'collision',
                'replay_url': '/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle'}

    def final_path(self):
        return self.outdir / GROUP / f'{MATCH}.repl26a'

    def test_raw_replay_is_accepted(self):
        fetch_replays.validate_replay(self.file(REPLAY_BYTES))

    def test_v2_transport_header_is_accepted(self):
        replay = REPLAY_BYTES[:8] + b'\0\0\x02\0\0\x02' + REPLAY_BYTES[14:]
        for payload in (replay, archive_bytes(replay)):
            with self.subTest(bundled=payload != replay):
                fetch_replays.validate_replay(self.file(payload))

    def test_bundle_member_is_accepted(self):
        fetch_replays.validate_replay(self.file(archive_bytes()))

    def test_html_truncated_magic_and_invalid_bundle_are_rejected(self):
        for payload in (b'<html>Service unavailable</html>', b'a62per\0\0', b'a62per',
                        archive_bytes(member='other.repl26a'),
                        archive_bytes(replay=b'not a replay'), archive_bytes()[:-10]):
            with self.subTest(payload=payload[:16]):
                with self.assertRaises(fetch_replays.FetchError):
                    fetch_replays.validate_replay(self.file(payload))

    def test_header_only_or_truncated_embedded_map_magic_is_rejected(self):
        for replay in (REPLAY_HEADER, REPLAY_HEADER + b'w62pam\0',
                       REPLAY_HEADER + b'badmagic' + bytes(range(32))):
            for payload in (replay, archive_bytes(replay)):
                with self.subTest(replay=replay[:22], bundled=payload != replay):
                    with self.assertRaises(fetch_replays.FetchError):
                        fetch_replays.validate_replay(self.file(payload))

    def test_zip_crc_is_checked(self):
        payload = bytearray(archive_bytes())
        # A stored ZIP member's local header has name/extra lengths at offsets
        # 26/28. Change a payload byte while preserving its replay magic.
        name_length, extra_length = struct.unpack_from('<HH', payload, 26)
        data_offset = 30 + name_length + extra_length
        payload[data_offset + len(REPLAY_BYTES) - 1] ^= 0x01
        with self.assertRaises(fetch_replays.FetchError):
            fetch_replays.validate_replay(self.file(payload))

    def test_download_saves_bundle_unchanged_then_skips_duplicate(self):
        payload = archive_bytes()
        client = ScriptedClient(payload=payload)
        final, downloaded = fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertEqual(final, self.final_path())
        self.assertTrue(downloaded)
        self.assertEqual(final.read_bytes(), payload)
        self.assertEqual(client.downloads[0][0],
                         '/api/v1/matches/187ad51c-b387-4db0-b9f5-157354de5398/bundle')
        self.assertNotEqual(client.downloads[0][1], final)
        self.assertFalse(client.downloads[0][1].exists())
        again, downloaded = fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertEqual(again, final)
        self.assertFalse(downloaded)
        self.assertEqual(len(client.downloads), 1)

    def test_existing_raw_replay_is_never_overwritten(self):
        final = self.final_path()
        final.parent.mkdir()
        final.write_bytes(REPLAY_BYTES)
        client = ScriptedClient(payload=archive_bytes())
        result, downloaded = fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertEqual(result, final)
        self.assertFalse(downloaded)
        self.assertEqual(final.read_bytes(), REPLAY_BYTES)
        self.assertFalse(client.downloads)

    def test_invalid_existing_replay_is_rejected_without_overwrite(self):
        final = self.final_path()
        final.parent.mkdir()
        final.write_bytes(b'<html>old failed download</html>')
        client = ScriptedClient()
        with self.assertRaises(fetch_replays.FetchError):
            fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertEqual(final.read_bytes(), b'<html>old failed download</html>')
        self.assertFalse(client.downloads)

    def test_interrupted_download_leaves_no_final_or_temp_file(self):
        client = ScriptedClient(payload=REPLAY_BYTES[:12], interrupted=True)
        with self.assertRaises((fetch_replays.FetchError, OSError)):
            fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertFalse(self.final_path().exists())
        self.assertEqual([path for path in self.outdir.rglob('*') if path.is_file()], [])

    def test_invalid_download_leaves_no_final_or_temp_file(self):
        client = ScriptedClient(payload=b'<html>gateway error</html>')
        with self.assertRaises(fetch_replays.FetchError):
            fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertFalse(self.final_path().exists())
        self.assertEqual([path for path in self.outdir.rglob('*') if path.is_file()], [])

    def test_exhausted_invalid_responses_leave_no_final_or_temp_file(self):
        client = fetch_replays.Client(delay=0, retries=2)
        client.opener = Mock()
        client.opener.open.side_effect = [Response(b'<html>busy</html>') for _ in range(3)]
        with patch.object(fetch_replays.time, 'sleep') as sleep:
            with self.assertRaises(fetch_replays.FetchError):
                fetch_replays.download_replay(client, self.record(), self.outdir)
        self.assertEqual(client.opener.open.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])
        self.assertFalse(self.final_path().exists())
        self.assertEqual([path for path in self.outdir.rglob('*') if path.is_file()], [])

    def test_untrusted_ids_cannot_escape_output_directory(self):
        for field in ('group_id', 'match_id'):
            record = self.record()
            record[field] = '../outside'
            with self.subTest(field=field):
                client = ScriptedClient()
                with self.assertRaises(fetch_replays.FetchError):
                    fetch_replays.download_replay(client, record, self.outdir)
                self.assertFalse(client.downloads)


class MainTests(unittest.TestCase):
    def test_rate_limit_stops_before_selecting_another_replay(self):
        selected = []

        def records(*args):
            for match_id in (MATCH, OTHER_MATCH):
                selected.append(match_id)
                yield {'group_id': GROUP, 'match_id': match_id}

        with patch.object(fetch_replays, 'iter_replays', side_effect=records), \
                patch.object(fetch_replays, 'download_replay') as download, \
                patch.object(fetch_replays.sys, 'stdout'), \
                patch.object(fetch_replays.sys, 'stderr'):
            download.side_effect = fetch_replays.RateLimitError('retry after 120 seconds', 120)
            result = fetch_replays.main(['--match-group', GROUP])
        self.assertEqual(result, 1)
        self.assertEqual(selected, [MATCH])
        self.assertEqual(download.call_count, 1)


class MetadataTests(unittest.TestCase):
    def test_plain_fetch_preserves_derived_elo_but_explicit_unknown_replaces_it(self):
        client = fetch_replays.Client(delay=0)
        detail = group_details(matches=[match()])
        detail['matches'][0]['startedAt'] = '2026-10-14T09:00:05Z'
        basic = next(fetch_replays.replay_records(detail))
        enriched = json.loads(json.dumps(basic))
        enriched['teams'][0].update(elo_at_time=1512.5, elo_source='rated_event')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f'{MATCH}.repl26a'
            path.write_bytes(archive_bytes())
            fetch_replays.write_metadata(client, enriched, path)
            retained = fetch_replays.write_metadata(client, basic, path)
            self.assertEqual(retained['teams'][0]['elo_at_time'], 1512.5)
            self.assertEqual(retained['teams'][0]['elo_source'], 'rated_event')
            basic['teams'][0].update(elo_at_time=None, elo_source='unknown')
            replaced = fetch_replays.write_metadata(client, basic, path)
            self.assertIsNone(replaced['teams'][0]['elo_at_time'])
            self.assertEqual(replaced['teams'][0]['elo_source'], 'unknown')

    def test_known_checksum_change_preserves_previous_sidecar_and_reports_error(self):
        client = fetch_replays.Client(delay=0)
        record = next(fetch_replays.replay_records(group_details(matches=[match()])))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f'{MATCH}.repl26a'
            path.write_bytes(archive_bytes())
            first = fetch_replays.write_metadata(client, record, path)
            metadata_bytes = path.with_suffix('.json').read_bytes()
            with path.open('ab') as replay:
                replay.write(b'changed')
            with self.assertRaisesRegex(fetch_replays.FetchError, 'checksum changed'):
                fetch_replays.write_metadata(client, record, path, expected_sha256=first['sha256'])
            self.assertEqual(path.with_suffix('.json').read_bytes(), metadata_bytes)

    def test_rating_snapshots_come_from_group_and_keep_match_start_time(self):
        detail = group_details(matches=[match()])
        detail['group']['ratedAt'] = '2026-10-14T09:00:15.000Z'
        detail['group']['sides'][0].update(side='a', ratingBefore=1512.345, ratingAfter=1522.875)
        detail['matches'][0].update(startedAt='2026-10-14T09:00:05.000Z', sides=[
            {'teamId': TEAM, 'ratingBefore': 9999, 'result': 'win', 'score': 1000}])
        record = next(fetch_replays.replay_records(detail))
        self.assertEqual(record['teams'][0]['elo_before'], 1512.345)
        self.assertEqual(record['teams'][0]['elo_after'], 1522.875)
        self.assertEqual(record['started_at'], '2026-10-14T09:00:05.000Z')
        self.assertEqual(record['rated_at'], '2026-10-14T09:00:15.000Z')
        self.assertNotIn('elo_at_time', record['teams'][0])

    def test_existing_sidecar_is_enriched_without_rehashing_unchanged_file(self):
        client = fetch_replays.Client(delay=0)
        record = next(fetch_replays.replay_records(group_details(matches=[match()])))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f'{MATCH}.repl26a'
            payload = archive_bytes()
            path.write_bytes(payload)
            path.with_suffix('.json').write_text(json.dumps({
                'fetched_at': '2026-10-14T09:10:00Z', 'personal_note': 'keep me'}), encoding='utf-8')
            first = fetch_replays.write_metadata(client, record, path)
            record['teams'][0].update(elo_at_time=1512.345, elo_source='rated_event')
            with patch.object(fetch_replays.hashlib, 'file_digest', side_effect=AssertionError('unneeded rehash')):
                updated = fetch_replays.write_metadata(client, record, path)
            self.assertEqual(updated['fetched_at'], '2026-10-14T09:10:00Z')
            self.assertEqual(updated['personal_note'], 'keep me')
            self.assertEqual(updated['teams'][0]['elo_at_time'], 1512.345)
            self.assertEqual(updated['sha256'], first['sha256'])
            self.assertEqual(path.read_bytes(), payload)


if __name__ == '__main__':
    unittest.main()
