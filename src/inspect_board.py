from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .monday_client import MondayApiError, MondayClient, fetch_board_inspection
from .settings import ROOT_DIR, AppSettings, get_monday_token, load_settings

DESCRIPTION_COLUMN_KEYWORDS = {"description", "details", "notes", "brief", "summary"}
COLUMN_CANDIDATE_KEYWORDS = {
    "priority": {"priority", "severity", "impact"},
    "planned_effort": {"effort", "hours", "estimate", "planned"},
    "owner": {"owner", "person", "assignee", "assigned"},
    "department": {"department", "audience", "team", "label"},
    "due_date": {"due", "deadline", "target"},
}


class InspectionError(RuntimeError):
    """Raised when board inspection data is incomplete or unusable."""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config).with_board_id(args.board_id)
    output_path = Path(args.output) if args.output else default_output_path(args.dry_run)

    if args.dry_run:
        payload = build_dry_run_payload(settings, args)
        write_json(output_path, payload)
        print(f"Dry run written to {output_path}")
        return 0

    missing = settings.missing_live_inspection_requirements()
    if missing:
        print(
            "Live inspection requires: " + ", ".join(missing) + ". "
            "Run with --dry-run to avoid calling monday.",
            file=sys.stderr,
        )
        return 2

    client = MondayClient(
        token=get_monday_token(),
        api_version=os.environ.get("MONDAY_API_VERSION", ""),
    )

    try:
        raw = fetch_board_inspection(
            client=client,
            board_id=settings.monday_board_id,
            sample_limit=args.sample_limit,
            activity_limit=args.activity_limit,
            include_activity=not args.skip_activity,
        )
        payload = normalize_inspection(raw, settings)
    except (MondayApiError, InspectionError) as exc:
        print(f"Board inspection failed: {exc}", file=sys.stderr)
        details = getattr(exc, "details", None)
        if details:
            print(json.dumps(details, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    write_json(output_path, payload)
    warning_count = len(payload.get("warnings", []))
    suffix = f" with {warning_count} warning(s)" if warning_count else ""
    print(f"Board inspection written to {output_path}{suffix}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect a monday board without mutating it.")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "config.yml"))
    parser.add_argument("--board-id", default=None, help="Override MONDAY_BOARD_ID/config board id.")
    parser.add_argument("--dry-run", action="store_true", help="Write the inspection plan only.")
    parser.add_argument("--output", default=None, help="Output JSON path. Use '-' for stdout.")
    parser.add_argument("--sample-limit", type=int, default=5, help="Sample item count to inspect.")
    parser.add_argument("--activity-limit", type=int, default=20, help="Activity record count to inspect.")
    parser.add_argument("--skip-activity", action="store_true", help="Skip board activity_logs query.")
    return parser.parse_args(argv)


def build_dry_run_payload(settings: AppSettings, args: argparse.Namespace) -> dict[str, Any]:
    status = settings.live_inspection_status()
    warnings = [
        f"{name} is not set; live inspection will fail until it is configured."
        for name, present in status.items()
        if not present
    ]
    return {
        "mode": "dry-run",
        "read_only": True,
        "generated_at": now_iso(settings.timezone),
        "config_path": str(settings.config_path),
        "board_id_configured": bool(settings.monday_board_id),
        "environment_status": status,
        "group_labels_from_config": settings.groups,
        "planned_output": str(default_output_path(False)),
        "planned_queries": [
            {
                "name": "InspectBoard",
                "read_only": True,
                "variables": {
                    "boardIds": ["<MONDAY_BOARD_ID>"],
                    "itemLimit": args.sample_limit,
                },
                "fields": [
                    "boards.id",
                    "boards.name",
                    "boards.groups",
                    "boards.columns",
                    "boards.items_page.items",
                    "items.column_values",
                    "items.updates",
                ],
            },
            {
                "name": "InspectBoardActivity",
                "read_only": True,
                "skipped": args.skip_activity,
                "variables": {
                    "boardIds": ["<MONDAY_BOARD_ID>"],
                    "activityLimit": args.activity_limit,
                },
                "fields": ["boards.activity_logs"],
            },
        ],
        "write_actions": [],
        "warnings": warnings,
    }


def normalize_inspection(raw: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    board_response = raw.get("board_response") or {}
    boards = (((board_response.get("data") or {}).get("boards")) or [])
    if not boards:
        raise InspectionError("No board was returned. Check MONDAY_BOARD_ID and permissions.")

    board = boards[0]
    groups = normalize_groups(board.get("groups") or [])
    columns = normalize_columns(board.get("columns") or [])
    item_page = board.get("items_page") or {}
    sample_items = normalize_items(item_page.get("items") or [], columns)
    activity_logs = normalize_activity_logs(raw.get("activity_response"))
    warnings = list(raw.get("warnings") or [])

    if not groups:
        warnings.append("No groups were returned from the board.")
    if not columns:
        warnings.append("No columns were returned from the board.")
    if not sample_items:
        warnings.append("No sample items were returned. Increase --sample-limit or check board access.")

    return {
        "mode": "live-read-only",
        "read_only": True,
        "generated_at": now_iso(settings.timezone),
        "board": {
            "id": str(board.get("id", "")),
            "name": board.get("name", ""),
        },
        "groups": groups,
        "columns": columns,
        "sample_items": sample_items,
        "activity_logs": activity_logs,
        "pagination": {
            "items_page_cursor_returned": bool(item_page.get("cursor")),
            "sample_item_count": len(sample_items),
        },
        "suggested_config": {
            "groups": suggest_group_mappings(groups, settings.groups),
            "columns": suggest_column_candidates(columns),
        },
        "write_actions": [],
        "warnings": warnings,
    }


def normalize_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(group.get("id", "")),
            "name": group.get("title", ""),
            "archived": bool(group.get("archived", False)),
            "deleted": bool(group.get("deleted", False)),
        }
        for group in groups
    ]


