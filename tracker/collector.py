"""Collect current leaderboard replays and build verified portable dashboard data."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

from replay_extract import decode, ITEMS, CHAMPIONS, ITEM_DESIGNS, CONSUMABLE_DESIGNS
from rankings import RatingIndex

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
sys.path.insert(0, str(WORKSPACE / 'reconstruction/tools'))
from fetch_replays import Client, RequestPacer, iter_ladder, download_replay, write_metadata
from replay_catalog import Catalog
from github_replay_state import GitHubApi, trusted_artifacts
from replay_snapshot import restore_snapshot

REPOSITORY = 'TTodlost/71319486889459735160'
CACHE = ROOT / '.cache'
DATA = ROOT / 'data.json'
STATE = ROOT / '.state.json'
COLLECTION_TOP = 20
SERIES_PER_TEAM = 25
WINDOW_HOURS = 24


def utc_text(instant):
    return instant.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def recent_groups(client, team_id, start, end, limit=SERIES_PER_TEAM):
    """Read a fixed head snapshot per team, independently of archive backfill."""
    params = {'scope': 'all', 'kind': 'ranked', 'team': team_id,
              'before': end + '_ffffffff-ffff-ffff-ffff-ffffffffffff'}
    groups = {}
    cursors = set()
    while True:
        response = client.get_json('/api/v1/match-groups', params)
        page = response.get('groups')
        if not isinstance(page, list) or type(response.get('more')) is not bool:
            raise ValueError('Invalid recent team-history response.')
        for group in page:
            created = group['createdAt']
            if start <= created < end and group.get('kind') == 'ranked' and group.get('status') == 'finished' and group.get('ratedAt'):
                groups[group['id']] = group
                if len(groups) == limit:
                    return list(groups.values())
        if not response['more'] or (page and page[-1]['createdAt'] < start):
            return list(groups.values())
        if not page:
            raise ValueError('Recent team-history pagination made no progress.')
        cursor = page[-1]['createdAt'] + '_' + page[-1]['id']
        if cursor in cursors:
            raise ValueError('Recent team-history pagination repeated a cursor.')
        cursors.add(cursor)
        params['before'] = cursor


def collect_current(progress=print, now=None):
    """Fetch current leaders' recent series before considering any old archive."""
    pacer = RequestPacer()
    client = Client(timeout=20, retries=3, pacer=pacer)
    ladder = [{'teamId': t['teamId'], 'name': t['name'], 'rank': t['rank'], 'rating': t.get('rating')}
              for t in iter_ladder(client) if type(t.get('rank')) is int and t['rank'] >= 1]
    leaders = [t for t in ladder if t['rank'] <= COLLECTION_TOP]
    if not leaders or len({t['teamId'] for t in ladder}) != len(ladder):
        raise ValueError('Current leaderboard is empty or has duplicate teams.')
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(hours=WINDOW_HOURS)
    start_text, end_text = utc_text(start), utc_text(end)
    groups = {}
    counts = {}
    for team in sorted(leaders, key=lambda t: t['rank']):
        selected = recent_groups(client, team['teamId'], start_text, end_text)
        counts[team['teamId']] = len(selected)
        groups.update((g['id'], g) for g in selected)
    progress(f'Current top {COLLECTION_TOP}: {len(groups)} recent completed series found.')
    archive = CACHE / 'current' / 'archive'
    match_ids = set()
    with Catalog(archive, client.base_url) as catalog:
        for group in groups.values():
            catalog.upsert_group(group)
            row = catalog.db.execute('select detail_json,detail_needed from groups where id=?', (group['id'],)).fetchone()
            if row['detail_needed'] or not row['detail_json']:
                detail = client.get_json('/api/v1/match-groups/' + group['id'])
                if detail.get('group', {}).get('id') != group['id']:
                    raise ValueError('Wrong recent series detail response.')
                catalog.store_detail(detail)
            else:
                detail = json.loads(row['detail_json'])
            for match in detail['matches']:
                if (match.get('startedAt') and start_text <= match['startedAt'] < end_text
                        and (match.get('statistics') or {}).get('outcome') in ('teamA', 'teamB')
                        and match.get('replayUrl')):
                    match_ids.add(match['id'])
        records = {r['match_id']: r for r in catalog.replay_records() if r['match_id'] in match_ids}
        if len(records) != len(match_ids) or not records:
            raise ValueError('No complete recent replay records were found.')

        def download(record):
            c = Client(timeout=20, retries=3, pacer=pacer)
            path, _ = download_replay(c, record, archive)
            public_record = {k: v for k, v in record.items() if k not in ('file_path', 'downloaded', 'error', 'sha256', 'byte_size')}
            metadata = write_metadata(c, public_record, path, expected_sha256=record.get('sha256'))
            return record['match_id'], path, metadata

        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(download, r) for r in records.values()]
            try:
                for number, task in enumerate(as_completed(tasks), 1):
                    mid, path, metadata = task.result()
                    catalog.mark_downloaded(mid, path, metadata['sha256'], metadata['bytes'])
                    if number % 200 == 0:
                        progress(f'Recent replays downloaded or checked: {number}/{len(records)}.')
            except BaseException:
                for task in tasks:
                    task.cancel()
                raise
    metadata = {'leaderboardUpdatedAt': end_text, 'sampleStartAt': start_text, 'sampleEndAt': end_text,
                'sampleWindowHours': WINDOW_HOURS, 'collectionTop': COLLECTION_TOP, 'seriesPerTeam': SERIES_PER_TEAM,
                'collection': {'teamsChecked': len(leaders), 'series': len(groups), 'seriesByTeam': counts},
                'sourceApi': client.base_url + '/api/v1/ladder'}
    return archive, match_ids, ladder, metadata


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


