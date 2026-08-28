"""Reusable trajectory loading, conversion, and satellite-map utilities.

The geographic conversion uses a vectorized local equirectangular tangent-plane
approximation on the WGS84 semi-major radius. It is appropriate for track-scale
distances around the supplied origin; use a geodesic/projection library for
large areas, high latitudes, or survey-grade positioning.
"""

from __future__ import annotations

import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


WGS84_RADIUS_M = 6_378_137.0
DEFAULT_X_COLUMN = "pos_x_m"
DEFAULT_Y_COLUMN = "pos_y_m"

SATELLITE_TILE_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
    "MapServer/tile/{z}/{y}/{x}"
)
SATELLITE_TILE_MAX_ZOOM = 19
SATELLITE_TILE_ATTRIBUTION = (
    "Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community"
)

NETWORK_PRIVACY_NOTICE = (
    "Map tiles are fetched over the network. The tile provider receives your "
    "IP address and tile coordinates, which approximate the viewed location."
)

DEFAULT_COLORS = (
    "#0065bd",
    "#e37222",
    "#34a853",
    "#a142f4",
    "#d93025",
    "#00a6a6",
)


@dataclass(frozen=True)
class TrajectorySpec:
    """Description of one CSV trajectory and its display adjustments."""

    path: Path
    label: Optional[str] = None
    color: Optional[str] = None
    x_offset_m: float = 0.0
    y_offset_m: float = 0.0


@dataclass(frozen=True)
class Trajectory:
    """A loaded trajectory with offsets already applied."""

    spec: TrajectorySpec
    x_m: np.ndarray
    y_m: np.ndarray

    @property
    def label(self) -> str:
        return self.spec.label or self.spec.path.stem or "Trajectory"


def parse_trajectory_spec(value: str) -> TrajectorySpec:
    """Parse ``PATH[,LABEL[,COLOR[,X_OFFSET[,Y_OFFSET]]]]``.

    CSV quoting is supported, so fields containing commas can be quoted. Empty
    label and color fields select the filename-derived label and default color.
    """
    try:
        fields = next(csv.reader([value], skipinitialspace=True))
    except csv.Error as exc:
        raise ValueError(f"Invalid trajectory specification: {exc}") from exc

    if not 1 <= len(fields) <= 5:
        raise ValueError(
            "Trajectory specification must contain path, optional label/color, "
            "and optional x/y offsets"
        )

    fields = [field.strip() for field in fields]
    if not fields[0]:
        raise ValueError("Trajectory path must not be empty")

    label = fields[1] if len(fields) >= 2 and fields[1] else None
    color = fields[2] if len(fields) >= 3 and fields[2] else None

    offsets = []
    for index, name in ((3, "x offset"), (4, "y offset")):
        text = fields[index] if len(fields) > index else ""
        try:
            offset = float(text) if text else 0.0
        except ValueError as exc:
            raise ValueError(f"Trajectory {name} must be a number: {text!r}") from exc
        if not math.isfinite(offset):
            raise ValueError(f"Trajectory {name} must be finite")
        offsets.append(offset)

    return TrajectorySpec(
        path=Path(fields[0]).expanduser(),
        label=label,
        color=color,
        x_offset_m=offsets[0],
        y_offset_m=offsets[1],
    )


