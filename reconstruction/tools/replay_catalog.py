"""Persistent public replay inventory and conservative historical Elo joins."""
from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
from uuid import UUID

try:
    from . import fetch_replays
except ImportError:  # Standalone tools imported from the tools directory.
    import fetch_replays

FetchError = fetch_replays.FetchError
TERMINAL = {"finished", "completed", "error"}


def timestamp(value, field):
    """Parse actual instants; reject naive or malformed API timestamps."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise FetchError(f"Invalid {field}: expected a timestamp or null")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timestamp has no timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as error:
        raise FetchError(f"Invalid {field}: {value!r}") from error


def utc_text(value, field):
    instant = timestamp(value, field)
    return instant.isoformat(timespec="microseconds") if instant is not None else None


def rating(value, field):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise FetchError(f"Invalid {field}: expected a finite number or null")
    try:
        result = float(value)
    except OverflowError as error:
        raise FetchError(f"Invalid {field}: expected a finite number or null") from error
    if not math.isfinite(result):
        raise FetchError(f"Invalid {field}: expected a finite number or null")
    return result


def json_text(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise FetchError(f"Invalid catalog data: {error}") from error


def origin(base_url):
    validated = fetch_replays.Client(base_url).base_url
    parts = urlsplit(validated)
    try:
        port = parts.port
    except ValueError as error:
        raise FetchError("Invalid Battlecode origin port") from error
    default_port = 443 if parts.scheme == "https" else 80
    hostname = parts.hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{parts.scheme.lower()}://{hostname}" + (f":{port}" if port and port != default_port else "")


class Catalog:
    """SQLite inventory rooted in one output directory and website origin.

    Each public operation commits its own short transaction. Rating derivations
    remain null until a complete discovery pass has established the timeline.
    """

    def __init__(self, outdir, base_url):
        self.outdir = Path(outdir).resolve()
        self.base_url = origin(base_url)
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.path = self.outdir / "catalog.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self._timelines = None
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY, kind TEXT, status TEXT,
                    created_at TEXT, finished_at TEXT, rated_at TEXT,
                    summary_json TEXT NOT NULL, detail_json TEXT,
                    detail_needed INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS rating_events (
                    group_id TEXT NOT NULL REFERENCES groups(id),
                    team_id TEXT NOT NULL, team_name TEXT, side TEXT,
                    rated_at TEXT, rating_before REAL, rating_after REAL,
                    PRIMARY KEY (group_id, team_id)
                );
                CREATE INDEX IF NOT EXISTS rating_events_team ON rating_events(team_id);
                CREATE TABLE IF NOT EXISTS replays (
                    match_id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id),
                    record_json TEXT NOT NULL, file_path TEXT,
                    downloaded INTEGER NOT NULL DEFAULT 0, sha256 TEXT,
                    byte_size INTEGER, error TEXT
                );
                CREATE INDEX IF NOT EXISTS replays_group ON replays(group_id);
                CREATE TABLE IF NOT EXISTS attempts (
                    kind TEXT NOT NULL, identifier TEXT NOT NULL, sequence INTEGER NOT NULL,
                    PRIMARY KEY (kind, identifier)
                );
                CREATE INDEX IF NOT EXISTS attempts_sequence ON attempts(sequence);
            """)
            saved = self.get_meta("base_url")
            if saved is not None and saved != self.base_url:
                raise FetchError(f"Catalog belongs to {saved}; requested {self.base_url}")
            if saved is None:
                self.set_meta("base_url", self.base_url)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def get_meta(self, key):
        row = self.db.execute("SELECT value_json FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def set_meta(self, key, value):
        if key == "base_url" and value != self.base_url:
            raise FetchError("Cannot change the catalog's website origin")
        encoded = json_text(value)
        with self.db:
            self.db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                            (key, encoded))
        if key in ("history_complete", "scan_state"):
            self._timelines = None

    def known_group_ids(self):
        return {row[0] for row in self.db.execute("SELECT id FROM groups").fetchall()}

    @staticmethod
    def _attempt_kind(kind):
        if kind not in ("detail", "download"):
            raise FetchError("Attempt kind must be detail or download")
        return kind

    def order_attempts(self, kind, ids):
        """Register new work at the queue tail, then try the oldest entry."""
        kind = self._attempt_kind(kind)
        identifiers = [fetch_replays.identifier(value) for value in ids]
        with self.db:
            attempted = {row[0]: row[1] for row in self.db.execute(
                "SELECT identifier,sequence FROM attempts WHERE kind=?", (kind,)).fetchall()}
            sequence = self.db.execute("SELECT COALESCE(MAX(sequence),0) FROM attempts").fetchone()[0]
            for value in identifiers:
                if value not in attempted:
                    sequence += 1
                    self.db.execute("INSERT INTO attempts VALUES (?,?,?)", (kind, value, sequence))
                    attempted[value] = sequence
        return sorted(identifiers, key=lambda value: attempted[value])

    def mark_attempt(self, kind, identifier):
        kind = self._attempt_kind(kind)
        identifier = fetch_replays.identifier(identifier)
        with self.db:
            # One SQL statement allocates and writes the next sequence atomically.
            # No foreign key: failed imports can be attempted before discovery.
            self.db.execute("""INSERT INTO attempts (kind,identifier,sequence)
                SELECT ?,?,COALESCE(MAX(sequence),0)+1 FROM attempts WHERE 1
                ON CONFLICT(kind,identifier) DO UPDATE SET sequence=excluded.sequence""", (kind, identifier))
            sequence = self.db.execute("SELECT sequence FROM attempts WHERE kind=? AND identifier=?",
                                       (kind, identifier)).fetchone()[0]
        return sequence

    def _prepare_group(self, group):
        if not isinstance(group, dict):
            raise FetchError("Expected a match-group object")
        group_id = fetch_replays.identifier(group.get("id"))
        dates = {key: utc_text(group.get(key), f"group.{key}")
                 for key in ("createdAt", "finishedAt", "ratedAt")}
        sides = fetch_replays.rows(group, "sides")
        events = []
        seen = set()
        for side in sides:
            team_id = fetch_replays.identifier(side.get("teamId"))
            if team_id in seen:
                raise FetchError(f"Duplicate team in group {group_id}")
            seen.add(team_id)
            before = rating(side.get("ratingBefore"), "side.ratingBefore")
            after = rating(side.get("ratingAfter"), "side.ratingAfter")
            events.append((group_id, team_id, side.get("teamName"), side.get("side"),
                           group.get("ratedAt"), before, after))
        return group_id, dates, json_text(group), events

    @staticmethod
    def _signature(group):
        return json_text({key: group.get(key) for key in ("status", "finishedAt", "ratedAt", "sides")})

    def _upsert_group(self, group, prepared):
        group_id, dates, encoded, events = prepared
        previous = self.db.execute("SELECT summary_json, detail_json, detail_needed FROM groups WHERE id=?",
                                   (group_id,)).fetchone()
        changed = previous is None or self._signature(json.loads(previous["summary_json"])) != self._signature(group)
        needed = (previous is None or previous["detail_json"] is None or previous["detail_needed"]
                  or group.get("status") not in TERMINAL or changed)
        self.db.execute("""INSERT INTO groups
            (id,kind,status,created_at,finished_at,rated_at,summary_json,detail_needed)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            kind=excluded.kind,status=excluded.status,created_at=excluded.created_at,
            finished_at=excluded.finished_at,rated_at=excluded.rated_at,
            summary_json=excluded.summary_json,detail_needed=excluded.detail_needed""",
            (group_id, group.get("kind"), group.get("status"), dates["createdAt"],
             dates["finishedAt"], dates["ratedAt"], encoded, int(bool(needed))))
        self.db.execute("DELETE FROM rating_events WHERE group_id=?", (group_id,))
        self.db.executemany("INSERT INTO rating_events VALUES (?,?,?,?,?,?,?)", events)
        return bool(needed)

    def upsert_group(self, group):
        prepared = self._prepare_group(group)
        with self.db:
            needed = self._upsert_group(group, prepared)
        self._timelines = None
        return needed

    def store_detail(self, data):
        if not isinstance(data, dict):
            raise FetchError("Expected a match-group detail object")
        group = data.get("group")
        prepared = self._prepare_group(group)
        group_id = prepared[0]
        matches = fetch_replays.rows(data, "matches")
        for match in matches:
            fetch_replays.identifier(match.get("id"))
            for key in ("createdAt", "startedAt", "finishedAt"):
                timestamp(match.get(key), f"match.{key}")
        records = list(fetch_replays.replay_records(data))
        encoded_records = []
        for record in records:
            if fetch_replays.identifier(record.get("group_id")) != group_id:
                raise FetchError("Replay record belongs to another match group")
            match_id = fetch_replays.identifier(record.get("match_id"))
            existing = self.db.execute("SELECT group_id FROM replays WHERE match_id=?", (match_id,)).fetchone()
            if existing is not None and existing[0] != group_id:
                raise FetchError(f"Replay {match_id} was already assigned to another match group")
            timestamp(record.get("started_at"), "record.started_at")
            timestamp(record.get("finished_at"), "record.finished_at")
            for side in fetch_replays.rows(record, "teams"):
                fetch_replays.identifier(side.get("id"))
                rating(side.get("elo_before"), "team.elo_before")
                rating(side.get("elo_after"), "team.elo_after")
            encoded_records.append((match_id, group_id, json_text(record)))
        detail_needed = (group.get("status") not in TERMINAL
                         or (group.get("kind") == "ranked" and group.get("status") != "error"
                             and group.get("ratedAt") is None)
                         or any(match.get("status") not in TERMINAL
                                or (match.get("status") != "error" and not match.get("replayUrl"))
                                for match in matches))
        detail_json = json_text(data)
        with self.db:
            self._upsert_group(group, prepared)
            self.db.execute("UPDATE groups SET detail_json=?, detail_needed=? WHERE id=?",
                            (detail_json, int(detail_needed), group_id))
            self.db.executemany("""INSERT INTO replays (match_id,group_id,record_json) VALUES (?,?,?)
                ON CONFLICT(match_id) DO UPDATE SET record_json=excluded.record_json""", encoded_records)
        self._timelines = None

    def group_ids_needing_detail(self):
        return [row[0] for row in self.db.execute(
            "SELECT id FROM groups WHERE detail_needed=1 OR detail_json IS NULL ORDER BY created_at DESC,id").fetchall()]

    def _load_timelines(self):
        if self._timelines is not None:
            return self._timelines
        events = {}
        pending = {}
        for row in self.db.execute("""SELECT e.*,g.kind,g.status,g.created_at FROM rating_events e
                                     JOIN groups g ON g.id=e.group_id""").fetchall():
            if row["kind"] == "ranked" and row["status"] != "error" and row["rated_at"] is None:
                pending.setdefault(row["team_id"], []).append(timestamp(row["created_at"], "pending.created_at"))
            if row["rated_at"] is None and row["rating_before"] is None and row["rating_after"] is None:
                continue  # An unranked or not-yet-rated group is not an Elo event.
            event = dict(row)
            event["instant"] = timestamp(event["rated_at"], "rating_event.rated_at")
            events.setdefault(event["team_id"], []).append(event)
        timelines = {}
        for team_id, team_events in events.items():
            undated = any(event["instant"] is None for event in team_events)
            dated = sorted((event for event in team_events if event["instant"] is not None),
                           key=lambda event: (event["instant"], event["group_id"]))
            buckets = []
            for event in dated:
                if not buckets or buckets[-1][0]["instant"] != event["instant"]:
                    buckets.append([event])
                else:
                    buckets[-1].append(event)
            problem = "history_gap" if undated else None
            after = 1500.0
            points = []
            baseline_valid = bool(buckets)
            for index, bucket in enumerate(buckets):
                pairs = {(event["rating_before"], event["rating_after"]) for event in bucket}
                event = bucket[0]
                bucket_problem = None
                if len(pairs) > 1:
                    bucket_problem = "ambiguous"
                elif event["rating_before"] is None or event["rating_after"] is None:
                    bucket_problem = "history_gap"
                elif event["rating_before"] != after:
                    bucket_problem = "history_gap"
                if index == 0:
                    baseline_valid = not undated and bucket_problem is None
                if bucket_problem == "ambiguous" or problem is None:
                    problem = bucket_problem or problem
                points.append({"instant": event["instant"], "event": event,
                               "problem": problem, "bucket_problem": bucket_problem,
                               "changed_at_time": any(before != value for before, value in pairs)})
                after = event["rating_after"]
            timelines[team_id] = {"times": [point["instant"] for point in points],
                                  "points": points, "baseline_valid": baseline_valid,
                                  "undated": undated}
        for team_id, created in pending.items():
            timeline = timelines.setdefault(team_id, {"times": [], "points": [],
                                                       "baseline_valid": False, "undated": False})
            timeline["pending_created"] = created
        self._timelines = timelines
        return timelines

    def _elo_at(self, team_id, started_at, complete, through=None):
        empty = {"elo_at_time": None, "elo_source": "unknown", "elo_observed_at": None,
                 "elo_source_group_id": None}
        instant = timestamp(started_at, "record.started_at")
        if instant is None:
            return {**empty, "elo_source": "missing_time"}
        if not complete:
            return empty
        if through is not None and instant > through:
            return {**empty, "elo_source": "after_snapshot"}
        timeline = self._load_timelines().get(team_id)
        if timeline is None:
            return empty
        if timeline["undated"]:
            return {**empty, "elo_source": "history_gap"}
        if any(created is None or created <= instant for created in timeline.get("pending_created", [])):
            return {**empty, "elo_source": "pending_rating"}
        points = timeline["points"]
        index = bisect_left(timeline["times"], instant)
        if index < len(points) and points[index]["instant"] == instant:
            equal = points[index]
            if equal["changed_at_time"] or equal["bucket_problem"] == "ambiguous":
                return {**empty, "elo_source": "ambiguous"}
            if equal["problem"] is not None:
                return {**empty, "elo_source": equal["problem"]}
        if index == 0:
            if not timeline["baseline_valid"]:
                return {**empty, "elo_source": "history_gap"}
            event = points[0]["event"]
            return {"elo_at_time": 1500.0, "elo_source": "initial",
                    "elo_observed_at": event["rated_at"], "elo_source_group_id": event["group_id"]}
        if index < len(points) and points[index]["bucket_problem"] == "history_gap":
            # The next observed before-value fails to confirm the preceding
            # interval. A missing update could have happened at any point since
            # the previous event, so do not extend its after-value to this time.
            return {**empty, "elo_source": "history_gap"}
        point = points[index - 1]
        if point["problem"] is not None:
            return {**empty, "elo_source": point["problem"]}
        event = point["event"]
        return {"elo_at_time": event["rating_after"], "elo_source": "rated_event",
                "elo_observed_at": event["rated_at"], "elo_source_group_id": event["group_id"]}

    def replay_records(self, limit=None, pending_only=False, team=None, kind=None, min_elo=None):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise FetchError("Catalog record limit must be a nonnegative integer")
        if min_elo is not None:
            min_elo = rating(min_elo, "min_elo")
        team_id = None
        if team is not None:
            if not isinstance(team, str):
                raise FetchError("Team filter must be an ID or exact historical name")
            try:
                team_id = str(UUID(team))
            except ValueError:
                pass
        clauses = []
        params = []
        if pending_only:
            clauses.append("r.downloaded=0")
        if kind is not None:
            clauses.append("g.kind=?")
            params.append(kind)
        query = "SELECT r.* FROM replays r JOIN groups g ON g.id=r.group_id"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY g.created_at DESC,r.match_id"
        saved = self.db.execute(query, params).fetchall()
        complete = self.get_meta("history_complete") is True and self.get_meta("scan_state") is None
        through = timestamp(self.get_meta("history_through"), "history_through")
        yielded = 0
        for row in saved:
            if limit is not None and yielded >= limit:
                return
            record = json.loads(row["record_json"])
            match_id = fetch_replays.identifier(row["match_id"])
            group_id = fetch_replays.identifier(row["group_id"])
            if (match_id != row["match_id"] or group_id != row["group_id"]
                    or not isinstance(record, dict)
                    or record.get("match_id") != match_id or record.get("group_id") != group_id):
                raise FetchError("Catalog replay identifiers are inconsistent")
            file_path = row["file_path"]
            if file_path is not None:
                if (file_path != f"{group_id}/{match_id}.repl26a"
                        or not (self.outdir / file_path).resolve().is_relative_to(self.outdir)):
                    raise FetchError("Catalog replay path must use its group/match path inside the output directory")
            elif row["downloaded"]:
                raise FetchError("Downloaded catalog replay is missing its file path")
            record.update(file_path=row["file_path"], downloaded=bool(row["downloaded"]),
                          sha256=row["sha256"], byte_size=row["byte_size"], error=row["error"])
            for side in record["teams"]:
                side.update(self._elo_at(side["id"], record.get("started_at"), complete, through))
            selected = record["teams"]
            if team is not None:
                selected = [side for side in selected if side["id"] == team_id] if team_id else [
                    side for side in selected if isinstance(side.get("name"), str)
                    and side["name"].casefold() == team.casefold()]
                if not selected:
                    continue
            if min_elo is not None and not any(side["elo_at_time"] is not None
                                              and side["elo_at_time"] >= min_elo for side in selected):
                continue
            yielded += 1
            yield record

    def mark_downloaded(self, match_id, path, sha256, byte_size):
        match_id = fetch_replays.identifier(match_id)
        known = self.db.execute("SELECT group_id FROM replays WHERE match_id=?", (match_id,)).fetchone()
        if known is None:
            raise FetchError(f"Replay is not in the catalog: {match_id}")
        path = Path(path)
        resolved = path.resolve() if path.is_absolute() else (self.outdir / path).resolve()
        try:
            relative = resolved.relative_to(self.outdir).as_posix()
        except ValueError as error:
            raise FetchError("Downloaded replay must be inside the catalog output directory") from error
        if relative != f"{known['group_id']}/{match_id}.repl26a":
            raise FetchError("Downloaded replay must use its catalog group/match path")
        if (not isinstance(sha256, str) or len(sha256) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in sha256)):
            raise FetchError("Invalid replay SHA-256")
        if type(byte_size) is not int or byte_size < 0:
            raise FetchError("Invalid replay byte size")
        with self.db:
            cursor = self.db.execute("""UPDATE replays SET file_path=?,downloaded=1,sha256=?,byte_size=?,error=NULL
                WHERE match_id=?""", (relative, sha256.lower(), byte_size, match_id))
            if cursor.rowcount != 1:
                raise FetchError(f"Replay is not in the catalog: {match_id}")

    def mark_failed(self, match_id, error):
        match_id = fetch_replays.identifier(match_id)
        with self.db:
            cursor = self.db.execute("UPDATE replays SET downloaded=0,error=? WHERE match_id=?", (str(error), match_id))
            if cursor.rowcount != 1:
                raise FetchError(f"Replay is not in the catalog: {match_id}")

    def summary(self):
        groups = self.db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        row = self.db.execute("""SELECT COUNT(*) AS replays, COALESCE(SUM(downloaded),0) AS downloaded,
            COALESCE(SUM(downloaded=0),0) AS pending,
            COALESCE(SUM(error IS NOT NULL),0) AS failed FROM replays""").fetchone()
        return {"groups": groups, **dict(row)}
