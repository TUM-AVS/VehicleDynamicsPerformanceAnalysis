"""Command-line entry point for candump conversion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.can_decoder import CanDecoderError, convert_log


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decode a candump CAN/CAN FD log using a source JSON file."
    )
    parser.add_argument("input_log", type=Path, help="candump input log")
    parser.add_argument("definitions", type=Path, help="source definition JSON")
    parser.add_argument("output_csv", type=Path, help="destination CSV")
    parser.add_argument(
        "--format",
        "--csv-mode",
        dest="csv_mode",
        choices=("long", "wide"),
        default="long",
        help="CSV layout (default: long)",
    )
    parser.add_argument(
        "--filter-duplicates",
        action="store_true",
        help="suppress unchanged payloads for each interface and CAN ID",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="report the number of converted message events",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        event_count = convert_log(
            args.input_log,
            args.definitions,
            args.output_csv,
            mode=args.csv_mode,
            filter_duplicates=args.filter_duplicates,
        )
    except (CanDecoderError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    if args.verbose:
        print(f"Wrote {event_count} decoded message events to {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
