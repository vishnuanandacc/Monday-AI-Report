from __future__ import annotations

import unittest
from pathlib import Path

from src.inspect_board import build_dry_run_payload, normalize_inspection, parse_args
from src.settings import AppSettings


class InspectBoardTests(unittest.TestCase):
    def test_dry_run_payload_has_no_write_actions(self) -> None:
        settings = AppSettings(
            config_path=Path("config/config.yml"),
            monday_board_id="123",
            groups={"on_deck": "On Deck"},
        )
        args = parse_args(["--dry-run", "--sample-limit", "3"])

        payload = build_dry_run_payload(settings, args)

        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["write_actions"], [])
        self.assertEqual(payload["planned_queries"][0]["variables"]["itemLimit"], 3)

    def test_normalize_inspection_suggests_group_and_columns(self) -> None:
        settings = AppSettings(
            config_path=Path("config/config.yml"),
            groups={"on_deck": "On Deck", "blocked": "Blocked"},
        )
        raw = {
            "board_response": {
                "data": {
                    "boards": [
                        {
                            "id": "123",
                            "name": "Issue City",
                            "groups": [
                                {"id": "topics", "title": "On Deck"},
                                {"id": "blocked", "title": "Blocked"},
                            ],
                            "columns": [
                                {
                                    "id": "priority",
                                    "title": "Priority",
                                    "type": "status",
                                    "settings_str": "{}",
                                },
                                {
                                    "id": "long_text",
                                    "title": "Description",
                                    "type": "long_text",
                                    "settings_str": "{}",
                                },
                            ],
                            "items_page": {
                                "cursor": None,
                                "items": [
                                    {
                                        "id": "999",
                                        "name": "Fix checkout issue",
                                        "url": "https://example.monday.com/items/999",
                                        "created_at": "2026-06-20T10:00:00Z",
                                        "updated_at": "2026-06-20T11:00:00Z",
                                        "group": {"id": "topics", "title": "On Deck"},
                                        "column_values": [
                                            {
                                                "id": "long_text",
                                                "type": "long_text",
                                                "text": "Investigate checkout failures.",
                                                "value": None,
                                            }
                                        ],
                                        "updates": [
                                            {
                                                "id": "u1",
                                                "body": "Started investigating payment errors.",
                                                "created_at": "2026-06-20T11:05:00Z",
                                                "creator": {"id": "1", "name": "Manager"},
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    ]
                }
            },
            "activity_response": {
                "data": {
                    "boards": [
                        {
                            "id": "123",
                            "activity_logs": [
                                {
                                    "id": "a1",
                                    "event": "move_pulse_into_group",
                                    "created_at": "2026-06-20T11:00:00Z",
                                    "user_id": "1",
                                    "data": '{"group_id": "topics"}',
                                }
                            ],
                        }
                    ]
                }
            },
            "warnings": [],
        }

        payload = normalize_inspection(raw, settings)

        self.assertEqual(payload["board"]["name"], "Issue City")
        self.assertTrue(payload["suggested_config"]["groups"]["on_deck"]["found"])
        self.assertEqual(payload["suggested_config"]["columns"]["priority"][0]["id"], "priority")
        self.assertEqual(payload["sample_items"][0]["description_candidates"][0]["title"], "Description")
        self.assertEqual(payload["activity_logs"][0]["data"]["group_id"], "topics")


if __name__ == "__main__":
    unittest.main()
