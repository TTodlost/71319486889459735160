import copy
import math
import threading
import unittest
from unittest.mock import patch

import app


def champion(kind=5, items=None, bought=None, late=None):
    held, raw, closing = [0] * 42, [0] * 42, [0] * 42
    for i, n in (items or {}).items():
        held[i] = n
    for i, n in (bought or {}).items():
        raw[i] = n
    for i, n in (late or {}).items():
        closing[i] = n
    return {'type': kind, 'name': 'Stellar' if kind == 5 else 'Cassi', 'items': held,
            'rawItemBuys': raw, 'lateItemBuys': closing, 'consumablesBought': [1, 0, 0, 0],
            'lateConsumableBuys': [0] * 4, 'spentGold': 150}


def team(tid, rank, win, champs):
    return {'id': tid, 'name': tid, 'rank': rank, 'rankMin': rank, 'rankMax': rank,
            'elo': 1700-rank, 'verified': True, 'win': win, 'champions': champs}


def fixture():
    return {'meta': {}, 'leaderboard': [{'teamId': 'a', 'rank': 1, 'rating': 1699, 'name': 'a'},
                                      {'teamId': 'b', 'rank': 2, 'rating': 1698, 'name': 'b'},
                                      {'teamId': 'c', 'rank': 40, 'rating': 1660, 'name': 'c'}],
            'champions': [{'id': 5, 'name': 'Stellar'}, {'id': 3, 'name': 'Cassi'}],
            'items': [{'id': 8, 'name': 'Power Shard Of Focus', 'purchasable': True},
                      {'id': 9, 'name': 'Power Emblem Of Focus', 'purchasable': False}],
            'matches': [
                {'id': 'mirror', 'url': 'https://example.com/mirror', 'map': 'collision', 'kind': 'ranked',
                 'startedAt': '2026-10-14T08:00:00Z', 'rounds': 500,
                 'teams': [team('a', 1, True, [champion(items={9: 1}, bought={8: 5}, late={8: 2})]),
                           team('b', 2, False, [champion(items={8: 2}, bought={8: 2})])]},
                {'id': 'mixed', 'url': 'https://example.com/mixed', 'map': 'expanse', 'kind': 'ranked',
                 'startedAt': '2026-10-14T08:10:00Z', 'rounds': 600,
                 'teams': [team('a', 1, False, [champion(3)]), team('c', 40, True, [champion(3)])]},
                {'id': 'unranked', 'url': 'https://example.com/test', 'map': 'collision', 'kind': 'unranked',
                 'startedAt': '2026-10-14T08:20:00Z', 'rounds': 400,
                 'teams': [team('a', 1, True, [champion(3)]), team('b', 2, False, [champion(3)])]}]}


