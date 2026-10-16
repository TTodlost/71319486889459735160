import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import app
from test_app import champion, fixture, team


def map_fixture():
    data = fixture()
    data['champions'].append({'id': 7, 'name': 'Unused champion'})
    data['items'].append({'id': 10, 'name': 'Unused item', 'purchasable': True})
    template = data['matches'][0]
    for match_id, map_name, rosters in (
        ('collision-second', 'collision', [
            team('a', 1, True, [champion(items={8: 1}, bought={8: 3})]),
            team('b', 2, False, [champion(3, items={9: 1})])]),
        ('expanse-ranked', 'expanse', [
            team('a', 1, False, [champion(3, items={8: 1})]),
            team('b', 2, True, [champion(items={9: 1})])]),
    ):
        match = copy.deepcopy(template)
        match.update(id=match_id, map=map_name, teams=rosters)
        data['matches'].append(match)
    return data


def row(entity_id, name, games, wins):
    return {'id': entity_id, 'name': name, 'games': games, 'wins': wins,
            'winRate': 100 * wins / games}


def expected_maps():
    # Collision has three Stellar appearances (two wins), but only two maps.
    # Its second winner bought and held a shard, contributing one appearance.
    return [
        {'map': 'collision', 'games': 2,
         'champions': [row(3, 'Cassi', 1, 0), row(5, 'Stellar', 3, 2)],
         'items': [row(9, 'Power Emblem Of Focus', 2, 1),
                   row(8, 'Power Shard Of Focus', 3, 2)],
         'teams': [row('a', 'a', 2, 2), row('b', 'b', 2, 0)]},
        {'map': 'expanse', 'games': 1,
         'champions': [row(3, 'Cassi', 1, 0), row(5, 'Stellar', 1, 1)],
         'items': [row(9, 'Power Emblem Of Focus', 1, 1),
                   row(8, 'Power Shard Of Focus', 1, 0)],
         'teams': [row('a', 'a', 1, 0), row('b', 'b', 1, 1)]},
    ]


def unicode_fixture():
    data = fixture()
    data['champions'] = [{'id': 5, 'name': 'Same'}, {'id': 3, 'name': 'Same'}]
    data['items'] = [{'id': 9, 'name': 'Same', 'purchasable': False},
                     {'id': 8, 'name': 'Same', 'purchasable': True}]
    template = data['matches'][0]
    template['teams'] = [team('b', 2, False, [champion(5, items={9: 1})]),
                         team('a', 1, True, [champion(3, items={8: 1})])]
    for entry in template['teams']:
        entry['name'] = 'Same'
    data['matches'] = []
    for map_name in ('😀', '\uffff', 'é', 'a', 'Z'):
        match = copy.deepcopy(template)
        match.update(id=map_name, map=map_name)
        data['matches'].append(match)
    return data


def changed_leaderboard_fixture():
    data = map_fixture()
    data['leaderboard'][0]['name'] = 'Current a'
    data['leaderboard'][1]['rank'] = 40
    data['leaderboard'][2].update(rank=2, name='Current c')
    # Current membership remains authoritative when historical rank is unknown.
    data['matches'][1]['teams'][0].update(verified=False, rank=None, rankMin=None, rankMax=None)
    return data