def normalize_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for column in columns:
        normalized.append(
            {
                "id": str(column.get("id", "")),
                "title": column.get("title", ""),
                "type": column.get("type", ""),
                "settings": parse_json_or_raw(column.get("settings_str")),
            }
        )
    return normalized


def normalize_items(items: list[dict[str, Any]], columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    column_by_id = {column["id"]: column for column in columns}
    normalized = []
    for item in items:
        group = item.get("group") or {}
        column_values = normalize_column_values(item.get("column_values") or [], column_by_id)
        normalized.append(
            {
                "id": str(item.get("id", "")),
                "name": item.get("name", ""),
                "url": item.get("url", ""),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
                "group": {
                    "id": str(group.get("id", "")),
                    "name": group.get("title", ""),
                },
                "column_values": column_values,
                "description_candidates": find_description_candidates(column_values),
                "updates": normalize_updates(item.get("updates") or []),
            }
        )
    return normalized


def normalize_column_values(
    values: list[dict[str, Any]],
    column_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = []
    for value in values:
        column = column_by_id.get(str(value.get("id", "")), {})
        normalized.append(
            {
                "id": str(value.get("id", "")),
                "title": column.get("title", ""),
                "type": value.get("type") or column.get("type", ""),
                "text": value.get("text", ""),
                "value": parse_json_or_raw(value.get("value")),
            }
        )
    return normalized


def normalize_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for update in updates:
        creator = update.get("creator") or {}
        body = update.get("body") or ""
        normalized.append(
            {
                "id": str(update.get("id", "")),
                "created_at": update.get("created_at", ""),
                "creator": {
                    "id": str(creator.get("id", "")),
                    "name": creator.get("name", ""),
                },
                "body_character_count": len(body),
                "body_preview": body[:500],
            }
        )
    return normalized


def normalize_activity_logs(activity_response: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not activity_response:
        return []
    boards = (((activity_response.get("data") or {}).get("boards")) or [])
    if not boards:
        return []
    logs = boards[0].get("activity_logs") or []
    return [
        {
            "id": str(log.get("id", "")),
            "event": log.get("event", ""),
            "created_at": log.get("created_at", ""),
            "user_id": str(log.get("user_id", "")),
            "data": parse_json_or_raw(log.get("data")),
        }
        for log in logs
    ]


def suggest_group_mappings(
    actual_groups: list[dict[str, Any]],
    configured_labels: dict[str, str],
) -> dict[str, Any]:
    by_name = {group["name"].casefold(): group for group in actual_groups}
    suggestions: dict[str, Any] = {}
    for role, label in configured_labels.items():
        match = by_name.get(str(label).casefold())
        suggestions[role] = {
            "configured_name": label,
            "found": bool(match),
            "id": match["id"] if match else None,
            "actual_name": match["name"] if match else None,
        }
    return suggestions


def suggest_column_candidates(columns: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    suggestions: dict[str, list[dict[str, str]]] = {}
    for semantic_name, keywords in COLUMN_CANDIDATE_KEYWORDS.items():
        matches = []
        for column in columns:
            haystack = f"{column.get('title', '')} {column.get('id', '')}".casefold()
            if any(keyword in haystack for keyword in keywords):
                matches.append(
                    {
                        "id": column.get("id", ""),
                        "title": column.get("title", ""),
                        "type": column.get("type", ""),
                    }
                )
        suggestions[semantic_name] = matches
    return suggestions


def find_description_candidates(column_values: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidates = []
    for value in column_values:
        title = str(value.get("title", ""))
        column_type = str(value.get("type", ""))
        text = str(value.get("text", ""))
        haystack = f"{title} {value.get('id', '')}".casefold()
        if not text:
            continue
        if column_type in {"long_text", "text", "doc"} or any(
            keyword in haystack for keyword in DESCRIPTION_COLUMN_KEYWORDS
        ):
            candidates.append(
                {
                    "column_id": value.get("id", ""),
                    "title": title,
                    "text_preview": text[:500],
                }
            )
    return candidates


def parse_json_or_raw(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def default_output_path(dry_run: bool) -> Path:
    if dry_run:
        return ROOT_DIR / "output" / "board_inspection_dry_run.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT_DIR / "output" / f"board_inspection_{stamp}.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if str(path) == "-":
        print(serialized)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def now_iso(timezone_name: str) -> str:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime.now(tz).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