def github_token():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token
    process = subprocess.run(['git', 'credential', 'fill'],
        input='protocol=https\nhost=github.com\n\n', text=True, capture_output=True, timeout=30,
        env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never'))
    credentials = dict(line.split('=', 1) for line in process.stdout.splitlines() if '=' in line)
    token = credentials.get('password')
    if not token:
        raise ValueError('GitHub artifact access needs existing Git credentials, GH_TOKEN, or GITHUB_TOKEN.')
    return token


def latest_snapshot(progress=print):
    api = GitHubApi(github_token())
    artifacts = trusted_artifacts(api, REPOSITORY, 'replay-sync.yml', 'main')
    if not artifacts:
        raise ValueError('No verified live replay artifact is available.')
    artifact = artifacts[0]
    current = read_json(STATE, {})
    if current.get('artifactId') == artifact['id'] and DATA.exists():
        progress('Latest artifact is already indexed.')
        return None, artifact
    folder = CACHE / 'snapshots' / str(artifact['id'])
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = folder / 'replay-state.tar.gz'
    receipt_path = folder / 'restore.json'
    receipt = read_json(receipt_path, {})
    restored_name = receipt.get('directory', '')
    archive = folder / restored_name
    restored = (restored_name.startswith('archive-') and len(restored_name) == 40
                and archive.resolve().parent == folder.resolve()
                and (archive / 'catalog.sqlite3').is_file())
    if not restored:
        if not snapshot.exists():
            progress(f'Downloading verified artifact {artifact["id"]}.')
            api.download_artifact(REPOSITORY, artifact, snapshot)
        progress('Verifying and restoring replay snapshot.')
        # Only publish a receipt after full verification. An interrupted
        # restore may leave a partial directory, which a retry must not reuse.
        archive = folder / ('archive-' + uuid.uuid4().hex)
        restore_snapshot(snapshot, archive)
        atomic_json(receipt_path, {'directory': archive.name, 'artifactId': artifact['id']})
    return archive, artifact


def fetch_histories(archive, start, end, progress=print):
    pacer = RequestPacer()
    client = Client(timeout=20, retries=3, pacer=pacer)
    ladder = list(iter_ladder(client))
    db = sqlite3.connect(f'file:{(archive / "catalog.sqlite3").resolve().as_posix()}?mode=ro', uri=True)
    try:
        ids = {t['teamId'] for t in ladder}
        ids.update(row[0] for row in db.execute('select distinct team_id from rating_events'))
    finally:
        db.close()
    before_time = datetime.fromisoformat(end.replace('Z', '+00:00')).astimezone(timezone.utc) + timedelta(milliseconds=1)
    before = before_time.isoformat(timespec='milliseconds').replace('+00:00', 'Z') + '_ffffffff-ffff-ffff-ffff-ffffffffffff'
    directory = archive.parent / 'histories'
    directory.mkdir(exist_ok=True)
    atomic_json(archive.parent / 'ladder.json', ladder)

    def fetch(tid):
        path = directory / (tid + '.json')
        # Histories are evidence snapshots, not immutable per-artifact cache.
        # A prior attempt may have been rejected for pending or missing events;
        # always re-query them until the complete dataset has been published.
        c = Client(timeout=20, retries=3, pacer=pacer)
        params = {'scope': 'all', 'kind': 'ranked', 'team': tid, 'before': before}
        groups = {}
        seen_cursors = set()
        while True:
            response = c.get_json('/api/v1/match-groups', params)
            page = response['groups']
            for g in page:
                groups[g['id']] = g
            if not response['more'] or (page and page[-1]['createdAt'] < start):
                break
            if not page:
                raise ValueError(f'Team history pagination did not progress for {tid}.')
            cursor = page[-1]['createdAt'] + '_' + page[-1]['id']
            if cursor in seen_cursors:
                raise ValueError(f'Team history cursor repeated for {tid}.')
            seen_cursors.add(cursor)
            params['before'] = cursor
        value = {'groups': sorted(groups.values(), key=lambda g: g['createdAt'], reverse=True),
                 'more': response['more'], 'request_before': before, 'request_start': start}
        atomic_json(path, value)
        return value

    completed = 0
    fetched = set()
    while ids:
        pending = sorted(ids - fetched)
        ids = set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = {pool.submit(fetch, tid): tid for tid in pending}
            try:
                for result in as_completed(tasks):
                    value = result.result()
                    fetched.add(tasks[result])
                    completed += 1
                    for g in value['groups']:
                        ids.update(s['teamId'] for s in g['sides'] if s['teamId'] not in fetched)
                    if completed % 50 == 0:
                        progress(f'Historical ratings: {completed} teams checked.')
            except BaseException:
                for task in tasks:
                    task.cancel()
                raise
        ids -= fetched
    return directory


def archive_matches(archive):
    db = sqlite3.connect(f'file:{(archive / "catalog.sqlite3").resolve().as_posix()}?mode=ro', uri=True)
    try:
        details = [json.loads(detail) for detail, in db.execute('select detail_json from groups where detail_json is not null')]
        official = {m['id']: m for detail in details for m in detail['matches']}
        series_sizes = {detail['group']['id']: len(detail['matches']) for detail in details}
        rows = list(db.execute('select match_id,file_path,record_json,sha256 from replays where downloaded=1 order by match_id'))
    finally:
        db.close()
    if not rows:
        raise ValueError('The archive contains no downloaded replays.')
    return official, rows, series_sizes


def series_elo_delta(team, kind):
    if kind != 'ranked':
        return None
    before, after = team.get('elo_before'), team.get('elo_after')
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (before, after)):
        return None
    return after - before


