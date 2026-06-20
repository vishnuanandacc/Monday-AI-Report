# Monday Weekly Report MVP

Barebones Python tooling for generating a weekly management report from an existing monday.com Kanban board. The MVP is intentionally incremental: start with board inspection, confirm the actual monday response shapes, then build snapshots and reporting.

## Current Phase

Implemented:

- Project skeleton
- Config loading with environment substitution
- Read-only monday GraphQL client
- Board inspection CLI
- Dry-run inspection output
- Unit tests using the Python standard library

Not implemented yet:

- Monday snapshot generation
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
