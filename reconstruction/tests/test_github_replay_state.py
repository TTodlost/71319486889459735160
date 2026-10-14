"""Trusted GitHub artifact persistence and bounded replay-sync execution."""
from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'reconstruction'))
from tools import github_replay_state as state
from tools import replay_snapshot
from tools.replay_catalog import Catalog


REPOSITORY = 'trusted-owner/cambc'
WORKFLOW = 'replay-sync.yml'
BRANCH = 'main'
WORKFLOW_ID = 51
BASE_URL = 'https://2026a.battlecode.co.uk'
ARTIFACT_PREFIX = 'battlecode-replay-state'
GROUP = '64013d4f-257c-43c5-bf6a-6b0fd598a7f4'
MATCH = '187ad51c-b387-4db0-b9f5-157354de5398'
TEAM = 'ee83f5e5-bcad-4923-86b4-b79ea6083d2e'
REPLAY_BYTES = (bytes.fromhex('6136327065720000000001000001')
                + b'w62pam\0\0' + bytes(range(1, 33)))
SIGNED_URL = 'https://artifact-storage.example/snapshot.zip?signature=private-signature'
TOKEN = 'private-test-token'


def artifact(artifact_id, *, run_id=None, created=None, **extra):
    run_id = run_id or 1000 + artifact_id
    result = {
        'id': artifact_id, 'name': f'{ARTIFACT_PREFIX}-{run_id}-1', 'expired': False,
        'created_at': created or f'2026-10-14T10:{artifact_id:02d}:00Z',
        'size_in_bytes': 1, 'digest': 'sha256:' + 'a' * 64,
        'archive_download_url': f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip',
        'workflow_run': {'id': run_id, 'head_branch': BRANCH},
    }
    result.update(extra)
    return result


def run(run_id, **extra):
    result = {
        'id': run_id, 'workflow_id': WORKFLOW_ID, 'event': 'schedule',
        'head_branch': BRANCH, 'head_repository': {'full_name': REPOSITORY},
        'run_attempt': 1,
    }
    result.update(extra)
    return result


def artifact_zip(snapshot_bytes, *, members=None):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, payload in members or [('replay-state.tar.gz', snapshot_bytes)]:
            archive.writestr(name, payload)
    return buffer.getvalue()


class FakeApi:
    def __init__(self, artifacts=(), *, pages=None, runs=None, snapshots=None):
        self.artifacts = list(artifacts)
        self.pages = list(pages) if pages is not None else [self.artifacts]
        all_artifacts = [item for page in self.pages for item in page]
        self.runs = {item['workflow_run']['id']: run(item['workflow_run']['id'])
                     for item in all_artifacts}
        self.runs.update(runs or {})
        self.snapshots = dict(snapshots or {})
        self.requests = []
        self.downloads = []
        self.deleted = []

    def json(self, method, path):
        self.requests.append((method, path))
        parsed = urlsplit(path)
        base = '/repos/trusted-owner/cambc/actions'
        if method == 'GET' and parsed.path == base + '/workflows/replay-sync.yml':
            return {'id': WORKFLOW_ID}
        if method == 'GET' and parsed.path == base + '/artifacts':
            query = parse_qs(parsed.query)
            if query.get('per_page') != ['100']:
                raise AssertionError(f'Expected artifact page size 100: {path}')
            page_number = int(query.get('page', ['1'])[0])
            rows = self.pages[page_number - 1] if page_number <= len(self.pages) else []
            return {'total_count': sum(map(len, self.pages)), 'artifacts': deepcopy(rows)}
        if method == 'GET' and parsed.path.startswith(base + '/runs/'):
            run_id = int(parsed.path.rsplit('/', 1)[-1])
            if run_id not in self.runs:
                raise AssertionError(f'Unexpected workflow run request: {path}')
            return deepcopy(self.runs[run_id])
        if method == 'DELETE' and parsed.path.startswith(base + '/artifacts/'):
            self.deleted.append(int(parsed.path.rsplit('/', 1)[-1]))
            return None
        raise AssertionError(f'Unexpected GitHub request: {method} {path}')

    def download_artifact(self, repository, item, destination):
        if repository != REPOSITORY:
            raise AssertionError(f'Unexpected repository: {repository}')
        self.downloads.append(item['id'])
        if item['id'] not in self.snapshots:
            raise AssertionError(f'Unexpected artifact download: {item["id"]}')
        Path(destination).write_bytes(self.snapshots[item['id']])


