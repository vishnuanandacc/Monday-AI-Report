# Monday Weekly Report MVP

Barebones Python tooling for generating a weekly management report from an existing monday.com Kanban board. The MVP is intentionally incremental: start with board inspection, confirm the actual monday response shapes, then build snapshots and reporting.

## Current Phase

Implemented:

- Project skeleton
- Config loading with environment substitution
- Read-only monday GraphQL client
- Board inspection CLI
- Dry-run inspection output
- Monday On Deck snapshot CLI
- Friday evidence collection CLI
- Deterministic metrics CLI
- Deterministic Markdown report renderer
- Guarded AI summary CLI with dry-run mode
- Manual snapshot GitHub Actions workflow
- Unit tests using the Python standard library

Not implemented yet:

- AI summary integration into Markdown report

## Configure

Copy `.env.example` to `.env` for local runs, or set the same values as GitHub Actions secrets. Do not commit `.env`.

Local `.env` values are loaded automatically. Real shell environment variables take precedence over `.env`.

Required for live board inspection:

- `MONDAY_BOARD_ID`
- `MONDAY_API_TOKEN` or `MONDAY_MCP_AUTH`

Optional:

- `REPORT_TIMEZONE`
- `MONDAY_API_VERSION`
- `OPENAI_MODEL`

## Local Commands

Dry run without calling monday:

```powershell
python -m src.inspect_board --dry-run
```

Live read-only board inspection:

```powershell
python -m src.inspect_board --sample-limit 5 --activity-limit 20
```

Snapshot dry run. This reads monday and writes only `output/snapshot_dry_run_YYYY-MM-DD.json`:

```powershell
python -m src.snapshot --week-start 2026-06-22 --dry-run
```

Real Monday snapshot. This writes `data/snapshots/YYYY-MM-DD.json` and refuses to overwrite by default:

```powershell
python -m src.snapshot --week-start 2026-06-22
```

Evidence collection dry run. This loads the snapshot, reads monday, and writes only `output/evidence_dry_run_YYYY-MM-DD.json`:

```powershell
python -m src.collect_evidence --week-start 2026-06-22 --dry-run
```

Metrics dry run. This reads evidence JSON locally and writes `output/metrics_dry_run_YYYY-MM-DD.json`:

```powershell
python -m src.metrics --week-start 2026-06-22 --dry-run
```

Report dry run. This reads evidence and metrics JSON locally and writes Markdown plus run metadata:

```powershell
python -m src.report --week-start 2026-06-22 --dry-run
```

AI summary dry run. This validates the schema and writes a placeholder without calling OpenAI:

```powershell
python -m src.ai_summary --week-start 2026-06-22 --dry-run
```

Live AI summary. This sends normalized evidence and metrics to OpenAI and writes structured JSON:

```powershell
python -m src.ai_summary --week-start 2026-06-22 --evidence output/evidence_dry_run_2026-06-22.json --metrics output/metrics_dry_run_2026-06-22.json --output output/ai_summary_2026-06-22.json
```

Run tests:

```powershell
python -m unittest discover -s tests
```

## Safety Rules

- No monday mutations are implemented.
- The inspector does not create groups, columns, items, updates, or automations.
- Tokens are never printed.
- Ticket comments are treated as untrusted source data.
- Reporting and AI generation are deferred until board response shapes are confirmed.
