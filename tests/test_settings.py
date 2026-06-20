from __future__ import annotations

import unittest

from src.settings import (
    get_monday_token,
    load_settings,
    parse_dotenv_text,
    parse_simple_yaml,
    resolve_env_placeholders,
)


class SettingsTests(unittest.TestCase):
    def test_parse_simple_yaml_nested_scalars(self) -> None:
        config = parse_simple_yaml(
            """
application:
  timezone: "America/Indiana/Indianapolis"
rules:
  include_backlog: false
  max_items: 10
"""
        )

        self.assertEqual(config["application"]["timezone"], "America/Indiana/Indianapolis")
        self.assertFalse(config["rules"]["include_backlog"])
        self.assertEqual(config["rules"]["max_items"], 10)

    def test_resolve_env_placeholders_with_default(self) -> None:
        resolved = resolve_env_placeholders(
            {"value": "${REPORT_TIMEZONE:-America/Indiana/Indianapolis}"},
            {},
        )

        self.assertEqual(resolved["value"], "America/Indiana/Indianapolis")

    def test_get_monday_token_prefers_api_token(self) -> None:
        token = get_monday_token(
            {
                "MONDAY_API_TOKEN": "Bearer direct-token",
                "MONDAY_MCP_AUTH": '{"token": "mcp-token"}',
            }
        )

        self.assertEqual(token, "direct-token")

    def test_get_monday_token_reads_json_mcp_auth(self) -> None:
        token = get_monday_token({"MONDAY_MCP_AUTH": '{"token": "mcp-token"}'})

        self.assertEqual(token, "mcp-token")

    def test_parse_dotenv_reads_simple_values_without_logging_secrets(self) -> None:
        values = parse_dotenv_text("MONDAY_BOARD_ID=123\nMONDAY_API_TOKEN='secret-token'\n")

        self.assertEqual(values["MONDAY_BOARD_ID"], "123")
        self.assertEqual(values["MONDAY_API_TOKEN"], "secret-token")

    def test_load_settings_uses_provided_environment_for_test_isolation(self) -> None:
        settings = load_settings(environ={"MONDAY_BOARD_ID": "456"})

        self.assertEqual(settings.monday_board_id, "456")

    def test_live_inspection_status_uses_token_environment(self) -> None:
        settings = load_settings(
            environ={
                "MONDAY_BOARD_ID": "456",
                "MONDAY_API_TOKEN": "secret-token",
            }
        )

        self.assertEqual(
            settings.live_inspection_status(
                {
                    "MONDAY_BOARD_ID": "456",
                    "MONDAY_API_TOKEN": "secret-token",
                }
            ),
            {
                "MONDAY_BOARD_ID": True,
                "MONDAY_API_TOKEN_or_MONDAY_MCP_AUTH": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