class HttpResponse(BytesIO):
    def __init__(self, payload, *, status=200, headers=None, url=None):
        super().__init__(payload)
        self.status = status
        self.headers = dict(headers or {})
        self.url = url

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url


class TrustedArtifactTests(unittest.TestCase):
    def test_only_expected_workflow_branch_repository_and_events_are_trusted(self):
        newest, oldest = artifact(8), artifact(1)
        wrong_workflow, fork, wrong_branch = artifact(2), artifact(3), artifact(4)
        pull_request, expired, wrong_name = artifact(5), artifact(6, expired=True), artifact(7, name='unrelated')
        api = FakeApi([oldest, wrong_workflow, fork, wrong_branch, pull_request, expired, wrong_name, newest],
                      runs={1002: run(1002, workflow_id=99),
                            1003: run(1003, head_repository={'full_name': 'untrusted-fork/cambc'}),
                            1004: run(1004, head_branch='feature'),
                            1005: run(1005, event='pull_request')})
        trusted = state.trusted_artifacts(api, REPOSITORY, WORKFLOW, BRANCH)
        self.assertEqual([item['id'] for item in trusted], [8, 1])
        self.assertFalse(api.deleted)

    def test_artifact_listing_is_paginated_before_newest_is_selected(self):
        oldest, newest = artifact(1), artifact(2)
        unrelated = [artifact(3, run_id=2000 + n, name=f'unrelated-{n}') for n in range(99)]
        api = FakeApi(pages=[[oldest, *unrelated], [newest]])
        trusted = state.trusted_artifacts(api, REPOSITORY, WORKFLOW, BRANCH)
        self.assertEqual([item['id'] for item in trusted], [2, 1])
        listing_requests = [path for method, path in api.requests if urlsplit(path).path.endswith('/artifacts')]
        self.assertEqual([parse_qs(urlsplit(path).query).get('page', ['1']) for path in listing_requests],
                         [['1'], ['2']])

    def test_artifact_name_must_identify_its_actual_workflow_run(self):
        wrong = artifact(1, name=f'{ARTIFACT_PREFIX}-9999-1')
        api = FakeApi([wrong])
        self.assertEqual(state.trusted_artifacts(api, REPOSITORY, WORKFLOW, BRANCH), [])


class RestoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='github_replay_state_tests_')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.outdir = self.root / 'restored'
        self.snapshot = self.root / 'downloaded.tar.gz'

    def packed_snapshot(self):
        source = self.root / 'source'
        source.mkdir()
        path = source / GROUP / f'{MATCH}.repl26a'
        path.parent.mkdir()
        path.write_bytes(REPLAY_BYTES)
        with Catalog(source, BASE_URL) as catalog:
            catalog.store_detail({
                'group': {'id': GROUP, 'kind': 'unranked', 'status': 'finished',
                          'createdAt': '2026-10-14T09:00:00Z', 'finishedAt': '2026-10-14T09:00:30Z',
                          'ratedAt': None, 'sides': [{'teamId': TEAM, 'teamName': 'Archived Team',
                                                    'ratingBefore': None, 'ratingAfter': None}]},
                'matches': [{'id': MATCH, 'status': 'finished', 'map': 'collision', 'position': 1,
                             'startedAt': '2026-10-14T09:00:10Z',
                             'replayUrl': f'/api/v1/matches/{MATCH}/bundle'}],
            })
            catalog.mark_downloaded(MATCH, path, hashlib.sha256(REPLAY_BYTES).hexdigest(), len(REPLAY_BYTES))
            catalog.set_meta('history_complete', True)
        packed = self.root / 'seed.tar.gz'
        replay_snapshot.pack_snapshot(source, packed)
        return packed

    def test_latest_trusted_artifact_restores_real_catalog_and_replay(self):
        packed = self.packed_snapshot()
        api = FakeApi([artifact(1), artifact(2)], snapshots={2: packed.read_bytes()})
        result = state.restore_state(api, REPOSITORY, WORKFLOW, BRANCH, self.outdir, self.snapshot)
        self.assertIsInstance(result, dict)
        self.assertEqual(api.downloads, [2])
        self.assertEqual((self.outdir / GROUP / f'{MATCH}.repl26a').read_bytes(), REPLAY_BYTES)
        with Catalog(self.outdir, BASE_URL) as restored:
            records = list(restored.replay_records())
            self.assertEqual([item['match_id'] for item in records], [MATCH])
            self.assertTrue(records[0]['downloaded'])
            self.assertTrue(restored.get_meta('history_complete'))

    def test_corrupt_latest_artifact_fails_closed_without_falling_back_to_older_backup(self):
        packed = self.packed_snapshot()
        api = FakeApi([artifact(1), artifact(2)],
                      snapshots={1: packed.read_bytes(), 2: packed.read_bytes()[:20]})
        with self.assertRaises((state.StateError, replay_snapshot.SnapshotError)):
            state.restore_state(api, REPOSITORY, WORKFLOW, BRANCH, self.outdir, self.snapshot,
                                bootstrap=True, seed=packed)
        self.assertEqual(api.downloads, [2])
        self.assertFalse((self.outdir / 'catalog.sqlite3').exists())
        self.assertFalse(list(self.outdir.glob('*/*.repl26a')))

    def test_missing_backup_requires_explicit_first_bootstrap(self):
        api = FakeApi()
        with self.assertRaises(state.StateError):
            state.restore_state(api, REPOSITORY, WORKFLOW, BRANCH, self.outdir, self.snapshot)
        self.assertFalse((self.outdir / 'catalog.sqlite3').exists())
        self.assertFalse(api.downloads)

    def test_explicit_first_bootstrap_creates_an_empty_archive_directory(self):
        result = state.restore_state(FakeApi(), REPOSITORY, WORKFLOW, BRANCH,
                                     self.outdir, self.snapshot, bootstrap=True)
        self.assertIsInstance(result, dict)
        self.assertTrue(self.outdir.is_dir())
        self.assertEqual(list(self.outdir.iterdir()), [])

    def test_bootstrap_does_not_silently_reuse_nonempty_unverified_output(self):
        self.outdir.mkdir()
        old = self.outdir / 'old-state.txt'
        old.write_text('preserve existing state', encoding='utf-8')
        with self.assertRaises(state.StateError):
            state.restore_state(FakeApi(), REPOSITORY, WORKFLOW, BRANCH,
                                self.outdir, self.snapshot, bootstrap=True)
        self.assertEqual(old.read_text(encoding='utf-8'), 'preserve existing state')

    def test_explicit_seed_restores_real_snapshot_when_bootstrapping(self):
        packed = self.packed_snapshot()
        api = FakeApi()
        state.restore_state(api, REPOSITORY, WORKFLOW, BRANCH, self.outdir, self.snapshot,
                            bootstrap=True, seed=packed)
        self.assertEqual((self.outdir / GROUP / f'{MATCH}.repl26a').read_bytes(), REPLAY_BYTES)
        self.assertFalse(api.downloads)

    def test_missing_explicit_seed_does_not_fall_back_to_an_empty_bootstrap(self):
        with self.assertRaises(state.StateError):
            state.restore_state(FakeApi(), REPOSITORY, WORKFLOW, BRANCH,
                                self.outdir, self.snapshot, bootstrap=True, seed=self.root / 'missing.tar.gz')
        self.assertFalse((self.outdir / 'catalog.sqlite3').exists())


class PruneTests(unittest.TestCase):
    def test_pruning_retains_current_and_previous_trusted_snapshot(self):
        api = FakeApi([artifact(1), artifact(2), artifact(3), artifact(4), artifact(5, name='other-workflow')])
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '1004'}):
            result = state.prune_state(api, REPOSITORY, WORKFLOW, BRANCH, uploaded_id=4)
        self.assertIsInstance(result, dict)
        self.assertEqual(set(api.deleted), {1, 2})
        self.assertNotIn(3, api.deleted)
        self.assertNotIn(4, api.deleted)
        self.assertNotIn(5, api.deleted)

    def test_retention_less_than_two_is_rejected_before_deletion(self):
        api = FakeApi([artifact(1), artifact(2), artifact(3)])
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '1003'}):
            with self.assertRaises(state.StateError):
                state.prune_state(api, REPOSITORY, WORKFLOW, BRANCH, uploaded_id=3, keep=1)
        self.assertFalse(api.deleted)

    def test_missing_or_untrusted_uploaded_artifact_never_authorizes_pruning(self):
        for uploaded_id in (999, 3):
            with self.subTest(uploaded_id=uploaded_id):
                api = FakeApi([artifact(1), artifact(2), artifact(3, expired=True)])
                with patch.dict(os.environ, {'GITHUB_RUN_ID': '1003'}):
                    with self.assertRaises(state.StateError):
                        state.prune_state(api, REPOSITORY, WORKFLOW, BRANCH, uploaded_id=uploaded_id)
                self.assertFalse(api.deleted)

    def test_older_uploaded_artifact_cannot_prune_a_newer_snapshot(self):
        api = FakeApi([artifact(1), artifact(2), artifact(3)])
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '1002'}):
            with self.assertRaises(state.StateError):
                state.prune_state(api, REPOSITORY, WORKFLOW, BRANCH, uploaded_id=2)
        self.assertFalse(api.deleted)

    def test_uploaded_artifact_must_belong_to_current_github_run(self):
        api = FakeApi([artifact(1), artifact(2), artifact(3)])
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '9999'}):
            with self.assertRaises(state.StateError):
                state.prune_state(api, REPOSITORY, WORKFLOW, BRANCH, uploaded_id=3)
        self.assertFalse(api.deleted)


class GitHubApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='github_artifact_transport_tests_')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.destination = self.root / 'replay-state.tar.gz'

    def client(self):
        api = state.GitHubApi(TOKEN)
        api.opener = Mock()
        return api

    def download_response(self, api, payload):
        api_url = 'https://api.github.com/repos/trusted-owner/cambc/actions/artifacts/2/zip'
        redirect = HTTPError(api_url, 302, 'Found', {'Location': SIGNED_URL}, BytesIO())
        api.opener.open.side_effect = [redirect, HttpResponse(
            payload, headers={'Content-Length': str(len(payload))}, url=SIGNED_URL)]

    def zipped_artifact(self, payload):
        return artifact(2, size_in_bytes=len(payload), digest='sha256:' + hashlib.sha256(payload).hexdigest())

    def test_json_authentication_is_sent_to_api_and_204_returns_none(self):
        api = self.client()
        api.opener.open.return_value = HttpResponse(b'{"id":51}', url='https://api.github.com')
        self.assertEqual(api.json('GET', '/repos/trusted-owner/cambc/actions/workflows/replay-sync.yml'),
                         {'id': 51})
        request = api.opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         'https://api.github.com/repos/trusted-owner/cambc/actions/workflows/replay-sync.yml')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
        api.opener.open.return_value = HttpResponse(b'', status=204, url='https://api.github.com')
        self.assertIsNone(api.json('DELETE', '/repos/trusted-owner/cambc/actions/artifacts/1'))

    def test_signed_artifact_redirect_uses_a_fresh_request_without_authentication(self):
        api = self.client()
        snapshot = b'opaque snapshot bytes; tar validation belongs to restore'
        payload = artifact_zip(snapshot)
        self.download_response(api, payload)
        api.download_artifact(REPOSITORY, self.zipped_artifact(payload), self.destination)
        self.assertEqual(self.destination.read_bytes(), snapshot)
        self.assertEqual(api.opener.open.call_count, 2)
        first, signed = [call.args[0] for call in api.opener.open.call_args_list]
        self.assertEqual(first.full_url,
                         'https://api.github.com/repos/trusted-owner/cambc/actions/artifacts/2/zip')
        self.assertEqual(first.get_header('Authorization'), 'Bearer ' + TOKEN)
        self.assertEqual(signed.full_url, SIGNED_URL)
        self.assertIsNone(signed.get_header('Authorization'))
        self.assertIsNone(signed.get_header('Cookie'))

    def test_non_https_artifact_redirect_is_rejected_before_unsigned_request(self):
        api = self.client()
        api.opener.open.side_effect = HTTPError(
            'https://api.github.com/repos/trusted-owner/cambc/actions/artifacts/2/zip', 302, 'Found',
            {'Location': 'http://artifact-storage.example/state.zip'}, BytesIO())
        with self.assertRaises(state.StateError):
            api.download_artifact(REPOSITORY, artifact(2), self.destination)
        self.assertEqual(api.opener.open.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_artifact_zip_digest_mismatch_does_not_publish_snapshot(self):
        api = self.client()
        payload = artifact_zip(b'snapshot')
        self.download_response(api, payload)
        item = self.zipped_artifact(payload)
        item['digest'] = 'sha256:' + '0' * 64
        with self.assertRaises(state.StateError):
            api.download_artifact(REPOSITORY, item, self.destination)
        self.assertFalse(self.destination.exists())

    def test_artifact_zip_must_have_exactly_one_expected_regular_member(self):
        bad_members = [
            [('unrelated.tar.gz', b'snapshot')],
            [('../replay-state.tar.gz', b'snapshot')],
            [('replay-state.tar.gz', b'snapshot'), ('other.txt', b'extra')],
        ]
        for members in bad_members:
            with self.subTest(members=[name for name, _ in members]):
                api = self.client()
                payload = artifact_zip(b'', members=members)
                self.download_response(api, payload)
                with self.assertRaises(state.StateError):
                    api.download_artifact(REPOSITORY, self.zipped_artifact(payload), self.destination)
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.root.parent / 'replay-state.tar.gz').exists())

    def test_artifact_zip_crc_is_verified_even_when_transport_digest_matches(self):
        api = self.client()
        snapshot = b'preserve the ZIP member size while changing its payload'
        payload = bytearray(artifact_zip(snapshot))
        name_length, extra_length = struct.unpack_from('<HH', payload, 26)
        payload[30 + name_length + extra_length + len(snapshot) - 1] ^= 1
        payload = bytes(payload)
        self.download_response(api, payload)
        with self.assertRaises(state.StateError):
            api.download_artifact(REPOSITORY, self.zipped_artifact(payload), self.destination)
        self.assertFalse(self.destination.exists())

    def test_artifact_zip_cannot_use_a_symlink_as_the_expected_member(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            member = zipfile.ZipInfo('replay-state.tar.gz')
            member.create_system = 3
            member.external_attr = 0o120777 << 16
            archive.writestr(member, '../outside.tar.gz')
        payload = buffer.getvalue()
        api = self.client()
        self.download_response(api, payload)
        with self.assertRaises(state.StateError):
            api.download_artifact(REPOSITORY, self.zipped_artifact(payload), self.destination)
        self.assertFalse(self.destination.exists())

    def test_signed_download_error_messages_do_not_expose_token_or_signed_query(self):
        api = self.client()
        error_text = f'failed {SIGNED_URL}; Authorization: Bearer {TOKEN}'

        def request(request, **kwargs):
            if request.full_url == SIGNED_URL:
                self.assertIsNone(request.get_header('Authorization'))
                raise URLError(error_text)
            raise HTTPError(request.full_url, 302, 'Found', {'Location': SIGNED_URL}, BytesIO())

        api.opener.open.side_effect = request
        errors = StringIO()
        with patch.object(state.time, 'sleep'), redirect_stderr(errors):
            with self.assertRaises(state.StateError) as raised:
                api.download_artifact(REPOSITORY, artifact(2), self.destination)
        reported = str(raised.exception) + errors.getvalue()
        self.assertNotIn(TOKEN, reported)
        self.assertNotIn('private-signature', reported)
        self.assertNotIn(SIGNED_URL, reported)
        self.assertFalse(self.destination.exists())


class DeadlineTests(unittest.TestCase):
    def test_normal_child_exit_preserves_status_and_configured_work_budgets(self):
        child = Mock()
        child.wait.return_value = 3
        child.poll.return_value = 3
        with patch.object(state.subprocess, 'Popen', return_value=child) as launch:
            result = state.run_sync(Path('archive'), seconds=17, max_pages=4, max_details=5, max_downloads=6)
        self.assertEqual(result, 3)
        child.wait.assert_called_once_with(timeout=17)
        command = launch.call_args.args[0]
        for flag, value in (('--max-pages', '4'), ('--max-details', '5'), ('--max-downloads', '6')):
            self.assertEqual(command[command.index(flag) + 1], value)
        child.send_signal.assert_not_called()
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_budget_interrupt_allows_child_to_save_checkpoint_and_exit(self):
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired('sync', 17), 130]
        child.poll.return_value = 130
        with patch.object(state.subprocess, 'Popen', return_value=child):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = state.run_sync(Path('archive'), seconds=17)
        self.assertEqual(result, 0)
        child.send_signal.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_unexpected_child_failure_after_interrupt_is_preserved(self):
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired('sync', 17), 7]
        child.poll.return_value = 7
        with patch.object(state.subprocess, 'Popen', return_value=child):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = state.run_sync(Path('archive'), seconds=17)
        self.assertEqual(result, 7)
        child.send_signal.assert_called_once()
        child.kill.assert_not_called()

    def test_unresponsive_child_is_terminated_killed_and_reaped_before_return(self):
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired('sync', 17),
                                  subprocess.TimeoutExpired('sync', 20),
                                  subprocess.TimeoutExpired('sync', 5), -9]
        child.poll.return_value = -9
        with patch.object(state.subprocess, 'Popen', return_value=child):
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                result = state.run_sync(Path('archive'), seconds=17)
        self.assertEqual(result, 124)
        child.send_signal.assert_called_once()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 4)
        names = [call[0] for call in child.mock_calls]
        self.assertLess(names.index('terminate'), names.index('kill'))
        reaped_at = max(index for index, name in enumerate(names) if name == 'wait')
        self.assertGreater(reaped_at, names.index('kill'),
                           'The killed child must be reaped before run_sync returns')


if __name__ == '__main__':
    unittest.main()
