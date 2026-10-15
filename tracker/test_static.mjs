import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createRequire} from 'node:module';
import {readFileSync, existsSync} from 'node:fs';
import {execFileSync} from 'node:child_process';

const require = createRequire(import.meta.url);
const {aggregate, validateDataset} = require('./web/stats.js');
const {createSnapshotSource} = require('./web/snapshot.js');
const champion = (type = 5, items = {}, bought = {}, late = {}) => {
  const values = entries => Array.from({length: 2}, (_, i) => entries[i] || 0);
  return {type, name: {5: 'Stellar', 3: 'Cassi', 4: 'Issac'}[type], items: values(items), rawItemBuys: values(bought),
    lateItemBuys: values(late), consumablesBought: [1, 0, 0, 0], lateConsumableBuys: [0, 0, 0, 0], spentGold: 150};
};
const team = (id, rank, win, champions, eloDelta) => ({id, name: id, rank, rankMin: rank, rankMax: rank, elo: 1700 - rank, verified: true, win, champions, eloDelta});
const fixture = () => ({meta: {}, champions: [{id: 5, name: 'Stellar'}, {id: 3, name: 'Cassi'}, {id: 4, name: 'Issac'}],
  items: [{id: 0, name: 'Power Shard Of Focus', purchasable: true}, {id: 1, name: 'Power Emblem Of Focus', purchasable: false}],
  matches: [
    {id: 'mirror', url: 'https://example.com/mirror', map: 'collision', kind: 'ranked', startedAt: '2026-10-14T08:00:00Z', rounds: 500, seriesMaps: 2,
      teams: [team('a', 1, true, [champion(5, {1: 1}, {0: 5}, {0: 2}), champion(3)], 12), team('b', 2, false, [champion(5, {0: 2}, {0: 2})], -12)]},
    {id: 'mixed', url: 'https://example.com/mixed', map: 'expanse', kind: 'ranked', startedAt: '2026-10-14T08:10:00Z', rounds: 600, seriesMaps: 4,
      teams: [team('a', 1, false, [champion(3)], -8), team('c', 40, true, [champion(4)], 8)]},
    {id: 'unranked', url: 'https://example.com/test', map: 'collision', kind: 'unranked', startedAt: '2026-10-14T08:20:00Z', rounds: 400, seriesMaps: 1,
      teams: [team('a', 1, true, [champion(3)], 12), team('b', 2, false, [champion(3)], -12)]}
  ]});

test('mirror champion appearances count both sides, without duplicating the game', () => {
  const result = aggregate(fixture());
  assert.equal(result.summary.games, 1);
  assert.equal(result.summary.teamObservations, 2);
  const stellar = result.champions.find(row => row.id === 5);
  assert.deepEqual([stellar.games, stellar.wins, stellar.losses, stellar.winRate, stellar.pickRate], [2, 1, 1, 50, 100]);
});

test('direct buys exclude combines; item win rate retains both outcomes', () => {
  const all = aggregate(fixture(), {outcome: 'all'});
  const shard = all.items.find(row => row.id === 0), emblem = all.items.find(row => row.id === 1);
  assert.equal(all.detail.observations, 3);
  assert.deepEqual([shard.buyGames, shard.units, shard.lateBuyGames, shard.lateUnits], [2, 7, 1, 2]);
  assert.deepEqual([emblem.buyRate, emblem.finalGames, emblem.finalRate], [null, 1, 100 / 3]);
  const winning = aggregate(fixture(), {champion: '5', outcome: 'wins'});
  assert.equal(winning.detail.observations, 1);
  assert.equal(winning.champions.find(row => row.id === 5).winRate, 50);
  const winningShard = winning.items.find(row => row.id === 0);
  assert.deepEqual([winningShard.winRate, winningShard.withItemWins, winningShard.withItemGames], [50, 1, 2]);
  assert.equal(winning.items.find(row => row.id === 1).finalRate, 100);
});

