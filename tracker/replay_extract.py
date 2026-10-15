"""Decode archive purchases without combat; verify against official match statistics."""
from __future__ import annotations


from formats import load_replay
from protocol import ITEM_DESIGNS, CONSUMABLE_DESIGNS, DRAFT_CHAMPION_PAIRS, select_champion, next_team_after_turn, CHAMPIONS, ITEMS, CONSUMABLES


def inventory_names(values):
    return {ITEMS[i]: n for i, n in enumerate(values) if n}


def decode(path, official):
    replay = load_replay(path)
    draft_count = 2 * replay.map.champions_per_team
    draft = replay.actions[:draft_count]
    assert len(draft) == draft_count and all(a.tag == 4 for a in draft)
    picks = [[a.values[0] for a in draft[t::2]] for t in (0, 1)]
    rosters = [[kind for pick in team for kind in DRAFT_CHAMPION_PAIRS[pick]] for team in picks]
    kinds = rosters[0] + rosters[1]
    n = len(kinds)
    teams = [0] * len(rosters[0]) + [1] * len(rosters[1])
    masks = [sum(1 << i for i, t in enumerate(teams) if t == wanted) for wanted in (0, 1)]
    tie_breaks = list(range(len(rosters[0]))) + list(range(len(rosters[1])))
    priorities = [0] * n
    scheduled = [0] * n
    selected = 0
    counts = [0, 0]
    team = 0
    round_number = 1
    actor = select_champion(masks[team], priorities, tie_breaks)
    selected |= 1 << actor
    inventories = [[0] * 42 for _ in kinds]
    bought = [[0] * 42 for _ in kinds]
    consumables = [[0] * 4 for _ in kinds]
    consumables_bought = [[0] * 4 for _ in kinds]
    spent = [0] * n
    purchases = []
    sales = []
    errors = []
    last_round = 0
    for position, a in enumerate(replay.actions[draft_count:], start=draft_count):
        last_round = round_number
        if a.tag == 0:
            if a.subtype == 0:
                value = a.values[0]
                scheduled[actor] = value if value < 0x80000000 else value - 0x100000000
            elif a.subtype in (6, 8):
                kind, count = a.values
                is_item = a.subtype == 6
                designs = ITEM_DESIGNS if is_item else CONSUMABLE_DESIGNS
                spent[actor] += designs[kind]['cost'] * count
                if is_item:
                    inventories[actor][kind] += count
                    bought[actor][kind] += count
                    ingredient = kind
                    while ITEM_DESIGNS[ingredient]['result'] is not None:
                        d = ITEM_DESIGNS[ingredient]
                        combined, inventories[actor][ingredient] = divmod(inventories[actor][ingredient], d['combine_count'])
                        inventories[actor][d['result']] += combined
                        bought[actor][d['result']] += combined
                        ingredient = d['result']
                else:
                    consumables[actor][kind] += count
                    consumables_bought[actor][kind] += count
                purchases.append({'round': round_number, 'champion': actor, 'champion_type': CHAMPIONS[kinds[actor]],
                                  'team': team, 'category': 'item' if is_item else 'consumable', 'kind': kind,
                                  'name': ITEMS[kind] if is_item else CONSUMABLES[kind], 'count': count,
                                  'gold': designs[kind]['cost'] * count, 'action_index': position})
            elif a.subtype == 9:
                consumables[actor][a.values[0]] -= 1
            elif a.subtype == 11:
                inventories[actor][a.values[0]] -= 1
                sales.append({'round': round_number, 'champion': actor, 'team': team, 'kind': a.values[0]})
        elif a.tag == 3:
            next_team, next_counts = next_team_after_turn(team, counts, masks)
            counts = list(next_counts)
            if next_team is None:
                round_number += 1
                team = (round_number - 1) % 2
                counts = [0, 0]
                priorities = scheduled.copy()
                selected = 0
            else:
                team = next_team
            actor = select_champion(masks[team] & ~selected, priorities, tie_breaks)
            selected |= 1 << actor
        elif a.tag != 5:
            raise ValueError(f'Unsupported action tag {a.tag} in {path}')
    stats = official['statistics']
    # A queued submission can win during prepare_champion in the next round,
    # before another input action is recorded. Only allow that increment when
    # the terminal end_turn has exhausted both rosters and advanced the round.
    allowed_rounds = {last_round}
    if replay.actions[-1].tag == 3 and round_number == last_round + 1 and counts == [0, 0]:
        allowed_rounds.add(round_number)
    if stats['round'] not in allowed_rounds:
        errors.append({'field': 'round', 'decoded_allowed': sorted(allowed_rounds), 'official': stats['round']})
    champions = stats['champions']
    if len(champions) != n:
        errors.append({'field': 'roster_size', 'decoded': n, 'official': len(champions)})
    expected = {c['champion']: c for c in champions}
    for i, kind in enumerate(kinds):
        for field, value in [('type', kind), ('team', teams[i]), ('items', inventories[i]),
                             ('itemsBought', bought[i]), ('consumables', consumables[i]),
                             ('consumablesBought', consumables_bought[i]), ('spentGold', spent[i])]:
            if value != expected[i][field]:
                errors.append({'champion': i, 'field': field, 'decoded': value, 'official': expected[i][field]})
    return purchases, sales, errors

