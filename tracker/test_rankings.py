"""Behavioral checks against small, independently specified rating histories."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from rankings import RatingIndex


def group(identifier, instant, before_a, after_a, before_b, after_b, a="a", b="b"):
    return {
        "id": identifier, "kind": "ranked", "status": "finished",
        "createdAt": instant, "ratedAt": instant,
        "sides": [{"teamId": a, "ratingBefore": before_a, "ratingAfter": after_a},
                  {"teamId": b, "ratingBefore": before_b, "ratingAfter": after_b}],
    }


class RatingIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, team, groups, more=False, **extra):
        (self.root / f"{team}.json").write_text(
            json.dumps({"groups": groups, "more": more, **extra}), encoding="utf-8")

    def write_both(self, groups, **kwargs):
        self.write("a", groups, **kwargs)
        self.write("b", groups, **kwargs)

    def test_joins_historical_updates_at_map_start(self):
        first = group("first", "2026-01-01T10:00:00Z", 1500, 1520, 1500, 1480)
        second = group("second", "2026-01-01T11:00:00Z", 1520, 1510, 1480, 1490)
        self.write_both([second, first])
        index = RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T12:00:00Z")
        self.assertEqual(index.at("2026-01-01T09:30:00Z")["a"]["elo"], 1500)
        self.assertEqual(index.at("2026-01-01T10:30:00Z")["a"]["elo"], 1520)
        self.assertEqual(index.at("2026-01-01T11:30:00Z")["a"]["elo"], 1510)
        self.assertEqual(index.at("2026-01-01T10:30:00Z")["b"]["rank"], 2)
        self.assertEqual(index.summary["rating_events"], 4)

    def test_relevant_gap_rejected_and_ancient_gap_ignored(self):
        first = group("first", "2026-01-01T08:00:00Z", 1500, 1520, 1500, 1480)
        second = group("second", "2026-01-01T09:00:00Z", 1530, 1540, 1470, 1460)
        self.write_both([second, first])
        with self.assertRaisesRegex(ValueError, "continuity gap.*team a"):
            RatingIndex(self.root, "2026-01-01T08:30:00Z", "2026-01-01T10:00:00Z")
        index = RatingIndex(self.root, "2026-01-01T09:30:00Z", "2026-01-01T10:00:00Z")
        self.assertEqual(index.at("2026-01-01T09:45:00Z")["a"]["elo"], 1540)

    def test_tied_boundary_uses_strictly_greater_rating(self):
        # Nineteen teams are strictly above two teams sharing twentieth place.
        for number in range(19):
            team = f"high-{number}"
            self.write(team, [group(f"g-{number}", "2026-01-01T08:00:00Z",
                                    1500, 1600 + number, 1500, 1500, a=team, b="anchor")])
        anchor_groups = []
        for path in self.root.glob("*.json"):
            anchor_groups.extend(json.loads(path.read_text())["groups"])
        self.write("anchor", anchor_groups)
        self.write("tie", [])
        index = RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z")
        state = index.at("2026-01-01T09:30:00Z")
        self.assertEqual(state["anchor"]["rank"], 20)
        self.assertEqual(state["tie"]["rank"], 20)
        self.assertEqual(state["tie"]["rankMax"], 20)

    def test_simultaneous_update_returns_safe_rank_bounds(self):
        update = group("swap", "2026-01-01T10:00:00Z", 1600, 1500, 1500, 1600)
        self.write_both([update])
        self.write("middle", [group("middle", "2026-01-01T08:00:00Z",
                                    1550, 1550, 1500, 1500, a="middle", b="anchor")])
        self.write("anchor", [group("middle", "2026-01-01T08:00:00Z",
                                    1550, 1550, 1500, 1500, a="middle", b="anchor")])
        index = RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")
        state = index.at("2026-01-01T10:00:00Z")
        # The middle player is second both before and after the swap, but can
        # be first or third while the simultaneous writes are being ordered.
        self.assertEqual((state["middle"]["rankMin"], state["middle"]["rankMax"]), (1, 3))
        self.assertIsNone(state["middle"]["rank"])
        self.assertTrue(state["middle"]["verified"])
        self.assertEqual(index.at("2026-01-01T10:00:01Z")["middle"]["rank"], 2)

    def test_missing_coverage_rejected_and_explicit_marker_accepted(self):
        update = group("late", "2026-01-01T10:00:00Z", 1500, 1510, 1500, 1490)
        self.write_both([update], more=True)
        with self.assertRaisesRegex(ValueError, "coverage gap.*team a"):
            RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")
        self.write_both([update], more=True, coverage_start="2026-01-01T08:00:00Z")
        index = RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")
        self.assertEqual(index.at("2026-01-01T09:30:00Z")["a"]["elo"], 1500)

    def test_empty_complete_history_has_initial_rating(self):
        self.write("initial", [])
        index = RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")
        self.assertEqual(index.at("2026-01-01T09:30:00Z")["initial"],
                         {"elo": 1500.0, "rank": 1, "rankMin": 1, "rankMax": 1, "verified": True})
        self.write("initial", [], more=True, coverage_start="2026-01-01T08:00:00Z")
        with self.assertRaisesRegex(ValueError, "Unknown initial rating baseline"):
            RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")

    def test_pending_group_and_missing_opponent_source_rejected(self):
        pending = group("pending", "2026-01-01T10:00:00Z", None, None, None, None)
        pending["ratedAt"] = None
        self.write_both([pending])
        with self.assertRaisesRegex(ValueError, "Pending or unresolved"):
            RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")
        self.write("a", [group("finished", "2026-01-01T10:00:00Z", 1500, 1510, 1500, 1490)])
        (self.root / "b.json").unlink()
        with self.assertRaisesRegex(ValueError, "Missing team-history.*opponent b"):
            RatingIndex(self.root, "2026-01-01T09:00:00Z", "2026-01-01T11:00:00Z")

    def test_timezone_offsets_and_query_range(self):
        self.write("initial", [])
        index = RatingIndex(self.root, "2026-01-01T12:00:00+03:00", "2026-01-01T10:00:00Z")
        self.assertEqual(index.at("2026-01-01T09:30:00Z")["initial"]["rank"], 1)
        with self.assertRaisesRegex(ValueError, "outside"):
            index.at("2026-01-01T10:00:01Z")
        with self.assertRaisesRegex(ValueError, "timestamp"):
            index.at("2026-01-01T09:30:00")


if __name__ == "__main__":
    unittest.main()
