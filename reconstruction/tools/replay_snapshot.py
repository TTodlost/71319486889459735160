"""Pack or restore a checked, portable snapshot of a public replay archive.

Only replay files, their JSON sidecars, and a consistent SQLite catalog are
durable state. Restores require an empty destination and never extract tar
members directly onto an existing archive.
"""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import tarfile
import tempfile
import zlib

if __package__:
    from . import fetch_replays as fetch
    from .replay_catalog import origin
    from .sync_replays import sync_lock
else:
    import fetch_replays as fetch
    from replay_catalog import origin
    from sync_replays import sync_lock

VERSION = 1
MANIFEST = "manifest.json"
CATALOG = "catalog.sqlite3"
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
REQUIRED_COLUMNS = {
    "meta": {"key", "value_json"},
    "groups": {"id", "kind", "status", "created_at", "finished_at", "rated_at",
               "summary_json", "detail_json", "detail_needed"},
    "rating_events": {"group_id", "team_id", "team_name", "side", "rated_at",
                      "rating_before", "rating_after"},
    "replays": {"match_id", "group_id", "record_json", "file_path", "downloaded",
                "sha256", "byte_size", "error"},
    "attempts": {"kind", "identifier", "sequence"},
}
SQLITE_TRANSIENT = {CATALOG + suffix for suffix in ("-wal", "-shm", "-journal")}


class SnapshotError(fetch.FetchError):
    """An unsafe or inconsistent archive snapshot."""


