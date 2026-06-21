from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .monday_client import MondayApiError, MondayClient
from .settings import ROOT_DIR, AppSettings, get_monday_token, load_settings

BOARD_ITEMS_PAGE_QUERY = """
query SnapshotBoardItems($boardIds: [ID!]!, $limit: Int!) {
  boards(ids: $boardIds) {
    id
    name
    items_page(limit: $limit) {
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
      }
    }
  }
}
"""

NEXT_ITEMS_PAGE_QUERY = """
query SnapshotNextItems($cursor: String!) {
  next_items_page(cursor: $cursor) {
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
    }
  }
}
"""

SNAPSHOT_FIELD_KEYS = (
    "priority",
    "planned_effort",
    "owner",
    "department",
    "workflow_status",
    "due_date",
    "origin",
    "user_story",
    "actual_effort",
)


class SnapshotError(RuntimeError):
    """Raised when snapshot generation cannot safely continue."""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config).with_board_id(args.board_id)
    week_start = resolve_week_start(args.week_start, settings.timezone)
    captured_at = args.captured_at or now_iso(settings.timezone)
    output_path = Path(args.output) if args.output else default_snapshot_path(week_start, args.dry_run)

    missing = settings.missing_live_inspection_requirements()
    if missing:
        print(
            "Snapshot requires: " + ", ".join(missing) + ".",
            file=sys.stderr,
        )
        return 2

    if not settings.group_ids.get("on_deck") and not settings.groups.get("on_deck"):
        print("Snapshot requires an On Deck group ID or label in config.", file=sys.stderr)
        return 2

    client = MondayClient(
        token=get_monday_token(),
        api_version=os.environ.get("MONDAY_API_VERSION", ""),
    )

    try:
        board = fetch_all_board_items(
            client=client,
            board_id=settings.monday_board_id,
            page_limit=args.page_limit,
            max_pages=args.max_pages,
        )
        payload = build_snapshot_payload(
            settings=settings,
            board=board,
            week_start=week_start,
            captured_at=captured_at,
            dry_run=args.dry_run,
        )
        write_json(output_path, payload, overwrite=args.overwrite or args.dry_run)
    except (MondayApiError, SnapshotError) as exc:
        print(f"Snapshot failed: {exc}", file=sys.stderr)
        details = getattr(exc, "details", None)
        if details:
            print(json.dumps(details, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    mode = "dry run" if args.dry_run else "snapshot"
    count = len(payload.get("committed_items", []))
    print(f"{mode.capitalize()} written to {output_path} ({count} On Deck item(s))")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture monday On Deck items for the reporting week.")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "config.yml"))
    parser.add_argument("--board-id", default=None, help="Override MONDAY_BOARD_ID/config board id.")
    parser.add_argument("--week-start", default=None, help="Reporting week Monday date, YYYY-MM-DD.")
    parser.add_argument("--captured-at", default=None, help="Override captured_at timestamp for tests/reruns.")
    parser.add_argument("--dry-run", action="store_true", help="Read monday and write preview under output/.")
    parser.add_argument("--output", default=None, help="Output JSON path. Use '-' for stdout.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing real snapshot file.")
    parser.add_argument("--page-limit", type=int, default=500, help="Items per first page request.")
    parser.add_argument("--max-pages", type=int, default=20, help="Safety cap for pagination.")
    return parser.parse_args(argv)


def fetch_all_board_items(
    client: MondayClient,
    board_id: str,
    page_limit: int = 500,
    max_pages: int = 20,
) -> dict[str, Any]:
    if page_limit < 1 or page_limit > 500:
        raise SnapshotError("--page-limit must be between 1 and 500.")
    if max_pages < 1:
        raise SnapshotError("--max-pages must be at least 1.")

    response = client.execute(
        BOARD_ITEMS_PAGE_QUERY,
        variables={"boardIds": [str(board_id)], "limit": page_limit},
        operation_name="SnapshotBoardItems",
    )
    boards = (((response.get("data") or {}).get("boards")) or [])
    if not boards:
        raise SnapshotError("No board was returned. Check MONDAY_BOARD_ID and permissions.")

    board = boards[0]
    item_page = board.get("items_page") or {}
    items = list(item_page.get("items") or [])
    cursor = item_page.get("cursor")
    page_count = 1

    while cursor:
        if page_count >= max_pages:
            raise SnapshotError(
                f"Pagination exceeded --max-pages={max_pages}; refusing to create incomplete snapshot."
            )
        next_response = client.execute(
            NEXT_ITEMS_PAGE_QUERY,
            variables={"cursor": cursor},
            operation_name="SnapshotNextItems",
        )
        next_page = (next_response.get("data") or {}).get("next_items_page") or {}
        items.extend(next_page.get("items") or [])
        cursor = next_page.get("cursor")
        page_count += 1

    return {
        "id": str(board.get("id", "")),
        "name": board.get("name", ""),
        "items": items,
        "page_count": page_count,
    }


def build_snapshot_payload(
    settings: AppSettings,
    board: dict[str, Any],
    week_start: date,
    captured_at: str,
    dry_run: bool,
) -> dict[str, Any]:
    on_deck_items = [
        normalize_snapshot_item(item, settings)
        for item in board.get("items", [])
        if is_on_deck_item(item, settings)
    ]
    warnings = board_mapping_warnings(settings)
    if dry_run:
        warnings.append("Dry run: official data/snapshots file was not written.")

    return {
        "week_start": week_start.isoformat(),
        "captured_at": captured_at,
        "board_id": str(board.get("id") or settings.monday_board_id),
        "board_name": board.get("name", ""),
        "mode": "dry-run" if dry_run else "snapshot",
        "source": {
            "page_count": board.get("page_count", 1),
            "total_items_read": len(board.get("items", [])),
            "on_deck_group_id": settings.group_ids.get("on_deck", ""),
            "on_deck_group_name": settings.groups.get("on_deck", "On Deck"),
        },
        "committed_items": on_deck_items,
        "warnings": warnings,
    }


def normalize_snapshot_item(item: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    group = item.get("group") or {}
    fields = useful_fields(item.get("column_values") or [], settings.column_ids)
    planned_effort = parse_number(field_text(fields, "planned_effort"))

    return {
        "item_id": str(item.get("id", "")),
        "name": item.get("name", ""),
        "item_url": item.get("url", ""),
        "group_id": str(group.get("id", "")),
        "group_name": group.get("title", ""),
        "priority": field_text(fields, "priority"),
        "planned_effort_hours": planned_effort,
        "owner": field_text(fields, "owner"),
        "department": field_text(fields, "department"),
        "workflow_status": field_text(fields, "workflow_status"),
        "due_date": field_text(fields, "due_date"),
        "origin": field_text(fields, "origin"),
        "user_story": field_text(fields, "user_story"),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
        "useful_fields": fields,
    }


def useful_fields(
    column_values: list[dict[str, Any]],
    column_ids: dict[str, str],
) -> dict[str, dict[str, Any]]:
    by_id = {str(value.get("id", "")): value for value in column_values}
    fields: dict[str, dict[str, Any]] = {}
    for key in SNAPSHOT_FIELD_KEYS:
        column_id = column_ids.get(key, "")
        if not column_id:
            continue
        value = by_id.get(column_id)
        if not value:
            fields[key] = {
                "column_id": column_id,
                "type": "",
                "text": "",
                "value": None,
                "missing": True,
            }
            continue
        fields[key] = {
            "column_id": column_id,
            "type": value.get("type", ""),
            "text": value.get("text") or "",
            "value": parse_json_or_raw(value.get("value")),
            "missing": False,
        }
    return fields


def is_on_deck_item(item: dict[str, Any], settings: AppSettings) -> bool:
    group = item.get("group") or {}
    group_id = str(group.get("id", ""))
    group_name = str(group.get("title", ""))
    configured_id = settings.group_ids.get("on_deck", "")
    configured_name = settings.groups.get("on_deck", "On Deck")
    if configured_id:
        return group_id == configured_id
    return group_name.casefold() == configured_name.casefold()


def board_mapping_warnings(settings: AppSettings) -> list[str]:
    warnings = []
    if not settings.group_ids.get("blocked"):
        warnings.append("No Blocked group ID is configured; inspection did not find a Blocked group.")
    if settings.groups.get("complete") == "Completed Items":
        warnings.append("Complete delivery group is configured as existing board group 'Completed Items'.")
    for key in ("priority", "planned_effort", "owner"):
        if not settings.column_ids.get(key):
            warnings.append(f"No column ID configured for {key}.")
    return warnings


def resolve_week_start(week_start: str | None, timezone_name: str) -> date:
    if week_start:
        try:
            return date.fromisoformat(week_start)
        except ValueError as exc:
            raise SnapshotError("--week-start must use YYYY-MM-DD format.") from exc

    today = datetime.now(resolve_timezone(timezone_name)).date()
    return today - timedelta(days=today.weekday())


def default_snapshot_path(week_start: date, dry_run: bool) -> Path:
    if dry_run:
        return ROOT_DIR / "output" / f"snapshot_dry_run_{week_start.isoformat()}.json"
    return ROOT_DIR / "data" / "snapshots" / f"{week_start.isoformat()}.json"


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = False) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if str(path) == "-":
        print(serialized)
        return
    if path.exists() and not overwrite:
        raise SnapshotError(f"Snapshot already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def now_iso(timezone_name: str) -> str:
    return datetime.now(resolve_timezone(timezone_name)).isoformat(timespec="seconds")


def resolve_timezone(timezone_name: str) -> ZoneInfo | timezone:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return timezone.utc


def field_text(fields: dict[str, dict[str, Any]], key: str) -> str:
    return str((fields.get(key) or {}).get("text") or "")


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group(0))


def parse_json_or_raw(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


if __name__ == "__main__":
    raise SystemExit(main())