def build_data(archive, history_dir, artifact, progress=print, *, match_ids=None, leaderboard=None, collection_meta=None):
    archive = Path(archive).resolve()
    official, rows, series_sizes = archive_matches(archive)
    if match_ids is not None:
        rows = [row for row in rows if row[0] in match_ids]
        if len(rows) != len(match_ids) or not rows:
            raise ValueError('Recent replay collection is incomplete.')
    records = [json.loads(row[2]) for row in rows]
    start = min(r['started_at'] for r in records)
    end = max(r['started_at'] for r in records)
    rating_index = RatingIndex(Path(history_dir), start, end) if history_dir is not None else None
    decoded_cache = CACHE / 'decoded'
    decoded_cache.mkdir(parents=True, exist_ok=True)
    games = []
    purchases_checked = champions_checked = 0
    for number, (mid, relative, raw, checksum) in enumerate(rows, 1):
        record = json.loads(raw)
        match = official[mid]
        stats = match.get('statistics')
        if not stats or stats['outcome'] not in ('teamA', 'teamB'):
            raise ValueError(f'Completed replay lacks usable statistics: {mid}.')
        path = archive / relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError(f'Replay checksum mismatch: {mid}.')
        cached = read_json(decoded_cache / (checksum + '.json'))
        # Cache includes a fingerprint of the independent official statistics.
        stat_hash = hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()
        if cached is None or cached.get('statHash') != stat_hash:
            purchases, sales, errors = decode(path, match)
            if errors:
                raise ValueError(f'Replay does not match official statistics: {mid}: {errors[:1]}')
            cached = {'statHash': stat_hash, 'purchases': purchases, 'sales': sales}
            atomic_json(decoded_cache / (checksum + '.json'), cached)
        purchases = cached['purchases']
        purchases_checked += len(purchases)
        ranks = rating_index.at(record['started_at']) if rating_index is not None else {}
        final_round = stats['round']
        late_start = final_round - math.ceil(final_round / 10) + 1
        winner = 0 if stats['outcome'] == 'teamA' else 1
        teams = []
        for team_number in (0, 1):
            side = 'a' if team_number == 0 else 'b'
            team = next(t for t in record['teams'] if t['side'] == side)
            assert (team['result'] == 'win') == (winner == team_number)
            rating = ranks.get(team['id'], {'elo': None, 'rank': None, 'rankMin': None, 'rankMax': None, 'verified': False})
            champions = []
            for c in stats['champions']:
                if c['team'] != team_number:
                    continue
                cp = [p for p in purchases if p['champion'] == c['champion']]
                direct = [0] * 42
                late = [0] * 42
                late_cons = [0] * 4
                late_gold = 0
                for p in cp:
                    if p['category'] == 'item':
                        direct[p['kind']] += p['count']
                    if p['round'] >= late_start:
                        late_gold += p['gold']
                        (late if p['category'] == 'item' else late_cons)[p['kind']] += p['count']
                champions.append({'handle': c['champion'], 'type': c['type'], 'name': CHAMPIONS[c['type']],
                    'items': c['items'], 'itemsBought': c['itemsBought'], 'rawItemBuys': direct,
                    'lateItemBuys': late, 'consumablesBought': c['consumablesBought'],
                    'lateConsumableBuys': late_cons, 'spentGold': c['spentGold'], 'lateGold': late_gold,
                    'gold': c['gold'], 'lastBuys': cp[-5:]})
            champions_checked += len(champions)
            teams.append({'side': side, 'id': team['id'], 'name': team['name'], 'win': winner == team_number,
                          'eloBefore': team.get('elo_before'), 'eloAfter': team.get('elo_after'),
                          'eloDelta': series_elo_delta(team, record['kind']),
                          **rating, 'champions': champions})
        games.append({'id': mid, 'groupId': record['group_id'],
            'url': f'https://2026a.battlecode.co.uk/game/match-groups/{record["group_id"]}',
            'map': record['map'], 'kind': record['kind'], 'startedAt': record['started_at'],
            'rounds': final_round, 'lateStartRound': late_start, 'seriesMaps': series_sizes[record['group_id']],
            'botFaults': stats.get('botFaults', []), 'teams': teams})
        if number % 200 == 0:
            progress(f'Replays verified: {number}/{len(rows)}.')
    data = {'meta': {'artifactId': artifact.get('id'), 'artifactCreatedAt': artifact.get('created_at'),
        'updatedAt': datetime.now(timezone.utc).isoformat(), 'archiveGames': len(games),
        'rankUniverse': len(leaderboard or []) if rating_index is None else len(rating_index.at(start)),
        'ratingAudit': rating_index.summary if rating_index is not None else None,
        'matchStartAt': start, 'matchEndAt': end, **(collection_meta or {}),
        'validation': {'files': len(games), 'purchaseActions': purchases_checked, 'championRecords': champions_checked},
        'sourceRepository': 'https://github.com/' + REPOSITORY,
        'rankMethod': 'Membership uses the current public leaderboard snapshot, not ranks at match time. Only teams whose current rank is within the selected top N contribute observations.',
        'lateMethod': 'Final ceil(rounds/10) rounds. Buy rate is the share of champion appearances with a direct purchase; automatic emblem combines are not additional purchases.',
        'eloMethod': 'Actual team Elo change for a ranked series divided by its total maps, summed over selected champion appearances. All five maps count a series once for a hero present throughout. Heroes share team Elo movement; this is an association, not individual causal credit.',
        'sampleNote': f'Latest {SERIES_PER_TEAM} completed ranked series per current top {COLLECTION_TOP} team within {WINDOW_HOURS} hours, deduplicated. Only completed maps with usable replay statistics are included. This is a capped sample, not every game in that window. Maps from the same series and repeated team bots are correlated.'},
        'leaderboard': leaderboard or [],
        'champions': [{'id': i, 'name': name} for i, name in enumerate(CHAMPIONS[:12])],
        'items': [{'id': i, 'name': name.replace('_', ' ').title(), 'purchasable': ITEM_DESIGNS[i]['purchasable'],
                   'cost': ITEM_DESIGNS[i]['cost'], 'result': ITEM_DESIGNS[i]['result']} for i, name in enumerate(ITEMS)],
        'matches': games}
    atomic_json(DATA, data)
    atomic_json(STATE, {'artifactId': artifact.get('id'), 'archive': str(archive),
                       'historyDir': str(Path(history_dir).resolve()) if history_dir is not None else None,
                       'updatedAt': data['meta']['updatedAt']})
    return data


def refresh(progress=print):
    # Never let an unchanged artifact or a persisted backfill cursor prevent a
    # current leaderboard and fresh head query on every dashboard build.
    archive, match_ids, ladder, metadata = collect_current(progress)
    progress('Validating recent replay purchase records against official statistics.')
    return build_data(archive, None, {}, progress, match_ids=match_ids,
                      leaderboard=ladder, collection_meta=metadata)
