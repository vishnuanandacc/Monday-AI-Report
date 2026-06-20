from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

MONDAY_API_URL = "https://api.monday.com/v2"

BOARD_INSPECTION_QUERY = """
query InspectBoard($boardIds: [ID!]!, $itemLimit: Int!) {
  boards(ids: $boardIds) {
    id
    name
    groups {
      id
      title
      archived
      deleted
    }
    columns {
      id
      title
      type
      settings_str
    }
    items_page(limit: $itemLimit) {
      cursor
      items {
        id
        name
        url
        created_at
        updated_at
        group {
          id
          title
        }
        column_values {
          id
          type
          text
          value
        }
        updates(limit: 5) {
          id
          body
          created_at
          creator {
            id
            name
          }
        }
      }
    }
  }
}
"""

BOARD_ACTIVITY_QUERY = """
query InspectBoardActivity($boardIds: [ID!]!, $activityLimit: Int!) {
  boards(ids: $boardIds) {
    id
    activity_logs(limit: $activityLimit) {
      id
      event
      data
      created_at
      user_id
    }
  }
}
"""


class MondayApiError(RuntimeError):
    def __init__(self, message: str, details: Any | None = None):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class MondayClient:
    token: str
    endpoint: str = MONDAY_API_URL
    api_version: str = ""
    timeout_seconds: int = 30
    max_retries: int = 2

    def execute(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        operation_name: str | None = None,
    ) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name

        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }
        if self.api_version:
            headers["API-Version"] = self.api_version

        for attempt in range(self.max_retries + 1):
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read().decode("utf-8")
                parsed = _parse_json_response(response_body)
                if parsed.get("errors"):
                    raise MondayApiError("monday GraphQL returned errors", parsed["errors"])
                return parsed
            except urllib.error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")
                if _should_retry(exc.code, attempt, self.max_retries):
                    time.sleep(2**attempt)
                    continue
                raise MondayApiError(
                    f"monday API HTTP {exc.code}",
                    _parse_json_response(response_body, fallback=response_body),
                ) from exc
            except urllib.error.URLError as exc:
                if _should_retry(503, attempt, self.max_retries):
                    time.sleep(2**attempt)
                    continue
                raise MondayApiError(f"Could not reach monday API: {exc.reason}") from exc

        raise MondayApiError("monday API request failed after retries")


def fetch_board_inspection(
    client: MondayClient,
    board_id: str,
    sample_limit: int = 5,
    activity_limit: int = 20,
    include_activity: bool = True,
) -> dict[str, Any]:
    variables = {"boardIds": [str(board_id)], "itemLimit": sample_limit}
    board_response = client.execute(
        BOARD_INSPECTION_QUERY,
        variables=variables,
        operation_name="InspectBoard",
    )

    warnings: list[str] = []
    activity_response: dict[str, Any] | None = None
    if include_activity:
        try:
            activity_response = client.execute(
                BOARD_ACTIVITY_QUERY,
                variables={"boardIds": [str(board_id)], "activityLimit": activity_limit},
                operation_name="InspectBoardActivity",
            )
        except MondayApiError as exc:
            warnings.append(
                "Activity log query failed; board structure and sample items were still retrieved."
            )
            warnings.append(str(exc))

    return {
        "board_response": board_response,
        "activity_response": activity_response,
        "warnings": warnings,
    }


def _parse_json_response(body: str, fallback: Any | None = None) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        if fallback is not None:
            return {"raw": fallback}
        raise MondayApiError("monday API returned non-JSON response")
    if not isinstance(parsed, dict):
        raise MondayApiError("monday API returned an unexpected JSON shape", parsed)
    return parsed


def _should_retry(status_code: int, attempt: int, max_retries: int) -> bool:
    return status_code in {429, 500, 502, 503, 504} and attempt < max_retries