def _linked(path):
    info = path.lstat()
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _root(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        if _linked(path) or not path.is_dir():
            raise SnapshotError(f"Archive directory must be a real directory: {path}")
    return path.resolve()


def _check_lock(root):
    lock = root / ".sync.lock"
    if lock.exists() or lock.is_symlink():
        if _linked(lock) or not lock.is_file():
            raise SnapshotError(f"Unsafe archive lock: {lock}")


def _uuid(value):
    try:
        return fetch.identifier(value) == value
    except fetch.FetchError:
        return False


def _payload_path(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or "\0" in name or name.startswith("/")):
        raise SnapshotError(f"Unsafe snapshot path: {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SnapshotError(f"Unsafe snapshot path: {name!r}")
    if name == CATALOG:
        return name
    if len(parts) == 2 and _uuid(parts[0]):
        filename = PurePosixPath(parts[1])
        if filename.suffix in (".repl26a", ".json") and _uuid(filename.stem):
            return name
    raise SnapshotError(f"Unexpected durable file in replay archive: {name!r}")


def _inventory(root):
    """Refuse unrelated files rather than uploading them accidentally."""
    files = []
    for entry in sorted(root.iterdir()):
        if _linked(entry):
            raise SnapshotError(f"Archive contains a link: {entry}")
        if entry.is_file():
            if entry.name == ".sync.lock" or entry.suffix == ".part" or entry.name in SQLITE_TRANSIENT:
                continue
            _payload_path(entry.name)
            files.append(entry)
        elif entry.is_dir() and _uuid(entry.name):
            for child in sorted(entry.iterdir()):
                if _linked(child) or not child.is_file():
                    raise SnapshotError(f"Unexpected archive entry: {child}")
                if child.suffix == ".part":
                    continue
                _payload_path(child.relative_to(root).as_posix())
                files.append(child)
        else:
            raise SnapshotError(f"Unexpected archive entry: {entry}")
    if root / CATALOG not in files:
        raise SnapshotError("Replay snapshot requires catalog.sqlite3")
    return files


def _database(path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)


def _validate_catalog(path):
    try:
        connection = _database(path)
        try:
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise SnapshotError("SQLite catalog failed quick_check")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table, required in REQUIRED_COLUMNS.items():
                if table not in tables:
                    raise SnapshotError(f"SQLite catalog is missing table {table}")
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if not required <= columns:
                    raise SnapshotError(f"SQLite catalog has incompatible table {table}")
            row = connection.execute("SELECT value_json FROM meta WHERE key='base_url'").fetchone()
            if row is None:
                raise SnapshotError("SQLite catalog is missing its website origin")
            base_url = json.loads(row[0])
            if not isinstance(base_url, str) or origin(base_url) != base_url:
                raise SnapshotError("SQLite catalog has an invalid website origin")
            for match_id, group_id, file_path, downloaded, encoded in connection.execute(
                    "SELECT match_id,group_id,file_path,downloaded,record_json FROM replays"):
                if not _uuid(match_id) or not _uuid(group_id):
                    raise SnapshotError("SQLite catalog has noncanonical replay identifiers")
                expected_path = f"{group_id}/{match_id}.repl26a"
                if file_path is not None and file_path != expected_path:
                    raise SnapshotError(f"SQLite catalog has an unsafe replay path: {file_path!r}")
                if downloaded not in (0, 1) or (downloaded and file_path is None):
                    raise SnapshotError(f"SQLite catalog has invalid replay download state: {match_id}")
                record = json.loads(encoded)
                if (not isinstance(record, dict) or record.get("match_id") != match_id
                        or record.get("group_id") != group_id):
                    raise SnapshotError(f"SQLite replay record identifiers do not match: {match_id}")
            return base_url
        finally:
            connection.close()
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise SnapshotError(f"Invalid SQLite replay catalog: {error}") from error


class _HashingReader:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.handle.read(size)
        self.digest.update(data)
        return data


def _add_file(archive, path, name, mtime_ns=None):
    info = path.stat()
    mtime_ns = info.st_mtime_ns if mtime_ns is None else mtime_ns
    member = tarfile.TarInfo(name)
    member.size = info.st_size
    member.mtime = mtime_ns // 1_000_000_000
    member.mode = 0o600
    with path.open("rb") as handle:
        reader = _HashingReader(handle)
        archive.addfile(member, reader)
        after = path.stat()
        if handle.read(1) or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            raise SnapshotError(f"File changed while snapshotting: {path}")
    return {"path": name, "size": info.st_size, "sha256": reader.digest.hexdigest(),
            "mtime_ns": mtime_ns}


def _summary(manifest):
    return {"version": manifest["version"], "source_base_url": manifest["source_base_url"],
            "file_count": len(manifest["files"]),
            "replay_count": sum(item["path"].endswith(".repl26a") for item in manifest["files"])}


def pack_snapshot(outdir, destination):
    """Atomically write a catalog backup plus all existing replays/sidecars."""
    root = _root(outdir)
    if not root.is_dir():
        raise SnapshotError(f"Replay archive does not exist: {root}")
    destination = Path(destination).resolve()
    if destination.is_relative_to(root):
        raise SnapshotError("Snapshot destination must be outside the replay archive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_lock(root)
    with sync_lock(root), tempfile.TemporaryDirectory(prefix="replay_snapshot_", dir=destination.parent) as temporary:
        files = _inventory(root)
        source_catalog = root / CATALOG
        catalog_mtime = source_catalog.stat().st_mtime_ns
        backup = Path(temporary) / CATALOG
        source = _database(source_catalog)
        try:
            target = sqlite3.connect(backup)
            try:
                source.backup(target)
            finally:
                target.close()
        except sqlite3.Error as error:
            raise SnapshotError(f"Invalid SQLite replay catalog: {error}") from error
        finally:
            source.close()
        base_url = _validate_catalog(backup)
        manifest = {"version": VERSION, "source_base_url": base_url, "files": []}
        packed = Path(temporary) / "snapshot.tar.gz"
        with tarfile.open(packed, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for path in files:
                name = path.relative_to(root).as_posix()
                if path.suffix == ".repl26a":
                    fetch.validate_replay(path)
                manifest["files"].append(_add_file(
                    archive, backup if name == CATALOG else path, name,
                    catalog_mtime if name == CATALOG else None))
            encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise SnapshotError("Replay snapshot manifest is too large")
            member = tarfile.TarInfo(MANIFEST)
            member.size = len(encoded)
            member.mode = 0o600
            archive.addfile(member, BytesIO(encoded))
        os.replace(packed, destination)
    return _summary(manifest)


def _manifest(raw):
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise SnapshotError("Invalid snapshot manifest JSON") from error
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != VERSION or not isinstance(data.get("files"), list)):
        raise SnapshotError("Unsupported or invalid snapshot manifest")
    base_url = data.get("source_base_url")
    try:
        valid_origin = isinstance(base_url, str) and origin(base_url) == base_url
    except fetch.FetchError:
        valid_origin = False
    if not valid_origin:
        raise SnapshotError("Snapshot manifest has an invalid website origin")
    names = set()
    for item in data["files"]:
        if not isinstance(item, dict):
            raise SnapshotError("Invalid snapshot manifest file record")
        name = _payload_path(item.get("path"))
        if name in names:
            raise SnapshotError(f"Duplicate snapshot manifest path: {name}")
        names.add(name)
        digest = item.get("sha256")
        if (type(item.get("size")) is not int or item["size"] < 0
                or type(item.get("mtime_ns")) is not int or not 0 <= item["mtime_ns"] <= 2**63 - 1
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise SnapshotError(f"Invalid snapshot manifest fields: {name}")
    if CATALOG not in names:
        raise SnapshotError("Snapshot manifest is missing catalog.sqlite3")
    return data


def _empty(root):
    for path in root.iterdir():
        if path.name != ".sync.lock" or _linked(path) or not path.is_file():
            raise SnapshotError(f"Restore requires an empty replay directory: {root}")


def _check_tar_end(archive, payload_end):
    """Drain gzip to its checked footer; tar readers stop at the first EOF block."""
    archive.fileobj.seek(payload_end)
    padding = 0
    while chunk := archive.fileobj.read(1024 * 1024):
        if chunk.strip(b"\0"):
            raise SnapshotError("Snapshot has data after the end of its tar members")
        padding += len(chunk)
    # Tar needs two complete zero EOF blocks. More complete zero padding is
    # valid, but a second tar or an incomplete block must never be ignored.
    if padding < 2 * tarfile.BLOCKSIZE or padding % tarfile.BLOCKSIZE:
        raise SnapshotError("Snapshot is missing complete tar EOF blocks")


def restore_snapshot(snapshot, outdir):
    """Validate the entire snapshot before publishing into an empty directory."""
    snapshot = Path(snapshot).resolve()
    root = _root(outdir)
    root.parent.mkdir(parents=True, exist_ok=True)
    _check_lock(root)
    with sync_lock(root):
        _empty(root)
        with tempfile.TemporaryDirectory(prefix="replay_restore_", dir=root.parent) as temporary:
            stage = Path(temporary)
            observed = {}
            seen = set()
            manifest = None
            try:
                with tarfile.open(snapshot, "r:gz") as archive:
                    payload_end = 0
                    for member in archive:
                        name = member.name
                        if name in seen:
                            raise SnapshotError(f"Duplicate snapshot member: {name}")
                        seen.add(name)
                        if (member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                                or member.sparse is not None or member.size < 0):
                            raise SnapshotError(f"Snapshot member is not a regular file: {name}")
                        payload_end = member.offset_data + (
                            (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE * tarfile.BLOCKSIZE)
                        if name != MANIFEST:
                            _payload_path(name)
                        handle = archive.extractfile(member)
                        if handle is None:
                            raise SnapshotError(f"Missing snapshot member content: {name}")
                        with handle:
                            if name == MANIFEST:
                                if member.size > MAX_MANIFEST_BYTES:
                                    raise SnapshotError("Replay snapshot manifest is too large")
                                manifest = _manifest(handle.read())
                                continue
                            destination = stage.joinpath(*name.split("/"))
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            digest = hashlib.sha256()
                            size = 0
                            with destination.open("xb") as output:
                                while chunk := handle.read(1024 * 1024):
                                    digest.update(chunk)
                                    size += len(chunk)
                                    output.write(chunk)
                            if size != member.size:
                                raise SnapshotError(f"Truncated snapshot member: {name}")
                            observed[name] = (size, digest.hexdigest())
                    _check_tar_end(archive, payload_end)
            except (tarfile.TarError, EOFError, OSError, zlib.error) as error:
                raise SnapshotError(f"Unable to read replay snapshot: {error}") from error
            if manifest is None:
                raise SnapshotError("Snapshot is missing manifest.json")
            expected = {item["path"] for item in manifest["files"]}
            if observed.keys() != expected:
                raise SnapshotError("Snapshot members do not match its manifest")
            for item in manifest["files"]:
                name = item["path"]
                if observed[name] != (item["size"], item["sha256"]):
                    raise SnapshotError(f"Snapshot size or checksum mismatch: {name}")
                path = stage.joinpath(*name.split("/"))
                if path.suffix == ".repl26a":
                    fetch.validate_replay(path)
                os.utime(path, ns=(item["mtime_ns"], item["mtime_ns"]))
            if _validate_catalog(stage / CATALOG) != manifest["source_base_url"]:
                raise SnapshotError("Snapshot website origin does not match its catalog")
            _empty(root)
            # Publish only checked files; preserve an interrupted publish rather
            # than deleting anything a subsequent invocation must inspect.
            for entry in sorted(stage.iterdir()):
                if (root / entry.name).exists():
                    raise SnapshotError(f"Restore destination appeared during publication: {entry.name}")
                if entry.is_file():
                    fetch.publish_new(entry, root / entry.name)
                    entry.unlink(missing_ok=True)
                else:
                    entry.rename(root / entry.name)
    return _summary(manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pack", "restore"))
    parser.add_argument("--outdir", type=Path, default=fetch.DEFAULT_OUTDIR)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (pack_snapshot(args.outdir, args.snapshot) if args.command == "pack"
                  else restore_snapshot(args.snapshot, args.outdir))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (fetch.FetchError, OSError, sqlite3.Error, tarfile.TarError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