class MapAggregationTests(unittest.TestCase):
    def test_default_overall_view_and_existing_statistics_are_preserved(self):
        data = map_fixture()
        overall = app.aggregate(data)
        self.assertEqual(overall['filters']['view'], 'overall')
        self.assertEqual(overall['mapStats'], [])
        self.assertEqual(app.aggregate(data, {'view': 'overall'}), overall)
        per_map = app.aggregate(data, {'view': 'maps'})
        self.assertEqual(per_map['filters']['view'], 'maps')
        # Changing the view must preserve the established overall statistics.
        for key in overall.keys() - {'filters', 'mapStats'}:
            self.assertEqual(per_map[key], overall[key], key)

    def test_per_map_counts_include_both_outcomes_and_only_observed_entities(self):
        result = app.aggregate(map_fixture(), {'view': 'maps'})
        self.assertEqual(result['mapStats'], expected_maps())

    def test_build_outcome_does_not_change_any_map_winrate(self):
        for outcome in ('wins', 'losses', 'all'):
            with self.subTest(outcome=outcome):
                result = app.aggregate(map_fixture(), {'view': 'maps', 'outcome': outcome})
                self.assertEqual(result['mapStats'], expected_maps())

    def test_selected_champion_only_filters_map_items(self):
        expected = expected_maps()
        expected[0]['items'][0] = row(9, 'Power Emblem Of Focus', 1, 1)
        expected[1]['items'] = [row(9, 'Power Emblem Of Focus', 1, 1)]
        result = app.aggregate(map_fixture(), {'view': 'maps', 'champion': '5', 'outcome': 'losses'})
        self.assertEqual(result['mapStats'], expected)
        absent = app.aggregate(map_fixture(), {'view': 'maps', 'champion': '7'})
        self.assertTrue(all(not stats['items'] for stats in absent['mapStats']))
        self.assertEqual([stats['champions'] for stats in absent['mapStats']],
                         [stats['champions'] for stats in expected_maps()])

    def test_either_cohort_counts_only_the_qualifying_side(self):
        expected = expected_maps()
        expanse = expected[1]
        expanse.update(games=2,
                       champions=[row(3, 'Cassi', 2, 0), row(5, 'Stellar', 1, 1)],
                       teams=[row('a', 'a', 2, 0), row('b', 'b', 1, 1)])
        result = app.aggregate(map_fixture(), {'view': 'maps', 'cohort': 'either'})
        self.assertEqual(result['mapStats'], expected)
        expanded = app.aggregate(map_fixture(), {'view': 'maps', 'top': '40'})
        expanse = expanded['mapStats'][1]
        self.assertEqual(expanse['games'], 2)
        self.assertEqual(expanse['champions'][0], row(3, 'Cassi', 3, 1))
        self.assertEqual(expanse['teams'][-1], row('c', 'c', 1, 1))

    def test_map_cohorts_and_team_names_use_the_current_leaderboard(self):
        result = app.aggregate(changed_leaderboard_fixture(), {'view': 'maps'})
        self.assertEqual(result['mapStats'], [
            {'map': 'expanse', 'games': 1,
             'champions': [row(3, 'Cassi', 2, 1)], 'items': [],
             'teams': [row('a', 'Current a', 1, 0), row('c', 'Current c', 1, 1)]},
        ])
        self.assertEqual(result['summary']['games'], 1)
        self.assertEqual(result['recentMatches'][0]['teams'][1]['currentRank'], 2)
        self.assertEqual(result['recentMatches'][0]['teams'][1]['rank'], 40)
        for stats in result['mapStats']:
            self.assertNotIn('b', [team['id'] for team in stats['teams']])

    def test_maps_preserve_the_snapshot_collection_limit(self):
        data = map_fixture()
        data['meta']['collectionTop'] = 20
        with self.assertRaisesRegex(ValueError, 'current top 20'):
            app.aggregate(data, {'view': 'maps', 'top': '21'})

    def test_selected_team_and_map_apply_before_champion_item_selection(self):
        expected = {'map': 'expanse', 'games': 1,
                    'champions': [row(3, 'Cassi', 1, 0)], 'items': [],
                    'teams': [row('a', 'a', 1, 0)]}
        query = {'view': 'maps', 'map': 'expanse', 'team': 'a', 'champion': '5'}
        self.assertEqual(app.aggregate(map_fixture(), query)['mapStats'], [expected])
        query.update(champion='3', outcome='wins')
        expected['items'] = [row(8, 'Power Shard Of Focus', 1, 0)]
        self.assertEqual(app.aggregate(map_fixture(), query)['mapStats'], [expected])

    def test_queue_filter_keeps_ranked_and_unranked_denominators_separate(self):
        unranked = {'map': 'collision', 'games': 1,
                    'champions': [row(3, 'Cassi', 2, 1)], 'items': [],
                    'teams': [row('a', 'a', 1, 1), row('b', 'b', 1, 0)]}
        self.assertEqual(app.aggregate(map_fixture(), {'view': 'maps', 'kind': 'unranked'})['mapStats'],
                         [unranked])
        expected = expected_maps()
        expected[0].update(games=3,
                           champions=[row(3, 'Cassi', 3, 1), row(5, 'Stellar', 3, 2)],
                           teams=[row('a', 'a', 3, 3), row('b', 'b', 3, 0)])
        self.assertEqual(app.aggregate(map_fixture(), {'view': 'maps', 'kind': 'all'})['mapStats'],
                         expected)

    def test_minimum_games_does_not_prune_per_map_aggregation(self):
        for minimum in ('0', '2', '10000'):
            with self.subTest(minGames=minimum):
                result = app.aggregate(map_fixture(), {'view': 'maps', 'minGames': minimum})
                self.assertEqual(result['filters']['minGames'], int(minimum))
                self.assertEqual(result['mapStats'], expected_maps())

    def test_no_qualifying_games_omits_maps(self):
        for query in ({'map': 'missing'}, {'team': 'missing'}, {'top': '1'}):
            with self.subTest(query=query):
                result = app.aggregate(map_fixture(), {'view': 'maps', **query})
                self.assertEqual(result['mapStats'], [])
        data = map_fixture()
        data['matches'] = []
        self.assertEqual(app.aggregate(data, {'view': 'maps'})['mapStats'], [])

    def test_maps_use_unicode_order_and_equal_entity_names_use_ids(self):
        stats = app.aggregate(unicode_fixture(), {'view': 'maps'})['mapStats']
        self.assertEqual([entry['map'] for entry in stats], ['Z', 'a', 'é', '\uffff', '😀'])
        for entry in stats:
            self.assertEqual([entity['id'] for entity in entry['champions']], [3, 5])
            self.assertEqual([entity['id'] for entity in entry['items']], [8, 9])
            self.assertEqual([entity['id'] for entity in entry['teams']], ['a', 'b'])

    def test_invalid_views_are_rejected(self):
        for view in ('map', 'MAPS', '', None, True):
            with self.subTest(view=view), self.assertRaises(ValueError):
                app.aggregate(map_fixture(), {'view': view})

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_browser_aggregation_matches_python_for_map_filters_and_ordering(self):
        queries = [
            {}, {'view': 'overall'}, {'view': 'maps'},
            {'view': 'maps', 'cohort': 'either'}, {'view': 'maps', 'top': '40'},
            {'view': 'maps', 'top': '1'}, {'view': 'maps', 'kind': 'unranked'},
            {'view': 'maps', 'kind': 'all'}, {'view': 'maps', 'map': 'missing'},
            {'view': 'maps', 'map': 'expanse', 'team': 'a', 'champion': '5'},
            {'view': 'maps', 'champion': '7'}, {'view': 'maps', 'minGames': '10000'},
            *({'view': 'maps', 'champion': '5', 'outcome': outcome}
              for outcome in ('wins', 'losses', 'all')),
        ]
        cases = [{'data': map_fixture(), 'query': query} for query in queries]
        cases.append({'data': unicode_fixture(), 'query': {'view': 'maps'}})
        cases.extend({'data': changed_leaderboard_fixture(), 'query': {'view': 'maps', **query}}
                     for query in ({}, {'cohort': 'either'}, {'team': 'c'}))
        capped = map_fixture()
        capped['meta']['collectionTop'] = 20
        cases.append({'data': capped, 'query': {'view': 'maps', 'top': '21'}})
        cases.extend({'data': map_fixture(), 'query': {'view': view}}
                     for view in ('map', 'MAPS', '', None, True))
        script = '''
const {aggregate} = require(process.argv[1]);
const cases = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(({data, query}) => {
  try {
    const result = aggregate(data, query);
    return {filters: result.filters, mapStats: result.mapStats};
  } catch (error) { return {error: true}; }
})));
'''
        process = subprocess.run(
            [shutil.which('node'), '-e', script, str(Path(__file__).parent / 'web' / 'stats.js')],
            input=json.dumps(cases), capture_output=True, text=True, encoding='utf-8',
            check=True, timeout=30)
        browser_results = json.loads(process.stdout)
        self.assertEqual(len(browser_results), len(cases))
        for case, browser in zip(cases, browser_results):
            with self.subTest(query=case['query']):
                try:
                    result = app.aggregate(case['data'], case['query'])
                    expected = {'filters': result['filters'], 'mapStats': result['mapStats']}
                except ValueError:
                    expected = {'error': True}
                self.assertEqual(browser, expected)


if __name__ == '__main__':
    unittest.main()