test('either cohort counts only qualifying sides; rank uncertainty excludes membership', () => {
  const result = aggregate(fixture(), {cohort: 'either'});
  assert.equal(result.summary.games, 2);
  assert.equal(result.summary.teamObservations, 3);
  assert.equal(result.champions.find(row => row.id === 4).games, 0);
  assert.ok(!result.teamRows.some(row => row.id === 'c'));
  const data = fixture();
  data.matches[0].teams[0].rankMax = 21;
  assert.equal(aggregate(data).summary.games, 0);
  data.matches[0].teams[0].verified = false;
  assert.equal(aggregate(data).summary.games, 0);
});

test('filters compose and empty samples use null rates', () => {
  assert.equal(aggregate(fixture(), {kind: 'unranked'}).summary.games, 1);
  assert.equal(aggregate(fixture(), {cohort: 'either', map: 'expanse', team: 'a'}).summary.teamObservations, 1);
  const result = aggregate(fixture(), {map: 'missing'});
  assert.equal(result.summary.games, 0);
  assert.equal(result.champions[0].winRate, null);
  assert.equal(result.items[0].buyRate, null);
  assert.equal(result.summary.start, null);
  assert.deepEqual(result.synergies, []);
});

test('actual series Elo movement is apportioned, with known coverage and net wins', () => {
  const result = aggregate(fixture(), {cohort: 'either', minGames: '0'});
  const cassi = result.champions.find(row => row.id === 3), stellar = result.champions.find(row => row.id === 5);
  assert.deepEqual([cassi.eloGained, cassi.eloAppearances, cassi.eloPerAppearance, cassi.netWins], [4, 2, 2, 0]);
  assert.deepEqual([stellar.eloGained, stellar.eloAppearances, stellar.eloPerAppearance], [0, 2, 0]);
  const unranked = aggregate(fixture(), {kind: 'unranked'}).champions.find(row => row.id === 3);
  assert.deepEqual([unranked.eloGained, unranked.eloAppearances, unranked.eloPerAppearance], [null, 0, null]);
  const unknown = fixture();
  unknown.matches[0].teams.forEach(side => { side.eloDelta = null; });
  assert.equal(aggregate(unknown).champions[0].eloGained, null);
  assert.equal(aggregate(fixture(), {minGames: '10000'}).champions.find(row => row.id === 5).games, 2);
});

test('teammate lift and opponent matchups use baseline rates and deduplicate types', () => {
  const data = fixture(), result = aggregate(data);
  assert.equal(result.synergies.length, 1);
  assert.deepEqual(result.synergies[0], {championA: {id: 3, name: 'Cassi'}, championB: {id: 5, name: 'Stellar'}, games: 1, wins: 1, winRate: 100, expectedWinRate: 75, lift: 25});
  const mirror = result.counters.find(row => row.champion.id === 5 && row.opponent.id === 5);
  assert.deepEqual([mirror.games, mirror.wins, mirror.winRate, mirror.expectedWinRate, mirror.lift], [2, 1, 50, 50, 0]);
  data.matches[0].teams[0].champions.push(champion(5));
  const duplicate = aggregate(data);
  assert.equal(duplicate.synergies[0].games, 1);
  assert.equal(duplicate.counters.find(row => row.champion.id === 5 && row.opponent.id === 5).games, 2);
  const onlyStellar = aggregate(data, {champion: '5', outcome: 'losses'});
  assert.ok(onlyStellar.counters.every(row => row.champion.id === 5));
  assert.deepEqual(onlyStellar.synergies, duplicate.synergies);
  const absent = aggregate(fixture(), {cohort: 'either', champion: '3'}).counters.find(row => row.opponent.id === 4);
  assert.deepEqual([absent.games, absent.expectedWinRate, absent.lift], [1, null, null]);
});

test('query validation and input immutability', () => {
  for (const query of [{top: '0'}, {top: '1.5'}, {top: '10001'}, {minGames: '-1'}, {minGames: '1.5'}, {minGames: null}, {minGames: true}, {minGames: 2.5}, {kind: 'typo'}, {champion: '999'}, {cohort: 'typo'}, {outcome: 'typo'}]) {
    assert.throws(() => aggregate(fixture(), query));
  }
  const data = fixture(), before = JSON.stringify(data);
  validateDataset(data);
  aggregate(data);
  assert.equal(JSON.stringify(data), before);
  const invalid = fixture();
  invalid.matches[0].teams[0].champions[0].rawItemBuys = [];
  assert.throws(() => validateDataset(invalid));
  assert.throws(() => validateDataset({meta: {}, matches: []}));
});

