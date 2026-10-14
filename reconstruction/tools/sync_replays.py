"""Maintain a resumable local archive of all public Battlecode replays."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import sqlite3
import time

if __package__:
    from . import fetch_replays as fetch
    from .replay_catalog import Catalog, origin
else:
    import fetch_replays as fetch
    from replay_catalog import Catalog, origin


def timestamp(value):
    if not isinstance(value, str):
        raise fetch.FetchError("Missing timestamp in global match history")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp has no timezone")
        return parsed.astimezone(timezone.utc)
    except ValueError as error:
        raise fetch.FetchError(f"Invalid API timestamp: {value!r}") from error


@contextmanager
def sync_lock(outdir):
    """A process lock that is released by the OS even after an interruption."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with (outdir / ".sync.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise fetch.FetchError(f"Another sync is using {outdir}") from error
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def discover(client, catalog, max_pages=None, full=False):
    if full:
        catalog.set_meta("scan_state", None)
        catalog.set_meta("discovery_frontier", None)
        catalog.set_meta("history_complete", False)
    state = catalog.get_meta("scan_state")
    if not state:
        state = {"before": None, "stop_at": catalog.get_meta("discovery_frontier"), "newest": None,
                 "observed_at": datetime.now(timezone.utc).isoformat()}
        # Persist before any request/write so a partial page cannot leave a
        # previously complete rating timeline looking current.
        catalog.set_meta("scan_state", state)
    discovered = pages = 0
    while max_pages is None or pages < max_pages:
        params = {"scope": "all"}
        if state["before"]:
            params["before"] = state["before"]
        data = client.get_json("/api/v1/match-groups", params)
        groups = fetch.rows(data, "groups")
        if type(data.get("more")) is not bool:
            raise fetch.FetchError("Invalid global match-history pagination")
        if not groups and data["more"]:
            raise fetch.FetchError("Global match-history pagination made no progress")
        if groups and state["newest"] is None:
            state["newest"] = {"created_at": groups[0].get("createdAt"),
                               "id": fetch.identifier(groups[0].get("id"))}
            timestamp(state["newest"]["created_at"])
        stop = False
        for group in groups:
            created = timestamp(group.get("createdAt"))
            catalog.upsert_group(group)
            discovered += 1
            if state["stop_at"] and created < timestamp(state["stop_at"]["created_at"]):
                stop = True
        pages += 1
        if pages % 10 == 0:
            print(f"History: indexed {discovered} groups this pass.", file=sys.stderr)
        if not data["more"] or stop:
            if not data["more"]:
                catalog.set_meta("history_complete", True)
            if state["newest"]:
                catalog.set_meta("discovery_frontier", state["newest"])
                # A resumed backfill covers its original head snapshot. Games
                # starting later need another pass to include newer Elo events.
                catalog.set_meta("history_through", state.get("observed_at") or state["newest"]["created_at"])
            catalog.set_meta("scan_state", None)
            break
        last = groups[-1]
        cursor = f"{last['createdAt']}_{fetch.identifier(last.get('id'))}"
        if cursor == state["before"]:
            raise fetch.FetchError("Global match-history pagination repeated a cursor")
        if state["before"]:
            previous_time = state["before"].rsplit("_", 1)[0]
            if timestamp(last["createdAt"]) > timestamp(previous_time):
                raise fetch.FetchError("Global match-history pagination moved forward")
        state["before"] = cursor
        catalog.set_meta("scan_state", state)
    return discovered


def local_group_ids(outdir):
    groups = set()
    for path in Path(outdir).glob("*/*.repl26a"):
        try:
            groups.add(fetch.identifier(path.parent.name))
        except fetch.FetchError:
            continue
    return groups


def metadata_record(record):
    return {key: value for key, value in record.items()
            if key not in ("file_path", "downloaded", "error", "sha256", "byte_size")}


