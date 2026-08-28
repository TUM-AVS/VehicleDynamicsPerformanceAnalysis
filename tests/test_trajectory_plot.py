import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.trajectory_plot import (
    WGS84_RADIUS_M,
    TrajectorySpec,
    load_trajectories,
    load_trajectory,
    local_xy_to_latlon,
    parse_trajectory_spec,
)
from tools.plot_trajectories import main as cli_main


class CoordinateConversionTests(unittest.TestCase):
    def test_origin_is_unchanged(self):
        latitude, longitude = local_xy_to_latlon(0.0, 0.0, 47.9, 8.7)

        self.assertAlmostEqual(float(latitude), 47.9)
        self.assertAlmostEqual(float(longitude), 8.7)

    def test_cardinal_offsets(self):
        origin_latitude = 47.9
        east_m = 25.0
        north_m = 40.0
        latitude, longitude = local_xy_to_latlon(
            np.array([east_m, 0.0]),
            np.array([0.0, north_m]),
            origin_latitude,
            8.7,
        )

        expected_east_degrees = np.degrees(
            east_m / (WGS84_RADIUS_M * np.cos(np.radians(origin_latitude)))
        )
        expected_north_degrees = np.degrees(north_m / WGS84_RADIUS_M)
        np.testing.assert_allclose(latitude, [origin_latitude, origin_latitude + expected_north_degrees])
        np.testing.assert_allclose(longitude, [8.7 + expected_east_degrees, 8.7])

    def test_rejects_antimeridian_crossing(self):
        with self.assertRaisesRegex(ValueError, "antimeridian"):
            local_xy_to_latlon(1_000.0, 0.0, 0.0, 179.999)


class InputSpecTests(unittest.TestCase):
    def test_parses_label_color_and_offsets(self):
        spec = parse_trajectory_spec("lap.csv,Reference,#0065bd,1.5,-0.5")

        self.assertEqual(spec.path, Path("lap.csv"))
        self.assertEqual(spec.label, "Reference")
        self.assertEqual(spec.color, "#0065bd")
        self.assertEqual(spec.x_offset_m, 1.5)
        self.assertEqual(spec.y_offset_m, -0.5)

    def test_allows_defaults_and_csv_quoted_fields(self):
        spec = parse_trajectory_spec('"lap, one.csv",,,,2')

        self.assertEqual(spec.path, Path("lap, one.csv"))
        self.assertIsNone(spec.label)
        self.assertIsNone(spec.color)
        self.assertEqual(spec.x_offset_m, 0.0)
        self.assertEqual(spec.y_offset_m, 2.0)

    def test_rejects_invalid_offset(self):
        with self.assertRaisesRegex(ValueError, "x offset"):
            parse_trajectory_spec("lap.csv,Reference,blue,sideways")


class TrajectoryLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_csv(self, name, data):
        path = self.directory / name
        pd.DataFrame(data).to_csv(path, index=False)
        return path

    def test_applies_offsets_without_changing_source_values(self):
        path = self.write_csv("lap.csv", {"pos_x_m": [1.0, 2.0], "pos_y_m": [3.0, 4.0]})
        trajectory = load_trajectory(
            TrajectorySpec(path, x_offset_m=1.5, y_offset_m=-0.5)
        )

        np.testing.assert_allclose(trajectory.x_m, [2.5, 3.5])
        np.testing.assert_allclose(trajectory.y_m, [2.5, 3.5])

    def test_loads_multiple_csv_files_with_column_overrides(self):
        first = self.write_csv("first.csv", {"east": [1.0], "north": [2.0]})
        second = self.write_csv("second.csv", {"east": [3.0], "north": [4.0]})

        trajectories = load_trajectories(
            [TrajectorySpec(first), TrajectorySpec(second)], "east", "north"
        )

        self.assertEqual(len(trajectories), 2)
        self.assertEqual([trajectory.label for trajectory in trajectories], ["first", "second"])
        self.assertEqual(float(trajectories[1].x_m[0]), 3.0)

    def test_rejects_missing_coordinate_column(self):
        path = self.write_csv("missing.csv", {"pos_x_m": [1.0]})

        with self.assertRaisesRegex(ValueError, "pos_y_m"):
            load_trajectory(TrajectorySpec(path))

    def test_rejects_non_finite_coordinates(self):
        path = self.write_csv("non_finite.csv", {"pos_x_m": [1.0, np.inf], "pos_y_m": [2.0, 3.0]})

        with self.assertRaisesRegex(ValueError, "finite"):
            load_trajectory(TrajectorySpec(path))

    def test_rejects_non_numeric_coordinates(self):
        path = self.write_csv("text.csv", {"pos_x_m": ["not-a-number"], "pos_y_m": [2.0]})

        with self.assertRaisesRegex(ValueError, "numeric"):
            load_trajectory(TrajectorySpec(path))

    def test_cli_opens_satellite_gui_with_user_origin(self):
        path = self.write_csv(
            "lap.csv",
            {"pos_x_m": [0.0, 1.0], "pos_y_m": [0.0, 2.0]},
        )

        with patch("tools.plot_trajectories.show_satellite_gui") as show_gui:
            result = cli_main(
                [
                    "--trajectory",
                    str(path),
                    "--origin-lat",
                    "47.9",
                    "--origin-lon",
                    "8.7",
                ]
            )

        self.assertEqual(result, 0)
        show_gui.assert_called_once()
        self.assertEqual(show_gui.call_args.args[1:3], (47.9, 8.7))


if __name__ == "__main__":
    unittest.main()
