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
- Manual snapshot GitHub Actions workflow
- Unit tests using the Python standard library

Not implemented yet:

- Friday evidence collection
- Metrics
- AI summary
- Markdown report rendering

## Configure

Copy `.env.example` to `.env` for local runs, or set the same values as GitHub Actions secrets. Do not commit `.env`.

Local `.env` values are loaded automatically. Real shell environment variables take precedence over `.env`.

Required for live board inspection:

- `MONDAY_BOARD_ID`
- `MONDAY_API_TOKEN` or `MONDAY_MCP_AUTH`

Optional:

- `REPORT_TIMEZONE`
- `MONDAY_API_VERSION`

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
