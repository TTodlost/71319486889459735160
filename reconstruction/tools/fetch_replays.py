"""Download public Battlecode replays through the website's JSON API.

Uses only Python 3.11's standard library. Bundles remain intact so both the
original viewer and cambc_sim can read the downloaded .repl26a files.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener
from uuid import UUID
import zipfile
import zlib

DEFAULT_BASE_URL = "https://2026a.battlecode.co.uk"
DEFAULT_OUTDIR = Path(__file__).resolve().parents[2] / "replays" / "website"
REPLAY_MAGIC = b"a62per\0\0"
MAP_MAGIC = b"w62pam\0\0"


class FetchError(RuntimeError):
    """A failed request or an unexpected website/download response."""


class RateLimitError(FetchError):
    """The website requires a cooldown before another request is made."""

    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


class _RetryableReplayResponse(FetchError):
    """Downloaded bytes failed validation and may be a transient response."""


def identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise FetchError(f"Invalid Battlecode ID: {value!r}") from error


class Client:
    def __init__(self, base_url=DEFAULT_BASE_URL, timeout=30, delay=.25, retries=3):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise FetchError("--base-url must be an HTTP(S) origin without a path or credentials")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.opener = build_opener()
        self.last_request = 0.0
        self._rate_limit_until = 0.0

    def url(self, path: str) -> str:
        resolved = urljoin(self.base_url + "/", path)
        parsed = urlsplit(resolved)
        origin = urlsplit(self.base_url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise FetchError("The website returned a replay URL on a different origin")
        if parsed.fragment:
            raise FetchError("Unexpected fragment in API URL")
        return resolved

    def _retry_delay(self, error, attempt: int) -> float:
        header = error.headers.get("Retry-After") if isinstance(error, HTTPError) else None
        wait = min(2 ** attempt, 10)
        if header:
            try:
                wait = float(header)
            except ValueError:
                try:
                    wait = parsedate_to_datetime(header).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    pass
        if not math.isfinite(wait):
            wait = min(2 ** attempt, 10)
        return max(0, wait)

    def _request(self, path, consume):
        url = self.url(path)
        remaining = self._rate_limit_until - time.monotonic()
        if remaining > 0:
            raise RateLimitError(f"Rate limited; retry after {math.ceil(remaining)} seconds", remaining)
        for attempt in range(self.retries + 1):
            pause = self.delay - (time.monotonic() - self.last_request)
            if pause > 0:
                time.sleep(pause)
            self.last_request = time.monotonic()
            try:
                request = Request(url, headers={"User-Agent": "cambc-replay-fetcher/1.0",
                                               "Accept": "application/json, application/zip, */*"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    return consume(response)
            except HTTPError as error:
                error.close()
                if error.code == 429:
                    wait = self._retry_delay(error, attempt)
                    if attempt == self.retries or wait > 60:
                        self._rate_limit_until = time.monotonic() + wait
                        raise RateLimitError(
                            f"HTTP 429 for {url}; retry after {math.ceil(wait)} seconds", wait) from error
                elif error.code not in (500, 502, 503, 504) or attempt == self.retries:
                    hint = "; public access requires a session on this event" if error.code in (401, 403) else ""
                    raise FetchError(f"HTTP {error.code} for {url}{hint}") from error
                else:
                    wait = self._retry_delay(error, attempt)
                if wait > 60:
                    raise FetchError(f"Rate limited; retry after {math.ceil(wait)} seconds") from error
            except _RetryableReplayResponse as error:
                if attempt == self.retries:
                    raise FetchError(f"Invalid replay response from {url}: {error}") from error
                wait = self._retry_delay(error, attempt)
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
                if attempt == self.retries:
                    raise FetchError(f"Request failed for {url}: {error}") from error
                wait = self._retry_delay(error, attempt)
            print(f"Retrying request in {wait:g}s: {url}", file=sys.stderr)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def get_json(self, path, params=None):
        if params:
            path += ("&" if "?" in path else "?") + urlencode(params)
        raw = self._request(path, lambda response: response.read())
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise FetchError(f"Expected JSON from {self.url(path)}; received a non-JSON response") from error
        if not isinstance(data, dict):
            raise FetchError(f"Expected a JSON object from {self.url(path)}")
        return data

    def download(self, path, destination):
        def consume(response):
            expected = response.headers.get("Content-Length")
            if expected is not None:
                try:
                    expected = int(expected)
                    if expected < 0:
                        raise ValueError("negative length")
                except ValueError as error:
                    raise FetchError("Invalid Content-Length in replay download response") from error
            with Path(destination).open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
                if expected is not None and output.tell() != expected:
                    raise http.client.IncompleteRead(b"")
            try:
                validate_replay(destination)
            except FetchError as error:
                raise _RetryableReplayResponse(str(error)) from error
        self._request(path, consume)


def rows(data, key):
    result = data.get(key)
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise FetchError(f"API response is missing the {key!r} list")
    return result


def iter_ladder(client, *, top=None, query=None) -> Iterator[dict]:
    seen = set()
    page = 1
    while True:
        params = {"page": page, "pageSize": 25}
        if query:
            params["q"] = query
        data = client.get_json("/api/v1/ladder", params)
        pages = data.get("pages")
        if (data.get("page") != page or type(pages) is not int or pages < 0):
            raise FetchError("Invalid ladder pagination response")
        teams = list(rows(data, "teams"))
        if "unrated" in data:
            teams += rows(data, "unrated")
        added = 0
        for team in teams:
            team_id = identifier(team.get("teamId"))
            if team_id in seen:
                continue
            seen.add(team_id)
            added += 1
            yield team
            if top is not None and len(seen) >= top:
                return
        if page >= pages:
            return
        if not added:
            raise FetchError("Ladder pagination made no progress")
        page += 1


def resolve_team(client, value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        pass
    if "/" in value and urlsplit(value).scheme:
        kind, team_id = parse_source(client, value)
        if kind != "team":
            raise FetchError("--team expects a team URL, ID, or exact ladder name")
        return team_id
    matches = [team for team in iter_ladder(client, query=value)
               if team.get("name", "").casefold() == value.casefold()]
    if not matches:
        raise FetchError(f"Team {value!r} was not found on the ladder; use its team URL or ID")
    if len(matches) != 1:
        raise FetchError(f"More than one team is named {value!r}; use its team URL or ID")
    return identifier(matches[0]["teamId"])


def iter_team_groups(client, team_id, *, max_groups=None, kind=None) -> Iterator[dict]:
    params = {"scope": "all", "team": identifier(team_id)}
    if kind:
        params["kind"] = kind
    seen = set()
    cursors = set()
    while True:
        data = client.get_json("/api/v1/match-groups", params)
        groups = rows(data, "groups")
        if type(data.get("more")) is not bool:
            raise FetchError("Invalid match-group pagination response")
        for group in groups:
            group_id = identifier(group.get("id"))
            if group_id in seen:
                continue
            seen.add(group_id)
            yield group
            if max_groups is not None and len(seen) >= max_groups:
                return
        if not data["more"]:
            return
        if not groups or not isinstance(groups[-1].get("createdAt"), str):
            raise FetchError("Match-group pagination is missing a cursor")
        cursor = f"{groups[-1]['createdAt']}_{identifier(groups[-1].get('id'))}"
        if cursor in cursors:
            raise FetchError("Match-group pagination repeated a cursor")
        cursors.add(cursor)
        params["before"] = cursor


def rating(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise FetchError("Invalid historical rating in match-group response")
    return value


def replay_records(data) -> Iterator[dict]:
    """Keep rating-event snapshots separate from an individual game's start."""
    group = data.get("group")
    if not isinstance(group, dict):
        raise FetchError("Missing match group in detail response")
    group_id = identifier(group.get("id"))
    group_sides = rows(group, "sides")
    for match in rows(data, "matches"):
        replay_url = match.get("replayUrl")
        if not replay_url:
            continue
        if not isinstance(replay_url, str):
            raise FetchError("Invalid replay URL in match response")
        match_sides = rows(match, "sides") if "sides" in match else []
        teams = []
        for side in group_sides:
            team_id = identifier(side.get("teamId"))
            result = next((entry for entry in match_sides if entry.get("teamId") == team_id), {})
            teams.append({"id": team_id, "name": side.get("teamName"), "side": side.get("side"),
                          "version": result.get("version", side.get("version")),
                          "result": result.get("result"), "score": result.get("score"),
                          "elo_before": rating(side.get("ratingBefore")),
                          "elo_after": rating(side.get("ratingAfter"))})
        yield {"group_id": group_id, "match_id": identifier(match.get("id")),
               "replay_url": replay_url, "map": match.get("map"), "position": match.get("position"),
               "kind": group.get("kind"), "created_at": group.get("createdAt"),
               "group_finished_at": group.get("finishedAt"), "rated_at": group.get("ratedAt"),
               "match_created_at": match.get("createdAt"), "started_at": match.get("startedAt"),
               "finished_at": match.get("finishedAt"), "status": match.get("status"),
               "rating_scope": "group_rating_event", "teams": teams}


