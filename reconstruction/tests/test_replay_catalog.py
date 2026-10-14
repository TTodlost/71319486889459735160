"""Persistent inventory and historical rating boundaries, without HTTP."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "reconstruction"))
from tools.replay_catalog import Catalog, FetchError

BASE = "https://2026a.battlecode.co.uk"
TEAM = str(UUID(int=1))
OTHER = str(UUID(int=2))


def group(number, *, kind="ranked", rated="2026-10-14T12:00:00Z",
          before=1500, after=1510, other_before=1500, other_after=1490,
          created="2026-10-14T09:00:00Z", status="finished"):
    return {"id": str(UUID(int=number)), "kind": kind, "status": status,
            "createdAt": created, "finishedAt": "2026-10-14T11:30:00Z", "ratedAt": rated,
            "sides": [{"side": "a", "teamId": TEAM, "teamName": "Mite", "ratingBefore": before,
                       "ratingAfter": after, "version": 1},
                      {"side": "b", "teamId": OTHER, "teamName": "Other", "ratingBefore": other_before,
                       "ratingAfter": other_after, "version": 2}]}


def unranked(number, **kwargs):
    return group(number, kind="unranked", rated=None, before=None, after=None,
                 other_before=None, other_after=None, **kwargs)


def detail(source, *, started="2026-10-14T13:00:00Z", available=True, status="finished"):
    match_id = str(UUID(int=UUID(source["id"]).int + 10000))
    match = {"id": match_id, "map": "collision", "position": 1, "status": status,
             "createdAt": source["createdAt"], "startedAt": started,
             "finishedAt": "2026-10-14T13:10:00Z",
             "replayUrl": f"/api/v1/matches/{match_id}/bundle" if available else None,
             "sides": [{"side": "a", "teamId": TEAM, "teamName": "Mite", "result": "win", "score": 100},
                       {"side": "b", "teamId": OTHER, "teamName": "Other", "result": "loss", "score": 80}]}
    return {"group": deepcopy(source), "matches": [match]}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="catalog-test-", dir=ROOT)
        self.outdir = Path(self.temporary.name)
        self.catalog = Catalog(self.outdir, BASE)

    def tearDown(self):
        self.catalog.close()
        self.temporary.cleanup()

    def rows(self):
        return list(self.catalog.replay_records())

    def side(self, record=None, index=0):
        return (record or self.rows()[0])["teams"][index]

    def complete(self):
        self.catalog.set_meta("history_complete", True)

    def test_restart_idempotence_and_origin_binding(self):
        source = unranked(100)
        self.assertTrue(self.catalog.upsert_group(source))
        self.catalog.store_detail(detail(source))
        self.assertFalse(self.catalog.upsert_group(source))
        self.catalog.store_detail(detail(source))
        self.catalog.set_meta("scan_state", {"cursor": "example", "pages": 3})
        self.catalog.close()
        self.catalog = Catalog(self.outdir, BASE + "/")
        self.assertEqual(self.catalog.known_group_ids(), {source["id"]})
        self.assertEqual(self.catalog.summary(), {"groups": 1, "replays": 1, "downloaded": 0,
                                                 "pending": 1, "failed": 0})
        self.assertEqual(self.catalog.get_meta("scan_state"), {"cursor": "example", "pages": 3})
        with self.assertRaisesRegex(FetchError, "belongs to"):
            Catalog(self.outdir, "https://another-event.example")
        with self.assertRaises(FetchError):
            self.catalog.set_meta("base_url", "https://another-event.example")

    def test_catalog_cannot_return_paths_outside_its_group_match_layout(self):
        source = unranked(161)
        self.catalog.store_detail(detail(source))
        record = self.rows()[0]
        expected = f"{source['id']}/{record['match_id']}.repl26a"
        for unsafe in ("../outside.repl26a", "/tmp/outside.repl26a", "C:/outside.repl26a",
                       expected.replace("/", "\\"), f"{source['id']}/other.repl26a"):
            with self.subTest(path=unsafe):
                with self.catalog.db:
                    self.catalog.db.execute("UPDATE replays SET file_path=?,downloaded=1", (unsafe,))
                with self.assertRaisesRegex(FetchError, "path"):
                    self.rows()
        # Missing files may still be cataloged: sync must be able to requeue them.
        with self.catalog.db:
            self.catalog.db.execute("UPDATE replays SET file_path=?", (expected,))
        self.assertEqual(self.rows()[0]['file_path'], expected)
        with self.catalog.db:
            self.catalog.db.execute("UPDATE replays SET file_path=NULL")
        with self.assertRaisesRegex(FetchError, "path"):
            self.rows()

    def test_catalog_replay_json_identifiers_must_match_database_columns(self):
        import json
        self.catalog.store_detail(detail(unranked(162)))
        original = self.rows()[0]
        for field in ("match_id", "group_id"):
            with self.subTest(field=field):
                record = deepcopy(original)
                record[field] = "../outside"
                with self.catalog.db:
                    self.catalog.db.execute("UPDATE replays SET record_json=?", (json.dumps(record),))
                with self.assertRaisesRegex(FetchError, "identifiers"):
                    self.rows()

    def test_group_summary_rates_both_sides_before_detail(self):
        rated = group(101, after=1510.123456789, other_after=1489.876543211)
        self.catalog.upsert_group(rated)
        self.catalog.store_detail(detail(unranked(102)))
        self.complete()
        record = self.rows()[0]
        self.assertEqual(self.side(record)["elo_at_time"], 1510.123456789)
        self.assertEqual(self.side(record, 1)["elo_at_time"], 1489.876543211)
        self.assertEqual(self.side(record)["elo_source_group_id"], rated["id"])
        self.assertEqual(self.side(record)["elo_observed_at"], rated["ratedAt"])

    def test_rated_time_order_is_independent_of_group_creation(self):
        first = group(103, created="2026-10-14T10:00:00Z")
        second = group(104, created="2026-10-14T08:00:00Z", rated="2026-10-14T13:00:00Z",
                       before=1510, after=1525, other_before=1490, other_after=1475)
        self.catalog.upsert_group(second)
        self.catalog.upsert_group(first)
        self.catalog.store_detail(detail(unranked(105), started="2026-10-14T13:30:00Z"))
        self.complete()
        self.assertEqual(self.side()["elo_at_time"], 1525)
        self.assertEqual(self.side()["elo_source_group_id"], second["id"])

    def test_actual_datetime_offsets_and_partial_history(self):
        source = group(106, rated="2026-10-14T15:00:00+03:00")
        self.catalog.upsert_group(source)
        self.catalog.store_detail(detail(unranked(107), started="2026-10-14T14:00:00+01:00"))
        self.assertEqual(self.side()["elo_source"], "unknown")
        self.assertIsNone(self.side()["elo_at_time"])
        self.complete()
        self.assertEqual(self.side()["elo_at_time"], 1510)
        self.catalog.set_meta("history_complete", False)
        self.assertIsNone(self.side()["elo_at_time"])

    def test_initial_baseline_and_group_ratings_are_separate(self):
        source = group(108)
        self.catalog.store_detail(detail(source, started="2026-10-14T11:00:00Z"))
        self.complete()
        side = self.side()
        self.assertEqual((side["elo_at_time"], side["elo_source"]), (1500, "initial"))
        self.assertEqual((side["elo_before"], side["elo_after"]), (1500, 1510))

    def test_active_incremental_scan_suppresses_as_of_elo(self):
        self.catalog.upsert_group(group(133))
        self.catalog.store_detail(detail(unranked(134)))
        self.complete()
        self.assertEqual(self.side()["elo_at_time"], 1510)
        self.catalog.set_meta("scan_state", {"before": "cursor", "had_complete": True})
        self.assertEqual(self.side()["elo_source"], "unknown")
        self.assertIsNone(self.side()["elo_at_time"])
        self.catalog.set_meta("scan_state", None)
        self.assertEqual(self.side()["elo_at_time"], 1510)

    def test_equal_timestamp_changed_event_is_ambiguous(self):
        self.catalog.upsert_group(group(109))
        self.catalog.store_detail(detail(unranked(110), started="2026-10-14T12:00:00Z"))
        self.complete()
        self.assertEqual(self.side()["elo_source"], "ambiguous")
        self.assertIsNone(self.side()["elo_at_time"])

    def test_equal_timestamp_without_rating_change_preserves_initial(self):
        self.catalog.upsert_group(group(135, after=1500, other_after=1500))
        self.catalog.store_detail(detail(unranked(136), started="2026-10-14T12:00:00Z"))
        self.complete()
        self.assertEqual((self.side()["elo_at_time"], self.side()["elo_source"]), (1500, "initial"))

    def test_missing_time_and_no_rating_history_are_explicit(self):
        self.catalog.upsert_group(group(111))
        self.catalog.store_detail(detail(unranked(112), started=None))
        self.complete()
        self.assertEqual(self.side()["elo_source"], "missing_time")
        self.catalog.store_detail(detail(unranked(113)))
        no_events = Catalog(self.outdir / "fresh", BASE)
        try:
            no_events.store_detail(detail(unranked(114)))
            no_events.set_meta("history_complete", True)
            self.assertEqual(next(no_events.replay_records())["teams"][0]["elo_source"], "unknown")
        finally:
            no_events.close()

    def test_chain_gap_invalidates_interval_before_gap_but_not_earlier_baseline(self):
        self.catalog.upsert_group(group(115))
        self.catalog.upsert_group(group(116, rated="2026-10-14T14:00:00Z", before=1600, after=1610,
                                       other_before=1400, other_after=1390))
        self.catalog.store_detail(detail(unranked(117), started="2026-10-14T13:00:00Z"))
        self.catalog.store_detail(detail(unranked(118), started="2026-10-14T15:00:00Z"))
        self.catalog.store_detail(detail(unranked(139), started="2026-10-14T11:00:00Z"))
        self.complete()
        records = {row["group_id"]: row for row in self.rows()}
        # 1510 at noon and 1600 before 14:00 imply an unobserved transition.
        # It could have occurred at 12:30, so a 13:00 replay has unknown Elo.
        self.assertEqual(self.side(records[str(UUID(int=117))])["elo_source"], "history_gap")
        self.assertIsNone(self.side(records[str(UUID(int=117))])["elo_at_time"])
        self.assertEqual(self.side(records[str(UUID(int=118))])["elo_source"], "history_gap")
        self.assertEqual(self.side(records[str(UUID(int=139))])["elo_at_time"], 1500)
        self.assertEqual(self.side(records[str(UUID(int=139))])["elo_source"], "initial")

    def test_missing_ratings_or_unanchored_initial_event_make_gap(self):
        source = group(119, before=1499)
        self.catalog.upsert_group(source)
        self.catalog.store_detail(detail(unranked(120)))
        self.complete()
        self.assertEqual(self.side()["elo_source"], "history_gap")
        source["sides"][0]["ratingBefore"] = 1500
        source["sides"][0]["ratingAfter"] = None
        self.catalog.upsert_group(source)
        self.assertEqual(self.side()["elo_source"], "history_gap")
        source["sides"][0]["ratingAfter"] = 1510
        source["ratedAt"] = None
        self.catalog.upsert_group(source)
        self.assertEqual(self.side()["elo_source"], "history_gap")

    def test_differing_ties_are_ambiguous_but_identical_snapshots_are_safe(self):
        self.catalog.upsert_group(group(121))
        second = group(122)
        self.catalog.upsert_group(second)
        self.catalog.store_detail(detail(unranked(123)))
        self.complete()
        self.assertEqual(self.side()["elo_at_time"], 1510)
        second["sides"][0]["ratingAfter"] = 1490
        self.catalog.upsert_group(second)
        self.assertEqual(self.side()["elo_source"], "ambiguous")

    def test_pending_old_group_refresh_and_newest_first_detail_queue(self):
        old = unranked(124, status="running", created="2026-10-13T09:00:00Z")
        newer = unranked(125)
        self.catalog.upsert_group(old)
        self.catalog.upsert_group(newer)
        self.catalog.store_detail(detail(old, available=False, status="running"))
        self.assertEqual(self.catalog.group_ids_needing_detail(), [newer["id"], old["id"]])
        old["status"] = "finished"
        self.assertTrue(self.catalog.upsert_group(old))
        self.catalog.store_detail(detail(old))
        self.assertEqual(self.catalog.group_ids_needing_detail(), [newer["id"]])
        self.catalog.store_detail(detail(newer, available=False))
        self.assertIn(newer["id"], self.catalog.group_ids_needing_detail())

    def test_finished_ranked_without_rating_stays_pending(self):
        source = group(126, rated=None, before=None, after=None, other_before=None, other_after=None)
        self.catalog.store_detail(detail(source))
        self.assertEqual(self.catalog.group_ids_needing_detail(), [source["id"]])
        source = group(126)
        self.assertTrue(self.catalog.upsert_group(source))
        self.catalog.store_detail(detail(source))
        self.assertEqual(self.catalog.group_ids_needing_detail(), [])

    def test_malformed_numbers_and_timestamps_do_not_commit(self):
        for value in (True, "1500", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(value=value):
                with self.assertRaises(FetchError):
                    self.catalog.upsert_group(group(127, before=value))
        for value in ("not-a-date", "2026-10-14T12:00:00", 42):
            with self.subTest(value=value):
                with self.assertRaises(FetchError):
                    self.catalog.upsert_group(group(128, rated=value))
        with self.assertRaises(FetchError):
            self.catalog.store_detail(detail(group(129), started="invalid"))
        self.assertEqual(self.catalog.summary()["groups"], 0)

    def test_match_cannot_move_between_groups(self):
        first = detail(unranked(137))
        self.catalog.store_detail(first)
        other = detail(unranked(138))
        other["matches"][0]["id"] = first["matches"][0]["id"]
        with self.assertRaisesRegex(FetchError, "another match group"):
            self.catalog.store_detail(other)
        self.assertEqual(self.catalog.summary()["groups"], 1)

    def test_download_failures_relative_paths_and_restart(self):
        source = unranked(130)
        self.catalog.store_detail(detail(source))
        record = self.rows()[0]
        match_id = record["match_id"]
        path = self.outdir / source["id"] / f"{match_id}.repl26a"
        self.catalog.mark_failed(match_id, "network interruption")
        self.assertEqual(self.catalog.summary()["failed"], 1)
        self.assertEqual(len(list(self.catalog.replay_records(pending_only=True))), 1)
        self.catalog.mark_downloaded(match_id, path, "a" * 64, 500)
        self.assertEqual(self.rows()[0]["file_path"], f"{source['id']}/{match_id}.repl26a")
        self.assertTrue(self.rows()[0]["downloaded"])
        self.assertIsNone(self.rows()[0]["error"])
        self.assertEqual(list(self.catalog.replay_records(pending_only=True)), [])
        self.catalog.store_detail(detail(source))
        self.assertTrue(self.rows()[0]["downloaded"])
        self.catalog.close()
        self.catalog = Catalog(self.outdir, BASE)
        self.assertEqual(self.catalog.summary()["downloaded"], 1)
        with self.assertRaises(FetchError):
            self.catalog.mark_downloaded(match_id, self.outdir.parent / "outside.repl26a", "a" * 64, 500)
        with self.assertRaises(FetchError):
            self.catalog.mark_downloaded(match_id, self.outdir / "wrong.repl26a", "a" * 64, 500)

    def test_filters_use_selected_team_elo_and_historical_names(self):
        self.catalog.upsert_group(group(131))
        self.catalog.store_detail(detail(unranked(132)))
        self.complete()
        self.assertEqual(len(list(self.catalog.replay_records(min_elo=1505))), 1)
        self.assertEqual(len(list(self.catalog.replay_records(team="mItE", min_elo=1505))), 1)
        self.assertEqual(list(self.catalog.replay_records(team=OTHER, min_elo=1505)), [])
        self.assertEqual(list(self.catalog.replay_records(kind="ranked")), [])
        self.assertEqual(list(self.catalog.replay_records(team="renamed team")), [])
        self.assertEqual(list(self.catalog.replay_records(limit=0)), [])

    def test_attempt_order_is_fair_for_failed_new_and_older_groups(self):
        newer = str(UUID(int=140))
        older = str(UUID(int=141))
        unattempted = str(UUID(int=142))
        self.assertEqual(self.catalog.order_attempts("detail", [newer, older]), [newer, older])
        first = self.catalog.mark_attempt("detail", newer)
        self.assertGreater(first, 0)
        self.assertEqual(self.catalog.known_group_ids(), set())  # Failed API import has no group yet.
        self.assertEqual(self.catalog.order_attempts("detail", [newer, older]), [older, newer])
        second = self.catalog.mark_attempt("detail", older)
        self.assertGreater(second, first)
        self.assertEqual(self.catalog.order_attempts("detail", [older, newer, unattempted]),
                         [newer, older, unattempted])
        self.catalog.mark_attempt("detail", newer)
        self.assertEqual(self.catalog.order_attempts("detail", [newer, older]), [older, newer])

    def test_attempt_sequences_persist_and_kinds_have_separate_priority(self):
        first_id = str(UUID(int=143))
        second_id = str(UUID(int=144))
        first = self.catalog.mark_attempt("download", first_id)
        self.catalog.close()
        self.catalog = Catalog(self.outdir, BASE)
        self.assertEqual(self.catalog.order_attempts("download", [first_id, second_id]), [first_id, second_id])
        self.assertEqual(self.catalog.order_attempts("detail", [first_id, second_id]), [first_id, second_id])
        second = self.catalog.mark_attempt("detail", second_id)
        self.assertGreater(second, first)
        third = self.catalog.mark_attempt("download", first_id)
        self.assertGreater(third, second)
        self.assertEqual(self.catalog.order_attempts("detail", [second_id, first_id]), [first_id, second_id])
        with self.assertRaises(FetchError):
            self.catalog.mark_attempt("other", first_id)
        with self.assertRaises(FetchError):
            self.catalog.order_attempts("detail", ["invalid-id"])

    def test_new_arrivals_cannot_jump_already_queued_work(self):
        first, older, arriving = (str(UUID(int=value)) for value in (150, 151, 152))
        self.catalog.order_attempts("download", [first, older])
        self.catalog.mark_attempt("download", first)
        self.catalog.close()
        self.catalog = Catalog(self.outdir, BASE)
        ordered = self.catalog.order_attempts("download", [arriving, first, older])
        self.assertEqual(ordered, [older, first, arriving])

    def test_game_start_after_discovery_snapshot_requires_newer_history(self):
        self.catalog.upsert_group(group(153))
        self.catalog.store_detail(detail(unranked(154)))
        self.complete()
        record = self.rows()[0]
        self.assertEqual(record['teams'][0]['elo_at_time'], 1510.0)
        self.catalog.set_meta('history_through', '2026-10-14T12:00:30Z')
        record = self.rows()[0]
        self.assertIsNone(record['teams'][0]['elo_at_time'])
        self.assertEqual(record['teams'][0]['elo_source'], 'after_snapshot')
        self.catalog.set_meta('history_through', record['started_at'])
        self.assertEqual(self.rows()[0]['teams'][0]['elo_at_time'], 1510.0)

    def test_old_unresolved_ranked_group_blocks_later_elo_until_refreshed(self):
        self.catalog.upsert_group(group(155))
        pending = group(156, created='2026-10-14T08:00:00Z', status='running',
                        rated=None, before=None, after=None, other_before=None, other_after=None)
        self.catalog.upsert_group(pending)
        self.catalog.store_detail(detail(unranked(157), started='2026-10-14T13:30:00Z'))
        self.complete()
        self.catalog.set_meta('history_through', '2026-10-14T14:00:00Z')
        record = self.rows()[0]
        self.assertIsNone(record['teams'][0]['elo_at_time'])
        self.assertEqual(record['teams'][0]['elo_source'], 'pending_rating')
        self.catalog.upsert_group(group(156, created=pending['createdAt'], rated='2026-10-14T13:00:00Z',
                                        before=1510, after=1520, other_before=1490, other_after=1480))
        self.assertEqual(self.rows()[0]['teams'][0]['elo_at_time'], 1520.0)

    def test_pending_rating_only_blocks_its_teams_and_times_after_creation(self):
        self.catalog.upsert_group(group(158))
        pending = group(159, created='2026-10-14T13:30:00Z', status='running',
                        rated=None, before=None, after=None, other_before=None, other_after=None)
        self.catalog.upsert_group(pending)
        self.catalog.store_detail(detail(unranked(160), started='2026-10-14T13:00:00Z'))
        self.complete()
        self.assertEqual(self.rows()[0]['teams'][0]['elo_at_time'], 1510.0)
        pending['createdAt'] = '2026-10-14T08:00:00Z'
        pending['sides'][0]['teamId'] = str(UUID(int=3))
        pending['sides'][1]['teamId'] = str(UUID(int=4))
        self.catalog.upsert_group(pending)
        self.assertEqual(self.rows()[0]['teams'][0]['elo_at_time'], 1510.0)


if __name__ == "__main__":
    unittest.main()
