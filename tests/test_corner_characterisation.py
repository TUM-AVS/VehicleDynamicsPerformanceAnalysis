import unittest

import pandas as pd

from src.corner_characterisation import CornerCharacterisation


class CornerCharacterisationTest(unittest.TestCase):
    def setUp(self):
        self.characterisation = CornerCharacterisation(
            data=pd.DataFrame(),
            n_lap_channel="n_lap",
            s_coord_channel="s_m",
            curvature_channel="curvature",
            lat_accel_channel="ay_mps2",
            velocity_channel="v_mps",
            r_cos_phi_channel="r_cos_phi",
        )

    def phases_for(self, values):
        corner = pd.DataFrame({"r_cos_phi": values})
        return self.characterisation._create_corner_phase_channel(corner).tolist()

    def test_distinct_phase_starts(self):
        phases = self.phases_for([-1.0, -0.98, -0.8, -0.4, -0.2, 0.0, 0.2])

        self.assertEqual(
            phases,
            ["Braking", "Entry", "Entry", "Mid", "Mid", "Exit", "Exit"],
        )

    def test_two_coincident_phase_starts(self):
        phases = self.phases_for([-0.2, -0.1, 0.0, 0.2])

        self.assertEqual(phases, ["Mid", "Mid", "Exit", "Exit"])

    def test_three_coincident_phase_starts(self):
        phases = self.phases_for([0.2, 0.3])

        self.assertEqual(phases, ["Exit", "Exit"])


if __name__ == "__main__":
    unittest.main()
