from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from src.collect_evidence import (
    build_evidence_package,
    classify_current_state,
    default_output_path,
    extract_item_id,
    group_role_for,
    week_end_for,
)
from src.settings import AppSettings


def settings() -> AppSettings:
    return AppSettings(
        config_path=Path("config/config.yml"),
        timezone="America/Indiana/Indianapolis",
        monday_board_id="6687876910",
        groups={
            "on_deck": "On Deck",
            "in_progress": "In Progress",
            "blocked": "Blocked",
            "pending_approval": "Pending Approval",
            "complete": "Completed Items",
        },
        group_ids={
            "on_deck": "group_mm3cp79e",
            "in_progress": "group_mm08qgj5",
            "blocked": "",
            "pending_approval": "group_mm40bb9j",
            "complete": "group_mkyhapxp",
            "backlog": "group_mkygpt1p",
        },
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


def board_item(item_id: str, group_id: str, group_name: str, name: str = "Item") -> dict:
    return {
        "id": item_id,
        "name": name,
        "url": f"https://example.monday.com/items/{item_id}",
        "created_at": "2026-06-22T10:00:00Z",
        "updated_at": "2026-06-22T11:00:00Z",
        "group": {"id": group_id, "title": group_name},
        "column_values": [
            {"id": "dropdown_mm40mgzx", "type": "dropdown", "text": "Marketing", "value": None},
            {"id": "multiple_person_mm085w1k", "type": "people", "text": "Vishnu Anand", "value": None},
            {"id": "status", "type": "status", "text": "On Deck", "value": None},
            {"id": "color_mm08pcn4", "type": "status", "text": "High", "value": None},
            {"id": "date4", "type": "date", "text": "2026-06-30", "value": '{"date":"2026-06-30"}'},
            {"id": "text_mkzhtmpk", "type": "text", "text": "Origin context", "value": None},
            {"id": "text_mkzkbv5k", "type": "text", "text": "User story", "value": None},
            {"id": "numeric_mkzhjxzr", "type": "numbers", "text": "2", "value": '"2"'},
            {"id": "numeric_mkzhxj85", "type": "numbers", "text": "1.5", "value": '"1.5"'},
        ],
        "updates": [
            {
                "id": f"u-{item_id}",
                "body": "Started investigation.",
                "created_at": "2026-06-22T12:00:00Z",
                "creator": {"id": "1", "name": "Manager"},
            }
        ],
    }


class EvidenceTests(unittest.TestCase):
    def test_build_evidence_package_separates_committed_and_potential_unplanned(self) -> None:
        snapshot = {
            "week_start": "2026-06-22",
            "captured_at": "2026-06-22T08:00:00-04:00",
            "board_id": "6687876910",
            "board_name": "Issue City (Backlog)",
            "committed_items": [
                {
                    "item_id": "111",
                    "name": "Committed item",
                    "priority": "High",
                    "planned_effort_hours": 2.0,
                    "owner": "",
                    "department": "Marketing",
                }
            ],
            "warnings": [],
        }
        board = {
            "id": "6687876910",
            "name": "Issue City (Backlog)",
            "page_count": 1,
            "items": [
                board_item("111", "group_mm40bb9j", "Pending Approval", "Committed item"),
                board_item("222", "group_mm08qgj5", "In Progress", "Unplanned item"),
                board_item("333", "group_mkygpt1p", "Backlog", "Backlog item"),
            ],
        }
        activity_logs = [
            {
                "id": "a1",
                "event": "move_pulse_into_group",
                "created_at": "17818840925645912",
                "user_id": "1",
                "data": '{"pulse_id": 111, "group_id": "group_mm40bb9j"}',
            }
        ]

        payload = build_evidence_package(
            settings=settings(),
            snapshot=snapshot,
            snapshot_path=Path("data/snapshots/2026-06-22.json"),
            board=board,
            activity_logs=activity_logs,
            updates_limit=20,
            dry_run=True,
        )

        committed = [item for item in payload["items"] if item["committed"]]
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0]["current_state"]["classification"], "delivered")
        self.assertEqual(payload["potential_unplanned_item_ids"], ["222"])
        self.assertEqual(payload["reporting_week"]["week_end"], "2026-06-26")
        self.assertEqual(payload["source"]["recent_updates_per_item_requested"], 20)

    def test_classify_current_state(self) -> None:
        self.assertEqual(classify_current_state("pending_approval", True), "delivered")
        self.assertEqual(classify_current_state("complete", True), "delivered")
        self.assertEqual(classify_current_state("on_deck", True), "not_delivered")
        self.assertEqual(classify_current_state("in_progress", True), "active_carryover")
        self.assertEqual(classify_current_state("blocked", True), "blocked_carryover")
        self.assertEqual(classify_current_state("unknown", False), "missing_or_unknown")

    def test_group_role_for_uses_group_ids_then_names(self) -> None:
        self.assertEqual(group_role_for("group_mm3cp79e", "Whatever", settings()), "on_deck")
        self.assertEqual(group_role_for("", "Completed Items", settings()), "complete")

    def test_extract_item_id_from_activity_data(self) -> None:
        self.assertEqual(extract_item_id({"pulse_id": 123}), "123")
        self.assertEqual(extract_item_id({"itemId": "456"}), "456")
        self.assertEqual(extract_item_id("not-json"), "")

    def test_default_output_paths(self) -> None:
        week_start = date(2026, 6, 22)

        self.assertIn("evidence_dry_run", str(default_output_path(week_start, True)))
        self.assertIn("weekly_evidence", str(default_output_path(week_start, False)))

    def test_week_end_for(self) -> None:
        self.assertEqual(week_end_for("2026-06-22"), "2026-06-26")
        self.assertEqual(week_end_for("bad-date"), "")


if __name__ == "__main__":
    unittest.main()
