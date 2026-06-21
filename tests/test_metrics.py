from __future__ import annotations

import unittest
from pathlib import Path

from src.metrics import (
    build_metrics_package,
    delivery_bucket,
    elapsed_hours,
    parse_timestamp,
    timing_evidence_for_item,
)
from src.settings import AppSettings


def settings() -> AppSettings:
    return AppSettings(
        config_path=Path("config/config.yml"),
        timezone="America/Indiana/Indianapolis",
        groups={
            "on_deck": "On Deck",
            "in_progress": "In Progress",
            "blocked": "Blocked",
            "pending_approval": "Pending Approval",
            "complete": "Completed Items",
        },
        group_ids={
            "on_deck": "group_on_deck",
            "in_progress": "group_in_progress",
            "blocked": "",
            "pending_approval": "group_pending",
            "complete": "group_complete",
        },
    )


def evidence_item(
    item_id: str,
    role: str,
    classification: str,
    committed: bool = True,
    potential_unplanned: bool = False,
    planned_effort: float | None = 2.0,
    workflow_status: str = "",
    updates: list[dict] | None = None,
    activity_logs: list[dict] | None = None,
) -> dict:
    return {
        "item_id": item_id,
        "name": f"Item {item_id}",
        "item_url": f"https://example.monday.com/items/{item_id}",
        "committed": committed,
        "potential_unplanned": potential_unplanned,
        "current_state": {
            "present_on_board": True,
            "group_id": f"group_{role}",
            "group_name": role.replace("_", " ").title(),
            "group_role": role,
            "classification": classification,
            "workflow_status": workflow_status,
            "created_at": "2026-06-22T09:00:00Z",
            "updated_at": "2026-06-22T12:00:00Z",
        },
        "fields": {
            "priority": "High",
            "owner": "Owner",
            "department": "Marketing",
            "planned_effort_hours": planned_effort,
            "actual_effort_hours": None,
            "due_date": "2026-06-26",
        },
        "updates": updates or [],
        "activity_logs": activity_logs or [],
    }


class MetricsTests(unittest.TestCase):
    def test_build_metrics_package_calculates_core_metrics(self) -> None:
        evidence = {
            "mode": "dry-run",
            "generated_at": "2026-06-22T12:00:00+00:00",
            "reporting_week": {"week_start": "2026-06-22", "week_end": "2026-06-26"},
            "board": {"id": "1", "name": "Board"},
            "snapshot": {"path": "data/snapshots/2026-06-22.json"},
            "items": [
                evidence_item("1", "pending_approval", "delivered", planned_effort=2.0),
                evidence_item("2", "complete", "delivered", planned_effort=3.0),
                evidence_item("3", "on_deck", "not_delivered", planned_effort=1.0),
                evidence_item("4", "in_progress", "active_carryover", planned_effort=4.0),
                evidence_item("5", "in_progress", "active_carryover", planned_effort=5.0, workflow_status="Blocked"),
                evidence_item(
                    "6",
                    "complete",
                    "delivered",
                    committed=False,
                    potential_unplanned=True,
                    planned_effort=None,
                ),
                evidence_item(
                    "7",
                    "in_progress",
                    "active_carryover",
                    committed=False,
                    potential_unplanned=True,
                    planned_effort=None,
                ),
            ],
            "warnings": [],
        }

        payload = build_metrics_package(
            evidence=evidence,
            settings=settings(),
            evidence_path=Path("output/evidence_dry_run_2026-06-22.json"),
            dry_run=True,
        )
        core = payload["core_metrics"]

        self.assertEqual(core["committed_items"], 5)
        self.assertEqual(core["committed_items_delivered"], 2)
        self.assertEqual(core["committed_items_still_on_deck"], 1)
        self.assertEqual(core["committed_items_in_progress"], 1)
        self.assertEqual(core["committed_items_blocked"], 1)
        self.assertEqual(core["committed_items_carried_over"], 3)
        self.assertEqual(core["commitment_delivery_percentage"], 40.0)
        self.assertEqual(core["planned_effort_committed"], 15.0)
        self.assertEqual(core["planned_effort_delivered"], 5.0)
        self.assertEqual(core["planned_effort_carried_over"], 10.0)
        self.assertEqual(core["unplanned_items_activated"], 2)
        self.assertEqual(core["unplanned_items_delivered"], 1)

    def test_delivery_bucket_treats_blocked_status_as_blocked(self) -> None:
        item = evidence_item("1", "in_progress", "active_carryover", workflow_status="Blocked")

        self.assertEqual(delivery_bucket(item), "blocked")

    def test_timing_evidence_uses_activity_and_update_timestamps(self) -> None:
        item = evidence_item(
            "1",
            "in_progress",
            "active_carryover",
            updates=[
                {
                    "id": "u1",
                    "created_at": "2026-06-22T10:30:00Z",
                    "body": "Started investigating the issue.",
                }
            ],
            activity_logs=[
                {
                    "id": "a1",
                    "event": "move_pulse_into_group",
                    "created_at": "2026-06-22T09:00:00Z",
                    "data": {"group_id": "group_on_deck"},
                }
            ],
        )

        timing = timing_evidence_for_item(item, settings())

        self.assertEqual(timing["activation"]["source"], "first documented move into On Deck")
        self.assertEqual(timing["first_work"]["source"], "substantive update/comment keyword")
        self.assertEqual(timing["elapsed_calendar_hours"], 1.5)

    def test_parse_timestamp_handles_monday_activity_tick_shape(self) -> None:
        parsed = parse_timestamp("17818840925645912")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)

    def test_elapsed_hours(self) -> None:
        self.assertEqual(elapsed_hours("2026-06-22T09:00:00Z", "2026-06-22T11:30:00Z"), 2.5)
        self.assertIsNone(elapsed_hours(None, "2026-06-22T11:30:00Z"))


if __name__ == "__main__":
    unittest.main()
