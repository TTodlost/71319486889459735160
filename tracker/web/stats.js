/* Pure aggregation shared by the static dashboard and offline Node checks. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BattlecodeStats = factory();
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';

  const percentage = (numerator, denominator) => denominator ? 100 * numerator / denominator : null;
  const sum = (values, get) => values.reduce((total, value) => total + Number(get(value)), 0);
  // Python sorts by Unicode code point, independently of the browser's locale.
  function compareText(left, right) {
    const a = Array.from(left), b = Array.from(right);
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      const difference = a[i].codePointAt(0) - b[i].codePointAt(0);
      if (difference) return difference;
    }
    return a.length - b.length;
  }
  function integer(value) {
    if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value);
    if (typeof value !== 'string' || !/^[+-]?\d+(?:_\d+)*$/.test(value.trim())) throw new Error('Expected an integer');
    return Number(value.trim().replaceAll('_', ''));
  }
  function currentLeaderboard(data) {
    if (!Array.isArray(data.leaderboard)) throw new Error('Current leaderboard snapshot is unavailable');
    const result = new Map();
    for (const row of data.leaderboard) {
      if (!row || typeof row !== 'object' || Array.isArray(row) || typeof row.teamId !== 'string' || !Number.isInteger(row.rank) || row.rank < 1 || result.has(row.teamId) ||
          (row.rating !== undefined && row.rating !== null && (typeof row.rating !== 'number' || !Number.isFinite(row.rating))) ||
          (row.name !== undefined && typeof row.name !== 'string')) throw new Error('Current leaderboard snapshot is invalid');
      result.set(row.teamId, row);
    }
    return result;
  }
  function snapshotCoverage(data) {
    const meta = data.meta || {};
    const times = (data.matches || []).map(match => match.startedAt).sort(compareText);
    return {
      firstMatchAt: meta.matchStartAt ?? times[0] ?? null,
      latestMatchAt: meta.matchEndAt ?? times.at(-1) ?? null,
      leaderboardUpdatedAt: meta.leaderboardUpdatedAt ?? null,
      windowStartAt: meta.sampleStartAt ?? null,
      windowEndAt: meta.sampleEndAt ?? null,
      windowHours: meta.sampleWindowHours ?? null,
      builtAt: meta.updatedAt ?? null,
      artifactCreatedAt: meta.artifactCreatedAt ?? null,
      collectionTop: meta.collectionTop ?? null,
      seriesPerTeam: meta.seriesPerTeam ?? null,
      games: (data.matches || []).length
    };
  }
  function aggregate(data, query = {}) {
    const top = integer(query.top ?? '20');
    if (top < 1 || top > 10000) throw new Error('top must be between 1 and 10000');
    if (Number.isInteger(data.meta?.collectionTop) && data.meta.collectionTop > 0 && top > data.meta.collectionTop) throw new Error(`This snapshot collects replays for only the current top ${data.meta.collectionTop} teams`);
    const leaderboard = currentLeaderboard(data);
    const minimum = Object.hasOwn(query, 'minGames') ? query.minGames : '10';
    if (!['number', 'string'].includes(typeof minimum) || (typeof minimum === 'number' && !Number.isInteger(minimum))) throw new Error('minGames must be an integer between 0 and 10000');
    const minGames = integer(minimum);
    if (minGames < 0 || minGames > 10000) throw new Error('minGames must be between 0 and 10000');
    const filters = {top, minGames, cohort: query.cohort ?? 'both', kind: query.kind ?? 'ranked',
      champion: query.champion ?? 'all', map: query.map ?? 'all', team: query.team ?? 'all', outcome: query.outcome ?? 'wins'};
    for (const [key, allowed] of [['cohort', ['both', 'either']], ['kind', ['ranked', 'unranked', 'all']], ['outcome', ['wins', 'losses', 'all']]]) {
      if (!allowed.includes(filters[key])) throw new Error(`Invalid ${key} filter`);
    }
    const championId = filters.champion === 'all' ? null : integer(filters.champion);
    if (championId !== null && !data.champions.some(champion => champion.id === championId)) throw new Error('Unknown champion');

    const games = [], observations = [];
    for (const match of data.matches) {
      if (filters.kind !== 'all' && match.kind !== filters.kind) continue;
      if (filters.map !== 'all' && match.map !== filters.map) continue;
      let eligible = match.teams.filter(team => leaderboard.has(team.id) && leaderboard.get(team.id).rank <= top);
      if ((filters.cohort === 'both' && eligible.length !== 2) || !eligible.length) continue;
      if (filters.team !== 'all') eligible = eligible.filter(team => team.id === filters.team);
      if (!eligible.length) continue;
      games.push(match);
      observations.push(...eligible.map(team => ({match, team})));
    }
    const allChampions = observations.flatMap(({match, team}) => team.champions.map(champion => ({match, team, champion})));
    const championRows = data.champions.map(definition => {
      const selected = allChampions.filter(row => row.champion.type === definition.id);
      const wins = sum(selected, row => row.team.win);
      const rated = selected.filter(row => row.match.kind === 'ranked' && typeof row.team.eloDelta === 'number' && Number.isFinite(row.team.eloDelta) && Number.isInteger(row.match.seriesMaps) && row.match.seriesMaps > 0);
      const eloGained = rated.length ? sum(rated, row => row.team.eloDelta / row.match.seriesMaps) : null;
      return {...definition, games: selected.length, wins, losses: selected.length - wins,
        netWins: 2 * wins - selected.length, eloGained, eloAppearances: rated.length,
        eloPerAppearance: rated.length ? eloGained / rated.length : null,
        winRate: percentage(wins, selected.length), pickRate: percentage(selected.length, observations.length),
        avgGold: selected.length ? sum(selected, row => row.champion.spentGold) / selected.length : null};
    }).sort((a, b) => b.games - a.games || compareText(a.name, b.name));
    const selectedChampions = allChampions.filter(row => championId === null || row.champion.type === championId);
    const detailChampions = selectedChampions.filter(row => filters.outcome === 'all' || (filters.outcome === 'wins') === Boolean(row.team.win));
    const denominator = detailChampions.length;
    const itemRows = data.items.map(definition => {
      const i = definition.id;
      const buys = sum(detailChampions, row => row.champion.rawItemBuys[i] > 0);
      const final = sum(detailChampions, row => row.champion.items[i] > 0);
      const late = sum(detailChampions, row => row.champion.lateItemBuys[i] > 0);
      // Item-conditioned win rate always uses both outcomes, as champion rates do.
      const withItem = selectedChampions.filter(row => row.champion.rawItemBuys[i] || row.champion.items[i]);
      return {...definition, buyGames: buys, buyRate: definition.purchasable ? percentage(buys, denominator) : null,
        finalGames: final, finalRate: percentage(final, denominator), lateBuyGames: late,
        lateBuyRate: definition.purchasable ? percentage(late, denominator) : null,
        units: sum(detailChampions, row => row.champion.rawItemBuys[i]),
        lateUnits: sum(detailChampions, row => row.champion.lateItemBuys[i]),
        avgFinalCount: denominator ? sum(detailChampions, row => row.champion.items[i]) / denominator : null,
        withItemGames: withItem.length, withItemWins: sum(withItem, row => row.team.win),
        winRate: percentage(sum(withItem, row => row.team.win), withItem.length)};
    }).sort((a, b) => b.finalGames - a.finalGames || b.buyGames - a.buyGames || compareText(a.name, b.name));
    const consumables = [[0, 'TP Crystal'], [2, 'Altar Lock']].map(([i, name]) => {
      const buys = sum(detailChampions, row => row.champion.consumablesBought[i] > 0);
      const late = sum(detailChampions, row => row.champion.lateConsumableBuys[i] > 0);
      return {id: i, name, buyGames: buys, buyRate: percentage(buys, denominator), lateBuyGames: late,
        lateBuyRate: percentage(late, denominator), units: sum(detailChampions, row => row.champion.consumablesBought[i]),
        lateUnits: sum(detailChampions, row => row.champion.lateConsumableBuys[i])};
    });
    const builds = new Map(), names = new Map(data.items.map(item => [item.id, item.name]));
    for (const {team, champion} of detailChampions) {
      const items = champion.items.map((count, i) => count ? `${count}× ${names.get(i)}` : '').filter(Boolean).join(' + ') || 'No items';
      const label = (filters.champion === 'all' ? `${champion.name}: ` : '') + items;
      if (!builds.has(label)) builds.set(label, []);
      builds.get(label).push(team.win);
    }
    const buildRows = [...builds].map(([label, wins]) => ({label, games: wins.length, wins: sum(wins, value => value), winRate: percentage(sum(wins, value => value), wins.length)}))
      .sort((a, b) => b.games - a.games || compareText(a.label, b.label));
    const teams = new Map();
    for (const {team} of observations) {
      if (!teams.has(team.id)) teams.set(team.id, []);
      teams.get(team.id).push(team);
    }
    const teamRows = [...teams].map(([id, values]) => ({id, name: leaderboard.get(id).name ?? values[0].name, games: values.length,
      currentRank: leaderboard.get(id).rank, currentElo: leaderboard.get(id).rating ?? null,
      wins: sum(values, team => team.win), winRate: percentage(sum(values, team => team.win), values.length)}))
      .sort((a, b) => b.games - a.games || compareText(a.name, b.name));
    const recent = [...games].sort((a, b) => compareText(b.startedAt, a.startedAt)).slice(0, 50).map(match => ({
      id: match.id, url: match.url, map: match.map, startedAt: match.startedAt, rounds: match.rounds,
      teams: match.teams.map(team => ({name: team.name, rank: team.rank, rankMin: team.rankMin, rankMax: team.rankMax, elo: team.elo, win: team.win,
        currentRank: leaderboard.get(team.id)?.rank ?? null, currentElo: leaderboard.get(team.id)?.rating ?? null}))
    }));
    const allTeams = new Map();
    for (const match of data.matches) for (const team of match.teams) allTeams.set(team.id, team.name);
    for (const [id, row] of leaderboard) if (row.name !== undefined) allTeams.set(id, row.name);
    const times = games.map(match => match.startedAt).sort(compareText);
    const definitions = new Map(data.champions.map(champion => [champion.id, {id: champion.id, name: champion.name}]));
    const baseline = new Map(championRows.map(champion => [champion.id, champion.winRate]));
    const pairs = new Map(), opponents = new Map();
    const uniqueTypes = team => [...new Set(team.champions.map(champion => champion.type))].sort((a, b) => a - b);
    const record = (bucket, a, b, win) => {
      const key = `${a},${b}`;
      if (!bucket.has(key)) bucket.set(key, {a, b, games: 0, wins: 0});
      const row = bucket.get(key);
      row.games++;
      row.wins += Number(win);
    };
    for (const {match, team} of observations) {
      const types = uniqueTypes(team);
      for (let i = 0; i < types.length; i++) for (let j = i + 1; j < types.length; j++) {
        if (championId === null || types[i] === championId || types[j] === championId) record(pairs, types[i], types[j], team.win);
      }
      for (const enemy of match.teams.filter(candidate => candidate.id !== team.id)) {
        for (const a of types) for (const b of uniqueTypes(enemy)) {
          if (championId === null || a === championId) record(opponents, a, b, team.win);
        }
      }
    }
    const synergies = [...pairs.values()].map(({a, b, games: pairGames, wins}) => {
      const expectedWinRate = (baseline.get(a) + baseline.get(b)) / 2;
      const winRate = percentage(wins, pairGames);
      return {championA: definitions.get(a), championB: definitions.get(b), games: pairGames, wins, winRate, expectedWinRate, lift: winRate - expectedWinRate};
    }).sort((a, b) => b.games - a.games || a.championA.id - b.championA.id || a.championB.id - b.championB.id);
    const counters = [...opponents.values()].map(({a, b, games: counterGames, wins}) => {
      const first = baseline.get(a), second = baseline.get(b);
      const expectedWinRate = first === null || second === null ? null : (first + (100 - second)) / 2;
      const winRate = percentage(wins, counterGames);
      return {champion: definitions.get(a), opponent: definitions.get(b), games: counterGames, wins, winRate, expectedWinRate, lift: expectedWinRate === null ? null : winRate - expectedWinRate};
    }).sort((a, b) => b.games - a.games || a.champion.id - b.champion.id || a.opponent.id - b.opponent.id);
    return {meta: data.meta, filters,
      summary: {games: games.length, teamObservations: observations.length, uniqueTeams: teams.size, start: times[0] ?? null, end: times.at(-1) ?? null},
      champions: championRows, items: itemRows, consumables, builds: buildRows.slice(0, 25), teamRows, recentMatches: recent, synergies, counters,
      options: {maps: [...new Set(data.matches.map(match => match.map))].sort(compareText),
        teams: [...allTeams].sort((a, b) => compareText(a[1], b[1])).map(([id, name]) => ({id, name})), champions: data.champions},
      detail: {observations: denominator, championName: data.champions.find(champion => String(champion.id) === filters.champion)?.name ?? 'All champions'}};
  }

  // Reject an incomplete/invalid deployment before replacing the last usable data.
  function validateDataset(data) {
    const fail = () => { throw new Error('The published snapshot has an invalid data format.'); };
    const object = value => value && typeof value === 'object' && !Array.isArray(value);
    const finite = value => typeof value === 'number' && Number.isFinite(value);
    if (!object(data) || !object(data.meta) || !Array.isArray(data.champions) || !Array.isArray(data.items) || !Array.isArray(data.matches)) fail();
    currentLeaderboard(data);
    const championIds = new Set(), itemIds = new Set();
    for (const champion of data.champions) {
      if (!object(champion) || !Number.isInteger(champion.id) || champion.id < 0 || typeof champion.name !== 'string' || championIds.has(champion.id)) fail();
      championIds.add(champion.id);
    }
    for (const item of data.items) {
      if (!object(item) || !Number.isInteger(item.id) || item.id < 0 || typeof item.name !== 'string' || typeof item.purchasable !== 'boolean' || itemIds.has(item.id)) fail();
      itemIds.add(item.id);
    }
    const width = Math.max(-1, ...itemIds) + 1;
    const counters = (values, length) => Array.isArray(values) && values.length >= length && values.every(value => Number.isInteger(value) && value >= 0);
    for (const match of data.matches) {
      if (!object(match) || typeof match.id !== 'string' || typeof match.url !== 'string' || typeof match.map !== 'string' || !['ranked', 'unranked'].includes(match.kind) ||
          typeof match.startedAt !== 'string' || !Number.isFinite(Date.parse(match.startedAt)) || !finite(match.rounds) || !Array.isArray(match.teams) || match.teams.length !== 2) fail();
      for (const team of match.teams) {
        if (!object(team) || typeof team.id !== 'string' || typeof team.name !== 'string' || typeof team.win !== 'boolean' || typeof team.verified !== 'boolean' ||
            (team.verified && (!finite(team.rankMax) || team.rankMax < 1)) || !Array.isArray(team.champions)) fail();
        for (const champion of team.champions) {
          if (!object(champion) || !championIds.has(champion.type) || typeof champion.name !== 'string' || !finite(champion.spentGold) ||
              !counters(champion.items, width) || !counters(champion.rawItemBuys, width) || !counters(champion.lateItemBuys, width) ||
              !counters(champion.consumablesBought, 3) || !counters(champion.lateConsumableBuys, 3) ||
              champion.items.some((count, i) => count && !itemIds.has(i))) fail();
        }
      }
    }
    return data;
  }
  return {aggregate, validateDataset, snapshotCoverage};
});
