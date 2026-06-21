from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .ai_summary import AISummaryError, validate_summary_payload
from .settings import ROOT_DIR, load_settings
from .snapshot import resolve_week_start

REPORT_VERSION = "0.1.0"


class ReportError(RuntimeError):
    """Raised when a report cannot safely be rendered."""


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings(args.config)
    week_start = resolve_week_start(args.week_start, settings.timezone)
    evidence_path = Path(args.evidence) if args.evidence else default_evidence_path(week_start, args.dry_run)
    metrics_path = Path(args.metrics) if args.metrics else default_metrics_path(week_start, args.dry_run)
    ai_summary_path = Path(args.ai_summary) if args.ai_summary else default_ai_summary_path(week_start, args.dry_run)
    report_path = Path(args.output) if args.output else default_report_path(week_start, args.dry_run)
    metadata_path = (
        Path(args.metadata_output)
        if args.metadata_output
        else default_metadata_path(week_start, args.dry_run)
    )

    try:
        evidence = load_json(evidence_path, "evidence")
        metrics = load_json(metrics_path, "metrics")
        ai_summary = load_json(ai_summary_path, "AI summary")
        validate_report_ai_summary(ai_summary)
        markdown = render_markdown_report(evidence=evidence, metrics=metrics, ai_summary=ai_summary)
        metadata = build_run_metadata(
            evidence=evidence,
            metrics=metrics,
            ai_summary=ai_summary,
            evidence_path=evidence_path,
            metrics_path=metrics_path,
            ai_summary_path=ai_summary_path,
            report_path=report_path,
            dry_run=args.dry_run,
        )
        write_text(report_path, markdown, overwrite=args.overwrite or args.dry_run)
        write_json(metadata_path, metadata, overwrite=args.overwrite or args.dry_run)
    except ReportError as exc:
        print(f"Report failed: {exc}", file=sys.stderr)
        return 1

    print(f"Report {'dry run' if args.dry_run else 'package'} written to {report_path}")
    print(f"Run metadata written to {metadata_path}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render deterministic weekly Markdown report.")
    parser.add_argument("--config", default=str(ROOT_DIR / "config" / "config.yml"))
    parser.add_argument("--week-start", default=None, help="Reporting week Monday date, YYYY-MM-DD.")
    parser.add_argument("--evidence", default=None, help="Evidence JSON path. Defaults by mode/week.")
    parser.add_argument("--metrics", default=None, help="Metrics JSON path. Defaults by mode/week.")
    parser.add_argument("--ai-summary", default=None, help="Required AI summary JSON path. Defaults by mode/week.")
    parser.add_argument("--dry-run", action="store_true", help="Read dry-run inputs and write dry-run outputs.")
    parser.add_argument("--output", default=None, help="Markdown output path.")
    parser.add_argument("--metadata-output", default=None, help="Run metadata JSON output path.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing existing final outputs.")
    return parser.parse_args(argv)


