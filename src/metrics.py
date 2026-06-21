from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .collect_evidence import ACTIVE_EVIDENCE_ROLES, DELIVERED_ROLES
from .settings import ROOT_DIR, AppSettings, load_settings
from .snapshot import parse_number, resolve_week_start

CARRYOVER_BUCKETS = {"on_deck", "in_progress", "blocked"}
FIRST_WORK_KEYWORDS = (
    "investigat",
    "started",
    "working",
    "implemented",
    "fixed",
    "resolved",
    "corrected",
    "completed",
    "contacted",
    "blocked",
    "dependency",
    "followed up",
)
GROUP_ID_KEYS = ("group_id", "to_group_id", "dest_group_id", "new_group_id")


class MetricsError(RuntimeError):
    """Raised when metrics cannot safely be calculated."""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config)
    week_start = resolve_week_start(args.week_start, settings.timezone)
    evidence_path = Path(args.evidence) if args.evidence else default_evidence_path(week_start, args.dry_run)
    output_path = Path(args.output) if args.output else default_output_path(week_start, args.dry_run)

    try:
        evidence = load_evidence(evidence_path)
        payload = build_metrics_package(
            evidence=evidence,
            settings=settings,
            evidence_path=evidence_path,
            dry_run=args.dry_run,
        )
        write_json(output_path, payload, overwrite=args.overwrite or args.dry_run)
    except MetricsError as exc:
        print(f"Metrics failed: {exc}", file=sys.stderr)
        return 1

    core = payload["core_metrics"]
    print(
        f"Metrics {'dry run' if args.dry_run else 'package'} written to {output_path} "
        f"({core['committed_items']} committed, {core['committed_items_delivered']} delivered)"
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate deterministic weekly metrics from evidence JSON.")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "config.yml"))
    parser.add_argument("--week-start", default=None, help="Reporting week Monday date, YYYY-MM-DD.")
    parser.add_argument("--evidence", default=None, help="Evidence JSON path. Defaults by mode/week.")
    parser.add_argument("--dry-run", action="store_true", help="Read dry-run evidence and write dry-run metrics.")
    parser.add_argument("--output", default=None, help="Output JSON path. Use '-' for stdout.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing final metrics file.")
    return parser.parse_args(argv)


def load_evidence(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise MetricsError(f"Evidence file not found: {path}")
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MetricsError(f"Evidence file is not valid JSON: {path}") from exc
    if not isinstance(evidence, dict):
        raise MetricsError(f"Evidence file has unexpected JSON shape: {path}")
    if not isinstance(evidence.get("items"), list):
        raise MetricsError("Evidence file is missing items list.")
    return evidence


def build_metrics_package(
    evidence: dict[str, Any],
    settings: AppSettings,
    evidence_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    items = evidence.get("items") or []
    committed_items = [item for item in items if item.get("committed")]
    unplanned_items = [item for item in items if item.get("potential_unplanned")]
    committed_metrics = committed_breakdown(committed_items)
    unplanned_metrics = unplanned_breakdown(unplanned_items)
    group_counts = current_item_count_by_active_group(items)
    timing = [timing_evidence_for_item(item, settings) for item in items]
    warnings = metrics_warnings(evidence, settings, committed_items, timing, dry_run)

    return {
        "version": "0.1.0",
        "mode": "dry-run" if dry_run else "metrics",
        "generated_at": now_utc_iso(),
        "reporting_week": evidence.get("reporting_week", {}),
        "board": evidence.get("board", {}),
        "source": {
            "evidence_path": str(evidence_path),
            "evidence_mode": evidence.get("mode", ""),
            "evidence_generated_at": evidence.get("generated_at", ""),
            "snapshot_path": (evidence.get("snapshot") or {}).get("path", ""),
        },
        "core_metrics": {
            **committed_metrics,
            **unplanned_metrics,
            "current_item_count_by_active_group": group_counts,
        },
        "committed_items": [item_metric_row(item) for item in committed_items],
        "unplanned_items": [item_metric_row(item) for item in unplanned_items],
        "timing_evidence": timing,
        "warnings": warnings,
    }


def committed_breakdown(committed_items: list[dict[str, Any]]) -> dict[str, Any]:
    buckets = [delivery_bucket(item) for item in committed_items]
    delivered = count_bucket(buckets, "delivered")
    on_deck = count_bucket(buckets, "on_deck")
    in_progress = count_bucket(buckets, "in_progress")
    blocked = count_bucket(buckets, "blocked")
    carryover = on_deck + in_progress + blocked

    planned_committed = sum_planned_effort(committed_items)
    planned_delivered = sum_planned_effort(
        item for item in committed_items if delivery_bucket(item) == "delivered"
    )
    planned_carryover = sum_planned_effort(
        item for item in committed_items if delivery_bucket(item) in CARRYOVER_BUCKETS
    )

    return {
        "committed_items": len(committed_items),
        "committed_items_delivered": delivered,
        "committed_items_still_on_deck": on_deck,
        "committed_items_in_progress": in_progress,
        "committed_items_blocked": blocked,
        "committed_items_carried_over": carryover,
        "committed_items_missing_or_unknown": count_bucket(buckets, "missing_or_unknown"),
        "commitment_delivery_percentage": percentage(delivered, len(committed_items)),
        "planned_effort_committed": planned_committed,
        "planned_effort_delivered": planned_delivered,
        "planned_effort_carried_over": planned_carryover,
    }


def unplanned_breakdown(unplanned_items: list[dict[str, Any]]) -> dict[str, Any]:
    delivered = sum(1 for item in unplanned_items if delivery_bucket(item) == "delivered")
    return {
        "unplanned_items_activated": len(unplanned_items),
        "unplanned_items_delivered": delivered,
    }


def current_item_count_by_active_group(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {role: 0 for role in ("on_deck", "in_progress", "blocked", "pending_approval", "complete")}
    for item in items:
        state = item.get("current_state") or {}
        if not state.get("present_on_board", True):
            continue
        role = str(state.get("group_role") or "unknown")
        if role in counts:
            counts[role] += 1
    return counts


def delivery_bucket(item: dict[str, Any]) -> str:
    state = item.get("current_state") or {}
    if not state.get("present_on_board", True):
        return "missing_or_unknown"

    role = str(state.get("group_role") or "")
    if role in DELIVERED_ROLES:
        return "delivered"
    if is_status_blocked(item):
        return "blocked"
    if role == "blocked":
        return "blocked"
    if role == "in_progress":
        return "in_progress"
    if role == "on_deck":
        return "on_deck"
    return "unknown"


def item_metric_row(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("current_state") or {}
    fields = item.get("fields") or {}
    bucket = delivery_bucket(item)
    return {
        "item_id": str(item.get("item_id", "")),
        "name": item.get("name", ""),
        "committed": bool(item.get("committed")),
        "potential_unplanned": bool(item.get("potential_unplanned")),
        "bucket": bucket,
        "current_group": state.get("group_name", ""),
        "current_group_role": state.get("group_role", ""),
        "workflow_status": state.get("workflow_status", ""),
        "priority": fields.get("priority", ""),
        "planned_effort_hours": numeric_or_none(fields.get("planned_effort_hours")),
        "item_url": item.get("item_url", ""),
    }


def timing_evidence_for_item(item: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    activation = activation_evidence(item, settings)
    first_work = first_work_evidence(item, settings)
    elapsed = elapsed_hours(activation.get("timestamp"), first_work.get("timestamp"))
    warnings = []
    if not activation.get("timestamp"):
        warnings.append("No reliable activation timestamp found.")
    if not first_work.get("timestamp"):
        warnings.append("No first-work evidence timestamp found.")
    if elapsed is None and activation.get("timestamp") and first_work.get("timestamp"):
        warnings.append("Could not calculate elapsed time from available timestamps.")

    return {
        "item_id": str(item.get("item_id", "")),
        "name": item.get("name", ""),
        "priority": (item.get("fields") or {}).get("priority", ""),
        "current_group": (item.get("current_state") or {}).get("group_name", ""),
        "current_group_role": (item.get("current_state") or {}).get("group_role", ""),
        "activation": activation,
        "first_work": first_work,
        "elapsed_calendar_hours": elapsed,
        "duration_type": "calendar_hours",
        "warnings": warnings,
    }


def activation_evidence(item: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    logs = sorted_activity(item)
    on_deck_log = first_activity_for_group(logs, settings.group_ids.get("on_deck", ""))
    if on_deck_log:
        return activity_timestamp_source(on_deck_log, "first documented move into On Deck")

    in_progress_log = first_activity_for_group(logs, settings.group_ids.get("in_progress", ""))
    if in_progress_log:
        return activity_timestamp_source(in_progress_log, "first documented move into In Progress")

    state = item.get("current_state") or {}
    created_at = parse_timestamp(state.get("created_at"))
    if created_at and state.get("group_role") in ACTIVE_EVIDENCE_ROLES:
        return {
            "timestamp": created_at.isoformat(),
            "source": "item created_at while currently in active/delivered group",
            "evidence": state.get("created_at", ""),
        }

    earliest_log = first_parsed_activity(logs)
    if earliest_log:
        return activity_timestamp_source(earliest_log, "earliest relevant activity timestamp")

    return {"timestamp": None, "source": "unknown", "evidence": ""}


def first_work_evidence(item: dict[str, Any], settings: AppSettings) -> dict[str, Any]:
    logs = sorted_activity(item)
    in_progress_log = first_activity_for_group(logs, settings.group_ids.get("in_progress", ""))
    if in_progress_log:
        return activity_timestamp_source(in_progress_log, "move to In Progress")

    delivered_group_ids = [settings.group_ids.get(role, "") for role in DELIVERED_ROLES]
    for group_id in delivered_group_ids:
        delivered_log = first_activity_for_group(logs, group_id)
        if delivered_log:
            return activity_timestamp_source(delivered_log, "move to delivered group")

    update = first_substantive_update(item.get("updates") or [])
    if update:
        timestamp = parse_timestamp(update.get("created_at"))
        return {
            "timestamp": timestamp.isoformat() if timestamp else None,
            "source": "substantive update/comment keyword",
            "evidence": update.get("created_at", ""),
            "update_id": update.get("id", ""),
        }

    if is_status_blocked(item):
        updated_at = parse_timestamp((item.get("current_state") or {}).get("updated_at"))
        return {
            "timestamp": updated_at.isoformat() if updated_at else None,
            "source": "blocked workflow status",
            "evidence": (item.get("current_state") or {}).get("workflow_status", ""),
        }

    return {"timestamp": None, "source": "unknown", "evidence": ""}


def first_activity_for_group(logs: list[dict[str, Any]], group_id: str) -> dict[str, Any] | None:
    if not group_id:
        return None
    for log in logs:
        data = log.get("data")
        if not isinstance(data, dict):
            continue
        if any(str(data.get(key, "")) == group_id for key in GROUP_ID_KEYS):
            return log
    return None


def first_parsed_activity(logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for log in logs:
        if parse_timestamp(log.get("created_at")):
            return log
    return None


def sorted_activity(item: dict[str, Any]) -> list[dict[str, Any]]:
    logs = list(item.get("activity_logs") or [])
    return sorted(logs, key=lambda log: parse_timestamp(log.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc))


def activity_timestamp_source(log: dict[str, Any], source: str) -> dict[str, Any]:
    timestamp = parse_timestamp(log.get("created_at"))
    return {
        "timestamp": timestamp.isoformat() if timestamp else None,
        "source": source,
        "evidence": log.get("created_at", ""),
        "activity_log_id": log.get("id", ""),
        "event": log.get("event", ""),
    }


def first_substantive_update(updates: list[dict[str, Any]]) -> dict[str, Any] | None:
    sorted_updates = sorted(
        updates,
        key=lambda update: parse_timestamp(update.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc),
    )
    for update in sorted_updates:
        body = str(update.get("body") or "").casefold()
        if any(keyword in body for keyword in FIRST_WORK_KEYWORDS):
            return update
    return None


def elapsed_hours(start: str | None, end: str | None) -> float | None:
    start_dt = parse_timestamp(start)
    end_dt = parse_timestamp(end)
    if not start_dt or not end_dt:
        return None
    return round((end_dt - start_dt).total_seconds() / 3600, 2)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        numeric = int(text)
        for divisor in (1, 1000, 1_000_000, 10_000_000):
            try:
                candidate = datetime.fromtimestamp(numeric / divisor, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
            if 2000 <= candidate.year <= 2100:
                return candidate
        return None

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def metrics_warnings(
    evidence: dict[str, Any],
    settings: AppSettings,
    committed_items: list[dict[str, Any]],
    timing: list[dict[str, Any]],
    dry_run: bool,
) -> list[str]:
    warnings = list(evidence.get("warnings") or [])
    if dry_run:
        warnings.append("Dry run: final weekly_metrics output was not written.")
    if not settings.group_ids.get("blocked"):
        warnings.append("Blocked metrics use workflow Status because no Blocked group ID is configured.")
    missing_effort = [
        item.get("item_id", "")
        for item in committed_items
        if numeric_or_none((item.get("fields") or {}).get("planned_effort_hours")) is None
    ]
    if missing_effort:
        warnings.append(f"{len(missing_effort)} committed item(s) have no planned effort.")
    missing_activation = [row.get("item_id", "") for row in timing if not (row.get("activation") or {}).get("timestamp")]
    if missing_activation:
        warnings.append(f"{len(missing_activation)} item(s) have no reliable activation timestamp.")
    return dedupe_preserve_order(str(warning) for warning in warnings if warning)


def sum_planned_effort(items: Any) -> float:
    total = 0.0
    for item in items:
        value = numeric_or_none((item.get("fields") or {}).get("planned_effort_hours"))
        if value is not None:
            total += value
    return round(total, 2)


def numeric_or_none(value: Any) -> float | None:
    parsed = parse_number(value)
    if parsed is None:
        return None
    return float(parsed)


def percentage(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def count_bucket(buckets: list[str], bucket: str) -> int:
    return sum(1 for value in buckets if value == bucket)


def is_status_blocked(item: dict[str, Any]) -> bool:
    status = str((item.get("current_state") or {}).get("workflow_status") or "")
    return status.casefold() == "blocked"


def default_evidence_path(week_start: date, dry_run: bool) -> Path:
    stem = "evidence_dry_run" if dry_run else "weekly_evidence"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def default_output_path(week_start: date, dry_run: bool) -> Path:
    stem = "metrics_dry_run" if dry_run else "weekly_metrics"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = False) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if str(path) == "-":
        print(serialized)
        return
    if path.exists() and not overwrite:
        raise MetricsError(f"Metrics output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dedupe_preserve_order(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
