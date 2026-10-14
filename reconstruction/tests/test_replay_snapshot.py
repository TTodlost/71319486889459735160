"""Portable replay snapshots and refusal of corrupt/unrelated archive data."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import gzip
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "reconstruction"))
from tools import fetch_replays as fetch, replay_snapshot as snapshot
from tools.replay_catalog import Catalog
from tools.sync_replays import sync_lock

BASE_URL = "https://2026a.battlecode.co.uk"
GROUP = "64013d4f-257c-43c5-bf6a-6b0fd598a7f4"
MATCH = "187ad51c-b387-4db0-b9f5-157354de5398"
TEAM = "ee83f5e5-bcad-4923-86b4-b79ea6083d2e"
OPPONENT = "8a859a54-6acb-4e46-9f54-59d7e955b06c"
# Valid replay/map transport headers, rather than a full simulation fixture.
REPLAY = bytes.fromhex("6136327065720000000001000001") + b"w62pam\0\0" + bytes(range(1, 33))
REPLAY_PATH = f"{GROUP}/{MATCH}.repl26a"
JSON_PATH = f"{GROUP}/{MATCH}.json"
CHECKPOINT = {"before": "2026-10-14T10:00:00Z_" + GROUP,
              "stop_at": None, "newest": {"id": GROUP, "created_at": "2026-10-14T10:00:00Z"}}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="replay_snapshot_tests_")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.target = self.root / "restored"
        self.archive = self.root / "state.tar.gz"
        self.catalog = Catalog(self.source, BASE_URL)
        self.addCleanup(self.catalog.close)
        group = {
            "id": GROUP, "kind": "ranked", "status": "finished",
            "createdAt": "2026-10-14T10:00:00Z", "finishedAt": "2026-10-14T10:00:20Z",
            "ratedAt": "2026-10-14T10:00:30Z",
            "sides": [
                {"side": "a", "teamId": TEAM, "teamName": "Historical Team",
                 "ratingBefore": 1500.0, "ratingAfter": 1512.5},
                {"side": "b", "teamId": OPPONENT, "teamName": "Other Team",
                 "ratingBefore": 1730.75, "ratingAfter": 1718.25},
            ],
        }
        self.catalog.store_detail({"group": group, "matches": [{
            "id": MATCH, "map": "collision", "position": 1, "status": "finished",
            "createdAt": group["createdAt"], "startedAt": "2026-10-14T10:00:10Z",
            "finishedAt": group["finishedAt"], "replayUrl": f"/api/v1/matches/{MATCH}/bundle",
        }]})
        self.catalog.set_meta("scan_state", CHECKPOINT)
        self.replay = self.source / REPLAY_PATH
        self.replay.parent.mkdir(parents=True)
        self.replay.write_bytes(REPLAY)
        # NTFS stores 100ns units; Linux also preserves this non-second value.
        os.utime(self.replay, ns=(1_789_877_111_123_456_700,) * 2)
        record = next(self.catalog.replay_records())
        metadata = fetch.write_metadata(fetch.Client(BASE_URL), record, self.replay)
        self.sidecar = self.source / JSON_PATH
        os.utime(self.sidecar, ns=(1_789_877_113_765_432_100,) * 2)
        self.catalog.mark_downloaded(MATCH, self.replay, metadata["sha256"], metadata["bytes"])

    def pack(self):
        return snapshot.pack_snapshot(self.source, self.archive)

    def members(self):
        with tarfile.open(self.archive, "r:gz") as archive:
            return [(member, archive.extractfile(member).read()) for member in archive]

    def write_members(self, members):
        with tarfile.open(self.archive, "w:gz") as archive:
            for member, data in members:
                member.size = len(data)
                archive.addfile(member, BytesIO(data) if member.isfile() else None)

    def tamper_manifest(self, mutate):
        members = self.members()
        for index, (member, raw) in enumerate(members):
            if member.name == snapshot.MANIFEST:
                manifest = json.loads(raw)
                mutate(manifest)
                members[index] = member, json.dumps(manifest).encode()
        self.write_members(members)

    def replace_payload(self, name, replacement):
        members = self.members()
        for index, (member, raw) in enumerate(members):
            if member.name == name:
                members[index] = member, replacement
            elif member.name == snapshot.MANIFEST:
                manifest = json.loads(raw)
                for item in manifest["files"]:
                    if item["path"] == name:
                        item.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
                members[index] = member, json.dumps(manifest).encode()
        self.write_members(members)

    def tamper_catalog(self, sql, params):
        raw = next(raw for member, raw in self.members() if member.name == snapshot.CATALOG)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            path = Path(directory) / snapshot.CATALOG
            path.write_bytes(raw)
            connection = sqlite3.connect(path)
            try:
                connection.execute(sql, params)
                connection.commit()
            finally:
                connection.close()
            self.replace_payload(snapshot.CATALOG, path.read_bytes())

    def assert_bad_restore(self):
        with self.assertRaises(fetch.FetchError):
            snapshot.restore_snapshot(self.archive, self.target)
        if self.target.exists():
            self.assertEqual([path.name for path in self.target.iterdir()], [".sync.lock"])
        self.assertFalse(list(self.root.glob("replay_restore_*")))

    def test_roundtrip_preserves_bytes_precise_mtimes_and_catalog_state(self):
        before_records = list(self.catalog.replay_records())
        before_ratings = self.catalog.db.execute(
            "SELECT team_id,rating_before,rating_after FROM rating_events ORDER BY team_id").fetchall()
        source_bytes = {name: (self.source / name).read_bytes() for name in (REPLAY_PATH, JSON_PATH)}
        source_mtimes = {name: (self.source / name).stat().st_mtime_ns for name in (REPLAY_PATH, JSON_PATH)}
        packed = self.pack()
        restored = snapshot.restore_snapshot(self.archive, self.target)
        self.assertEqual(packed, restored)
        self.assertEqual(packed, {"version": 1, "source_base_url": BASE_URL,
                                  "file_count": 3, "replay_count": 1})
        for name in source_bytes:
            self.assertEqual((self.target / name).read_bytes(), source_bytes[name])
            self.assertEqual((self.target / name).stat().st_mtime_ns, source_mtimes[name])
        with Catalog(self.target, BASE_URL) as catalog:
            self.assertEqual(list(catalog.replay_records()), before_records)
            self.assertEqual(catalog.get_meta("scan_state"), CHECKPOINT)
            after = catalog.db.execute(
                "SELECT team_id,rating_before,rating_after FROM rating_events ORDER BY team_id").fetchall()
            self.assertEqual([tuple(row) for row in after], [tuple(row) for row in before_ratings])
        self.assertEqual(json.loads((self.target / JSON_PATH).read_text())["file_mtime_ns"],
                         (self.target / REPLAY_PATH).stat().st_mtime_ns)

    def test_backup_captures_committed_wal_without_wal_sidecars(self):
        self.catalog.db.execute("PRAGMA journal_mode=WAL")
        self.catalog.db.execute("PRAGMA wal_autocheckpoint=0")
        self.catalog.set_meta("wal_checkpoint", {"committed": "must survive"})
        self.assertTrue((self.source / "catalog.sqlite3-wal").is_file())
        self.pack()
        self.assertNotIn("catalog.sqlite3-wal", {member.name for member, _ in self.members()})
        snapshot.restore_snapshot(self.archive, self.target)
        with Catalog(self.target, BASE_URL) as catalog:
            self.assertEqual(catalog.get_meta("wal_checkpoint"), {"committed": "must survive"})
            self.assertEqual(catalog.db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_missing_cataloged_replay_is_preserved_as_missing_without_changing_database(self):
        self.replay.unlink()
        self.pack()
        snapshot.restore_snapshot(self.archive, self.target)
        self.assertFalse((self.target / REPLAY_PATH).exists())
        self.assertEqual((self.target / JSON_PATH).read_bytes(), self.sidecar.read_bytes())
        with Catalog(self.target, BASE_URL) as catalog:
            self.assertTrue(next(catalog.replay_records())["downloaded"])

    def test_transient_downloads_and_lock_are_excluded(self):
        (self.source / "tmp.part").write_bytes(b"unfinished root download")
        (self.replay.parent / "tmp.part").write_bytes(b"unfinished replay download")
        self.pack()
        names = {member.name for member, _ in self.members()}
        self.assertEqual(names, {snapshot.MANIFEST, snapshot.CATALOG, REPLAY_PATH, JSON_PATH})

    def test_unexpected_file_refused_and_previous_snapshot_preserved(self):
        self.archive.write_bytes(b"previous snapshot")
        (self.source / ".env").write_bytes(b"unrelated private configuration")
        with self.assertRaisesRegex(fetch.FetchError, "Unexpected durable"):
            self.pack()
        self.assertEqual(self.archive.read_bytes(), b"previous snapshot")

    def test_corrupt_replay_refused_without_publishing_snapshot(self):
        self.replay.write_bytes(b"bad replay")
        with self.assertRaises(fetch.FetchError):
            self.pack()
        self.assertFalse(self.archive.exists())

    def test_snapshot_inside_archive_is_refused(self):
        with self.assertRaisesRegex(fetch.FetchError, "outside"):
            snapshot.pack_snapshot(self.source, self.source / "state.tar.gz")

    def test_existing_catalog_or_replay_is_not_overwritten(self):
        self.pack()
        for name in (snapshot.CATALOG, REPLAY_PATH):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.root) as directory:
                target = Path(directory)
                existing = target / name
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes(b"existing content")
                with self.assertRaisesRegex(fetch.FetchError, "empty"):
                    snapshot.restore_snapshot(self.archive, target)
                self.assertEqual(existing.read_bytes(), b"existing content")

    def test_pack_and_restore_respect_sync_lock(self):
        self.pack()
        with sync_lock(self.source):
            with self.assertRaisesRegex(fetch.FetchError, "Another sync"):
                self.pack()
        with sync_lock(self.target):
            with self.assertRaisesRegex(fetch.FetchError, "Another sync"):
                snapshot.restore_snapshot(self.archive, self.target)

    def test_path_traversal_absolute_drive_and_backslash_paths_are_refused(self):
        self.pack()
        original = self.members()
        for name in ("../escape.txt", "/tmp/escape.txt", "C:/escape.txt", "C:escape.txt",
                     "..\\escape.txt", GROUP + "//" + MATCH + ".json",
                     GROUP + "/../escape.txt", ".env"):
            with self.subTest(name=name):
                malicious = tarfile.TarInfo(name)
                self.write_members(original + [(malicious, b"outside data")])
                self.assert_bad_restore()
        self.assertFalse((self.root / "escape.txt").exists())

    def test_links_directories_and_devices_are_refused(self):
        self.pack()
        original = self.members()
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind):
                malicious = tarfile.TarInfo("linked")
                malicious.type = kind
                malicious.linkname = "../escape.txt"
                self.write_members(original + [(malicious, b"")])
                self.assert_bad_restore()

    def test_duplicate_tar_member_is_refused(self):
        self.pack()
        members = self.members()
        self.write_members(members + [members[0]])
        self.assert_bad_restore()

    def test_duplicate_manifest_entry_is_refused(self):
        self.pack()
        self.tamper_manifest(lambda manifest: manifest["files"].append(dict(manifest["files"][0])))
        self.assert_bad_restore()

    def test_checksum_mismatch_is_refused(self):
        self.pack()
        self.tamper_manifest(lambda manifest: manifest["files"][0].update(sha256="0" * 64))
        self.assert_bad_restore()

    def test_size_mismatch_is_refused(self):
        self.pack()
        self.tamper_manifest(lambda manifest: manifest["files"][0].update(size=999999))
        self.assert_bad_restore()

    def test_missing_payload_or_manifest_is_refused(self):
        self.pack()
        original = self.members()
        for omitted in (REPLAY_PATH, snapshot.MANIFEST, snapshot.CATALOG):
            with self.subTest(omitted=omitted):
                self.write_members([(member, data) for member, data in original if member.name != omitted])
                self.assert_bad_restore()

    def test_manifest_rejects_version_invalid_hash_and_negative_mtime(self):
        self.pack()
        original = self.members()
        mutations = (lambda manifest: manifest.update(version=2),
                     lambda manifest: manifest["files"][0].update(sha256="x" * 64),
                     lambda manifest: manifest["files"][0].update(mtime_ns=-1),
                     lambda manifest: manifest.update(source_base_url="https://example.com/path"))
        for mutate in mutations:
            self.write_members(original)
            self.tamper_manifest(mutate)
            self.assert_bad_restore()

    def test_manifest_origin_must_match_catalog(self):
        self.pack()
        self.tamper_manifest(lambda manifest: manifest.update(source_base_url="https://example.com"))
        self.assert_bad_restore()

    def test_unrelated_or_damaged_sqlite_is_not_accepted_as_catalog(self):
        self.catalog.close()
        database = self.source / snapshot.CATALOG
        database.unlink()
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(fetch.FetchError, "missing table"):
            self.pack()
        database.write_bytes(b"not a database")
        with self.assertRaisesRegex(fetch.FetchError, "Invalid SQLite"):
            self.pack()

    def test_payload_validation_is_required_even_with_valid_checksum(self):
        self.pack()
        members = self.members()
        replacement = b"invalid replay headers"
        for index, (member, raw) in enumerate(members):
            if member.name == REPLAY_PATH:
                members[index] = member, replacement
            elif member.name == snapshot.MANIFEST:
                manifest = json.loads(raw)
                for item in manifest["files"]:
                    if item["path"] == REPLAY_PATH:
                        item.update(size=len(replacement), sha256=hashlib.sha256(replacement).hexdigest())
                members[index] = member, json.dumps(manifest).encode()
        self.write_members(members)
        self.assert_bad_restore()

    def test_truncated_gzip_does_not_publish_files(self):
        self.pack()
        self.archive.write_bytes(self.archive.read_bytes()[:50])
        self.assert_bad_restore()

    def test_missing_or_corrupt_gzip_footer_does_not_publish_files(self):
        self.pack()
        original = self.archive.read_bytes()
        corrupt_crc = bytearray(original)
        corrupt_crc[-8] ^= 1
        corrupt_size = bytearray(original)
        corrupt_size[-4] ^= 1
        for malformed in (original[:-8], original[:-1], corrupt_crc, corrupt_size):
            with self.subTest(footer=bytes(malformed[-8:])):
                self.archive.write_bytes(malformed)
                self.assert_bad_restore()

    def test_trailing_data_and_second_tar_are_not_ignored(self):
        self.pack()
        original = self.archive.read_bytes()
        raw = gzip.decompress(original)
        for malformed in (original + b"UNEXPECTED DATA",
                          gzip.compress(raw + b"UNEXPECTED DATA"),
                          gzip.compress(raw + raw), original + original):
            self.archive.write_bytes(malformed)
            self.assert_bad_restore()

    def test_missing_or_incomplete_tar_eof_blocks_are_refused(self):
        self.pack()
        original = self.archive.read_bytes()
        members = self.members()
        final = members[-1][0]
        payload_end = final.offset_data + (final.size + 511) // 512 * 512
        raw = gzip.decompress(original)
        for end in (payload_end, payload_end + 512, payload_end + 1023):
            self.archive.write_bytes(gzip.compress(raw[:end]))
            self.assert_bad_restore()

    def test_complete_tar_zero_padding_is_allowed(self):
        self.pack()
        self.archive.write_bytes(gzip.compress(gzip.decompress(self.archive.read_bytes()) + b"\0" * 1024))
        result = snapshot.restore_snapshot(self.archive, self.target)
        self.assertEqual(result["replay_count"], 1)
        self.assertEqual((self.target / REPLAY_PATH).read_bytes(), REPLAY)

    def test_pack_rejects_unsafe_catalog_paths_without_replacing_snapshot(self):
        self.pack()
        original = self.archive.read_bytes()
        paths = ("../../outside.repl26a", "/tmp/outside.repl26a", "C:/outside.repl26a",
                 REPLAY_PATH.replace("/", "\\"), GROUP + "/" + TEAM + ".repl26a",
                 REPLAY_PATH.upper(), None)
        for path in paths:
            with self.subTest(path=path):
                with self.catalog.db:
                    self.catalog.db.execute("UPDATE replays SET file_path=? WHERE match_id=?", (path, MATCH))
                with self.assertRaises(fetch.FetchError):
                    self.pack()
                self.assertEqual(self.archive.read_bytes(), original)

    def test_restore_rejects_unsafe_catalog_paths_with_valid_database_checksum(self):
        self.pack()
        original = self.members()
        for path in ("../../outside.repl26a", "C:/outside.repl26a", None):
            with self.subTest(path=path):
                self.write_members(original)
                self.tamper_catalog("UPDATE replays SET file_path=? WHERE match_id=?", (path, MATCH))
                self.assert_bad_restore()
        self.assertFalse((self.root / "outside.repl26a").exists())

    def test_record_identifiers_must_match_canonical_catalog_columns(self):
        self.pack()
        original = self.members()
        for field, value in (("match_id", TEAM), ("group_id", GROUP.upper())):
            with self.subTest(field=field):
                self.write_members(original)
                record = json.loads(self.catalog.db.execute(
                    "SELECT record_json FROM replays WHERE match_id=?", (MATCH,)).fetchone()[0])
                record[field] = value
                self.tamper_catalog("UPDATE replays SET record_json=? WHERE match_id=?",
                                    (json.dumps(record), MATCH))
                self.assert_bad_restore()

    def test_cli_pack_restore_and_failure_status(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(snapshot.main(["pack", "--outdir", str(self.source),
                                            "--snapshot", str(self.archive)]), 0)
        self.assertEqual(json.loads(output.getvalue())["replay_count"], 1)
        with redirect_stdout(StringIO()):
            self.assertEqual(snapshot.main(["restore", "--outdir", str(self.target),
                                            "--snapshot", str(self.archive)]), 0)
        with redirect_stderr(StringIO()):
            self.assertEqual(snapshot.main(["restore", "--outdir", str(self.target),
                                            "--snapshot", str(self.archive)]), 1)


if __name__ == "__main__":
    unittest.main()