def sync_once(client, catalog, *, max_pages=None, max_details=None, max_downloads=None, full=False):
    if origin(client.base_url) != catalog.base_url:
        raise fetch.FetchError("Client origin does not match the replay catalog")
    stats = {"discovered": 0, "details": 0, "new_downloads": 0, "existing": 0, "errors": 0}
    try:
        stats["discovered"] = discover(client, catalog, max_pages, full)
    except (fetch.FetchError, OSError) as error:
        stats["errors"] += 1
        print(f"Discovery failed; checkpoint retained: {error}", file=sys.stderr)
    outdir = Path(catalog.outdir)
    local_groups = local_group_ids(outdir)
    # Import earlier fetcher downloads even if their groups predate a bounded scan.
    imported = local_groups - catalog.known_group_ids()
    detail_ids = list(imported) + list(catalog.group_ids_needing_detail())
    detail_ids = list(dict.fromkeys(detail_ids))
    detail_ids.sort(key=lambda group_id: group_id not in local_groups)
    detail_ids = catalog.order_attempts("detail", detail_ids)
    for group_id in detail_ids:
        if max_details is not None and stats["details"] >= max_details:
            break
        stats["details"] += 1
        catalog.mark_attempt("detail", group_id)
        try:
            data = client.get_json(f"/api/v1/match-groups/{group_id}")
            group = data.get("group")
            if not isinstance(group, dict) or fetch.identifier(group.get("id")) != group_id:
                raise fetch.FetchError(f"Wrong detail response for group {group_id}")
            catalog.store_detail(data)
        except (fetch.FetchError, OSError) as error:
            stats["errors"] += 1
            print(f"Group {group_id}: {error}", file=sys.stderr)

    # Refresh old metadata once the rating timeline becomes complete, and detect
    # deleted files. API discovery and download completion have separate cursors.
    for record in catalog.replay_records():
        if not record.get("downloaded"):
            continue
        path = outdir / record["file_path"]
        if not path.is_file():
            catalog.mark_failed(record["match_id"], "Local replay file is missing")
            continue
        try:
            fetch.write_metadata(client, metadata_record(record), path, expected_sha256=record.get("sha256"))
        except (fetch.FetchError, OSError) as error:
            catalog.mark_failed(record["match_id"], str(error))
            stats["errors"] += 1
            print(f"Metadata {record['match_id']}: {error}", file=sys.stderr)

    attempts = 0
    pending = {record["match_id"]: record for record in catalog.replay_records(pending_only=True)}
    for match_id in catalog.order_attempts("download", list(pending)):
        if max_downloads is not None and attempts >= max_downloads:
            break
        attempts += 1
        record = pending[match_id]
        catalog.mark_attempt("download", match_id)
        try:
            path, fresh = fetch.download_replay(client, record, outdir)
            metadata = fetch.write_metadata(client, metadata_record(record), path,
                                            expected_sha256=record.get("sha256") if not fresh else None)
            catalog.mark_downloaded(record["match_id"], path, metadata["sha256"], metadata["bytes"])
            stats["new_downloads"] += int(fresh)
            stats["existing"] += int(not fresh)
            print(f"{'Downloaded' if fresh else 'Indexed'}: {path}")
        except (fetch.FetchError, OSError) as error:
            catalog.mark_failed(record["match_id"], str(error))
            stats["errors"] += 1
            print(f"Replay {record['match_id']}: {error}", file=sys.stderr)
    stats.update(catalog.summary())
    stats["groups_pending"] = len(list(catalog.group_ids_needing_detail()))
    stats["history_complete"] = bool(catalog.get_meta("history_complete"))
    stats["discovery_in_progress"] = catalog.get_meta("scan_state") is not None
    return stats


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def list_replays(catalog, args):
    selected = []
    for record in catalog.replay_records(team=args.team, kind=args.kind, min_elo=args.min_elo):
        if not args.include_pending and not record["downloaded"]:
            continue
        selected.append(record)
        if args.limit is not None and len(selected) >= args.limit:
            break
    if args.json:
        print(json.dumps(selected, indent=2, ensure_ascii=False))
        return
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["match_id", "started_at", "map", "kind", "teams_and_elo_at_start", "downloaded", "file"])
    for record in selected:
        labels = []
        for team in record["teams"]:
            elo = team.get("elo_at_time")
            display = "unknown" if elo is None else f"{elo:.2f}"
            labels.append(f"{team['name']} ({display}; {team.get('elo_source', 'unknown')})")
        writer.writerow([record["match_id"], record.get("started_at"), record["map"], record["kind"],
                         " vs ".join(labels), record["downloaded"], record.get("file_path")])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=fetch.DEFAULT_OUTDIR)
    parser.add_argument("--base-url", default=fetch.DEFAULT_BASE_URL)
    parser.add_argument("--max-pages", type=fetch.positive_int, help="discovery pages this pass; checkpoint resumes later")
    parser.add_argument("--max-details", type=nonnegative_int, help="maximum group-detail requests this pass")
    parser.add_argument("--max-downloads", type=nonnegative_int, help="maximum replay download attempts this pass")
    parser.add_argument("--full", action="store_true", help="rescan all history, preserving existing replays")
    parser.add_argument("--watch", type=fetch.nonnegative_float, help="repeat sync every N seconds until interrupted")
    parser.add_argument("--timeout", type=fetch.nonnegative_float, default=30)
    parser.add_argument("--delay", type=fetch.nonnegative_float, default=.25)
    parser.add_argument("--retries", type=nonnegative_int, default=3)
    parser.add_argument("--list", action="store_true", help="query the local catalog without network access")
    parser.add_argument("--team", help="filter --list by team ID or exact historical name")
    parser.add_argument("--kind", choices=("ranked", "unranked", "tournament"))
    parser.add_argument("--min-elo", type=fetch.nonnegative_float, help="minimum known Elo at game start for --list")
    parser.add_argument("--limit", type=fetch.positive_int, default=100)
    parser.add_argument("--json", action="store_true", help="output --list as JSON instead of CSV")
    parser.add_argument("--include-pending", action="store_true", help="also list cataloged replays awaiting download")
    args = parser.parse_args(argv)
    if args.timeout == 0 or args.watch == 0:
        parser.error("--timeout and --watch must be positive")
    if not args.list and (args.team or args.kind or args.min_elo is not None or args.json or args.include_pending):
        parser.error("query filters require --list")
    if args.list and (args.watch or args.full):
        parser.error("--list cannot be combined with --watch or --full")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.list:
            if not (args.outdir / "catalog.sqlite3").exists():
                raise fetch.FetchError("No catalog found; run a sync first")
            with Catalog(args.outdir, args.base_url) as catalog:
                list_replays(catalog, args)
            return 0
        client = fetch.Client(args.base_url, args.timeout, args.delay, args.retries)
        with sync_lock(args.outdir), Catalog(args.outdir, client.base_url) as catalog:
            while True:
                try:
                    stats = sync_once(client, catalog, max_pages=args.max_pages, max_details=args.max_details,
                                      max_downloads=args.max_downloads, full=args.full)
                    print(json.dumps(stats, sort_keys=True))
                    result = int(stats["errors"] > 0)
                except (fetch.FetchError, OSError) as error:
                    print(f"Sync failed; checkpoint retained: {error}", file=sys.stderr)
                    result = 1
                if args.watch is None:
                    return result
                args.full = False
                print(f"Next sync in {args.watch:g} seconds.", file=sys.stderr)
                remaining = args.watch
                while remaining > 0:
                    pause = min(remaining, 60)
                    time.sleep(pause)
                    remaining -= pause
    except (fetch.FetchError, OSError, sqlite3.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped; the next sync resumes the saved checkpoint.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