class AggregationTests(unittest.TestCase):
    def test_mirror_picks_count_both_sides_and_one_game(self):
        result = app.aggregate(fixture())
        self.assertEqual(result['summary']['games'], 1)
        self.assertEqual(result['summary']['teamObservations'], 2)
        stellar = next(c for c in result['champions'] if c['id'] == 5)
        self.assertEqual((stellar['games'], stellar['wins'], stellar['losses']), (2, 1, 1))
        self.assertEqual(stellar['winRate'], 50)
        self.assertEqual(stellar['pickRate'], 100)

    def test_direct_buys_and_combines_have_different_denominators(self):
        result = app.aggregate(fixture(), {'outcome': 'all'})
        shard, emblem = sorted(result['items'], key=lambda r: r['id'])
        self.assertEqual(result['detail']['observations'], 2)
        self.assertEqual((shard['buyRate'], shard['units'], shard['lateBuyRate']), (100, 7, 50))
        self.assertEqual((emblem['buyRate'], emblem['finalRate']), (None, 50))
        self.assertEqual(shard['finalRate'], 50)

    def test_build_outcome_does_not_inflate_champion_winrate(self):
        result = app.aggregate(fixture(), {'outcome': 'wins', 'champion': '5'})
        self.assertEqual(result['detail']['observations'], 1)
        self.assertEqual(result['champions'][0]['winRate'], 50)
        self.assertEqual(next(i for i in result['items'] if i['id'] == 9)['finalRate'], 100)

    def test_either_cohort_excludes_low_rank_team_observations(self):
        result = app.aggregate(fixture(), {'cohort': 'either'})
        self.assertEqual(result['summary']['games'], 2)
        self.assertEqual(result['summary']['teamObservations'], 3)
        cassi = next(c for c in result['champions'] if c['id'] == 3)
        self.assertEqual((cassi['games'], cassi['wins']), (1, 0))
        self.assertNotIn('c', [t['id'] for t in result['teamRows']])

    def test_ranked_unranked_and_map_filters(self):
        self.assertEqual(app.aggregate(fixture(), {'kind': 'unranked'})['summary']['games'], 1)
        self.assertEqual(app.aggregate(fixture(), {'cohort': 'either', 'map': 'expanse'})['summary']['games'], 1)
        empty = app.aggregate(fixture(), {'map': 'missing'})
        self.assertEqual(empty['summary']['games'], 0)
        self.assertIsNone(empty['champions'][0]['winRate'])

    def test_current_leaderboard_membership_replaces_historical_rank(self):
        data = fixture()
        data['leaderboard'][1]['rank'] = 40
        data['leaderboard'][2]['rank'] = 2
        data['matches'][1]['teams'][0].update(verified=False, rank=None, rankMin=None, rankMax=None)
        result = app.aggregate(data)
        self.assertEqual(result['summary']['games'], 1)
        self.assertEqual(result['recentMatches'][0]['id'], 'mixed')
        self.assertEqual(result['summary']['teamObservations'], 2)
        self.assertEqual(result['champions'][0]['winRate'], 50)
        promoted = next(row for row in result['teamRows'] if row['id'] == 'c')
        self.assertEqual(promoted['currentRank'], 2)
        self.assertEqual(result['recentMatches'][0]['teams'][1]['rank'], 40)
        self.assertEqual(result['recentMatches'][0]['teams'][1]['currentRank'], 2)

    def test_current_rank_cutoff_and_missing_membership(self):
        data = fixture()
        data['leaderboard'][0]['rank'] = 20
        self.assertEqual(app.aggregate(data)['summary']['games'], 1)
        data['leaderboard'][0]['rank'] = 21
        self.assertEqual(app.aggregate(data)['summary']['games'], 0)
        data['leaderboard'] = []
        self.assertEqual(app.aggregate(data, {'cohort': 'either'})['summary']['games'], 0)

    def test_missing_or_invalid_current_leaderboard_is_rejected(self):
        for leaderboard in (None, {}, [{'teamId': 'a', 'rank': True}], [{'teamId': 'a', 'rank': 0}],
                            [{'teamId': 'a', 'rank': 1}, {'teamId': 'a', 'rank': 2}]):
            data = fixture()
            data['leaderboard'] = leaderboard
            with self.subTest(leaderboard=leaderboard), self.assertRaisesRegex(ValueError, 'Current leaderboard'):
                app.aggregate(data)
        data = fixture()
        data['meta']['collectionTop'] = 20
        with self.assertRaisesRegex(ValueError, 'current top 20'):
            app.aggregate(data, {'top': '21'})

    def test_invalid_filters_rejected(self):
        for query in ({'top': '0'}, {'kind': 'typo'}, {'champion': '999'}, {'cohort': 'typo'}):
            with self.assertRaises(ValueError):
                app.aggregate(fixture(), query)

    def test_series_elo_is_apportioned_once_across_five_maps(self):
        data = fixture()
        original = copy.deepcopy(data['matches'][0])
        original['seriesMaps'] = 5
        original['teams'][0]['eloDelta'] = 7.5
        original['teams'][1]['eloDelta'] = -7.5
        original['teams'][1]['champions'] = [champion(3)]
        data['matches'] = []
        for number in range(5):
            match = copy.deepcopy(original)
            match['id'] = f'map-{number}'
            match['teams'][0]['win'] = number < 3
            match['teams'][1]['win'] = number >= 3
            data['matches'].append(match)
        for outcome in ('wins', 'losses', 'all'):
            with self.subTest(outcome=outcome):
                rows = {c['id']: c for c in app.aggregate(data, {'outcome': outcome})['champions']}
                self.assertEqual((rows[5]['games'], rows[5]['wins'], rows[5]['netWins']), (5, 3, 1))
                self.assertEqual(rows[5]['winRate'], 60)
                self.assertEqual((rows[5]['eloGained'], rows[5]['eloAppearances'], rows[5]['eloPerAppearance']),
                                 (7.5, 5, 1.5))
                self.assertEqual((rows[3]['eloGained'], rows[3]['netWins']), (-7.5, -1))
        # A partial archive attributes only its observed maps' shares.
        data['matches'] = data['matches'][:1]
        self.assertEqual(next(c for c in app.aggregate(data)['champions'] if c['id'] == 5)['eloGained'], 1.5)

    def test_unknown_unranked_and_nonfinite_elo_are_not_zero_gain(self):
        data = fixture()
        data['matches'] = data['matches'][:1]
        match = data['matches'][0]
        match['seriesMaps'] = 5
        for delta in (None, math.nan, math.inf, -math.inf, True):
            with self.subTest(delta=delta):
                match['teams'][0]['eloDelta'] = delta
                row = app.aggregate(data)['champions'][0]
                self.assertEqual(row['eloAppearances'], 0)
                self.assertIsNone(row['eloGained'])
                self.assertIsNone(row['eloPerAppearance'])
        match['teams'][0]['eloDelta'] = 0
        row = app.aggregate(data)['champions'][0]
        self.assertEqual((row['eloGained'], row['eloAppearances'], row['eloPerAppearance']), (0, 1, 0))
        match['teams'][0]['eloDelta'] = 10
        for maps in (None, 0, True, 2.5, math.inf):
            with self.subTest(seriesMaps=maps):
                match['seriesMaps'] = maps
                self.assertIsNone(app.aggregate(data)['champions'][0]['eloGained'])
        match.update(seriesMaps=5, kind='unranked')
        self.assertIsNone(app.aggregate(data, {'kind': 'all'})['champions'][0]['eloGained'])

    def test_item_winrate_exposes_both_outcome_denominator_once(self):
        data = fixture()
        data['matches'][0]['teams'][0]['champions'] = [champion(items={8: 1}, bought={8: 3})]
        # Winner bought and held the item; loser only held it. Both count once.
        data['matches'][0]['teams'][1]['champions'] = [champion(items={8: 1})]
        for outcome in ('wins', 'losses', 'all'):
            result = app.aggregate(data, {'outcome': outcome})
            shard = next(i for i in result['items'] if i['id'] == 8)
            self.assertEqual((shard['withItemGames'], shard['withItemWins'], shard['winRate']), (2, 1, 50))
            self.assertEqual(result['detail']['observations'], 2 if outcome == 'all' else 1)

    def test_synergy_pairs_are_unordered_deduplicated_and_use_full_baselines(self):
        data = fixture()
        data['champions'] = [{'id': 1, 'name': 'Alpha'}, {'id': 2, 'name': 'Beta'}, {'id': 3, 'name': 'Gamma'}]
        template = data['matches'][0]
        data['matches'] = []
        rosters = [([2, 1, 1], [3], True), ([1, 2], [3], False), ([1], [2, 3], True)]
        for number, (own, enemy, win) in enumerate(rosters):
            match = copy.deepcopy(template)
            match['id'] = f'pair-{number}'
            match['teams'] = [team('a', 1, win, [champion(i) for i in own]),
                              team('b', 2, not win, [champion(i) for i in enemy])]
            data['matches'].append(match)
        result = app.aggregate(data, {'outcome': 'wins'})
        pairs = {(r['championA']['id'], r['championB']['id']): r for r in result['synergies']}
        self.assertEqual(list(pairs), [(1, 2), (2, 3)])
        self.assertEqual((pairs[(1, 2)]['games'], pairs[(1, 2)]['wins'], pairs[(1, 2)]['winRate']), (2, 1, 50))
        # Alpha:3 wins/4 appearances, Beta:1/3. Pair baseline keeps both outcomes.
        expected = (75 + 100 / 3) / 2
        self.assertAlmostEqual(pairs[(1, 2)]['expectedWinRate'], expected)
        self.assertAlmostEqual(pairs[(1, 2)]['lift'], 50 - expected)
        chosen = app.aggregate(data, {'champion': '1', 'outcome': 'losses'})
        self.assertEqual(chosen['synergies'], [pairs[(1, 2)]])

    def test_counters_use_opposite_team_deduplicate_and_keep_mirror_outcomes(self):
        data = fixture()
        data['champions'] = [{'id': 1, 'name': 'Alpha'}, {'id': 2, 'name': 'Beta'}, {'id': 3, 'name': 'Gamma'}]
        match = data['matches'][0]
        match['teams'] = [team('a', 1, True, [champion(2), champion(1), champion(2)]),
                          team('b', 2, False, [champion(3), champion(1), champion(3)])]
        data['matches'] = [match]
        result = app.aggregate(data)
        rows = {(r['champion']['id'], r['opponent']['id']): r for r in result['counters']}
        mirror = rows[(1, 1)]
        self.assertEqual((mirror['games'], mirror['wins'], mirror['winRate']), (2, 1, 50))
        self.assertEqual((mirror['expectedWinRate'], mirror['lift']), (50, 0))
        self.assertEqual((rows[(2, 3)]['games'], rows[(2, 3)]['wins']), (1, 1))
        self.assertEqual((rows[(3, 2)]['games'], rows[(3, 2)]['wins']), (1, 0))
        self.assertNotIn((2, 2), rows)
        chosen = app.aggregate(data, {'champion': '2', 'outcome': 'losses'})
        self.assertEqual({r['champion']['id'] for r in chosen['counters']}, {2})
        self.assertEqual({r['opponent']['id'] for r in chosen['counters']}, {1, 3})
        self.assertEqual(next(r for r in chosen['counters'] if r['opponent']['id'] == 3)['wins'], 1)

    def test_counter_unknown_opponent_baseline_remains_unknown(self):
        data = fixture()
        match = data['matches'][0]
        match['teams'] = [team('a', 1, True, [champion(5)]), team('b', 40, False, [champion(3)])]
        data['leaderboard'][1]['rank'] = 40
        data['matches'] = [match]
        result = app.aggregate(data, {'cohort': 'either'})
        self.assertEqual(len(result['counters']), 1)
        row = result['counters'][0]
        self.assertEqual((row['champion']['id'], row['opponent']['id'], row['winRate']), (5, 3, 100))
        self.assertIsNone(row['expectedWinRate'])
        self.assertIsNone(row['lift'])

    def test_minimum_games_is_validated_but_only_ui_filters_arrays(self):
        data = fixture()
        data['matches'] = data['matches'][:1]
        data['matches'][0]['teams'][0]['champions'].append(champion(3))
        baseline = app.aggregate(data)
        self.assertEqual(baseline['filters']['minGames'], 10)
        self.assertTrue(baseline['synergies'])
        for minimum in ('0', '10000', 10):
            result = app.aggregate(data, {'minGames': minimum})
            self.assertEqual(result['filters']['minGames'], int(minimum))
            for key in ('champions', 'synergies', 'counters'):
                self.assertEqual(result[key], baseline[key])
        for minimum in ('-1', '10001', '1.5', 'word', None, True, 2.5):
            with self.subTest(minGames=minimum), self.assertRaises(ValueError):
                app.aggregate(data, {'minGames': minimum})

    def test_failed_refresh_preserves_verified_dataset(self):
        done = threading.Event()
        with patch.object(app.collector, 'read_json', return_value=fixture()):
            tracker = app.Tracker()
        original = tracker.data

        def fail(progress):
            done.set()
            raise ValueError('rating history gap')

        with patch.object(app.collector, 'refresh', side_effect=fail):
            self.assertTrue(tracker.refresh())
            self.assertTrue(done.wait(1))
            for _ in range(100):
                with tracker.lock:
                    if not tracker.status['running']:
                        break
                threading.Event().wait(.01)
        self.assertIs(tracker.data, original)
        self.assertIn('rating history gap', tracker.status['lastError'])


if __name__ == '__main__':
    unittest.main()