def render_markdown_report(
    evidence: dict[str, Any],
    metrics: dict[str, Any],
    ai_summary: dict[str, Any],
) -> str:
    lines: list[str] = []
    core = metrics.get("core_metrics") or {}
    reporting_week = metrics.get("reporting_week") or evidence.get("reporting_week") or {}
    board = metrics.get("board") or evidence.get("board") or {}
    snapshot = evidence.get("snapshot") or {}
    ai_source = ai_summary.get("source") or {}
    executive = ai_summary.get("executive_summary") or {}
    generated_at = now_utc_iso()

    lines.extend(
        [
            f"# Weekly Monday Report - {reporting_week.get('week_start', '')}",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Board | {escape_md(board.get('name', ''))} |",
            f"| Reporting week | {escape_md(reporting_week_label(reporting_week))} |",
            f"| Snapshot timestamp | {escape_md(snapshot.get('captured_at', ''))} |",
            f"| Report generation timestamp | {generated_at} |",
            f"| Report version | {REPORT_VERSION} |",
            f"| AI model | {escape_md(ai_source.get('model', 'unknown'))} |",
            f"| AI prompt version | {escape_md(ai_source.get('prompt_version', 'unknown'))} |",
            "",
            "## Executive Summary",
            "",
            escape_md(executive.get("summary", "")),
            "",
            "### AI-Identified Notable Facts",
            "",
            *bullet_lines(executive.get("notable_facts") or ["None documented by AI summary."]),
            "",
            "### AI-Identified Risks",
            "",
            *bullet_lines(executive.get("risks") or ["None documented by AI summary."]),
            "",
            "### Deterministic Metrics",
            "",
            f"- Monday commitments: {num(core.get('committed_items'))}",
            f"- Delivered committed items: {num(core.get('committed_items_delivered'))}",
            f"- Carryover committed items: {num(core.get('committed_items_carried_over'))}",
            f"- Commitment delivery percentage: {percent(core.get('commitment_delivery_percentage'))}",
            f"- Planned effort committed: {hours(core.get('planned_effort_committed'))}",
            f"- Planned effort carried over: {hours(core.get('planned_effort_carried_over'))}",
            f"- Potential unplanned items activated: {num(core.get('unplanned_items_activated'))}",
            f"- Potential unplanned items delivered: {num(core.get('unplanned_items_delivered'))}",
            "- Python calculated all numeric metrics. The AI summary did not score KPIs.",
            "",
        ]
    )

    lines.extend(render_commitment_summary(core))
    lines.extend(render_main_items_delivered(metrics, evidence, ai_summary))
    lines.extend(render_carryover_and_blockers(metrics, evidence, ai_summary))
    lines.extend(render_unplanned_work(metrics, evidence, ai_summary))
    lines.extend(render_kpi_supporting_evidence(ai_summary))
    lines.extend(render_missing_information(evidence, metrics, ai_summary))
    lines.extend(render_next_week_context(evidence))
    return "\n".join(lines).rstrip() + "\n"


def render_commitment_summary(core: dict[str, Any]) -> list[str]:
    rows = [
        ("Monday commitments", num(core.get("committed_items"))),
        ("Delivered", num(core.get("committed_items_delivered"))),
        ("Still On Deck", num(core.get("committed_items_still_on_deck"))),
        ("In Progress", num(core.get("committed_items_in_progress"))),
        ("Blocked", num(core.get("committed_items_blocked"))),
        ("Carryover", num(core.get("committed_items_carried_over"))),
        ("Commitment delivery percentage", percent(core.get("commitment_delivery_percentage"))),
        ("Planned effort committed", hours(core.get("planned_effort_committed"))),
        ("Planned effort delivered", hours(core.get("planned_effort_delivered"))),
        ("Planned effort carried over", hours(core.get("planned_effort_carried_over"))),
        ("Unplanned items activated", num(core.get("unplanned_items_activated"))),
        ("Unplanned items delivered", num(core.get("unplanned_items_delivered"))),
    ]
    lines = ["## Commitment Summary", "", "| Metric | Result |", "|---|---:|"]
    lines.extend(f"| {label} | {value} |" for label, value in rows)
    lines.append("")
    return lines


def render_main_items_delivered(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
    ai_summary: dict[str, Any],
) -> list[str]:
    delivered = [item for item in metrics.get("committed_items", []) if item.get("bucket") == "delivered"]
    lines = ["## Main Items Delivered", ""]
    ai_items = ai_summary.get("major_completed_work") or []
    if ai_items:
        lines.extend(ai_completed_work_table(ai_items))
        lines.append("")
    if not delivered:
        lines.extend(["No committed items are currently in Pending Approval or Completed Items.", ""])
        return lines
    lines.extend(["### Deterministic Item Table", ""])
    lines.extend(metric_table(delivered, evidence, include_status=True))
    lines.append("")
    return lines


def render_carryover_and_blockers(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
    ai_summary: dict[str, Any],
) -> list[str]:
    carryover = [
        item
        for item in metrics.get("committed_items", [])
        if item.get("bucket") in {"on_deck", "in_progress", "blocked", "missing_or_unknown", "unknown"}
    ]
    lines = ["## Carryover And Blockers", ""]
    ai_items = ai_summary.get("carryover_and_blockers") or []
    if ai_items:
        lines.extend(ai_carryover_table(ai_items))
        lines.append("")
    if not carryover:
        lines.extend(["No committed carryover items were found.", ""])
        return lines
    lines.extend(["### Deterministic Item Table", ""])
    lines.extend(metric_table(carryover, evidence, include_status=True))
    lines.append("")
    return lines


