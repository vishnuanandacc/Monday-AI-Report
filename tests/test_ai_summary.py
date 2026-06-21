from __future__ import annotations

import unittest
from pathlib import Path

from src.ai_summary import (
    AISummaryError,
    build_ai_input_package,
    dry_run_summary,
    extract_response_text,
    redact_openai_error,
    validate_summary_payload,
)
from src.settings import AppSettings


def settings() -> AppSettings:
    return AppSettings(
        config_path=Path("config/config.yml"),
        ai={
            "model": "gpt-5.5",
            "max_comments_per_item": 1,
            "max_characters_per_item": 20,
        },
    )


def evidence() -> dict:
    return {
        "reporting_week": {"week_start": "2026-06-22", "week_end": "2026-06-26"},
        "board": {"id": "1", "name": "Board"},
        "items": [
            {
                "item_id": "111",
                "name": "Committed item",
                "item_url": "https://example.monday.com/items/111",
                "committed": True,
                "potential_unplanned": False,
                "current_state": {
                    "group_name": "On Deck",
                    "group_role": "on_deck",
                    "workflow_status": "On Deck",
                    "classification": "not_delivered",
                    "created_at": "2026-06-22T09:00:00Z",
                    "updated_at": "2026-06-22T10:00:00Z",
                },
                "fields": {
                    "priority": "High",
                    "owner": "Owner",
                    "department": "Marketing",
                    "planned_effort_hours": 2.0,
                    "origin": "This is a long source field that should be truncated.",
                    "user_story": "As a manager I need a useful report.",
                },
                "updates": [
                    {
                        "created_at": "2026-06-22T10:00:00Z",
                        "creator": {"name": "Manager"},
                        "body": "Started investigating a long issue with many details.",
                    },
                    {
                        "created_at": "2026-06-22T11:00:00Z",
                        "creator": {"name": "Manager"},
                        "body": "Second comment should be omitted by max comment limit.",
                    },
                ],
                "activity_logs": [
                    {
                        "event": "move_pulse_into_group",
                        "created_at": "2026-06-22T09:00:00Z",
                        "data": {"pulse_id": 111, "group_id": "group_on_deck", "large": "ignored"},
                    }
                ],
                "evidence_quality": {"missing": []},
            }
        ],
        "warnings": ["Evidence warning"],
    }


def metrics() -> dict:
    return {
        "reporting_week": {"week_start": "2026-06-22", "week_end": "2026-06-26"},
        "board": {"id": "1", "name": "Board"},
        "core_metrics": {
            "committed_items": 1,
            "committed_items_delivered": 0,
            "committed_items_carried_over": 1,
            "unplanned_items_activated": 0,
        },
        "timing_evidence": [
            {
                "item_id": "111",
                "name": "Committed item",
                "priority": "High",
                "current_group": "On Deck",
                "activation": {
                    "source": "first documented move into On Deck",
                    "timestamp": "2026-06-22T09:00:00+00:00",
                },
                "first_work": {"source": "unknown", "timestamp": None},
                "elapsed_calendar_hours": None,
                "warnings": ["No first-work evidence timestamp found."],
            }
        ],
        "warnings": ["Metrics warning"],
    }


class AISummaryTests(unittest.TestCase):
    def test_build_ai_input_package_truncates_and_limits_comments(self) -> None:
        payload = build_ai_input_package(evidence(), metrics(), settings())
        item = payload["committed_items"][0]

        self.assertEqual(len(item["updates"]), 1)
        self.assertIn("[truncated]", item["updates"][0]["body"])
        self.assertNotIn("large", item["activity_logs"][0]["data"])
        self.assertEqual(payload["python_calculated_metrics"]["committed_items"], 1)

    def test_dry_run_summary_is_schema_valid_and_guarded(self) -> None:
        ai_input = build_ai_input_package(evidence(), metrics(), settings())
        summary = dry_run_summary(
            ai_input=ai_input,
            model="gpt-5.5",
            evidence_path=Path("output/evidence.json"),
            metrics_path=Path("output/metrics.json"),
        )

        validate_summary_payload(summary)
        self.assertFalse(summary["guardrail_attestation"]["kpi_scoring_performed"])
        self.assertIn("Dry run placeholder", summary["executive_summary"]["summary"])

    def test_validate_summary_rejects_guardrail_violation(self) -> None:
        ai_input = build_ai_input_package(evidence(), metrics(), settings())
        summary = dry_run_summary(
            ai_input=ai_input,
            model="gpt-5.5",
            evidence_path=Path("output/evidence.json"),
            metrics_path=Path("output/metrics.json"),
        )
        summary["guardrail_attestation"]["invented_measurements"] = True

        with self.assertRaises(AISummaryError):
            validate_summary_payload(summary)

    def test_extract_response_text_handles_output_content(self) -> None:
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "{\"ok\": true}"},
                    ],
                }
            ]
        }

        self.assertEqual(extract_response_text(response), "{\"ok\": true}")

    def test_redact_openai_error_does_not_mutate_without_key(self) -> None:
        self.assertEqual(redact_openai_error("plain error"), "plain error")


if __name__ == "__main__":
    unittest.main()