def iter_replays(client, group_ids) -> Iterator[dict]:
    seen_groups = set()
    seen_matches = set()
    for value in group_ids:
        group_id = identifier(value)
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        data = client.get_json(f"/api/v1/match-groups/{group_id}")
        group = data.get("group")
        if not isinstance(group, dict) or identifier(group.get("id")) != group_id:
            raise FetchError(f"Unexpected match group in response for {group_id}")
        for record in replay_records(data):
            match_id = record["match_id"]
            if match_id in seen_matches:
                continue
            seen_matches.add(match_id)
            yield record


def validate_replay(path):
    path = Path(path)
    def check_header(replay):
        # Replay and embedded-map headers are independent of engine version.
        header = replay.read(26)
        if (len(header) != 26 or header[:8] != REPLAY_MAGIC
                or header[14:22] != MAP_MAGIC):
            raise FetchError(f"{path}: invalid or truncated replay/map header")

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                members = [member for member in archive.infolist() if member.filename == "replay.repl26a"]
                if len(members) != 1:
                    raise FetchError(f"{path}: bundle must contain one replay.repl26a")
                with archive.open(members[0]) as replay:
                    check_header(replay)
                    while replay.read(1024 * 1024):
                        pass  # Reading the whole member verifies its ZIP CRC.
        else:
            with path.open("rb") as replay:
                check_header(replay)
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as error:
        if isinstance(error, FetchError):
            raise
        raise FetchError(f"{path}: corrupt replay bundle ({error})") from error


