/** Play public replays with the competition's pinned visualiser engine. */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { WASI } from 'node:wasi';

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE_DIR = join(HERE, '.cache', 'engine', '2.2.0');
const ENGINES = {
  '0.0.1': 'ce302dc6666ae43592f6936e572bc4baca92a345bff13ef7f986609ac666a0dc',
  '0.0.2': 'e123b59d9dea198762ab7a04db9b3cb6d06dfa991088e41c24750fc4cb88b947',
};
const modules = new Map();
const OUTCOMES = ['ongoing', 'teamA', 'teamB', 'draw'];
const DRAFT_PAIRS = [[0], [1], [2], [3, 4], [5], [6], [7], [8], [9], [10], [11], [12]];

function safeInteger(value) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) throw Error('Unsafe replay statistics integer');
  return number;
}

/** Public Game_Statistics_GetChampion ABI, checked against inline API statistics. */
export function readChampion(view, pointer) {
  const u32 = offset => view.getUint32(pointer + offset, true);
  const u64 = offset => safeInteger(view.getBigUint64(pointer + offset, true));
  const array = (offset, count, step, read) => Array.from({ length: count }, (_, i) => read(offset + i * step));
  const result = {
    champion: u32(0), team: u32(4), type: u32(8), kills: u32(12), deaths: u32(16), assists: u32(20),
    earnedGold: u64(24), collectedGems: u64(32), submittedGems: u64(40), spentGold: u64(48),
    gold: u32(56), gems: u32(60), itemsBought: array(64, 42, 8, u64),
    consumablesBought: array(400, 4, 8, u64), items: array(432, 42, 4, u32),
    consumables: array(600, 4, 4, u32),
  };
  for (const [i, name] of ['monsterDamageDone', 'monsterDamageTaken', 'championDamageDone',
    'championDamageTaken', 'healingDone', 'shieldingDone'].entries()) {
    const value = view.getFloat64(pointer + 616 + i * 8, true);
    if (!Number.isFinite(value) || value < 0) throw Error(`Invalid replay statistic ${name}`);
    result[name] = value;
  }
  return result;
}

/** Public Game_Statistics_GetSummary ABI is identical in both pinned engines. */
export function readSummary(view, pointer) {
  const u32 = offset => view.getUint32(pointer + offset, true);
  const packed = u32(0);
  const outcome = OUTCOMES[u32(4)];
  const champions = u32(12);
  if (!outcome || outcome === 'ongoing' || champions < 2 || champions > 12) {
    throw Error('Replay has no completed match statistics');
  }
  const teams = [0, 1].map(team => {
    const count = u32(32 + team * 4);
    if (count < 1 || count > 5) throw Error('Invalid replay draft size');
    const draftPicks = Array.from({ length: count }, (_, i) => u32(40 + team * 20 + i * 4));
    if (draftPicks.some(pick => !DRAFT_PAIRS[pick])) throw Error('Invalid replay draft choice');
    return { team, draftPicks, submittedGems: safeInteger(view.getBigUint64(pointer + 16 + team * 8, true)) };
  });
  return {
    engineVersionPacked: packed,
    engineVersion: [packed & 255, (packed >>> 8) & 255, (packed >>> 16) & 255].join('.'),
    schemaVersion: 2, outcome, round: u32(8), teams, champions,
  };
}

export function validateSummary(statistics, match) {
  if (!match || !Number.isInteger(match.rounds) || statistics.round !== match.rounds) {
    throw Error(`Replay summary round ${statistics.round} does not match published match rounds`);
  }
  const sides = ['a', 'b'].map((side, index) =>
    match.sides?.find(row => row.side === side) ?? match.sides?.[index]);
  if (!sides.every(Boolean)) throw Error('Published match is missing its sides');
  const winner = sides.findIndex(side => side.result === 'win');
  const expectedOutcome = winner >= 0 ? ['teamA', 'teamB'][winner]
    : sides.every(side => side.result === 'draw') ? 'draw' : undefined;
  // A platform coinflip settles a simulated draw. Preserve that draw so the
  // collector can apply its existing exclusion of games without an engine winner.
  const coinflipDraw = match.coinflip === true && statistics.outcome === 'draw';
  if (!expectedOutcome || (statistics.outcome !== expectedOutcome && !coinflipDraw)) {
    throw Error('Replay summary outcome does not match published match winner');
  }
  for (const [team, side] of sides.entries()) {
    if (!Number.isSafeInteger(side.score) || statistics.teams[team].submittedGems !== side.score) {
      throw Error(`Replay summary team ${team} score does not match published match`);
    }
    if (side.draft?.length && JSON.stringify(side.draft.map(Number)) !== JSON.stringify(statistics.teams[team].draftPicks)) {
      throw Error(`Replay summary team ${team} draft does not match published match`);
    }
  }
  const expected = statistics.teams.flatMap(team => team.draftPicks.flatMap(pick =>
    DRAFT_PAIRS[pick].map(type => ({ team: team.team, type }))));
  if (statistics.champions.length !== expected.length || statistics.champions.some((champion, i) =>
    champion.champion !== i || champion.team !== expected[i].team || champion.type !== expected[i].type)) {
    throw Error('Replay summary champion roster does not match its draft');
  }
  return statistics;
}