test('optional parity with the independently implemented Python aggregation and local archive', {skip: !process.env.TRACKER_PARITY_DIR}, () => {
  const directory = process.env.TRACKER_PARITY_DIR;
  const archive = JSON.parse(readFileSync(new URL('data.json', `file:///${directory.replaceAll('\\', '/')}/`), 'utf8'));
  validateDataset(archive);
  const queries = [{}, {cohort: 'either'}, {top: '10000', kind: 'all', outcome: 'all'}, {champion: '5'}, {champion: '3', outcome: 'losses'},
    {top: '1', cohort: 'either'}, {kind: 'unranked'}, {map: archive.matches[0].map, cohort: 'either'},
    {top: '10000', team: archive.matches[0].teams[0].id, kind: 'all'}, {map: 'missing'}, {minGames: '0'}, {minGames: '10000'}];
  const samples = [fixture(), archive];
  const python = 'import sys,json;sys.path.insert(0,sys.argv[1]);import app;p=json.load(sys.stdin);json.dump([[app.aggregate(d,q) for q in p["queries"]] for d in p["samples"]],sys.stdout,allow_nan=False)';
  const expected = JSON.parse(execFileSync(process.env.TRACKER_PARITY_PYTHON || 'python', ['-c', python, directory], {
    input: JSON.stringify({samples, queries}), encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
    env: {...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8'}
  }));
  for (let sample = 0; sample < samples.length; sample++) for (let query = 0; query < queries.length; query++) {
    assert.deepEqual(aggregate(samples[sample], queries[query]), expected[sample][query], `sample ${sample}, query ${JSON.stringify(queries[query])}`);
  }
});

test('static snapshot reload coalesces checks and retains prior data on every failure', async () => {
  let mode = 'success', calls = 0, fetchedURL, fetchedOptions;
  const data = fixture();
  const source = createSnapshotSource({url: 'https://example.com/71319486889459735160/data.json', now: () => 1234,
    validate: validateDataset, fetcher: async (url, options) => {
      calls++;
      fetchedURL = url;
      fetchedOptions = options;
      if (mode === 'network') throw new Error('offline');
      return {ok: mode !== 'http', status: 503, json: async () => {
        if (mode === 'json') throw new Error('truncated');
        return mode === 'invalid' ? {matches: []} : data;
      }};
    }});
  const first = source.reload(), second = source.reload();
  assert.strictEqual(first, second);
  assert.strictEqual(await first, data);
  assert.equal(calls, 1);
  assert.equal(fetchedURL, 'https://example.com/71319486889459735160/data.json?v=1234');
  assert.equal(fetchedOptions.credentials, 'omit');
  assert.equal(fetchedOptions.cache, 'no-store');
  for (mode of ['network', 'http', 'json', 'invalid']) {
    await assert.rejects(source.reload());
    assert.strictEqual(source.current, data);
  }
});

test('assets resolve at site root and a GitHub Pages project prefix, with safe DOM APIs', () => {
  const html = readFileSync(new URL('./web/index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('./web/app.js', import.meta.url), 'utf8');
  const css = readFileSync(new URL('./web/style.css', import.meta.url), 'utf8');
  const assets = [...html.matchAll(/(?:src|href)="(\.\/[^"#]+\.(?:js|css))"/g)].map(match => match[1]);
  assert.deepEqual(assets, ['./style.css', './stats.js', './snapshot.js', './app.js']);
  for (const base of ['https://example.com/', 'https://example.com/71319486889459735160/']) for (const asset of assets) {
    assert.ok(new URL(asset, base).href.startsWith(base));
    assert.ok(existsSync(new URL(`./web/${asset.slice(2)}`, import.meta.url)));
  }
  assert.doesNotMatch(html, /(?:src|href)="\/(?!\/)/);
  assert.doesNotMatch(html, /<(?:script|link)[^>]+(?:src|href)="https?:/);
  assert.doesNotMatch(css, /@import|url\(\s*["']?https?:/);
  assert.doesNotMatch(app, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\/api\//);
  assert.match(app, /setInterval\(reloadData, 300000\)/);
  assert.match(html, /actions\/workflows\/replay-sync\.yml/);
});