def publish_new(temporary, destination):
    if os.name == "nt":
        # Windows rename is atomic and fails if the destination already exists.
        temporary.rename(destination)
    else:
        # POSIX rename overwrites; an exclusive hard link preserves other runs.
        destination.hardlink_to(temporary)


def download_replay(client, record, outdir):
    destination = (Path(outdir) / identifier(record["group_id"])
                   / f"{identifier(record['match_id'])}.repl26a")
    if destination.exists():
        try:
            validate_replay(destination)
        except FetchError as error:
            raise FetchError(f"Existing replay is invalid; move it aside before retrying: {destination}") from error
        return destination, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        client.download(record["replay_url"], temporary_path)
        validate_replay(temporary_path)
        # Exclusive creation avoids overwriting a replay produced by another run.
        try:
            publish_new(temporary_path, destination)
        except FileExistsError:
            validate_replay(destination)
            return destination, False
        return destination, True
    finally:
        temporary_path.unlink(missing_ok=True)


def write_metadata(client, record, destination, *, expected_sha256=None):
    metadata = destination.with_suffix(".json")
    previous = {}
    if metadata.exists():
        try:
            previous = json.loads(metadata.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as error:
            raise FetchError(f"Invalid existing metadata: {metadata}") from error
        if not isinstance(previous, dict):
            raise FetchError(f"Invalid existing metadata: {metadata}")
    stat = destination.stat()
    previous_hash = previous.get("sha256")
    if (previous.get("bytes") == stat.st_size and previous.get("file_mtime_ns") == stat.st_mtime_ns
            and isinstance(previous_hash, str) and len(previous_hash) == 64
            and all(char in "0123456789abcdef" for char in previous_hash)):
        sha256 = previous_hash
    else:
        validate_replay(destination)
        with destination.open("rb") as replay:
            sha256 = hashlib.file_digest(replay, "sha256").hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise FetchError(f"Local replay checksum changed; move it aside before downloading again: {destination}")
    merged_record = dict(record)
    same_match = all(previous.get(key) == record.get(key) for key in ("group_id", "match_id", "started_at"))
    derived = {"elo_at_time", "elo_source", "elo_observed_at", "elo_source_group_id"}
    previous_teams = previous.get("teams", [])
    if isinstance(previous_teams, list):
        saved_teams = {team.get("id"): team for team in previous_teams if isinstance(team, dict)}
        merged_record["teams"] = [
            {**{key: value for key, value in saved_teams.get(team.get("id"), {}).items()
                if same_match or key not in derived}, **team}
            for team in record.get("teams", [])]
    data = {**previous, **merged_record, "replay_url": client.url(record["replay_url"]),
            "group_url": f"{client.base_url}/game/match-groups/{record['group_id']}",
            "file": destination.name, "bytes": stat.st_size, "sha256": sha256,
            "file_mtime_ns": stat.st_mtime_ns,
            "fetched_at": previous.get("fetched_at") or datetime.now(timezone.utc).isoformat()}
    if data == previous:
        return data
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=metadata.parent,
                                         suffix=".part", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(data, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
        temporary_path.replace(metadata)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return data


def parse_source(client, value):
    url = urlsplit(client.url(value))
    parts = url.path.rstrip("/").split("/")
    if parts == ["", "game", "ladder"]:
        return "ladder", None
    if len(parts) == 4 and parts[:2] == ["", "game"]:
        if parts[2] in ("team", "match-groups"):
            return ("team" if parts[2] == "team" else "group"), identifier(parts[3])
    raise FetchError(f"Expected a ladder, team, or match-group URL: {value!r}")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite, nonnegative number")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help="ladder, team, or match-group URLs (default: whole ladder)")
    parser.add_argument("--team", action="append", default=[], help="team ID, URL, or exact name; repeatable")
    parser.add_argument("--match-group", action="append", default=[], help="match-group ID or URL; repeatable")
    parser.add_argument("--top", type=positive_int, help="use only the first N ladder teams")
    parser.add_argument("--max-groups", type=positive_int, help="latest N match groups per team")
    parser.add_argument("--max-replays", type=positive_int, help="select at most N replay files, including existing files")
    parser.add_argument("--kind", choices=("ranked", "unranked", "tournament"), help="filter team match history")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--dry-run", action="store_true", help="list replay URLs without writing files")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=nonnegative_float, default=30)
    parser.add_argument("--delay", type=nonnegative_float, default=.25, help="minimum seconds between requests (default: .25)")
    parser.add_argument("--retries", type=int, default=3,
                        help="retries for transient HTTP/network failures or invalid replay responses (default: 3)")
    args = parser.parse_args(argv)
    if args.timeout == 0 or args.retries < 0:
        parser.error("--timeout must be positive and --retries must be nonnegative")
    try:
        client = Client(args.base_url, args.timeout, args.delay, args.retries)
        selections = [parse_source(client, source) for source in args.sources]
        selections.extend(("team", resolve_team(client, team)) for team in args.team)
        for value in args.match_group:
            if urlsplit(value).scheme:
                selection = parse_source(client, value)
                if selection[0] != "group":
                    raise FetchError("--match-group expects a match-group ID or URL")
                selections.append(selection)
            else:
                selections.append(("group", identifier(value)))
        if not selections:
            selections = [("ladder", None)]

        def groups():
            seen_teams = set()
            for kind, value in selections:
                if kind == "group":
                    yield value
                    continue
                teams = (iter_ladder(client, top=args.top) if kind == "ladder"
                         else [{"teamId": value}])
                for team in teams:
                    team_id = identifier(team["teamId"])
                    if team_id in seen_teams:
                        continue
                    seen_teams.add(team_id)
                    print(f"Team: {team.get('name', team_id)}", file=sys.stderr)
                    for group in iter_team_groups(client, team_id, max_groups=args.max_groups, kind=args.kind):
                        yield group["id"]

        selected = downloaded = skipped = failed = 0
        for record in iter_replays(client, groups()):
            selected += 1
            if args.dry_run:
                print(f"{record['group_id']} {record['match_id']} {record['map']} {client.url(record['replay_url'])}")
            else:
                try:
                    destination, fresh = download_replay(client, record, args.outdir)
                    write_metadata(client, record, destination)
                    downloaded += int(fresh)
                    skipped += int(not fresh)
                    print(f"{'Downloaded' if fresh else 'Exists'}: {destination}")
                except RateLimitError:
                    raise
                except (FetchError, OSError) as error:
                    failed += 1
                    print(f"Failed {record['match_id']}: {error}", file=sys.stderr)
            if args.max_replays is not None and selected >= args.max_replays:
                break
        print(f"Selected {selected}; downloaded {downloaded}; existing {skipped}; failed {failed}.")
        return 1 if failed else 0
    except (FetchError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; completed replay files are safe to reuse.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