export function replayVersion(replay) {
  if (replay.length < 14 || !replay.subarray(0, 8).equals(Buffer.from('a62per\0\0'))) {
    throw Error('Invalid replay header');
  }
  const fileVersion = [...replay.subarray(8, 11)].join('.');
  const engineVersion = [...replay.subarray(11, 14)].join('.');
  if (!ENGINES[engineVersion] || fileVersion !== engineVersion) {
    throw Error(`Unsupported replay version ${fileVersion} / ${engineVersion}`);
  }
  return engineVersion;
}

export function deriveSummary(replay, match, { engineDir = ENGINE_DIR } = {}) {
  const version = replayVersion(replay);
  const path = join(engineDir, `v${version.replaceAll('.', '_')}.wasm`);
  let module = modules.get(path);
  if (!module) {
    const bytes = readFileSync(path);
    if (createHash('sha256').update(bytes).digest('hex') !== ENGINES[version]) {
      throw Error(`Unexpected competition engine hash for ${version}`);
    }
    module = new WebAssembly.Module(bytes);
    modules.set(path, module);
  }
  const wasi = new WASI({ version: 'preview1', args: [], env: {}, preopens: {}, returnOnExit: true });
  const instance = new WebAssembly.Instance(module, wasi.getImportObject());
  wasi.initialize(instance);
  const engine = instance.exports;
  const allocations = [];
  const allocate = size => {
    const pointer = engine.Engine_Malloc(size);
    if (!pointer) throw Error('Competition engine allocation failed');
    allocations.push(pointer);
    return pointer;
  };
  const view = () => new DataView(engine.memory.buffer);
  const check = (status, operation) => {
    if (status) throw Error(`Competition engine ${operation} failed with status ${status}`);
  };
  let handle;
  try {
    const bytes = allocate(replay.length);
    new Uint8Array(engine.memory.buffer, bytes, replay.length).set(replay);
    const output = allocate(1024);
    check(engine.Game_FromReplayFileBytes(bytes, replay.length, output), 'load replay');
    handle = view().getBigUint64(output, true);
    check(engine.Game_Visualiser_Timeline_GetMain(handle, output), 'get timeline');
    const timeline = view().getBigUint64(output, true);
    check(engine.Game_Visualiser_Timeline_Info(handle, timeline, output, output + 8, output + 16), 'read timeline');
    const size = view().getUint32(output, true);
    if (!size || size > 10_000_000) throw Error('Invalid replay action count');
    check(engine.Game_Runner_SetCheckpointsEnabled(handle, 0), 'disable timeline checkpoints');
    check(engine.Game_Visualiser_Timeline_Seek(handle, timeline, size, 0), 'play replay');
    check(engine.Game_Runner_GetState(handle, output), 'read final state');
    if (view().getUint32(output, true) !== 2) throw Error('Replay ended before the match finished');
    check(engine.Game_Statistics_GetSummary(handle, output), 'read summary');
    const statistics = readSummary(view(), output);
    if (statistics.engineVersion !== version) throw Error('Replay summary engine version mismatch');
    const count = statistics.champions;
    statistics.champions = Array.from({ length: count }, (_, index) => {
      check(engine.Game_Statistics_GetChampion(handle, index, output), 'read champion');
      return readChampion(view(), output);
    });
    // Bot failures are transport metadata, rather than simulation state.
    statistics.botFaults = ['a', 'b'].map((side, index) => {
      const row = match.sides?.find(row => row.side === side) ?? match.sides?.[index];
      return row?.botError ? 1 : 0;
    });
    return validateSummary(statistics, match);
  } finally {
    if (handle !== undefined) engine.Game_Destroy(handle);
    for (const pointer of allocations.reverse()) engine.Engine_Free(pointer);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const request = JSON.parse(readFileSync(0, 'utf8'));
    if (typeof request.replayBase64 !== 'string' || request.replayBase64.length > 134_217_728) {
      throw Error('Missing or oversized replay payload');
    }
    const replay = Buffer.from(request.replayBase64, 'base64');
    if (replay.toString('base64') !== request.replayBase64) throw Error('Invalid base64 replay payload');
    process.stdout.write(JSON.stringify(deriveSummary(replay, request.match)) + '\n');
  } catch (error) {
    process.stderr.write(`Replay summary: ${error.message}\n`);
    process.exitCode = 1;
  }
}
