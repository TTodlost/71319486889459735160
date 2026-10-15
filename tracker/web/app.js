(() => {
  'use strict';

  const defaults = {top: '20', minGames: '10', cohort: 'both', kind: 'ranked', champion: 'all', map: 'all', team: 'all', outcome: 'wins'};
  const state = {filters: {...defaults}, data: null, sort: 'eloGained', direction: -1, refreshing: false, lastCheck: null};
  const source = BattlecodeSnapshot.createSnapshotSource({url: new URL('./data.json', location.href).href, validate: BattlecodeStats.validateDataset});
  const $ = id => document.getElementById(id);
  const number = value => Number.isFinite(Number(value)) && value !== null && value !== '' ? Number(value) : null;
  const count = value => number(value) === null ? '—' : new Intl.NumberFormat().format(Number(value));
  const rate = value => number(value) === null ? '—' : `${Number(value).toFixed(1)}%`;
  const signed = value => number(value) === null ? '—' : `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(1)}`;
  const compactDate = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric'}).format(date) : 'Date unavailable';
  };
  const fullDate = value => {
    const date = new Date(value);
    return value && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat(undefined, {month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short'}).format(date) : 'Date unavailable';
  };
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  };
  const text = (id, value) => { $(id).textContent = value; };
  const humanize = value => String(value ?? '').replaceAll('_', ' ').toLowerCase().replace(/\b\w/g, letter => letter.toUpperCase()).replace(/\bOf\b/g, 'of');
  const outcomeName = () => ({wins: 'wins', losses: 'losses', all: 'all results'}[state.filters.outcome]);
  const empty = (body, columns, message) => {
    const row = make('tr');
    const cell = make('td', 'empty-cell', message);
    cell.colSpan = columns;
    row.append(cell);
    body.replaceChildren(row);
  };
  const initials = name => String(name ?? '?').split(/[\s_]+/).filter(Boolean).slice(0, 2).map(word => word[0]).join('').toUpperCase();
  function avatar(champion) {
    const element = make('span', 'avatar', initials(champion.name));
    const id = Number(champion.id);
    element.style.setProperty('--avatar-hue', String(Number.isFinite(id) ? (id * 29 + 115) % 360 : 145));
    element.setAttribute('aria-hidden', 'true');
    return element;
  }
  function safeLink(value) {
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }
  function readURL() {
    const query = new URLSearchParams(location.search);
    const next = {...defaults};
    for (const key of Object.keys(defaults)) if (query.has(key)) next[key] = query.get(key);
    const top = Number(next.top);
    next.top = String(Number.isInteger(top) && top >= 1 && top <= 10000 ? top : 20);
    const minGames = Number(next.minGames);
    next.minGames = String(Number.isInteger(minGames) && minGames >= 0 && minGames <= 10000 ? minGames : 10);
    if (!['both', 'either'].includes(next.cohort)) next.cohort = defaults.cohort;
    if (!['ranked', 'unranked', 'all'].includes(next.kind)) next.kind = defaults.kind;
    if (!['wins', 'losses', 'all'].includes(next.outcome)) next.outcome = defaults.outcome;
    if (next.champion !== 'all' && !/^\d+$/.test(next.champion)) next.champion = 'all';
    state.filters = next;
  }
  function ensureOption(select, value, label) {
    if (![...select.options].some(option => option.value === value)) select.append(new Option(label || value, value));
    select.value = value;
  }
  function syncControls() {
    for (const key of ['top', 'minGames', 'cohort', 'kind']) $(`filter-${key}`).value = state.filters[key];
    for (const key of ['champion', 'map', 'team']) ensureOption($(`filter-${key}`), state.filters[key]);
    document.querySelectorAll('input[name="outcome"]').forEach(input => { input.checked = input.value === state.filters.outcome; });
  }
  function updateURL() {
    const query = new URLSearchParams(state.filters);
    history.pushState(null, '', `${location.pathname}?${query.toString()}${location.hash}`);
  }
  function changeFilter(key, value) {
    if (state.filters[key] === String(value)) return;
    state.filters[key] = String(value);
    updateURL();
    syncControls();
    loadStats();
  }
  function updateOptions(options = {}) {
    const fill = (id, entries, first) => {
      const select = $(id);
      select.replaceChildren(new Option(first, 'all'));
      for (const entry of entries || []) {
        const item = typeof entry === 'string' ? {id: entry, name: humanize(entry)} : entry;
        select.append(new Option(String(item.name), String(item.id)));
      }
    };
    fill('filter-map', options.maps, 'All maps');
    fill('filter-team', options.teams, 'All qualifying teams');
    fill('filter-champion', options.champions, 'All champions');
    syncControls();
  }
  function rateBar(value) {
    const wrapper = make('span', `rate-wrap${number(value) !== null && Number(value) < 50 ? ' below' : ''}`);
    wrapper.append(make('span', 'rate-number', rate(value)));
    const track = make('span', 'rate-track');
    track.setAttribute('aria-hidden', 'true');
    const fill = make('span', 'rate-fill');
    fill.style.width = `${Math.min(100, Math.max(0, number(value) || 0))}%`;
    track.append(fill);
    wrapper.append(track);
    return wrapper;
  }
  function renderChampions(champions) {
    const body = $('champion-body');
    const rows = champions.filter(champion => champion.games > 0 && champion.games >= Number(state.filters.minGames)).sort((a, b) => {
      if (state.sort === 'name') return String(a.name).localeCompare(String(b.name)) * state.direction;
      const aValue = number(a[state.sort]);
      const bValue = number(b[state.sort]);
      if (aValue === null && bValue === null) return b.games - a.games || String(a.name).localeCompare(String(b.name));
      if (aValue === null) return 1;
      if (bValue === null) return -1;
      return (aValue - bValue) * state.direction || b.games - a.games || String(a.name).localeCompare(String(b.name));
    });
    body.replaceChildren();
    if (!rows.length) empty(body, 10, 'No champions meet the minimum sample. Lower minimum appearances or broaden the cohort.');
    for (const champion of rows) {
      const selected = String(champion.id) === state.filters.champion;
      const row = make('tr', selected ? 'selected' : '');
      const cell = make('td');
      const identity = make('div', 'champion-cell');
      const button = make('button', 'champion-name', champion.name);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(selected));
      button.setAttribute('aria-label', `Explore ${champion.name} item choices and builds`);
      identity.append(avatar(champion), button);
      cell.append(identity);
      row.append(cell);
      const elo = make('td', 'numeric elo-cell', signed(champion.eloGained));
      elo.append(make('span', 'fraction', `${count(champion.eloAppearances)} / ${count(champion.games)} rated`));
      elo.title = 'Actual team series Elo movement divided by the official number of maps, for each appearance. Shared team movement, not causal credit.';
      row.append(elo, make('td', 'numeric dim', signed(champion.eloPerAppearance)));
      for (const key of ['games', 'wins', 'losses']) row.append(make('td', `numeric${key === 'losses' ? ' dim' : ''}`, count(champion[key])));
      row.append(make('td', 'numeric', `${champion.netWins > 0 ? '+' : ''}${count(champion.netWins)}`));
      const win = make('td');
      win.append(rateBar(champion.winRate));
      row.append(win, make('td', 'numeric', rate(champion.pickRate)), make('td', 'numeric dim', number(champion.avgGold) === null ? '—' : count(Math.round(champion.avgGold))));
      row.addEventListener('click', () => changeFilter('champion', selected ? 'all' : champion.id));
      body.append(row);
    }
    document.querySelectorAll('[data-sort]').forEach(button => {
      const active = button.dataset.sort === state.sort;
      button.closest('th').setAttribute('aria-sort', active ? (state.direction > 0 ? 'ascending' : 'descending') : 'none');
      button.querySelector('.sort-marker').textContent = active ? (state.direction > 0 ? '↑' : '↓') : '';
    });
    const cards = $('champion-cards');
    cards.replaceChildren();
    for (const champion of [...rows].sort((a, b) => (number(b.eloGained) ?? -Infinity) - (number(a.eloGained) ?? -Infinity) || b.games - a.games).slice(0, 4)) {
      const selected = String(champion.id) === state.filters.champion;
      const card = make('button', `champion-card${selected ? ' selected' : ''}`);
      card.type = 'button';
      card.setAttribute('aria-pressed', String(selected));
      card.setAttribute('aria-label', `${champion.name}, ${count(champion.games)} appearances, ${signed(champion.eloGained)} apportioned Elo gained. Explore item choices.`);
      const info = make('span', 'card-info');
      info.append(make('strong', '', champion.name), make('span', '', `${count(champion.games)} appearances`));
      const win = make('span', 'card-rate', signed(champion.eloGained));
      win.append(make('small', '', 'ELO GAINED'));
      card.append(avatar(champion), info, win);
      card.addEventListener('click', () => changeFilter('champion', selected ? 'all' : champion.id));
      cards.append(card);
    }
    cards.hidden = !cards.childElementCount;
  }
  function renderRateCell(value, numerator, denominator, className = '') {
    const cell = make('td', `rate-cell ${className}`);
    cell.append(make('span', 'rate-main', rate(value)), make('span', 'fraction', `${count(numerator)} / ${count(denominator)}`));
    return cell;
  }
  function renderItems(items, denominator) {
    const body = $('item-body');
    body.replaceChildren();
    const meaningful = items.filter(item => Number(item.withItemGames) || Number(item.buyGames) || Number(item.finalGames) || Number(item.lateBuyGames)).sort((a, b) => Number(b.buyRate || 0) - Number(a.buyRate || 0) || Number(b.finalRate || 0) - Number(a.finalRate || 0));
    if (!meaningful.length) empty(body, 7, 'No item purchases or final held items in this sample. Try another champion or outcome.');
    for (const item of meaningful) {
      const row = make('tr');
      const cell = make('td');
      const identity = make('div', 'item-cell');
      const emblem = String(item.name).toUpperCase().includes('EMBLEM');
      const icon = make('span', `item-icon${emblem ? ' emblem' : ''}`, emblem ? '◆' : '◇');
      icon.setAttribute('aria-hidden', 'true');
      const label = make('span', 'item-label', humanize(item.name));
      label.append(make('span', 'item-sub', emblem ? 'Combined emblem' : 'Shard'));
      identity.append(icon, label);
      cell.append(identity);
      const automatic = item.purchasable === false;
      const boughtCell = automatic ? make('td', 'automatic-cell', 'Automatic combine') : renderRateCell(item.buyRate, item.buyGames, denominator);
      const closingCell = automatic ? make('td', 'numeric dim', '—') : renderRateCell(item.lateBuyRate, item.lateBuyGames, denominator, 'closing-column');
      const itemWin = renderRateCell(item.winRate, item.withItemWins, item.withItemGames);
      itemWin.title = 'Win rate among appearances buying or finishing with this item, using both outcomes. The sample below is wins / appearances with item.';
      row.append(cell, itemWin, boughtCell, renderRateCell(item.finalRate, item.finalGames, denominator), closingCell, make('td', 'numeric', automatic ? '—' : count(item.units)), make('td', 'numeric dim', number(item.avgFinalCount) === null ? '—' : Number(item.avgFinalCount).toFixed(2)));
      body.append(row);
    }
  }
  function renderBuilds(builds) {
    const list = $('build-list');
    list.replaceChildren();
    if (!builds.length) list.append(make('p', 'empty-cell', 'No final builds in this sample. Broaden the filters to see more results.'));
    builds.slice(0, 8).forEach((build, index) => {
      const row = make('div', 'build-row');
      const description = make('div', 'build-description', humanize(build.label || 'No items held'));
      const metrics = make('div', 'build-metrics');
      metrics.append(make('span', '', `${count(build.games)} appearances`), make('small', '', `${count(build.wins)} wins · ${rate(build.winRate)} WR`));
      row.append(make('span', 'build-rank', String(index + 1).padStart(2, '0')), description, metrics);
      list.append(row);
    });
  }
  function renderConsumables(consumables, denominator) {
    const list = $('consumable-list');
    list.replaceChildren();
    if (!consumables.length) list.append(make('p', 'empty-cell', 'No consumable purchases in this sample.'));
    for (const consumable of consumables) {
      const row = make('div', 'consumable-row');
      const top = make('div', 'consumable-top');
      const name = make('div', 'consumable-name');
      const icon = make('span', 'consumable-icon', Number(consumable.id) === 0 ? '↗' : '⌑');
      icon.setAttribute('aria-hidden', 'true');
      name.append(icon, make('span', '', humanize(consumable.name)));
      const metrics = make('div', 'rate-cell');
      metrics.style.textAlign = 'right';
      metrics.append(make('span', 'rate-main positive', rate(consumable.buyRate)), make('span', 'fraction', `${count(consumable.buyGames)} / ${count(denominator)}`));
      top.append(name, metrics);
      const stats = make('div', 'consumable-stats');
      const closing = make('span', '', 'Closing ');
      closing.append(make('strong', '', `${rate(consumable.lateBuyRate)} (${count(consumable.lateBuyGames)} / ${count(denominator)})`));
      const units = make('span', '', 'Units ');
      units.append(make('strong', '', count(consumable.units)));
      stats.append(closing, units);
      row.append(top, stats);
      list.append(row);
    }
  }
  function renderTeams(teams) {
    const body = $('team-body');
    body.replaceChildren();
    if (!teams.length) empty(body, 7, 'No qualifying teams for these filters.');
    for (const team of [...teams].sort((a, b) => Number(b.games) - Number(a.games) || Number(b.winRate || 0) - Number(a.winRate || 0))) {
      const row = make('tr');
      const cell = make('td');
      const button = make('button', `team-name-button${state.filters.team === String(team.id) ? ' selected' : ''}`, team.name);
      button.type = 'button';
      button.title = `Filter statistics to ${team.name}`;
      button.addEventListener('click', () => { changeFilter('team', state.filters.team === String(team.id) ? 'all' : team.id); });
      cell.append(button);
      const win = make('td');
      win.append(rateBar(team.winRate));
      row.append(cell, make('td', 'numeric', number(team.currentRank) === null ? '—' : `#${count(team.currentRank)}`), make('td', 'numeric dim', number(team.currentElo) === null ? '—' : count(Math.round(team.currentElo))), make('td', 'numeric', count(team.games)), make('td', 'numeric', count(team.wins)), make('td', 'numeric dim', count(Number(team.games) - Number(team.wins))), win);
      body.append(row);
    }
  }
  function renderMatchups(data) {
    const minimum = Number(state.filters.minGames);
    const pairs = (data.synergies || []).filter(row => row.games >= minimum);
    const renderPairs = (id, rows, emptyMessage) => {
      const body = $(id);
      body.replaceChildren();
      if (!rows.length) empty(body, 4, emptyMessage);
      for (const pair of rows.slice(0, 10)) {
        const row = make('tr');
        row.append(make('td', '', `${pair.championA.name} + ${pair.championB.name}`), make('td', 'numeric', count(pair.games)), renderRateCell(pair.winRate, pair.wins, pair.games), make('td', 'numeric', `${signed(pair.lift)} pp`));
        row.lastChild.title = `Pair win rate minus average individual win rate (${rate(pair.expectedWinRate)}).`;
        body.append(row);
      }
    };
    renderPairs('synergy-body', pairs.filter(row => row.lift !== null && row.lift > 0).sort((a, b) => b.lift - a.lift || b.games - a.games || a.championA.id - b.championA.id || a.championB.id - b.championB.id), 'No positive-lift teammate pairs meet the minimum appearance count.');
    renderPairs('antisynergy-body', pairs.filter(row => row.lift !== null && row.lift < 0).sort((a, b) => a.lift - b.lift || b.games - a.games || a.championA.id - b.championA.id || a.championB.id - b.championB.id), 'No negative-lift teammate pairs meet the minimum appearance count.');
    const body = $('counter-body');
    body.replaceChildren();
    const counters = (data.counters || []).filter(row => row.games >= minimum).sort((a, b) => a.winRate - b.winRate || b.games - a.games || a.champion.id - b.champion.id || a.opponent.id - b.opponent.id);
    if (!counters.length) empty(body, 5, 'No opposing champion matchups meet the minimum appearance count.');
    for (const counter of counters.slice(0, 15)) {
      const row = make('tr');
      row.append(make('td', '', counter.champion.name), make('td', '', counter.opponent.name), make('td', 'numeric', count(counter.games)), renderRateCell(counter.winRate, counter.wins, counter.games), make('td', 'numeric', counter.lift === null ? '—' : `${signed(counter.lift)} pp`));
      row.lastChild.title = counter.expectedWinRate === null ? 'No qualifying baseline observations for the opponent.' : `Win rate minus baseline expectation (${rate(counter.expectedWinRate)}).`;
      body.append(row);
    }
    text('matchup-caption', `Minimum ${count(minimum)} team appearances per pair. Both outcomes; ${state.filters.champion === 'all' ? 'all champions' : data.detail.championName}.`);
  }
  function renderMatches(matches) {
    const list = $('match-list');
    list.replaceChildren();
    text('match-count', `${count(matches.length)} RECENT MATCHES`);
    if (!matches.length) list.append(make('p', 'empty-cell', 'No source matches for these filters.'));
    for (const match of matches) {
      const href = safeLink(match.url);
      const row = make(href ? 'a' : 'div', 'match-row');
      if (href) { row.href = href; row.target = '_blank'; row.rel = 'noopener noreferrer'; row.setAttribute('aria-label', `${(match.teams || []).map(team => team.name).join(' versus ')} on ${match.map}. Open official match in a new tab.`); }
      const date = make('span', 'match-date');
      date.title = fullDate(match.startedAt);
      date.append(make('strong', '', compactDate(match.startedAt)));
      const instant = new Date(match.startedAt);
      if (match.startedAt && !Number.isNaN(instant.getTime())) date.append(make('span', '', new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit'}).format(instant)));
      row.append(date);
      for (const team of (match.teams || []).slice(0, 2)) {
        const side = make('span', `match-team${team.win ? ' winner' : ''}`);
        const info = make('span', 'match-team-text');
        info.append(make('span', 'match-team-name', team.name));
        const rank = number(team.currentRank) !== null ? `Current #${count(team.currentRank)}` : 'Not on current leaderboard';
        info.append(make('span', 'match-team-details', `${rank} · ${number(team.currentElo) === null ? 'Current Elo unavailable' : `${count(Math.round(team.currentElo))} Elo`}`));
        side.append(info);
        if (team.win) side.append(make('span', 'win-label', 'WIN'));
        row.append(side);
      }
      const map = make('span', 'match-map', humanize(match.map));
      map.append(make('small', '', `${count(match.rounds)} rounds`));
      row.append(map, make('span', 'match-open', href ? '↗' : '—'));
      list.append(row);
    }
  }
  function render(data) {
    const summary = data.summary || {};
    const meta = data.meta || {};
    const detail = data.detail || {};
    const coverage = BattlecodeStats.snapshotCoverage(source.current);
    const observations = number(detail.observations) ?? 0;
    const selected = state.filters.champion === 'all' ? 'All champions' : detail.championName || (data.options?.champions || []).find(champion => String(champion.id) === state.filters.champion)?.name || 'Selected champion';
    text('snapshot-date', coverage.latestMatchAt ? fullDate(coverage.latestMatchAt) : 'No matches published in this window');
    text('archive-size', `${count(coverage.games)} maps in published partial sample`);
    text('leaderboard-date', fullDate(coverage.leaderboardUpdatedAt));
    text('window-range', coverage.windowStartAt && coverage.windowEndAt ? `${fullDate(coverage.windowStartAt)} — ${fullDate(coverage.windowEndAt)}` : 'Requested window unavailable');
    text('window-label', coverage.windowHours ? `REQUESTED ${count(coverage.windowHours)}-HOUR WINDOW` : 'REQUESTED WINDOW');
    text('match-range', coverage.firstMatchAt && coverage.latestMatchAt ? `${fullDate(coverage.firstMatchAt)} — ${fullDate(coverage.latestMatchAt)}` : 'No downloaded maps in this publication');
    text('build-date', fullDate(coverage.builtAt));
    text('collection-limit', coverage.collectionTop ? `Replay collection targets the current top ${count(coverage.collectionTop)}${coverage.seriesPerTeam ? `, up to ${count(coverage.seriesPerTeam)} completed ranked series per team` : ''}${coverage.windowHours ? ` within the requested ${count(coverage.windowHours)} hours` : ''}. This is a partial sample; additional games may be unavailable or awaiting download.` : 'Published matches are a partial sample of downloaded replays.');
    text('coverage-status', coverage.latestMatchAt ? `Newest published match: ${fullDate(coverage.latestMatchAt)}. Dataset build and archive upload times do not indicate when a game was played.` : 'No games are published for this requested window yet. A new dataset build does not itself add recent matches.');
    text('summary-games', count(summary.games));
    text('summary-observations', count(summary.teamObservations));
    text('summary-teams', count(summary.uniqueTeams));
    text('summary-period', summary.start ? `${fullDate(summary.start)} — ${fullDate(summary.end)}` : 'No matches in this sample');
    text('summary-rank', `Current leaderboard top ${state.filters.top}`);
    text('summary-detail', count(observations));
    text('summary-detail-label', `BUILD SAMPLE · ${outcomeName().toUpperCase()}`);
    text('summary-detail-caption', `${selected === 'All champions' ? 'All champion' : selected} appearances`);
    text('cohort-caption', state.filters.cohort === 'both' ? `Both sides are in the published current leaderboard’s top ${state.filters.top}.` : `At least one current top ${state.filters.top} side; only those qualifying sides contribute observations.`);
    text('selected-champion-name', selected);
    text('item-denominator', `${count(observations)} champion appearances · ${outcomeName()} · purchase and inventory rates use this denominator.`);
    const games = number(summary.games) ?? 0;
    $('sample-warning').hidden = games >= 100;
    text('sample-warning', games === 0 ? 'No qualifying maps. Broaden the Elo cohort or change the queue, map or team.' : `Limited sample · ${count(games)} maps. Repeated maps from a series are correlated; rates are preliminary.`);
    text('builds-caption', `Exact end-of-match inventories across ${count(observations)} champion appearances.`);
    text('rank-method', `Cohort membership uses the current leaderboard snapshot from ${fullDate(coverage.leaderboardUpdatedAt)}. Historical match-start ranks do not determine inclusion. Teams absent from that snapshot are excluded.`);
    if (typeof meta.sampleNote === 'string') text('sample-method-note', meta.sampleNote);
    const validation = meta.validation;
    let validationText = 'Purchase attribution is checked against official match statistics; final inventories come from the official match data.';
    if (typeof validation === 'string') validationText = validation;
    else if (validation && typeof validation === 'object') {
      const counters = validation.counts || validation;
      const checked = counters.files ?? counters.files_checked ?? counters.filesChecked ?? counters.games;
      const actions = counters.purchaseActions ?? counters.purchase_actions_checked ?? counters.purchaseActionsChecked;
      if (checked !== undefined) validationText = `${count(checked)} replay files validated${actions !== undefined ? ` across ${count(actions)} purchase actions` : ''}. Final inventories come from official match statistics.`;
    }
    text('validation-note', validationText);
    text('artifact-note', `${meta.artifactId ? `Archive artifact ${meta.artifactId}` : 'Direct public API collection'}${coverage.artifactCreatedAt ? ` · archive uploaded ${fullDate(coverage.artifactCreatedAt)}` : ''}${coverage.builtAt ? ` · dataset built ${fullDate(coverage.builtAt)}` : ''}. Match times and leaderboard snapshot are listed separately above.`);
    updateOptions(data.options);
    renderChampions(data.champions || []);
    renderItems(data.items || [], observations);
    renderBuilds(data.builds || []);
    renderConsumables(data.consumables || [], observations);
    renderTeams(data.teamRows || []);
    renderMatchups(data);
    renderMatches(data.recentMatches || []);
  }
  function loadStats() {
    if (!source.current) return;
    try {
      const collectionTop = source.current.meta.collectionTop;
      const maximum = Number.isInteger(collectionTop) && collectionTop > 0 ? Math.min(collectionTop, 10000) : 10000;
      $('filter-top').max = String(maximum);
      if (Number(state.filters.top) > maximum) {
        state.filters.top = String(maximum);
        history.replaceState(null, '', `${location.pathname}?${new URLSearchParams(state.filters)}${location.hash}`);
        syncControls();
      }
      if (state.filters.champion !== 'all' && !source.current.champions.some(champion => String(champion.id) === state.filters.champion)) {
        state.filters.champion = 'all';
        history.replaceState(null, '', `${location.pathname}?${new URLSearchParams(state.filters)}${location.hash}`);
      }
      const data = BattlecodeStats.aggregate(source.current, state.filters);
      state.data = data;
      render(data);
      $('error-banner').hidden = true;
    } catch (error) {
      text('error-text', `${error.message} Could not calculate the selected statistics.`);
      $('error-banner').hidden = false;
    }
  }
  async function reloadData() {
    if (state.refreshing) return;
    state.refreshing = true;
    $('refresh-button').disabled = true;
    $('refresh-button').classList.add('running');
    $('refresh-status').classList.add('running');
    text('refresh-button-text', 'Checking…');
    text('refresh-status-text', 'Checking deployment');
    if (!source.current) {
      document.body.classList.add('is-loading');
      $('main').setAttribute('aria-busy', 'true');
    }
    try {
      await source.reload();
      state.lastCheck = new Date();
      $('refresh-error').hidden = true;
      loadStats();
    } catch (error) {
      $('refresh-error').hidden = false;
      const reason = (error.name === 'AbortError' ? 'The snapshot request timed out' : error.message).replace(/[.!?]$/, '');
      text('refresh-error', `Could not load the published snapshot: ${reason}.${source.current ? ' Previous valid data remains available; filters still work.' : ' Retry using Check for updates.'}`);
      if (!source.current) {
        text('snapshot-date', 'Snapshot unavailable');
        text('archive-size', 'Check for updates to retry');
        for (const [id, columns] of [['champion-body', 10], ['item-body', 7], ['team-body', 7], ['synergy-body', 4], ['antisynergy-body', 4], ['counter-body', 5]]) empty($(id), columns, 'Statistics unavailable. Check for updates to retry.');
        for (const id of ['build-list', 'consumable-list', 'match-list']) $(id).replaceChildren(make('p', 'empty-cell', 'Statistics unavailable.'));
      }
    } finally {
      state.refreshing = false;
      $('refresh-button').disabled = false;
      $('refresh-button').classList.remove('running');
      $('refresh-status').classList.remove('running');
      text('refresh-button-text', 'Check for updates');
      text('refresh-status-text', state.lastCheck ? `Checked ${new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit'}).format(state.lastCheck)}` : 'Published snapshot');
      document.body.classList.remove('is-loading');
      $('main').setAttribute('aria-busy', 'false');
    }
  }
  $('filters').addEventListener('submit', event => event.preventDefault());
  for (const key of ['cohort', 'kind', 'map', 'team', 'champion']) $(`filter-${key}`).addEventListener('change', event => changeFilter(key, event.target.value));
  for (const [key, minimum] of [['top', 1], ['minGames', 0]]) {
    const input = $(`filter-${key}`);
    const apply = () => {
      const value = Number(input.value);
      if (input.value !== '' && Number.isInteger(value) && value >= minimum && value <= Number(input.max || 10000)) {
        changeFilter(key, value);
        return true;
      }
      return false;
    };
    input.addEventListener('input', apply);
    input.addEventListener('change', () => { if (!apply()) { input.reportValidity(); input.value = state.filters[key]; } });
  }
  document.querySelectorAll('input[name="outcome"]').forEach(input => input.addEventListener('change', () => { if (input.checked) changeFilter('outcome', input.value); }));
  document.querySelectorAll('[data-sort]').forEach(button => button.addEventListener('click', () => {
    if (state.sort === button.dataset.sort) state.direction *= -1;
    else { state.sort = button.dataset.sort; state.direction = state.sort === 'name' ? 1 : -1; }
    if (state.data) renderChampions(state.data.champions || []);
  }));
  $('reset-filters').addEventListener('click', () => {
    state.filters = {...defaults};
    updateURL();
    syncControls();
    loadStats();
  });
  $('refresh-button').addEventListener('click', reloadData);
  $('retry-button').addEventListener('click', reloadData);
  window.addEventListener('popstate', () => { readURL(); syncControls(); loadStats(); });
  readURL();
  syncControls();
  reloadData();
  setInterval(reloadData, 300000);
})();