def local_xy_to_latlon(
    x_m: object,
    y_m: object,
    origin_lat_deg: float,
    origin_lon_deg: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Convert local east/north metres to latitude/longitude arrays.

    ``x_m`` points east and ``y_m`` points north. Inputs are NumPy-broadcast,
    allowing scalars and array-like values without per-row Python loops.
    """
    if not math.isfinite(origin_lat_deg) or not -90.0 <= origin_lat_deg <= 90.0:
        raise ValueError("Origin latitude must be finite and between -90 and 90 degrees")
    if not math.isfinite(origin_lon_deg) or not -180.0 <= origin_lon_deg <= 180.0:
        raise ValueError("Origin longitude must be finite and between -180 and 180 degrees")

    cosine_latitude = math.cos(math.radians(origin_lat_deg))
    if abs(cosine_latitude) < 1e-12:
        raise ValueError("Local east/west conversion is undefined at the poles")

    try:
        x_values, y_values = np.broadcast_arrays(
            np.asarray(x_m, dtype=float), np.asarray(y_m, dtype=float)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Local coordinates must be numeric and broadcast-compatible") from exc

    if not np.isfinite(x_values).all() or not np.isfinite(y_values).all():
        raise ValueError("Local coordinates must contain only finite values")

    latitude = origin_lat_deg + np.degrees(y_values / WGS84_RADIUS_M)
    longitude = origin_lon_deg + np.degrees(
        x_values / (WGS84_RADIUS_M * cosine_latitude)
    )
    if np.any((longitude < -180.0) | (longitude > 180.0)):
        raise ValueError(
            "Trajectory crosses the antimeridian, which is not supported by the "
            "interactive map path renderer"
        )
    return latitude, longitude


def load_trajectory(
    spec: TrajectorySpec,
    x_column: str = DEFAULT_X_COLUMN,
    y_column: str = DEFAULT_Y_COLUMN,
) -> Trajectory:
    """Load and validate one trajectory CSV, applying its metre offsets."""
    if not x_column or not y_column:
        raise ValueError("Coordinate column names must not be empty")
    if not math.isfinite(spec.x_offset_m) or not math.isfinite(spec.y_offset_m):
        raise ValueError(f"Offsets for {spec.path} must be finite")

    try:
        frame = pd.read_csv(spec.path)
    except (OSError, pd.errors.ParserError, UnicodeError) as exc:
        raise ValueError(f"Could not read trajectory CSV {spec.path}: {exc}") from exc

    missing = [name for name in (x_column, y_column) if name not in frame.columns]
    if missing:
        raise ValueError(
            f"Trajectory CSV {spec.path} is missing coordinate column(s): "
            f"{', '.join(missing)}"
        )
    if frame.empty:
        raise ValueError(f"Trajectory CSV {spec.path} contains no data rows")

    try:
        x_values = pd.to_numeric(frame[x_column], errors="raise").to_numpy(
            dtype=float, copy=True
        )
        y_values = pd.to_numeric(frame[y_column], errors="raise").to_numpy(
            dtype=float, copy=True
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Coordinate columns in {spec.path} must contain only numeric values"
        ) from exc

    if not np.isfinite(x_values).all() or not np.isfinite(y_values).all():
        raise ValueError(f"Coordinate columns in {spec.path} must contain only finite values")

    return Trajectory(
        spec=spec,
        x_m=x_values + spec.x_offset_m,
        y_m=y_values + spec.y_offset_m,
    )


def load_trajectories(
    specs: Iterable[TrajectorySpec],
    x_column: str = DEFAULT_X_COLUMN,
    y_column: str = DEFAULT_Y_COLUMN,
) -> list[Trajectory]:
    """Load any positive number of trajectory specifications."""
    specs = list(specs)
    if not specs:
        raise ValueError("At least one trajectory is required")
    return [load_trajectory(spec, x_column, y_column) for spec in specs]


def _color_for(trajectory: Trajectory, index: int) -> str:
    return trajectory.spec.color or DEFAULT_COLORS[index % len(DEFAULT_COLORS)]


def _validate_loaded_trajectory(trajectory: Trajectory) -> None:
    if trajectory.x_m.shape != trajectory.y_m.shape:
        raise ValueError(f"Coordinate arrays for {trajectory.label} must have matching shapes")
    if trajectory.x_m.size == 0:
        raise ValueError(f"Trajectory {trajectory.label} contains no coordinates")
    if not np.isfinite(trajectory.x_m).all() or not np.isfinite(trajectory.y_m).all():
        raise ValueError(f"Trajectory {trajectory.label} contains non-finite coordinates")


def show_satellite_gui(
    trajectories: Sequence[Trajectory],
    origin_lat_deg: float,
    origin_lon_deg: float,
    title: str = "Trajectory Viewer",
    initial_zoom: int = 17,
    line_width: int = 6,
    window_size: Tuple[int, int] = (1280, 1000),
) -> None:
    """Open the Esri satellite-map GUI and block until its window closes.

    Tk and ``tkintermapview`` are intentionally imported and initialized only
    here. Tile use is subject to the selected provider's terms and privacy
    policy; the attribution and network notice are displayed in the GUI.
    """
    if not trajectories:
        raise ValueError("At least one trajectory is required")
    if not 0 <= initial_zoom <= SATELLITE_TILE_MAX_ZOOM:
        raise ValueError("Initial zoom must be between zero and max zoom")
    if line_width <= 0:
        raise ValueError("Line width must be positive")
    local_xy_to_latlon(0.0, 0.0, origin_lat_deg, origin_lon_deg)

    try:
        import tkinter as tk
    except ImportError as exc:
        raise RuntimeError(
            "The trajectory map GUI requires Python's tkinter support. "
            "Install the Tk package provided by your operating system."
        ) from exc

    try:
        import tkintermapview
    except ImportError as exc:
        raise RuntimeError(
            "The trajectory map GUI requires the optional 'tkintermapview' "
            "package. Install it with: pip install tkintermapview"
        ) from exc

    converted = []
    for trajectory in trajectories:
        _validate_loaded_trajectory(trajectory)
        latitude, longitude = local_xy_to_latlon(
            trajectory.x_m, trajectory.y_m, origin_lat_deg, origin_lon_deg
        )
        converted.append(list(zip(latitude.tolist(), longitude.tolist())))

    try:
        root = tk.Tk(className="TrajectoryViewer")
    except tk.TclError as exc:
        raise RuntimeError(
            "Could not initialize the Tk map GUI. Ensure a graphical display is available."
        ) from exc

    root.title(title)
    root.geometry(f"{window_size[0]}x{window_size[1]}")

    notice = tk.Label(
        root,
        text=f"{SATELLITE_TILE_ATTRIBUTION} | {NETWORK_PRIVACY_NOTICE}",
        anchor="w",
        padx=6,
        pady=3,
    )
    notice.pack(side="bottom", fill="x")

    map_widget = tkintermapview.TkinterMapView(root, corner_radius=0)
    map_widget.set_tile_server(SATELLITE_TILE_URL, max_zoom=SATELLITE_TILE_MAX_ZOOM)
    map_widget.pack(fill="both", expand=True)
    map_widget.set_position(origin_lat_deg, origin_lon_deg)
    map_widget.set_zoom(initial_zoom)

    legend = tk.Frame(map_widget, bg="#f0f0f0", borderwidth=1, relief="solid")
    legend.place(relx=0.99, rely=0.01, anchor="ne")
    tk.Label(
        legend,
        text="Trajectories",
        font=("Helvetica", 11, "bold"),
        bg="#f0f0f0",
    ).pack(padx=10, pady=(5, 2), anchor="w")

    for index, (trajectory, coordinates) in enumerate(zip(trajectories, converted)):
        color = _color_for(trajectory, index)
        if len(coordinates) == 1:
            map_widget.set_marker(*coordinates[0], text=trajectory.label)
        else:
            map_widget.set_path(coordinates, color=color, width=line_width)

        item = tk.Frame(legend, bg="#f0f0f0")
        item.pack(fill="x", padx=10, pady=(0, 4))
        tk.Label(item, bg=color, width=2, height=1, relief="sunken").pack(side="left")
        tk.Label(item, text=trajectory.label, bg="#f0f0f0", anchor="w").pack(
            side="left", padx=(5, 0)
        )

    print(f"{SATELLITE_TILE_ATTRIBUTION}\n{NETWORK_PRIVACY_NOTICE}", file=sys.stderr)
    root.mainloop()
