import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';
import test from 'node:test';
import { deriveSummary, readChampion, readSummary, replayVersion, validateSummary } from './replay_summary.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, 'fixtures', 'replay-summary');
const engineOptions = process.env.TRACKER_ENGINE_DIR ? { engineDir: process.env.TRACKER_ENGINE_DIR } : {};
const fixture = name => ({
  ...JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), 'utf8')),
  replay: gunzipSync(readFileSync(join(FIXTURES, `${name}.repl26a.gz`))),
});
const legacy = fixture('legacy');
const recent = fixture('recent');

test('replaying a legacy public match exactly reproduces every inline API statistic', () => {
  assert.deepEqual(deriveSummary(legacy.replay, legacy.match, engineOptions), legacy.statistics);
});

test('new replay format without inline statistics agrees with public results and draft', () => {
  assert.equal(recent.match.statistics, undefined);
  assert.equal(replayVersion(recent.replay), '0.0.2');
  const statistics = deriveSummary(recent.replay, recent.match, engineOptions);
  assert.equal(statistics.round, 539);
  assert.equal(statistics.outcome, 'teamA');
  assert.deepEqual(statistics.teams.map(team => team.submittedGems), [1010, 564]);
  assert.deepEqual(statistics.teams.map(team => team.draftPicks), [[8, 4, 3, 0], [4, 3, 10, 8]]);
  assert.deepEqual(statistics.champions.map(champion => champion.type), [9, 5, 3, 4, 0, 5, 3, 4, 11, 9]);
  assert.ok(statistics.champions.some(champion => champion.spentGold > 0));
  assert.equal(statistics.champions.reduce((sum, champion) => sum + champion.submittedGems, 0), 1574);
});

test('the engine rejects a truncated replay instead of generating partial statistics', () => {
  assert.throws(() => deriveSummary(recent.replay.subarray(0, 50), recent.match, engineOptions), /load replay/);
});

test('a valid replay ending after draft cannot masquerade as a completed match', () => {
  const incomplete = gunzipSync(readFileSync(join(FIXTURES, 'incomplete.repl26a.gz')));
  assert.throws(() => deriveSummary(incomplete, legacy.match, engineOptions), /before the match finished/);
});

test('replay header rejects unknown or mismatched engine formats before loading an engine', () => {
  assert.throws(() => replayVersion(Buffer.from('invalid')), /header/);
  const unknown = Buffer.from(recent.replay);
  unknown[13] = 3;
  assert.throws(() => replayVersion(unknown), /Unsupported replay version/);
  unknown[13] = 1;
  assert.throws(() => replayVersion(unknown), /Unsupported replay version/);
});

test('published rounds, winner, scores and draft are independent validation checks', () => {
  const statistics = legacy.statistics;
  for (const [field, change, message] of [
    ['round', match => { match.rounds += 1; }, /round/],
    ['winner', match => { match.sides[0].result = 'loss'; match.sides[1].result = 'win'; }, /outcome/],
    ['score', match => { match.sides[0].score += 1; }, /score/],
    ['draft', match => { match.sides[0].draft = [0, 0, 0, 0]; }, /draft/],
  ]) {
    const match = structuredClone(legacy.match);
    change(match);
    assert.throws(() => validateSummary(statistics, match), message, field);
  }
  const badRoster = structuredClone(statistics);
  badRoster.champions[0].type = 0;
  assert.throws(() => validateSummary(badRoster, legacy.match), /roster/);
});

test('a platform coinflip preserves the simulated draw and still validates scores', () => {
  const statistics = { ...legacy.statistics, outcome: 'draw' };
  assert.throws(() => validateSummary(statistics, legacy.match), /outcome/);
  assert.equal(validateSummary(statistics, { ...legacy.match, coinflip: true }).outcome, 'draw');
  const invalid = structuredClone(legacy.match);
  invalid.coinflip = true;
  invalid.sides[0].score += 1;
  assert.throws(() => validateSummary(statistics, invalid), /score/);
  assert.throws(() => validateSummary({ ...statistics, outcome: 'teamB' },
    { ...legacy.match, coinflip: true }), /outcome/);
});

test('summary ABI rejects ongoing outcomes and invalid draft bounds', () => {
  const memory = new DataView(new ArrayBuffer(80));
  assert.throws(() => readSummary(memory, 0), /completed/);
  memory.setUint32(4, 1, true);
  memory.setUint32(12, 2, true);
  memory.setUint32(32, 6, true);
  assert.throws(() => readSummary(memory, 0), /draft size/);
});

test('champion ABI rejects unsafe integer counts and nonfinite metrics', () => {
  const memory = new DataView(new ArrayBuffer(664));
  memory.setBigUint64(24, 2n ** 63n, true);
  assert.throws(() => readChampion(memory, 0), /Unsafe/);
  memory.setBigUint64(24, 0n, true);
  memory.setFloat64(616, NaN, true);
  assert.throws(() => readChampion(memory, 0), /Invalid replay statistic/);
});