def render_unplanned_work(
    metrics: dict[str, Any],
    evidence: dict[str, Any],
    ai_summary: dict[str, Any],
) -> list[str]:
    unplanned = metrics.get("unplanned_items", [])
    lines = ["## Unplanned Work", ""]
    ai_items = ai_summary.get("unplanned_work") or []
    if ai_items:
        lines.extend(ai_unplanned_table(ai_items))
        lines.append("")
    if not unplanned:
        lines.extend(["No potential unplanned items were identified from current active or delivered board groups.", ""])
        return lines
    lines.extend(["### Deterministic Item Table", ""])
    lines.extend(metric_table(unplanned, evidence, include_status=True))
    lines.append("")
    lines.append("Potential unplanned work is heuristic until activity history confirms activation during the reporting week.")
    lines.append("")
    return lines


def render_kpi_supporting_evidence(ai_summary: dict[str, Any]) -> list[str]:
    kpi = ai_summary.get("kpi_supporting_evidence") or {}
    timing_rows = kpi.get("issue_resolution_activity") or []
    lines = [
        "## KPI-Supporting Evidence",
        "",
        "### Issue Resolution Activity",
        "",
        "| Ticket | Priority | Activation evidence | First work evidence | Elapsed time | Current group | Evidence quality |",
        "|---|---|---|---|---:|---|---|",
    ]
    for row in timing_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_md(row.get("ticket_title", "")),
                    escape_md(row.get("priority", "")),
                    escape_md(row.get("activation_evidence", "")),
                    escape_md(row.get("first_work_evidence", "")),
                    elapsed_label(row.get("elapsed_calendar_hours")),
                    escape_md(row.get("current_group", "")),
                    escape_md(row.get("evidence_quality", "")),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "### Candidate Optimizations",
            "",
            *ai_optimization_table(kpi.get("candidate_optimizations") or []),
            "",
            "### Weekly Recap And Monthly Analysis Work",
            "",
            *ai_reporting_table(kpi.get("weekly_and_monthly_reporting") or []),
            "",
        ]
    )
    return lines


def render_missing_information(
    evidence: dict[str, Any],
    metrics: dict[str, Any],
    ai_summary: dict[str, Any],
) -> list[str]:
    missing = (
        list(ai_summary.get("missing_or_unclear_information") or [])
        + list(metrics.get("warnings") or [])
        + list(evidence.get("warnings") or [])
    )
    for item in evidence.get("items", []):
        quality = item.get("evidence_quality") or {}
        for detail in quality.get("missing") or []:
            missing.append(f"{item.get('item_id', '')}: {detail}")
    missing = dedupe(str(value) for value in missing if value)
    lines = ["## Missing Or Unclear Information", ""]
    if not missing:
        lines.extend(["No missing-information warnings were generated.", ""])
        return lines
    lines.extend(f"- {escape_md(value)}" for value in missing)
    lines.append("")
    return lines


def render_next_week_context(evidence: dict[str, Any]) -> list[str]:
    items = evidence.get("items", [])
    roles = [
        ("On Deck", "on_deck"),
        ("In Progress", "in_progress"),
        ("Blocked", "blocked"),
    ]
    lines = ["## Current Next-Week Context", ""]
    for title, role in roles:
        current = [
            item
            for item in items
            if (item.get("current_state") or {}).get("group_role") == role
            or (role == "blocked" and str((item.get("current_state") or {}).get("workflow_status", "")).casefold() == "blocked")
        ]
        lines.extend([f"### {title}", ""])
        if not current:
            lines.extend(["None found.", ""])
            continue
        lines.extend(evidence_item_table(current))
        lines.append("")
    lines.append("This section is context only and does not create next week's commitment set.")
    lines.append("")
    return lines


