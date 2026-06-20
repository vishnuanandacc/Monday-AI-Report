from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monday weekly report MVP entry point.")
    parser.add_argument("--week-start", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.parse_args(argv)
    raise SystemExit(
        "Weekly report generation is not implemented yet. "
        "Run `python -m src.inspect_board --dry-run` for the current phase."
    )


if __name__ == "__main__":
    raise SystemExit(main())
