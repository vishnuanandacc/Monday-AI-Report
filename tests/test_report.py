from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from src.report import (
    build_run_metadata,
    default_metadata_path,
    default_report_path,
    render_markdown_report,
)


def evidence() -> dict:
    return {
        "mode": "dry-run",
        "generated_at": "2026-06-22T12:00:00+00:00",
        "reporting_week": {"week_start": "2026-06-22", "week_end": "2026-06-26"},
        "board": {"id": "6687876910", "name": "Issue City (Backlog)"},
        "snapshot": {
            "path": "data/snapshots/2026-06-22.json",
            "captured_at": "2026-06-22T08:00:00-04:00",
            "committed_count": 2,
            "warnings": [],
        },
        "items": [
            {
                "item_id": "111",
                "name": "Delivered item",
                "item_url": "https://example.monday.com/items/111",
                "committed": True,
                "potential_unplanned": False,
                "current_state": {
                    "group_name": "Pending Approval",
                    "group_role": "pending_approval",
                    "workflow_status": "Done",
                },
                "fields": {
                    "priority": "High",
                    "department": "Marketing",
                    "planned_effort_hours": 2.0,
                },
                "evidence_quality": {"missing": []},
            },
            {
                "item_id": "222",
                "name": "Carryover item",
                "item_url": "https://example.monday.com/items/222",
                "committed": True,
                "potential_unplanned": False,
                "current_state": {
                    "group_name": "On Deck",
                    "group_role": "on_deck",
                    "workflow_status": "On Deck",
                },
                "fields": {
                    "priority": "Low",
                    "department": "E-commerce",
                    "planned_effort_hours": None,
                },
                "evidence_quality": {"missing": ["No recent activity logs were retrieved for this item."]},
            },
        ],
        "warnings": ["Potential unplanned work is heuristic."],
    }


def metrics() -> dict:
    return {
        "mode": "dry-run",
        "generated_at": "2026-06-22T12:30:00+00:00",
        "reporting_week": {"week_start": "2026-06-22", "week_end": "2026-06-26"},
        "board": {"id": "6687876910", "name": "Issue City (Backlog)"},
        "source": {"snapshot_path": "data/snapshots/2026-06-22.json"},
        "core_metrics": {
            "committed_items": 2,
            "committed_items_delivered": 1,
            "committed_items_still_on_deck": 1,
            "committed_items_in_progress": 0,
            "committed_items_blocked": 0,
            "committed_items_carried_over": 1,
            "commitment_delivery_percentage": 50.0,
            "planned_effort_committed": 2.0,
            "planned_effort_delivered": 2.0,
            "planned_effort_carried_over": 0.0,
            "unplanned_items_activated": 0,
            "unplanned_items_delivered": 0,
        },
        "committed_items": [
            {
                "item_id": "111",
                "name": "Delivered item",
                "bucket": "delivered",
                "current_group": "Pending Approval",
                "workflow_status": "Done",
                "priority": "High",
                "planned_effort_hours": 2.0,
                "item_url": "https://example.monday.com/items/111",
            },
            {
                "item_id": "222",
                "name": "Carryover item",
                "bucket": "on_deck",
                "current_group": "On Deck",
                "workflow_status": "On Deck",
                "priority": "Low",
                "planned_effort_hours": None,
                "item_url": "https://example.monday.com/items/222",
            },
        ],
        "unplanned_items": [],
        "timing_evidence": [
            {
                "item_id": "111",
                "name": "Delivered item",
                "priority": "High",
                "current_group": "Pending Approval",
                "activation": {
                    "source": "first documented move into On Deck",
                    "timestamp": "2026-06-22T09:00:00+00:00",
                },
                "first_work": {
                    "source": "move to delivered group",
                    "timestamp": "2026-06-22T11:00:00+00:00",
                },
                "elapsed_calendar_hours": 2.0,
                "warnings": [],
            }
        ],
        "warnings": ["1 committed item(s) have no planned effort."],
    }


class ReportTests(unittest.TestCase):
    def test_render_markdown_report_includes_required_sections(self) -> None:
        markdown = render_markdown_report(evidence(), metrics())

        self.assertIn("# Weekly Monday Report - 2026-06-22", markdown)
        self.assertIn("## Commitment Summary", markdown)
        self.assertIn("## Main Items Delivered", markdown)
        self.assertIn("## Carryover And Blockers", markdown)
        self.assertIn("## KPI-Supporting Evidence", markdown)
        self.assertIn("Commitment delivery percentage | 50.0%", markdown)
        self.assertIn("Delivered item", markdown)
        self.assertIn("Not generated in this deterministic phase", markdown)

    def test_build_run_metadata_records_sources_and_items(self) -> None:
        metadata = build_run_metadata(
            evidence=evidence(),
            metrics=metrics(),
            evidence_path=Path("output/evidence_dry_run_2026-06-22.json"),
            metrics_path=Path("output/metrics_dry_run_2026-06-22.json"),
            report_path=Path("output/weekly_report_dry_run_2026-06-22.md"),
            dry_run=True,
        )

        self.assertEqual(metadata["board_id"], "6687876910")
        self.assertEqual(metadata["snapshot_file_used"], "data/snapshots/2026-06-22.json")
        self.assertEqual(metadata["ai_model_used"], "not-used")
        self.assertEqual(metadata["item_ids_processed"], ["111", "222"])
        self.assertTrue(metadata["warnings"])

    def test_default_report_paths_separate_dry_run_from_final(self) -> None:
        week_start = date(2026, 6, 22)

        self.assertIn("weekly_report_dry_run", str(default_report_path(week_start, True)))
        self.assertIn("weekly_report", str(default_report_path(week_start, False)))
        self.assertIn("run_metadata_dry_run", str(default_metadata_path(week_start, True)))
        self.assertIn("run_metadata", str(default_metadata_path(week_start, False)))


if __name__ == "__main__":
    unittest.main()