def metric_table(rows: list[dict[str, Any]], evidence: dict[str, Any], include_status: bool) -> list[str]:
    by_id = {str(item.get("item_id", "")): item for item in evidence.get("items", [])}
    headers = ["Ticket", "Priority", "Department", "Planned effort", "Current group", "Workflow status", "Link"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        evidence_item = by_id.get(str(row.get("item_id", "")), {})
        fields = evidence_item.get("fields") or {}
        values = [
            escape_md(row.get("name", "")),
            escape_md(row.get("priority", "")),
            escape_md(fields.get("department", "")),
            hours(row.get("planned_effort_hours")),
            escape_md(row.get("current_group", "")),
            escape_md(row.get("workflow_status", "")) if include_status else "",
            link_label(row.get("item_url", "")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def evidence_item_table(rows: list[dict[str, Any]]) -> list[str]:
    headers = ["Ticket", "Priority", "Department", "Current group", "Workflow status", "Link"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for item in rows:
        fields = item.get("fields") or {}
        state = item.get("current_state") or {}
        values = [
            escape_md(item.get("name", "")),
            escape_md(fields.get("priority", "")),
            escape_md(fields.get("department", "")),
            escape_md(state.get("group_name", "")),
            escape_md(state.get("workflow_status", "")),
            link_label(item.get("item_url", "")),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def bullet_lines(values: list[Any]) -> list[str]:
    return [f"- {escape_md(value)}" for value in values]


def ai_completed_work_table(rows: list[dict[str, Any]]) -> list[str]:
    headers = ["Ticket", "Delivery state", "What was solved", "Documented impact", "Evidence gaps"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = [
            escape_md(row.get("ticket_title", "")),
            escape_md(row.get("delivery_state", "")),
            escape_md(row.get("what_was_solved", "")),
            escape_md(row.get("documented_impact", "")),
            escape_md("; ".join(row.get("missing_evidence") or [])),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def ai_carryover_table(rows: list[dict[str, Any]]) -> list[str]:
    headers = ["Ticket", "Current group", "Summary", "Documented blocker", "Documented next action", "Evidence gaps"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = [
            escape_md(row.get("ticket_title", "")),
            escape_md(row.get("current_group", "")),
            escape_md(row.get("summary", "")),
            escape_md(row.get("documented_blocker", "")),
            escape_md(row.get("documented_next_action", "")),
            escape_md("; ".join(row.get("missing_evidence") or [])),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def ai_unplanned_table(rows: list[dict[str, Any]]) -> list[str]:
    headers = ["Ticket", "Current group", "Delivered", "AI summary", "Evidence gaps"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = [
            escape_md(row.get("ticket_title", "")),
            escape_md(row.get("current_group", "")),
            "Yes" if row.get("delivered") else "No",
            escape_md(row.get("summary", "")),
            escape_md("; ".join(row.get("missing_evidence") or [])),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def ai_optimization_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No AI-supported candidate optimizations were identified from the supplied evidence."]
    headers = ["Candidate optimization", "Delivery state", "Change implemented", "Impact classification", "Evidence gaps"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = [
            escape_md(row.get("ticket_title", "")),
            escape_md(row.get("delivery_state", "")),
            escape_md(row.get("change_implemented", "")),
            escape_md(row.get("impact_classification", "")),
            escape_md("; ".join(row.get("missing_evidence") or [])),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def ai_reporting_table(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["No AI-supported reporting deliverables were identified from the supplied evidence."]
    headers = ["Deliverable", "Audience", "Delivery state", "Findings", "Recommendations found", "Evidence gaps"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        values = [
            escape_md(row.get("deliverable_title", "")),
            escape_md(row.get("intended_audience", "")),
            escape_md(row.get("delivery_state", "")),
            escape_md("; ".join(row.get("main_findings") or [])),
            str(row.get("recommendations_found", 0)),
            escape_md("; ".join(row.get("missing_evidence") or [])),
        ]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def build_run_metadata(
    evidence: dict[str, Any],
    metrics: dict[str, Any],
    ai_summary: dict[str, Any],
    evidence_path: Path,
    metrics_path: Path,
    ai_summary_path: Path,
    report_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    item_ids = [str(item.get("item_id", "")) for item in evidence.get("items", []) if item.get("item_id")]
    ai_source = ai_summary.get("source") or {}
    warnings = dedupe(
        list(evidence.get("warnings") or [])
        + list(metrics.get("warnings") or [])
        + list(ai_summary.get("missing_or_unclear_information") or [])
        + (["Dry run: final report output was not written."] if dry_run else [])
    )
    return {
        "run_id": str(uuid.uuid4()),
        "git_commit_sha": git_commit_sha(),
        "reporting_week": (metrics.get("reporting_week") or evidence.get("reporting_week") or {}),
        "board_id": str((metrics.get("board") or evidence.get("board") or {}).get("id", "")),
        "snapshot_file_used": (evidence.get("snapshot") or {}).get("path", ""),
        "evidence_file_used": str(evidence_path),
        "metrics_file_used": str(metrics_path),
        "ai_summary_file_used": str(ai_summary_path),
        "report_file": str(report_path),
        "item_ids_processed": item_ids,
        "ai_model_used": ai_source.get("model", "unknown"),
        "prompt_version": ai_source.get("prompt_version", "unknown"),
        "warnings": warnings,
        "errors": [],
        "generation_timestamp": now_utc_iso(),
        "mode": "dry-run" if dry_run else "report",
    }


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ReportError(f"{label.capitalize()} file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"{label.capitalize()} file is not valid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"{label.capitalize()} file has unexpected JSON shape: {path}")
    return payload


def validate_report_ai_summary(payload: dict[str, Any]) -> None:
    try:
        validate_summary_payload(payload)
    except AISummaryError as exc:
        raise ReportError(f"AI summary is invalid: {exc}") from exc


def default_evidence_path(week_start: date, dry_run: bool) -> Path:
    stem = "evidence_dry_run" if dry_run else "weekly_evidence"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def default_metrics_path(week_start: date, dry_run: bool) -> Path:
    stem = "metrics_dry_run" if dry_run else "weekly_metrics"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def default_ai_summary_path(week_start: date, dry_run: bool) -> Path:
    stem = "ai_summary_dry_run" if dry_run else "ai_summary"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def default_report_path(week_start: date, dry_run: bool) -> Path:
    stem = "weekly_report_dry_run" if dry_run else "weekly_report"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.md"


def default_metadata_path(week_start: date, dry_run: bool) -> Path:
    stem = "run_metadata_dry_run" if dry_run else "run_metadata"
    return ROOT_DIR / "output" / f"{stem}_{week_start.isoformat()}.json"


def write_text(path: Path, content: str, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise ReportError(f"Report output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any], overwrite: bool = False) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if path.exists() and not overwrite:
        raise ReportError(f"Metadata output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def git_commit_sha() -> str:
    git = shutil.which("git") or bundled_git_path()
    if not git:
        return "unavailable"
    try:
        result = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=ROOT_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unavailable"


def bundled_git_path() -> str:
    candidate = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "native"
        / "git"
        / "cmd"
        / "git.exe"
    )
    return str(candidate) if candidate.exists() else ""


def reporting_week_label(reporting_week: dict[str, Any]) -> str:
    start = reporting_week.get("week_start", "")
    end = reporting_week.get("week_end", "")
    return f"{start} to {end}" if start and end else str(start or end)


def evidence_label(value: dict[str, Any]) -> str:
    timestamp = value.get("timestamp")
    source = value.get("source", "unknown")
    return f"{source}: {timestamp}" if timestamp else source


def elapsed_label(value: Any) -> str:
    if value is None:
        return "Unavailable"
    return f"{value} hours"


def link_label(url: str) -> str:
    if not url:
        return ""
    escaped = escape_md(url)
    return f"[monday]({escaped})"


def num(value: Any) -> str:
    if value is None:
        return "0"
    return str(value)


def percent(value: Any) -> str:
    if value is None:
        return "0.0%"
    return f"{value}%"


def hours(value: Any) -> str:
    if value is None:
        return ""
    return f"{value}"


def escape_md(value: Any) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")


def dedupe(values: Any) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
