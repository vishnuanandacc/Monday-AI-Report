from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .monday_client import MondayApiError, MondayClient
from .settings import ROOT_DIR, AppSettings, get_monday_token, load_settings
from .snapshot import field_text, parse_json_or_raw, parse_number, resolve_week_start, useful_fields

BOARD_EVIDENCE_QUERY = """
query CollectBoardEvidence($boardIds: [ID!]!, $limit: Int!, $updatesLimit: Int!) {
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
        updates(limit: $updatesLimit) {
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

NEXT_EVIDENCE_PAGE_QUERY = """
query CollectNextEvidencePage($cursor: String!, $updatesLimit: Int!) {
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
      updates(limit: $updatesLimit) {
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
"""

BOARD_ACTIVITY_QUERY = """
query CollectBoardActivity($boardIds: [ID!]!, $activityLimit: Int!) {
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

DELIVERED_ROLES = {"pending_approval", "complete"}
CARRYOVER_ROLES = {"on_deck", "in_progress", "blocked"}
ACTIVE_EVIDENCE_ROLES = {"on_deck", "in_progress", "blocked", "pending_approval", "complete"}


class EvidenceError(RuntimeError):
    """Raised when evidence collection cannot safely continue."""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config).with_board_id(args.board_id)
    week_start = resolve_week_start(args.week_start, settings.timezone)
    snapshot_path = Path(args.snapshot) if args.snapshot else default_snapshot_path(week_start)
    output_path = Path(args.output) if args.output else default_output_path(week_start, args.dry_run)

    missing = settings.missing_live_inspection_requirements()
    if missing:
        print("Evidence collection requires: " + ", ".join(missing) + ".", file=sys.stderr)
        return 2

    client = MondayClient(
        token=get_monday_token(),
        api_version=os.environ.get("MONDAY_API_VERSION", ""),
    )

    try:
        snapshot = load_snapshot(snapshot_path)
        board = fetch_current_board_evidence(
            client=client,
            board_id=settings.monday_board_id or str(snapshot.get("board_id", "")),
            page_limit=args.page_limit,
            max_pages=args.max_pages,
            updates_limit=args.updates_limit,
        )
        activity_logs = fetch_board_activity_logs(
            client=client,
            board_id=settings.monday_board_id or str(snapshot.get("board_id", "")),
            activity_limit=args.activity_limit,
        )
        payload = build_evidence_package(
            settings=settings,
            snapshot=snapshot,
            snapshot_path=snapshot_path,
            board=board,
            activity_logs=activity_logs,
            updates_limit=args.updates_limit,
            dry_run=args.dry_run,
        )
        write_json(output_path, payload, overwrite=args.overwrite or args.dry_run)
    except (MondayApiError, EvidenceError) as exc:
        print(f"Evidence collection failed: {exc}", file=sys.stderr)
        details = getattr(exc, "details", None)
        if details:
            print(json.dumps(details, indent=2, sort_keys=True), file=sys.stderr)
        return 1

    item_count = len(payload.get("items", []))
    unplanned_count = len(payload.get("potential_unplanned_items", []))
    print(
        f"Evidence {'dry run' if args.dry_run else 'package'} written to {output_path} "
        f"({item_count} item(s), {unplanned_count} potential unplanned)"
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect normalized monday evidence for a weekly report.")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "config.yml"))
    parser.add_argument("--board-id", default=None, help="Override MONDAY_BOARD_ID/config board id.")
    parser.add_argument("--week-start", default=None, help="Reporting week Monday date, YYYY-MM-DD.")
    parser.add_argument("--snapshot", default=None, help="Snapshot file path. Defaults to data/snapshots/YYYY-MM-DD.json.")
    parser.add_argument("--dry-run", action="store_true", help="Write preview output without treating it as final.")
    parser.add_argument("--output", default=None, help="Output JSON path. Use '-' for stdout.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing final evidence file.")
    parser.add_argument("--page-limit", type=int, default=500, help="Items per first page request.")
    parser.add_argument("--max-pages", type=int, default=20, help="Safety cap for pagination.")
    parser.add_argument("--updates-limit", type=int, default=20, help="Recent updates/comments per item.")
    parser.add_argument("--activity-limit", type=int, default=200, help="Recent board activity records.")
    return parser.parse_args(argv)


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EvidenceError(f"Snapshot file not found: {path}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"Snapshot file is not valid JSON: {path}") from exc
    if not isinstance(snapshot, dict):
        raise EvidenceError(f"Snapshot file has unexpected JSON shape: {path}")
    if not snapshot.get("week_start"):
        raise EvidenceError("Snapshot is missing week_start.")
    if not isinstance(snapshot.get("committed_items"), list):
        raise EvidenceError("Snapshot is missing committed_items list.")
    return snapshot


def fetch_current_board_evidence(
    client: MondayClient,
    board_id: str,
    page_limit: int = 500,
    max_pages: int = 20,
    updates_limit: int = 20,
) -> dict[str, Any]:
    if page_limit < 1 or page_limit > 500:
        raise EvidenceError("--page-limit must be between 1 and 500.")
    if max_pages < 1:
        raise EvidenceError("--max-pages must be at least 1.")
    if updates_limit < 0:
        raise EvidenceError("--updates-limit must be at least 0.")

    response = client.execute(
        BOARD_EVIDENCE_QUERY,
        variables={"boardIds": [str(board_id)], "limit": page_limit, "updatesLimit": updates_limit},
        operation_name="CollectBoardEvidence",
    )
    boards = (((response.get("data") or {}).get("boards")) or [])
    if not boards:
        raise EvidenceError("No board was returned. Check MONDAY_BOARD_ID and permissions.")

    board = boards[0]
    item_page = board.get("items_page") or {}
    items = list(item_page.get("items") or [])
    cursor = item_page.get("cursor")
    page_count = 1

    while cursor:
        if page_count >= max_pages:
            raise EvidenceError(
                f"Pagination exceeded --max-pages={max_pages}; refusing to create incomplete evidence."
            )
        next_response = client.execute(
            NEXT_EVIDENCE_PAGE_QUERY,
            variables={"cursor": cursor, "updatesLimit": updates_limit},
            operation_name="CollectNextEvidencePage",
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


def fetch_board_activity_logs(
    client: MondayClient,
    board_id: str,
    activity_limit: int = 200,
) -> list[dict[str, Any]]:
    if activity_limit <= 0:
        return []
    response = client.execute(
        BOARD_ACTIVITY_QUERY,
        variables={"boardIds": [str(board_id)], "activityLimit": activity_limit},
        operation_name="CollectBoardActivity",
    )
    boards = (((response.get("data") or {}).get("boards")) or [])
    if not boards:
        return []
    return boards[0].get("activity_logs") or []


def build_evidence_package(
    settings: AppSettings,
    snapshot: dict[str, Any],
    snapshot_path: Path,
    board: dict[str, Any],
    activity_logs: list[dict[str, Any]],
    updates_limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    week_start = str(snapshot.get("week_start", ""))
    committed_by_id = {
        str(item.get("item_id", "")): item
        for item in snapshot.get("committed_items", [])
        if item.get("item_id")
    }
    current_by_id = {str(item.get("id", "")): item for item in board.get("items", [])}
    active_group_ids = configured_group_ids(settings, ACTIVE_EVIDENCE_ROLES)
    relevant_ids = set(committed_by_id)
    relevant_ids.update(
        str(item.get("id", ""))
        for item in board.get("items", [])
        if is_relevant_current_item(item, active_group_ids, settings)
    )

    normalized_activity = [normalize_activity_log(log) for log in activity_logs]
    activity_by_item = group_activity_by_item_id(normalized_activity)
    items = [
        normalize_evidence_item(
            item_id=item_id,
            current_item=current_by_id.get(item_id),
            snapshot_item=committed_by_id.get(item_id),
            activity_logs=activity_by_item.get(item_id, []),
            settings=settings,
        )
        for item_id in sorted(relevant_ids, key=item_sort_key(committed_by_id, current_by_id))
    ]
    potential_unplanned = [
        item
        for item in items
        if not item["committed"] and item["current_state"]["group_role"] in ACTIVE_EVIDENCE_ROLES
    ]

    warnings = evidence_warnings(settings, snapshot, current_by_id, activity_logs, dry_run)
    return {
        "version": "0.1.0",
        "mode": "dry-run" if dry_run else "evidence",
        "generated_at": now_utc_iso(),
        "reporting_week": {
            "week_start": week_start,
            "week_end": week_end_for(week_start),
        },
        "board": {
            "id": str(board.get("id") or snapshot.get("board_id", "")),
            "name": board.get("name") or snapshot.get("board_name", ""),
        },
        "snapshot": {
            "path": str(snapshot_path),
            "captured_at": snapshot.get("captured_at", ""),
            "committed_count": len(committed_by_id),
            "warnings": snapshot.get("warnings", []),
        },
        "source": {
            "page_count": board.get("page_count", 1),
            "total_items_read": len(board.get("items", [])),
            "activity_logs_read": len(activity_logs),
            "recent_updates_per_item_requested": updates_limit,
        },
        "items": items,
        "committed_item_ids": sorted(committed_by_id),
        "potential_unplanned_item_ids": [item["item_id"] for item in potential_unplanned],
        "potential_unplanned_items": potential_unplanned,
        "activity_logs": normalized_activity,
        "warnings": warnings,
    }


def normalize_evidence_item(
    item_id: str,
    current_item: dict[str, Any] | None,
    snapshot_item: dict[str, Any] | None,
    activity_logs: list[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    if current_item:
        group = current_item.get("group") or {}
        fields = useful_fields(current_item.get("column_values") or [], settings.column_ids)
        updates = normalize_updates(current_item.get("updates") or [])
        group_id = str(group.get("id", ""))
        group_name = group.get("title", "")
        name = current_item.get("name", "")
        item_url = current_item.get("url", "")
        created_at = current_item.get("created_at", "")
        updated_at = current_item.get("updated_at", "")
    else:
        fields = {}
        updates = []
        group_id = ""
        group_name = ""
        name = (snapshot_item or {}).get("name", "")
        item_url = (snapshot_item or {}).get("item_url", "")
        created_at = (snapshot_item or {}).get("created_at", "")
        updated_at = (snapshot_item or {}).get("updated_at", "")

    group_role = group_role_for(group_id, group_name, settings)
    committed = snapshot_item is not None
    classification = classify_current_state(group_role, current_item is not None)
    priority = field_text(fields, "priority") or str((snapshot_item or {}).get("priority") or "")
    planned_effort = parse_number(field_text(fields, "planned_effort"))
    if planned_effort is None:
        planned_effort = (snapshot_item or {}).get("planned_effort_hours")

    return {
        "item_id": item_id,
        "name": name,
        "item_url": item_url,
        "committed": committed,
        "potential_unplanned": not committed and group_role in ACTIVE_EVIDENCE_ROLES,
        "snapshot": snapshot_item,
        "current_state": {
            "present_on_board": current_item is not None,
            "group_id": group_id,
            "group_name": group_name,
            "group_role": group_role,
            "classification": classification,
            "workflow_status": field_text(fields, "workflow_status"),
            "created_at": created_at,
            "updated_at": updated_at,
        },
        "fields": {
            "priority": priority,
            "owner": field_text(fields, "owner") or str((snapshot_item or {}).get("owner") or ""),
            "department": field_text(fields, "department") or str((snapshot_item or {}).get("department") or ""),
            "planned_effort_hours": planned_effort,
            "actual_effort_hours": parse_number(field_text(fields, "actual_effort")),
            "due_date": field_text(fields, "due_date") or str((snapshot_item or {}).get("due_date") or ""),
            "origin": field_text(fields, "origin") or str((snapshot_item or {}).get("origin") or ""),
            "user_story": field_text(fields, "user_story") or str((snapshot_item or {}).get("user_story") or ""),
            "useful_fields": fields,
        },
        "updates": updates,
        "activity_logs": activity_logs,
        "evidence_quality": item_evidence_quality(current_item, updates, activity_logs, settings),
    }


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
                "body": body,
                "body_character_count": len(body),
            }
        )
    return normalized


def normalize_activity_log(log: dict[str, Any]) -> dict[str, Any]:
    data = parse_json_or_raw(log.get("data"))
    return {
        "id": str(log.get("id", "")),
        "event": log.get("event", ""),
        "created_at": str(log.get("created_at", "")),
        "user_id": str(log.get("user_id", "")),
        "item_id": extract_item_id(data),
        "data": data,
    }


def configured_group_ids(settings: AppSettings, roles: set[str]) -> set[str]:
    return {settings.group_ids.get(role, "") for role in roles if settings.group_ids.get(role, "")}


def is_relevant_current_item(
    item: dict[str, Any],
    active_group_ids: set[str],
    settings: AppSettings,
) -> bool:
    group = item.get("group") or {}
    group_id = str(group.get("id", ""))
    group_name = str(group.get("title", ""))
    role = group_role_for(group_id, group_name, settings)
    return group_id in active_group_ids or role in ACTIVE_EVIDENCE_ROLES


def classify_current_state(group_role: str, present_on_board: bool) -> str:
    if not present_on_board:
        return "missing_or_unknown"
    if group_role in DELIVERED_ROLES:
        return "delivered"
    if group_role == "on_deck":
        return "not_delivered"
    if group_role == "in_progress":
        return "active_carryover"
    if group_role == "blocked":
        return "blocked_carryover"
    return "not_reported"


def group_role_for(group_id: str, group_name: str, settings: AppSettings) -> str:
    for role, configured_id in settings.group_ids.items():
        if configured_id and group_id == configured_id:
            return role
    for role, configured_name in settings.groups.items():
        if configured_name and group_name.casefold() == configured_name.casefold():
            return role
    return "unknown"


def group_activity_by_item_id(activity_logs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for log in activity_logs:
        item_id = str(log.get("item_id") or "")
        if item_id:
            grouped.setdefault(item_id, []).append(log)
    return grouped


def extract_item_id(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("pulse_id", "item_id", "pulseId", "itemId"):
            if data.get(key):
                return str(data[key])
    return ""


def item_evidence_quality(
    current_item: dict[str, Any] | None,
    updates: list[dict[str, Any]],
    activity_logs: list[dict[str, Any]],
    settings: AppSettings,
) -> dict[str, Any]:
    missing = []
    if current_item is None:
        missing.append("Current item state could not be found on board.")
    if not updates:
        missing.append("No recent updates/comments were retrieved for this item.")
    if not activity_logs:
        missing.append("No recent activity logs were retrieved for this item.")
    if not settings.group_ids.get("blocked"):
        missing.append("Blocked is represented by status data or is unavailable as a group.")
    return {
        "has_current_state": current_item is not None,
        "has_recent_updates": bool(updates),
        "has_recent_activity": bool(activity_logs),
        "missing": missing,
    }


def evidence_warnings(
    settings: AppSettings,
    snapshot: dict[str, Any],
    current_by_id: dict[str, dict[str, Any]],
    activity_logs: list[dict[str, Any]],
    dry_run: bool,
) -> list[str]:
    warnings = []
    if dry_run:
        warnings.append("Dry run: final weekly_evidence output was not written.")
    if not settings.group_ids.get("blocked"):
        warnings.append("No Blocked group ID is configured; blocked work may appear in the workflow Status column.")
    if settings.groups.get("complete") == "Completed Items":
        warnings.append("Complete delivery state is mapped to existing board group 'Completed Items'.")
    missing_committed = [
        str(item.get("item_id"))
        for item in snapshot.get("committed_items", [])
        if str(item.get("item_id")) not in current_by_id
    ]
    if missing_committed:
        warnings.append(f"{len(missing_committed)} committed item(s) were not found in current board retrieval.")
    if not activity_logs:
        warnings.append("No activity logs were retrieved; activation and movement history may be incomplete.")
    warnings.append("Potential unplanned work is heuristic until activity history confirms activation during the reporting week.")
    return warnings


def default_snapshot_path(week_start: date) -> Path:
    return ROOT_DIR / "data" / "snapshots" / f"{week_start.isoformat()}.json"


def default_output_path(week_start: date, dry_run: bool) -> Path:
    stem = "evidence_dry_run" if dry_run else "weekly_evidence"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = False) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if str(path) == "-":
        print(serialized)
        return
    if path.exists() and not overwrite:
        raise EvidenceError(f"Evidence output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def week_end_for(week_start: str) -> str:
    try:
        start = date.fromisoformat(week_start)
    except ValueError:
        return ""
    return (start + timedelta(days=4)).isoformat()


def item_sort_key(
    committed_by_id: dict[str, dict[str, Any]],
    current_by_id: dict[str, dict[str, Any]],
):
    committed_order = {item_id: index for index, item_id in enumerate(committed_by_id)}

    def sort_key(item_id: str) -> tuple[int, int | str]:
        if item_id in committed_order:
            return (0, committed_order[item_id])
        name = str((current_by_id.get(item_id) or {}).get("name", ""))
        return (1, name.casefold())

    return sort_key


if __name__ == "__main__":
    raise SystemExit(main())
