"""Local Battlecode high-Elo tracker. Python3.11 standard library only."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
import json
import math
from pathlib import Path
import threading
import time
from urllib.parse import parse_qs, urlsplit

import collector

ROOT = Path(__file__).resolve().parent


def percentage(numerator, denominator):
    return 100 * numerator / denominator if denominator else None


def current_leaderboard(data):
    rows = data.get('leaderboard')
    if not isinstance(rows, list):
        raise ValueError('Current leaderboard snapshot is unavailable')
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('teamId'), str)
                or type(row.get('rank')) is not int or row['rank'] < 1
                or row['teamId'] in result):
            raise ValueError('Current leaderboard snapshot is invalid')
        rating = row.get('rating')
        if rating is not None and (type(rating) not in (int, float) or not math.isfinite(rating)):
            raise ValueError('Current leaderboard snapshot is invalid')
        if 'name' in row and not isinstance(row['name'], str):
            raise ValueError('Current leaderboard snapshot is invalid')
        result[row['teamId']] = row
    return result


def aggregate(data, query=None):
    q = query or {}
    top = int(q.get('top', '20'))
    if not 1 <= top <= 10000:
        raise ValueError('top must be between 1 and 10000')
    collection_top = data.get('meta', {}).get('collectionTop')
    if type(collection_top) is int and collection_top > 0 and top > collection_top:
        raise ValueError(f'This snapshot collects replays for only the current top {collection_top} teams')
    leaderboard = current_leaderboard(data)
    minimum = q.get('minGames', '10')
    if type(minimum) not in (int, str):
        raise ValueError('minGames must be an integer between 0 and 10000')
    try:
        minimum = int(minimum)
    except ValueError:
        raise ValueError('minGames must be an integer between 0 and 10000') from None
    if not 0 <= minimum <= 10000:
        raise ValueError('minGames must be an integer between 0 and 10000')
    filters = {'top': top, 'minGames': minimum, 'cohort': q.get('cohort', 'both'), 'kind': q.get('kind', 'ranked'),
               'champion': q.get('champion', 'all'), 'map': q.get('map', 'all'),
               'team': q.get('team', 'all'), 'outcome': q.get('outcome', 'wins')}
    for key, allowed in [('cohort', ('both', 'either')), ('kind', ('ranked', 'unranked', 'all')),
                         ('outcome', ('wins', 'losses', 'all'))]:
        if filters[key] not in allowed:
            raise ValueError(f'Invalid {key} filter')
    if filters['champion'] != 'all':
        champion = int(filters['champion'])
        if champion not in {c['id'] for c in data['champions']}:
            raise ValueError('Unknown champion')
    games = []
    observations = []
    for match in data['matches']:
        if filters['kind'] != 'all' and match['kind'] != filters['kind']:
            continue
        if filters['map'] != 'all' and match['map'] != filters['map']:
            continue
        eligible = [t for t in match['teams'] if t['id'] in leaderboard and leaderboard[t['id']]['rank'] <= top]
        if (filters['cohort'] == 'both' and len(eligible) != 2) or not eligible:
            continue
        if filters['team'] != 'all':
            eligible = [t for t in eligible if t['id'] == filters['team']]
        if not eligible:
            continue
        games.append(match)
        observations.extend((match, team) for team in eligible)
    all_champions = [(m, t, c) for m, t in observations for c in t['champions']]

    def elo_share(match, team):
        delta, maps = team.get('eloDelta'), match.get('seriesMaps')
        if (match['kind'] == 'ranked' and type(delta) in (int, float) and math.isfinite(delta)
                and type(maps) is int and maps > 0):
            return delta / maps
        return None

    champion_rows = []
    for definition in data['champions']:
        selected = [(m, t, c) for m, t, c in all_champions if c['type'] == definition['id']]
        wins = sum(t['win'] for _, t, _ in selected)
        changes = [change for match, team, _ in selected if (change := elo_share(match, team)) is not None]
        champion_rows.append(dict(definition, games=len(selected), wins=wins, losses=len(selected)-wins,
            netWins=2 * wins - len(selected), eloGained=sum(changes) if changes else None,
            eloAppearances=len(changes), eloPerAppearance=sum(changes) / len(changes) if changes else None,
            winRate=percentage(wins, len(selected)), pickRate=percentage(len(selected), len(observations)),
            avgGold=sum(c['spentGold'] for _, _, c in selected) / len(selected) if selected else None))
    champion_rows.sort(key=lambda row: (-row['games'], row['name']))
    definitions = {c['id']: {'id': c['id'], 'name': c['name']} for c in data['champions']}
    baseline = {c['id']: c['winRate'] for c in champion_rows}
    selected_id = None if filters['champion'] == 'all' else int(filters['champion'])
    pair_counts = defaultdict(Counter)
    counter_counts = defaultdict(Counter)
    for match, team in observations:
        own_types = sorted({c['type'] for c in team['champions']})
        for champion_a, champion_b in combinations(own_types, 2):
            if selected_id is not None and selected_id not in (champion_a, champion_b):
                continue
            count = pair_counts[(champion_a, champion_b)]
            count['games'] += 1
            count['wins'] += team['win']
        opponent_types = {c['type'] for other in match['teams'] if other['id'] != team['id']
                          for c in other['champions']}
        for champion in own_types:
            if selected_id is not None and champion != selected_id:
                continue
            for opponent in opponent_types:
                count = counter_counts[(champion, opponent)]
                count['games'] += 1
                count['wins'] += team['win']
    synergies = []
    for (champion_a, champion_b), count in pair_counts.items():
        rate = percentage(count['wins'], count['games'])
        a_rate, b_rate = baseline.get(champion_a), baseline.get(champion_b)
        expected = (a_rate + b_rate) / 2 if a_rate is not None and b_rate is not None else None
        synergies.append({'championA': definitions[champion_a], 'championB': definitions[champion_b],
            **count, 'winRate': rate, 'expectedWinRate': expected,
            'lift': rate - expected if expected is not None else None})
    synergies.sort(key=lambda row: (-row['games'], row['championA']['id'], row['championB']['id']))
    counters = []
    for (champion, opponent), count in counter_counts.items():
        rate = percentage(count['wins'], count['games'])
        own_rate, opponent_rate = baseline.get(champion), baseline.get(opponent)
        expected = (own_rate + (100 - opponent_rate)) / 2 if own_rate is not None and opponent_rate is not None else None
        counters.append({'champion': definitions[champion], 'opponent': definitions[opponent],
            **count, 'winRate': rate, 'expectedWinRate': expected,
            'lift': rate - expected if expected is not None else None})
    counters.sort(key=lambda row: (-row['games'], row['champion']['id'], row['opponent']['id']))
    selected_champions = [(m, t, c) for m, t, c in all_champions
                          if filters['champion'] == 'all' or c['type'] == int(filters['champion'])]
    detail_champions = [(m, t, c) for m, t, c in selected_champions
        if filters['outcome'] == 'all' or (filters['outcome'] == 'wins') == bool(t['win'])]
    denominator = len(detail_champions)
    item_rows = []
    for definition in data['items']:
        i = definition['id']
        buys = sum(c['rawItemBuys'][i] > 0 for _, _, c in detail_champions)
        final = sum(c['items'][i] > 0 for _, _, c in detail_champions)
        late = sum(c['lateItemBuys'][i] > 0 for _, _, c in detail_champions)
        # Item-conditioned winrate uses both outcomes even when build display is wins only.
        with_item = [t for _, t, c in selected_champions if c['rawItemBuys'][i] or c['items'][i]]
        item_rows.append(dict(definition, buyGames=buys,
            buyRate=percentage(buys, denominator) if definition['purchasable'] else None,
            finalGames=final, finalRate=percentage(final, denominator), lateBuyGames=late,
            lateBuyRate=percentage(late, denominator) if definition['purchasable'] else None,
            units=sum(c['rawItemBuys'][i] for _, _, c in detail_champions),
            lateUnits=sum(c['lateItemBuys'][i] for _, _, c in detail_champions),
            avgFinalCount=sum(c['items'][i] for _, _, c in detail_champions) / denominator if denominator else None,
            withItemGames=len(with_item), withItemWins=sum(t['win'] for t in with_item),
            winRate=percentage(sum(t['win'] for t in with_item), len(with_item))))
    item_rows.sort(key=lambda row: (-row['finalGames'], -row['buyGames'], row['name']))
    consumables = []
    for i, name in [(0, 'TP Crystal'), (2, 'Altar Lock')]:
        buys = sum(c['consumablesBought'][i] > 0 for _, _, c in detail_champions)
        late = sum(c['lateConsumableBuys'][i] > 0 for _, _, c in detail_champions)
        consumables.append({'id': i, 'name': name, 'buyGames': buys, 'buyRate': percentage(buys, denominator),
            'lateBuyGames': late, 'lateBuyRate': percentage(late, denominator),
            'units': sum(c['consumablesBought'][i] for _, _, c in detail_champions),
            'lateUnits': sum(c['lateConsumableBuys'][i] for _, _, c in detail_champions)})
    builds = defaultdict(list)
    names = {item['id']: item['name'] for item in data['items']}
    for _, t, c in detail_champions:
        items = ' + '.join(f'{count}× {names[i]}' for i, count in enumerate(c['items']) if count) or 'No items'
        label = (c['name'] + ': ' if filters['champion'] == 'all' else '') + items
        builds[label].append(t['win'])
    build_rows = [{'label': label, 'games': len(wins), 'wins': sum(wins), 'winRate': percentage(sum(wins), len(wins))}
                  for label, wins in builds.items()]
    build_rows.sort(key=lambda row: (-row['games'], row['label']))
    teams = defaultdict(list)
    for _, t in observations:
        teams[t['id']].append(t)
    team_rows = [{'id': tid, 'name': leaderboard[tid].get('name', values[0]['name']), 'games': len(values),
                  'currentRank': leaderboard[tid]['rank'], 'currentElo': leaderboard[tid].get('rating'),
                  'wins': sum(t['win'] for t in values), 'winRate': percentage(sum(t['win'] for t in values), len(values))}
                 for tid, values in teams.items()]
    team_rows.sort(key=lambda row: (-row['games'], row['name']))
    recent = []
    for match in sorted(games, key=lambda m: m['startedAt'], reverse=True)[:50]:
        recent.append({k: match[k] for k in ('id', 'url', 'map', 'startedAt', 'rounds')})
        recent[-1]['teams'] = [{k: t[k] for k in ('name', 'rank', 'rankMin', 'rankMax', 'elo', 'win')}
                               for t in match['teams']]
        for source, team in zip(match['teams'], recent[-1]['teams']):
            current = leaderboard.get(source['id'], {})
            team.update(currentRank=current.get('rank'), currentElo=current.get('rating'))
    all_teams = {t['id']: t['name'] for m in data['matches'] for t in m['teams']}
    all_teams.update((tid, row['name']) for tid, row in leaderboard.items() if 'name' in row)
    selected_name = next((c['name'] for c in data['champions'] if str(c['id']) == filters['champion']), 'All champions')
    return {'meta': data['meta'], 'filters': filters,
        'summary': {'games': len(games), 'teamObservations': len(observations), 'uniqueTeams': len(teams),
                    'start': min((m['startedAt'] for m in games), default=None),
                    'end': max((m['startedAt'] for m in games), default=None)},
        'champions': champion_rows, 'items': item_rows, 'consumables': consumables,
        'synergies': synergies, 'counters': counters,
        'builds': build_rows[:25], 'teamRows': team_rows, 'recentMatches': recent,
        'options': {'maps': sorted({m['map'] for m in data['matches']}),
                    'teams': [{'id': tid, 'name': name} for tid, name in sorted(all_teams.items(), key=lambda kv: kv[1])],
                    'champions': data['champions']},
        'detail': {'observations': denominator, 'championName': selected_name}}


class Tracker:
    def __init__(self):
        self.data = collector.read_json(collector.DATA)
        self.lock = threading.Lock()
        self.status = {'running': False, 'lastError': None, 'lastRefresh': None, 'message': 'Ready'}

    def refresh(self):
        with self.lock:
            if self.status['running']:
                return False
            self.status.update(running=True, lastError=None, message='Checking archive')

        def run():
            try:
                def progress(message):
                    with self.lock:
                        self.status['message'] = message
                    print(message, flush=True)
                value = collector.refresh(progress)
                with self.lock:
                    self.data = value
                    self.status.update(lastRefresh=datetime.now(timezone.utc).isoformat(), message='Archive up to date')
            except Exception as exc:
                # Failed refresh never replaces the previous verified dataset.
                with self.lock:
                    self.status.update(lastError=f'{type(exc).__name__}: {exc}', message='Refresh failed; previous data retained')
            finally:
                with self.lock:
                    self.status['running'] = False
        threading.Thread(target=run, daemon=True).start()
        return True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, tracker, **kwargs):
        self.tracker = tracker
        super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

    def respond(self, status, data):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/api/status':
            with self.tracker.lock:
                self.respond(200, dict(self.tracker.status))
        elif url.path in ('/api/stats', '/api/data', '/data.json'):
            with self.tracker.lock:
                data = self.tracker.data
            if data is None:
                self.respond(503, {'error': 'No verified dataset yet. Refresh or import an archive.'})
                return
            try:
                q = {key: values[-1] for key, values in parse_qs(url.query).items()}
                self.respond(200, aggregate(data, q) if url.path == '/api/stats' else data)
            except (ValueError, TypeError) as exc:
                self.respond(400, {'error': str(exc)})
        elif url.path.startswith('/api/'):
            self.respond(404, {'error': 'Unknown endpoint'})
        else:
            super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path != '/api/refresh':
            self.respond(404, {'error': 'Unknown endpoint'})
            return
        # Local UI requests only; do not let an unrelated website trigger work.
        host = self.headers.get('Host')
        origin = self.headers.get('Origin')
        if origin and origin not in ('http://' + host, 'https://' + host):
            self.respond(403, {'error': 'Refresh must originate from this tracker.'})
            return
        started = self.tracker.refresh()
        self.respond(202, {'started': started, **self.tracker.status})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve', help='Serve dashboard, checking for new artifacts every 300 seconds')
    serve.add_argument('--port', type=int, default=8765)
    serve.add_argument('--interval', type=int, default=300, help='Refresh interval seconds; 0 disables scheduled checks')
    sub.add_parser('refresh', help='Fetch and verify the newest archive, repairing historical ranks')
    seed = sub.add_parser('import', help='Index an existing verified archive and per-team rating histories')
    seed.add_argument('--archive', type=Path, required=True)
    seed.add_argument('--histories', type=Path, required=True)
    seed.add_argument('--artifact-id', type=int, required=True)
    seed.add_argument('--artifact-created-at', required=True)
    args = parser.parse_args()
    if args.command == 'refresh':
        collector.refresh()
    elif args.command == 'import':
        collector.build_data(args.archive, args.histories, {'id': args.artifact_id, 'created_at': args.artifact_created_at})
    else:
        if args.interval < 0:
            parser.error('--interval must be nonnegative')
        tracker = Tracker()
        server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, tracker=tracker))
        if tracker.data is None:
            tracker.refresh()
        if args.interval:
            def watch():
                while True:
                    time.sleep(args.interval)
                    tracker.refresh()
            threading.Thread(target=watch, daemon=True).start()
        print(f'Battlecode tracker: http://127.0.0.1:{args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
