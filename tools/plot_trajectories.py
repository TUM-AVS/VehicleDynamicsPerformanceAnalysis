#!/usr/bin/env python3
"""Command-line entry point for the satellite trajectory viewer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.trajectory_plot import (  # noqa: E402
    load_trajectories,
    parse_trajectory_spec,
    show_satellite_gui,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plot one or more local-coordinate CSV trajectories over Esri "
            "World Imagery. The GUI fetches map tiles over the network."
        )
    )
    parser.add_argument(
        "-t",
        "--trajectory",
        action="append",
        required=True,
        metavar="PATH[,LABEL[,COLOR[,X_OFFSET[,Y_OFFSET]]]]",
        help="repeat for each trajectory; offsets are in metres",
    )
    parser.add_argument("--x-column", default="pos_x_m", help="local east coordinate column")
    parser.add_argument("--y-column", default="pos_y_m", help="local north coordinate column")
    parser.add_argument("--origin-lat", required=True, type=float, help="local origin latitude")
    parser.add_argument("--origin-lon", required=True, type=float, help="local origin longitude")
    parser.add_argument("--title", default="Trajectory Viewer")
    parser.add_argument("--line-width", type=int, default=6)
    parser.add_argument("--initial-zoom", type=int, default=17)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        specs = [parse_trajectory_spec(value) for value in args.trajectory]
        trajectories = load_trajectories(specs, args.x_column, args.y_column)
        show_satellite_gui(
            trajectories,
            args.origin_lat,
            args.origin_lon,
            title=args.title,
            initial_zoom=args.initial_zoom,
            line_width=args.line_width,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
