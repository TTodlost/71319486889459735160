"""Historical Elo ranks reconstructed from covered public per-team histories.

The universe comprises the team IDs with history files. It is an observed
public universe, not proof of every team that ever existed or was deleted.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
import json
import math
from pathlib import Path


INITIAL_RATING = 1500.0


def _time(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone is required")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Invalid {label} timestamp: {value!r}") from error


def _rating(value, label: str) -> float:
    try:
        result = float(value) if type(value) in (int, float) else float("nan")
    except OverflowError:
        result = float("nan")
    if not math.isfinite(result):
        raise ValueError(f"Invalid {label} rating: {value!r}")
    return result


@dataclass(frozen=True)
class _Event:
    at: datetime
    before: float
    after: float
    group_id: str


@dataclass(frozen=True)
class _Point:
    at: datetime
    before: float
    after: float
    low: float
    high: float


def _points(team_id: str, events: list[_Event]) -> list[_Point]:
    """Collapse a timestamp without assigning an arbitrary same-time order."""
    result = []
    for instant, bucket_iter in groupby(sorted(events, key=lambda e: (e.at, e.group_id)),
                                      key=lambda e: e.at):
        bucket = list(bucket_iter)
        if len(bucket) == 1:
            event = bucket[0]
            before, after = event.before, event.after
        else:
            # Multiple updates for one team require a consistent rating chain.
            # The degree counts identify the endpoints of a directed Euler
            # trail. A closed chain needs an earlier observed rating anchor.
            degree = Counter()
            outgoing = defaultdict(list)
            for event in bucket:
                degree[event.before] += 1
                degree[event.after] -= 1
                outgoing[event.before].append(event.after)
            starts = [value for value, delta in degree.items() if delta == 1]
            ends = [value for value, delta in degree.items() if delta == -1]
            if len(starts) == len(ends) == 1 and all(abs(delta) <= 1 for delta in degree.values()):
                before, after = starts[0], ends[0]
            elif all(delta == 0 for delta in degree.values()) and (len(degree) == 1 or result and result[-1].after in degree):
                before = after = result[-1].after if result else next(iter(degree))
            else:
                raise ValueError(f"Ambiguous simultaneous rating events for team {team_id} at {instant.isoformat()}")
            stack, trail = [before], []
            while stack:
                if outgoing[stack[-1]]:
                    stack.append(outgoing[stack[-1]].pop())
                else:
                    trail.append(stack.pop())
            if len(trail) != len(bucket) + 1:
                raise ValueError(f"Disconnected simultaneous rating events for team {team_id} at {instant.isoformat()}")
        values = [value for event in bucket for value in (event.before, event.after)]
        result.append(_Point(instant, before, after, min(values), max(values)))
    return result


class RatingIndex:
    """Validate a public history snapshot and answer ranks at map start times.

    ``verified`` describes the source coverage and rank bounds. At an exact
    rating timestamp, ``elo`` is the pre-update display value; ``rankMin`` and
    ``rankMax`` include all possible before/after states. ``rank`` is populated
    only when those bounds agree. Safe top-N membership uses ``rankMax <= N``.
    """

    def __init__(self, history_dir: Path, required_start: str, required_end: str):
        self.start = _time(required_start, "required_start")
        self.end = _time(required_end, "required_end")
        if self.start > self.end:
            raise ValueError("required_start must not be after required_end")
        history_dir = Path(history_dir)
        files = sorted(history_dir.glob("*.json"))
        if not files:
            raise ValueError(f"No team-history JSON files in {history_dir}")
        sources = {}
        groups = {}
        own_events = {}
        empty_histories = 0
        pending = []

        for path in files:
            team_id = path.stem
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError(f"Cannot read team history {path.name}: {error}") from error
            if not isinstance(data, dict) or not isinstance(data.get("groups"), list) or type(data.get("more")) is not bool:
                raise ValueError(f"Invalid API history response in {path.name}")
            created_times = []
            own_events[team_id] = []
            own_seen = set()
            for group in data["groups"]:
                if not isinstance(group, dict) or not isinstance(group.get("id"), str):
                    raise ValueError(f"Invalid match group in {path.name}")
                group_id = group["id"]
                created = _time(group.get("createdAt"), f"group {group_id} createdAt")
                created_times.append(created)
                if group.get("kind") != "ranked":
                    continue
                sides = group.get("sides")
                if not isinstance(sides, list) or len(sides) != 2 or any(not isinstance(s, dict) for s in sides):
                    raise ValueError(f"Invalid ranked sides in group {group_id}")
                side_ids = [s.get("teamId") for s in sides]
                if any(not isinstance(tid, str) or not tid for tid in side_ids) or len(set(side_ids)) != 2 or team_id not in side_ids:
                    raise ValueError(f"Team {team_id} is missing or duplicated in group {group_id}")
                rated = _time(group["ratedAt"], f"group {group_id} ratedAt") if group.get("ratedAt") else None
                values = []
                for side in sides:
                    if rated is not None:
                        before = _rating(side.get("ratingBefore"), f"group {group_id} before")
                        after = _rating(side.get("ratingAfter"), f"group {group_id} after")
                    else:
                        before, after = side.get("ratingBefore"), side.get("ratingAfter")
                        if before is not None or after is not None:
                            raise ValueError(f"Undated rating values in group {group_id}")
                    values.append((side["teamId"], before, after))
                    if rated is not None and side["teamId"] == team_id and group_id not in own_seen:
                        own_events[team_id].append(_Event(rated, before, after, group_id))
                own_seen.add(group_id)
                signature = (created, group.get("status"), rated, tuple(sorted(values)))
                if group_id in groups and groups[group_id] != signature:
                    raise ValueError(f"Conflicting snapshots for rating group {group_id}")
                groups[group_id] = signature
                if rated is None and group.get("status") != "error" and created <= self.end:
                    pending.append((team_id, group_id))
            marker = min(created_times) if created_times else None
            if data.get("coverage_start") is not None:
                explicit = _time(data["coverage_start"], f"{path.name} coverage_start")
                marker = min(marker, explicit) if marker else explicit
            if data["more"] and (marker is None or marker >= self.start):
                raise ValueError(f"History coverage gap for team {team_id}: oldest returned group must precede {required_start}")
            if data["more"] and not own_events[team_id]:
                raise ValueError(f"Unknown initial rating baseline for team {team_id}: truncated history has no rated event")
            sources[team_id] = data
            empty_histories += not own_events[team_id]

        if pending:
            team_id, group_id = pending[0]
            raise ValueError(f"Pending or unresolved ranked rating for team {team_id}, group {group_id}, before required_end")

        all_events = {team_id: [] for team_id in sources}
        for group_id, (_, _, rated, values) in groups.items():
            for team_id, before, after in values:
                if team_id not in sources:
                    raise ValueError(f"Missing team-history file for observed opponent {team_id} in group {group_id}")
                if rated is not None:
                    all_events[team_id].append(_Event(rated, before, after, group_id))

        checks = 0

        def validate(team_id, points):
            nonlocal checks
            for previous, current in zip(points, points[1:]):
                if previous.at < self.end and current.at > self.start:
                    checks += 1
                    if previous.after != current.before:
                        raise ValueError(
                            f"Rating continuity gap for team {team_id} between "
                            f"{previous.at.isoformat()} ({previous.after}) and "
                            f"{current.at.isoformat()} ({current.before})")

        # Validate each team's own response before opponent deduplication can
        # accidentally conceal a missing page in that team's history.
        for team_id, events in own_events.items():
            validate(team_id, _points(team_id, events))
        own_checks = checks
        self._points = {team_id: _points(team_id, events) for team_id, events in all_events.items()}
        for team_id, points in self._points.items():
            validate(team_id, points)
        self._times = {team_id: [point.at for point in points] for team_id, points in self._points.items()}
        self.summary = {
            "teams": len(sources), "universe": len(sources), "groups": len(groups),
            "rated_groups": sum(row[2] is not None for row in groups.values()),
            "rating_events": sum(len(events) for events in all_events.values()),
            "empty_histories": empty_histories, "initial_rating": INITIAL_RATING,
            "continuity_checks": own_checks, "continuity_gaps": 0,
            "required_start": self.start.isoformat(), "required_end": self.end.isoformat(),
            "universe_warning": "Ranks cover the observed public teams; teams absent from both the ladder and archived history cannot be ruled out.",
        }

    def at(self, iso_time: str) -> dict:
        instant = _time(iso_time, "query")
        if instant < self.start or instant > self.end:
            raise ValueError("Query timestamp is outside the verified required interval")
        bounds = {}
        for team_id, points in self._points.items():
            index = bisect_left(self._times[team_id], instant)
            if index < len(points) and points[index].at == instant:
                point = points[index]
                bounds[team_id] = (point.before, point.low, point.high)
            else:
                value = points[index - 1].after if index else points[0].before if points else INITIAL_RATING
                bounds[team_id] = (value, value, value)
        result = {}
        lows = sorted(low for _, low, _ in bounds.values())
        highs = sorted(high for _, _, high in bounds.values())
        for team_id, (elo, low, high) in bounds.items():
            rank_min = 1 + len(lows) - bisect_right(lows, high)
            # The team's own possible high value is not a rival.
            rank_max = 1 + len(highs) - bisect_right(highs, low) - int(high > low)
            result[team_id] = {
                "elo": elo, "rank": rank_min if rank_min == rank_max else None,
                "rankMin": rank_min, "rankMax": rank_max, "verified": True,
            }
        return result
