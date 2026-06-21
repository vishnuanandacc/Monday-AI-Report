from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from src.settings import AppSettings
from src.snapshot import (
    build_snapshot_payload,
    default_snapshot_path,
    is_on_deck_item,
    normalize_snapshot_item,
    parse_number,
    resolve_week_start,
)


def test_settings() -> AppSettings:
    return AppSettings(
        config_path=Path("config/config.yml"),
        timezone="America/Indiana/Indianapolis",
        monday_board_id="6687876910",
        groups={"on_deck": "On Deck", "complete": "Completed Items"},
        group_ids={"on_deck": "group_mm3cp79e", "blocked": "", "complete": "group_mkyhapxp"},
        column_ids={
            "department": "dropdown_mm40mgzx",
            "owner": "multiple_person_mm085w1k",
            "workflow_status": "status",
            "priority": "color_mm08pcn4",
            "due_date": "date4",
            "origin": "text_mkzhtmpk",
            "user_story": "text_mkzkbv5k",
            "planned_effort": "numeric_mkzhjxzr",
            "actual_effort": "numeric_mkzhxj85",
        },
    )


def item(group_id: str, group_name: str = "On Deck") -> dict:
    return {
        "id": "111",
        "name": "Fix revenue report",
        "url": "https://example.monday.com/items/111",
        "created_at": "2026-06-20T10:00:00Z",
        "updated_at": "2026-06-20T11:00:00Z",
        "group": {"id": group_id, "title": group_name},
        "column_values": [
            {"id": "dropdown_mm40mgzx", "type": "dropdown", "text": "Finance", "value": None},
            {"id": "multiple_person_mm085w1k", "type": "people", "text": "Vishnu Anand", "value": None},
            {"id": "status", "type": "status", "text": "Working on it", "value": None},
            {"id": "color_mm08pcn4", "type": "status", "text": "High", "value": None},
            {"id": "date4", "type": "date", "text": "2026-06-30", "value": '{"date":"2026-06-30"}'},
            {"id": "text_mkzhtmpk", "type": "text", "text": "Finance reported inaccurate revenue.", "value": None},
            {"id": "text_mkzkbv5k", "type": "text", "text": "As finance, I need accurate revenue.", "value": None},
            {"id": "numeric_mkzhjxzr", "type": "numbers", "text": "3.5", "value": '"3.5"'},
            {"id": "numeric_mkzhxj85", "type": "numbers", "text": "", "value": None},
        ],
    }


class SnapshotTests(unittest.TestCase):
    def test_is_on_deck_item_uses_configured_group_id(self) -> None:
        self.assertTrue(is_on_deck_item(item("group_mm3cp79e"), test_settings()))
        self.assertFalse(is_on_deck_item(item("group_mm08qgj5", "In Progress"), test_settings()))

    def test_normalize_snapshot_item_maps_useful_fields(self) -> None:
        normalized = normalize_snapshot_item(item("group_mm3cp79e"), test_settings())

        self.assertEqual(normalized["priority"], "High")
        self.assertEqual(normalized["planned_effort_hours"], 3.5)
        self.assertEqual(normalized["owner"], "Vishnu Anand")
        self.assertEqual(normalized["department"], "Finance")
        self.assertEqual(normalized["due_date"], "2026-06-30")

    def test_build_snapshot_payload_filters_on_deck_items(self) -> None:
        settings = test_settings()
        board = {
            "id": "6687876910",
            "name": "Issue City (Backlog)",
            "page_count": 1,
            "items": [
                item("group_mm3cp79e", "On Deck"),
                item("group_mm08qgj5", "In Progress"),
            ],
        }

        payload = build_snapshot_payload(
            settings=settings,
            board=board,
            week_start=date(2026, 6, 22),
            captured_at="2026-06-22T08:00:00-04:00",
            dry_run=True,
        )

        self.assertEqual(payload["week_start"], "2026-06-22")
        self.assertEqual(len(payload["committed_items"]), 1)
        self.assertEqual(payload["committed_items"][0]["group_name"], "On Deck")
        self.assertIn("Dry run", payload["warnings"][-1])

    def test_parse_number_handles_blank_and_labeled_values(self) -> None:
        self.assertIsNone(parse_number(""))
        self.assertEqual(parse_number("about 2.25 hours"), 2.25)

    def test_resolve_week_start_defaults_to_monday_for_explicit_input(self) -> None:
        self.assertEqual(resolve_week_start("2026-06-22", "America/Indiana/Indianapolis"), date(2026, 6, 22))

    def test_default_snapshot_paths_separate_dry_run_from_real_snapshot(self) -> None:
        week_start = date(2026, 6, 22)

        self.assertIn("output", str(default_snapshot_path(week_start, dry_run=True)))
        self.assertIn("data", str(default_snapshot_path(week_start, dry_run=False)))


if __name__ == "__main__":
    unittest.main()
