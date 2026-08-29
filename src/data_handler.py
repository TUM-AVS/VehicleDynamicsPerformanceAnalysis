"""Script containing the DataFile class and function for creating list of DataFile objects."""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import subprocess
import sys
import tkinter as tk
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tkinter import filedialog
from tkinter import messagebox

import numpy as np
import pandas as pd
import plotly.express as px
from sklearn.neighbors import KNeighborsRegressor

from .can_decoder import read_can_log
from .mcap_reader import read_mcap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")
) / "vehicle-dynamics-performance-analysis"
CACHE_FILE = CACHE_DIR / "last_data_path"

class DataFile:
    """Handles the data loading and writing as well as the unification of the data."""
    def __init__(
        self,
        file_path: Path,
        vehicle_config_path: Path | str | None = None,
        config_path: Path | str | None = None,
    ) -> None:
        """Class initialization."""
        self.data: pd.DataFrame = pd.DataFrame()
        self.read_success = False
        self.file_path = Path(file_path)
        self.file_name = self.file_path.name
        self.folder_path = self.file_path.parent
        self.lap_change_idx = [[0]]
        environment_config_path = os.environ.get("VDPA_CONFIG_DIR")
        if config_path is not None:
            selected_config_path = Path(config_path)
        elif environment_config_path:
            selected_config_path = Path(environment_config_path)
        else:
            selected_config_path = CONFIG_DIR
        self.config_path = selected_config_path.expanduser().resolve()
        self.parser_config = self.read_parser_config()
        self.topic_list = self.read_topic_list()
        self.filter_list = self.read_filter_list()
        self.parser_config_idx = None
        self.cornering_stiffness_estimation_enabled = False
        environment_path = os.environ.get("VDPA_VEHICLE_CONFIG_DIR")
        if vehicle_config_path is not None:
            config_path = Path(vehicle_config_path)
        elif environment_path:
            config_path = Path(environment_path)
        else:
            config_path = CONFIG_DIR / "vehicle_handler"
        self.veh_config_path = config_path.expanduser().resolve()
        self.vehicle_handler = None


    @staticmethod
    def process_mat_data(data_dict):
        """
        Filters for numeric signals, finds the lowest and highest timestamp, 
        creates a common time vector, and interpolates all signals to this common vector.

        Args:
            data_dict (dict): A dictionary where keys are signal names and values
                            are pandas Series with a DatetimeIndex.

        Returns:
            pandas.DataFrame: A new DataFrame with all signals interpolated to a
                            common 100 Hz time vector. The DataFrame includes a 
                            'time_s' column representing seconds from the start.
        """
        if not data_dict:
            return pd.DataFrame()

        # Filter out signals that do not have a numeric data type
        numeric_data_dict = {}
        for signal_name, signal_series in data_dict.items():
            if pd.api.types.is_numeric_dtype(signal_series):
                numeric_data_dict[signal_name] = signal_series
            else:
                logging.info("Skipping non-numeric signal: %s", signal_name)
        
        # If no numeric signals are left after filtering, return an empty DataFrame
        if not numeric_data_dict:
            logging.warning("No numeric signals found to process.")
            return pd.DataFrame()


        # Find the earliest and latest timestamps across all *numeric* signals
        min_timestamp = min(series.index.min() for series in numeric_data_dict.values())
        max_timestamp = max(series.index.max() for series in numeric_data_dict.values())

        # Calculate the time difference in seconds
        time_difference_seconds = (max_timestamp - min_timestamp).total_seconds()
        logging.info(
            "Time difference between first and last timestamp: %.2f seconds",
            time_difference_seconds,
        )

        # Construct a common time vector with a 100 Hz sampling rate
        sampling_rate = 100  # Hz
        # Add 1 to num_samples to ensure the last timestamp is included
        num_samples = int(np.ceil(time_difference_seconds * sampling_rate)) + 1
        common_time_vector = pd.to_timedelta(np.arange(num_samples) / sampling_rate, unit='s')
        
        # Create a new DatetimeIndex starting from the earliest timestamp
        common_datetime_index = min_timestamp + common_time_vector

        # Create a new DataFrame with the common DatetimeIndex
        interpolated_df = pd.DataFrame(index=common_datetime_index)

        # Interpolate each numeric signal to the common time vector
        for signal_name, signal_series in numeric_data_dict.items():
            # Combine the original and new index, sort, and then interpolate
            # Using .loc to avoid potential reindexing issues with duplicate indices
            signal_series = signal_series.loc[~signal_series.index.duplicated(keep='first')]
            combined_index = signal_series.index.union(common_datetime_index)
            reindexed_series = signal_series.reindex(combined_index)
            interpolated_series = reindexed_series.interpolate(method='time')
            
            # Select only the values corresponding to the common time vector
            interpolated_df[signal_name] = interpolated_series.loc[common_datetime_index]

        # Add time_s as a column (seconds from start) and reset the index
        interpolated_df['time_s'] = (interpolated_df.index - min_timestamp).total_seconds()
        
        # Reorder columns to have 'time_s' first
        cols = ['time_s'] + [col for col in interpolated_df.columns if col != 'time_s']
        interpolated_df = interpolated_df[cols]
        
        interpolated_df.reset_index(drop=True, inplace=True)

        return interpolated_df

    def read(self) -> None:
        logging.info(" Reading of data from data file %s", self.file_name)
        if self.file_name.endswith('.csv'):
            self.data = pd.read_csv(self.file_path, sep='[\t,]', engine='python')
            self.data = self.data.dropna(axis=1, how='all')
            self.read_success = True
        elif self.file_name.endswith('.mat'):
            try:
                import scipy.io

                mat_data = scipy.io.loadmat(self.file_path)
                # Filter out meta keys like '__header__', '__version__', '__globals__'
                signal_keys = [k for k in mat_data.keys() if not k.startswith('__')]
                
                all_signals_data = {}
                for key in signal_keys:
                    value = mat_data[key]
                    
                    # Ensure the value is a NumPy array with two columns
                    if isinstance(value, np.ndarray) and value.ndim == 2 and value.shape[1] == 2:
                        # The first column is time, and the second is the data value.
                        # The time values in your example appear to be Unix timestamps.
                        # We convert them to datetime objects and set them as the index.
                        time_stamps = pd.to_datetime(value[:, 0], unit='s')
                        all_signals_data[key] = pd.Series(value[:, 1], index=time_stamps, name=key)

                if all_signals_data:
                    # Combine all signals into a single DataFrame
                    self.data = self.process_mat_data(all_signals_data)
                    self.read_success = True
                else:
                    self.read_success = False
                    
            except Exception:
                logging.exception('Failed to read MAT file:')
                self.read_success = False
        elif self.file_name.endswith('.mcap'):
            try:
                self.data = read_mcap(self.file_path, self.topic_list)
                self.read_success = True
            except Exception:
                logging.exception('Failed to read MCAP file:')
                self.read_success = False
        elif self.file_name.endswith(('.log', '.candump')):
            try:
                definition_path = select_can_definition_file(self.folder_path)
                self.data = read_can_log(
                    self.file_path,
                    definition_path,
                )
                self.read_success = True
            except Exception:
                logging.exception('Failed to read CAN log:')
                self.read_success = False

        if self.read_success:
            logging.info('Read %s', self.file_name)
        else:
            logging.warning('Reading of %s not successful', self.file_name)

    
    def read_parser_config(self) -> pd.DataFrame:
        """Read the CSV file in the the config folder of this module."""
        return pd.read_csv(self.config_path / "parser_config.csv")
    
    def read_topic_list(self) -> list:
        """Read the CSV file in the the config folder of this module."""
        return pd.read_csv(
            self.config_path / "topic_list.csv", header=None
        ).iloc[:, 0].tolist()
    
    def read_filter_list(self) -> list:
        """Read the CSV file in the the config folder of this module."""
        return pd.read_csv(self.config_path / "filter_config.csv")

    def read_veh_config(self) -> list:
        """Return valid vehicle configuration directories in deterministic order."""
        if not self.veh_config_path.is_dir():
            raise FileNotFoundError(
                f"Vehicle configuration directory does not exist: {self.veh_config_path}"
            )

        required_files = ("vehicle_config.yaml", "tire_config.yaml")
        car_config_list = sorted(
            path.name
            for path in self.veh_config_path.iterdir()
            if path.is_dir() and all((path / name).is_file() for name in required_files)
        )
        if not car_config_list:
            raise RuntimeError(
                f"No valid vehicle configurations found in {self.veh_config_path}. "
                "Each vehicle directory must contain vehicle_config.yaml and tire_config.yaml."
            )
        return car_config_list
    
    def select_vehicle(self) -> None:
        """
        Function to select car config from drop down menu.

        """
        import tum_types_py  # Registers vehicle parameter types with pybind11.
        import vehicle_handler_py as vh

        veh_list = self.read_veh_config()
        question = f'With which car was the log {self.file_name} acquired?'
        (
            self.veh_config_name,
            self.cornering_stiffness_estimation_enabled,
        ) = drop_down_gui(
            question,
            veh_list,
            checkbox_label='Estimate instantaneous cornering stiffness',
        )
        logging.info(' Selected Car Configuration: %s', self.veh_config_name)
        self.vehicle_handler = vh.VehicleHandler.from_path(
            str(self.veh_config_path), self.veh_config_name
        )
        return 
    
    def get_vehicle_handler(self):
        return self.vehicle_handler
    
    def check_signals(self, required_columns: list[str], forbidden_columns: list[str]) -> (bool, str):
        """
        Checks if required columns are present and forbidden columns are absent in the dataframe.
        Returns a tuple with a boolean indicating success and a string with details of missing or existing columns.
        
        :param data: DataFrame containing the data to check
        :param required_columns: List of columns that must be present
        :param forbidden_columns: List of columns that must not be present
        :return: Tuple (bool, str) where bool indicates if conditions are met, and str contains details if not
        """
        missing_required = [elem for elem in required_columns if elem not in self.data.columns]
        existing_forbidden = [elem for elem in forbidden_columns if elem in self.data.columns]
        
        if not missing_required and not existing_forbidden:
            return True, ""
        
        issues = []
        if missing_required:
            issues.append(f"missing required columns: {', '.join(missing_required)}")
        if existing_forbidden:
            issues.append(f"existing forbidden columns: {', '.join(existing_forbidden)}")
        
        return False, '; '.join(issues)
    
    def calc_signals(self) -> None:
        # Postprocessing parameter
        v_min = 3  # m/s

        # # Add lap identifier channel
        # lap_flags = np.zeros(len(self.data))
        # lap_flags[self.lap_change_idx] = 1
        # n_lap = lap_flags.cumsum().astype(int)
        # self.data['n_lap'] = n_lap

        # Calculate the average sample frequency
        check_list = ['time_s']
        check_not_list = []
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.sample_frequency = 1 / np.average(np.diff(self.data['time_s']))
                logging.debug('Average Sample frequency: %s Hz', self.sample_frequency)
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calcualate sampling frequency because of data column(s): ({issues})')

        #Convert lat/long to x/y via origin coordinates from user input
        check_list = ['pos_lat', 'pos_long']
        check_not_list = ['pos_x_m', 'pos_y_m']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            root = None
            try:
                # --- open Tkinter dialog ---
                import tkinter.simpledialog as simpledialog
                root = tk.Tk()
                root.withdraw()  # hide main window

                origin_text = simpledialog.askstring(
                    title="Set Geo Origin",
                    prompt="Enter origin as: lat, lon",
                    parent=root,
                )

                if origin_text is None:
                    raise RuntimeError("Origin input cancelled by user")

                # --- parse input ---
                try:
                    lat0_str, lon0_str = origin_text.split(',')
                    lat0 = float(lat0_str.strip())
                    lon0 = float(lon0_str.strip())
                except Exception:
                    raise ValueError("Invalid format. Use: lat, lon")

                # --- load data as numpy arrays ---
                lat = np.asarray(self.data['pos_lat'], dtype=float)
                lon = np.asarray(self.data['pos_long'], dtype=float)

                # --- vectorized conversion ---
                R = 6378137.0  # Earth radius [m] (WGS84)

                lat_rad = np.deg2rad(lat)
                lon_rad = np.deg2rad(lon)
                lat0_rad = np.deg2rad(lat0)
                lon0_rad = np.deg2rad(lon0)

                dlat = lat_rad - lat0_rad
                dlon = lon_rad - lon0_rad

                x = R * dlon * np.cos(lat0_rad)   # East
                y = R * dlat                      # North

                # --- store results ---
                self.data['pos_x_m'] = x
                self.data['pos_y_m'] = y
            except Exception:
                logging.exception(
                    'Failed to convert pos_lat and pos_long to Cartesian coordinates'
                )
            finally:
                if root is not None:
                    root.destroy()
        else:
            logging.info(f'Did not calcualate pos_x_m, pos_y_m because of data column(s): ({issues})')        

        # Calc s coordinate from positions if no distance channel is available
        check_list = ['time_s', 'pos_x_m', 'pos_y_m']
        check_not_list = ['s_m', 's_norm_m']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                x_values = self.data['pos_x_m'].to_numpy(dtype=float)
                y_values = self.data['pos_y_m'].to_numpy(dtype=float)
                time_values = self.data['time_s'].to_numpy(dtype=float)
                line_start, line_end = select_start_finish_line(
                    x_values,
                    y_values,
                    self.file_name,
                )

                positions = np.column_stack((x_values, y_values))
                movement = positions[1:] - positions[:-1]
                line_vector = line_end - line_start
                if np.linalg.norm(line_vector) <= 1e-9:
                    raise ValueError('Start/finish line endpoints must be distinct')

                offset = line_start - positions[:-1]
                denominator = (
                    movement[:, 0] * line_vector[1]
                    - movement[:, 1] * line_vector[0]
                )
                valid = np.abs(denominator) > 1e-12
                path_fraction = np.full(len(movement), np.nan)
                line_fraction = np.full(len(movement), np.nan)
                path_fraction[valid] = (
                    offset[valid, 0] * line_vector[1]
                    - offset[valid, 1] * line_vector[0]
                ) / denominator[valid]
                line_fraction[valid] = (
                    offset[valid, 0] * movement[valid, 1]
                    - offset[valid, 1] * movement[valid, 0]
                ) / denominator[valid]
                intersects = (
                    valid
                    & (path_fraction >= 0)
                    & (path_fraction <= 1)
                    & (line_fraction >= 0)
                    & (line_fraction <= 1)
                )
                candidates = np.flatnonzero(intersects) + 1

                directional_crossings = []
                for direction in (-1, 1):
                    directed = candidates[
                        np.sign(denominator[candidates - 1]).astype(int)
                        == direction
                    ]
                    debounced = []
                    for index in directed:
                        if (
                            not debounced
                            or time_values[index] - time_values[debounced[-1]] >= 5
                        ):
                            debounced.append(int(index))
                    directional_crossings.append(np.asarray(debounced, dtype=int))

                crossings = max(directional_crossings, key=len)
                if len(crossings) < 2:
                    raise ValueError(
                        'The selected start/finish line must have at least two '
                        'crossings in one travel direction'
                    )

                step_distance = np.hypot(np.diff(x_values), np.diff(y_values))
                cumulative_distance = np.concatenate(
                    (np.array([0.0]), np.cumsum(step_distance))
                )
                reset_origins = np.zeros(len(cumulative_distance))
                reset_origins[crossings] = cumulative_distance[crossings]
                self.data['s_m'] = (
                    cumulative_distance - np.maximum.accumulate(reset_origins)
                )
                logging.info(
                    'Calculated s_m using %d start/finish crossings',
                    len(crossings),
                )
            except Exception:
                logging.exception(
                    'Failed to calculate s_m from Cartesian coordinates'
                )
        else:
            logging.info(f'Did not calculate s_m because of data column(s): ({issues})')

        # Calc vy if missing ad beta is available
        check_list = ['vx_mps', 'beta_rad']
        check_not_list = ['vy_mps']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['vy_mps'] = self.data['vx_mps'] * np.tan(self.data['beta_rad'])
                logging.info('Calculated vy')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calcualate vy because of data column(s): ({issues})')

        # Calc total speed
        check_list = ['vx_mps', 'vy_mps']
        check_not_list = ['v_mps']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['v_mps'] = np.sqrt(self.data['vx_mps'] ** 2 + self.data['vy_mps'] ** 2)
                logging.info('Calculated total speed')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calcualate total speed because of data column(s): ({issues})')

        # Calc beta
        check_list = ['vx_mps', 'vy_mps']
        check_not_list = ['beta_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['beta_rad'] = np.where(
                    self.data['vx_mps'] >= v_min,
                    np.arctan2(self.data['vy_mps'], self.data['vx_mps']),
                    0
                )
                logging.info('Calculated beta')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate beta because of data column(s): ({issues})')
        
        # Calc beta
        check_list = ['ax_mps2', 'ay_mps2']
        check_not_list = ['a_total_mps2']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['a_total_mps2'] = np.sqrt(self.data['ax_mps2']**2 + self.data['ay_mps2'] **2)
                logging.info('Calculated total_acceleration')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate total_acceleration because of data column(s): ({issues})')

        # Calc Radius
        check_list = ['v_mps', 'ay_mps2', 'beta_rad']
        check_not_list = ['radius_m']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                max_radius = 10000
                # Radius by speed and lat acceleration corrected with beta
                self.data['radius_m'] = np.where(
                (self.data['v_mps'] >= v_min) & (self.data['ay_mps2'] != 0),
                (self.data['v_mps'] ** 2) / (self.data['ay_mps2'] * np.cos(self.data['beta_rad'])),
                0  
                )
                # Clamp the radius
                self.data['radius_m'] = np.clip(self.data['radius_m'], -max_radius, max_radius)
                logging.info('Calculated radius_m')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate radius_m because of data column(s): ({issues})')

        # Calc curvature
        check_list = ['radius_m']
        check_not_list = ['curvature', 'curvature_smoothed']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['curvature'] = np.where(self.data['radius_m'] != 0, 1 / self.data['radius_m'], 0)
                self.data['curvature_smoothed'] = self.data['curvature'].rolling(
                    window=30, center=True, min_periods=1
                ).mean()
                logging.info('Calculated curvature')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate curvature because of data column(s): ({issues})')
        
        # Calc slip angles
        check_list = ['vx_mps', 'vy_mps', 'yaw_rate_radps', 'delta_f_rad']
        check_not_list=['alpha_fl_rad', 'alpha_fr_rad', 'alpha_rl_rad', 'alpha_rr_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                slip_angles = self.vehicle_handler.calc_slip_angles_vectorized(
                    np.array(self.data['vx_mps'], dtype=np.float64),
                    np.array(self.data['vy_mps'], dtype=np.float64),
                    np.array(self.data['yaw_rate_radps'], dtype=np.float64),
                    np.array(self.data['delta_f_rad'], dtype=np.float64)
                )
                self.data['alpha_fl_rad'] = np.where(self.data['vx_mps'] >= v_min, slip_angles[0], 0)
                self.data['alpha_fr_rad'] = np.where(self.data['vx_mps'] >= v_min, slip_angles[1], 0)
                self.data['alpha_rl_rad'] = np.where(self.data['vx_mps'] >= v_min, slip_angles[2], 0)
                self.data['alpha_rr_rad'] = np.where(self.data['vx_mps'] >= v_min, slip_angles[3], 0)
                logging.info(' Calculated slip angles')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate slip angles because of data column(s): ({issues})')

        # Average axle slip angles
        check_list = ['alpha_fl_rad', 'alpha_fr_rad', 'alpha_rl_rad', 'alpha_rr_rad']
        check_not_list = ['alpha_f_rad', 'alpha_r_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['alpha_f_rad'] = (self.data['alpha_fl_rad'] + self.data['alpha_fr_rad']) / 2
                self.data['alpha_r_rad'] = (self.data['alpha_rl_rad'] + self.data['alpha_rr_rad']) / 2
                logging.info('Calculated average slip angles')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate average slip angles because of data column(s): ({issues})')

        # Calc slip ratio by wheel rotating speed
        check_list = ['vx_mps', 'vy_mps', 'yaw_rate_radps', 'delta_f_rad', 'whl_fl_radps', 'whl_fr_radps', 'whl_rl_radps', 'whl_rr_radps']
        check_not_list = ['whl_slip_fl', 'whl_slip_fr', 'whl_slip_rl', 'whl_slip_rr']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            slip_ratio = self.vehicle_handler.calc_long_slip_vectorized(
                np.array(self.data['vx_mps'], dtype=np.float64),
                np.array(self.data['vy_mps'], dtype=np.float64),
                np.array(self.data['yaw_rate_radps'], dtype=np.float64),
                np.array(self.data['delta_f_rad'], dtype=np.float64),
                np.maximum(v_min, np.array(self.data['whl_fl_radps'], dtype=np.float64)),
                np.maximum(v_min, np.array(self.data['whl_fr_radps'], dtype=np.float64)),
                np.maximum(v_min, np.array(self.data['whl_rl_radps'], dtype=np.float64)),
                np.maximum(v_min, np.array(self.data['whl_rr_radps'], dtype=np.float64))
            )
            self.data['whl_slip_fl'] = np.where(self.data['vx_mps'] >= v_min, slip_ratio[0], 0) 
            self.data['whl_slip_fr'] = np.where(self.data['vx_mps'] >= v_min, slip_ratio[1], 0)
            self.data['whl_slip_rl'] = np.where(self.data['vx_mps'] >= v_min, slip_ratio[2], 0)
            self.data['whl_slip_rr'] = np.where(self.data['vx_mps'] >= v_min, slip_ratio[3], 0)
            logging.info(' Calculated slip ratio')
        else:
            logging.info(f'Did not calculate slip ratio by rotating wheel speed because of data column(s): ({issues})')

        # Calc slip ratio by wheel speed over ground
        check_list = ['vx_mps', 'whl_fl_mps', 'whl_fr_mps', 'whl_rl_mps', 'whl_rr_mps']
        check_not_list = ['whl_slip_fl', 'whl_slip_fr', 'whl_slip_rl', 'whl_slip_rr']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['whl_slip_fl'] = np.clip(np.where(self.data['vx_mps'] >= v_min, (self.data['whl_fl_mps'] - self.data['vx_mps']) / self.data['vx_mps'],0), -1.0, 1.0)
                self.data['whl_slip_fr'] = np.clip(np.where(self.data['vx_mps'] >= v_min, (self.data['whl_fr_mps'] - self.data['vx_mps']) / self.data['vx_mps'],0), -1.0, 1.0)
                self.data['whl_slip_rl'] = np.clip(np.where(self.data['vx_mps'] >= v_min, (self.data['whl_rl_mps'] - self.data['vx_mps']) / self.data['vx_mps'],0), -1.0, 1.0)
                self.data['whl_slip_rr'] = np.clip(np.where(self.data['vx_mps'] >= v_min, (self.data['whl_rr_mps'] - self.data['vx_mps']) / self.data['vx_mps'],0), -1.0, 1.0)
                logging.info(' Calculated slip ratio')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate slip ratio by wheel speed over ground because of data column(s): ({issues})')

        # Calc combinded slip 
        check_list = ['whl_slip_fl', 'whl_slip_fr', 'whl_slip_rl', 'whl_slip_rr', 'alpha_fl_rad', 'alpha_fr_rad', 'alpha_rl_rad', 'alpha_rr_rad']
        check_not_list = ['whl_slip_combined_fl', 'whl_slip_combined_fr', 'whl_slip_combined_rl', 'whl_slip_combined_rr']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['whl_slip_combined_fl'] = np.sqrt(np.tan(self.data['whl_slip_fl'])**2 + self.data['alpha_fl_rad']**2)
                self.data['whl_slip_combined_fr'] = np.sqrt(np.tan(self.data['whl_slip_fr'])**2 + self.data['alpha_fr_rad']**2)
                self.data['whl_slip_combined_rl'] = np.sqrt(np.tan(self.data['whl_slip_rl'])**2 + self.data['alpha_rl_rad']**2)
                self.data['whl_slip_combined_rr'] = np.sqrt(np.tan(self.data['whl_slip_rr'])**2 + self.data['alpha_rr_rad']**2)
                logging.info(' Calculated combined slip')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate combined slip because of data column(s): ({issues})')

        # Calc longitudinal slip velcoity
        check_list = ['v_mps', 'whl_slip_fl', 'whl_slip_fr', 'whl_slip_rl', 'whl_slip_rr']
        check_not_list = ['long_slip_velocity_fl', 'long_slip_velocity_fr', 'long_slip_velocity_rl', 'long_slip_velocity_rr']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['long_slip_velocity_fl'] = self.data['whl_slip_fl'] * self.data['v_mps']
                self.data['long_slip_velocity_fr'] = self.data['whl_slip_fr'] * self.data['v_mps']
                self.data['long_slip_velocity_rl'] = self.data['whl_slip_rl'] * self.data['v_mps']
                self.data['long_slip_velocity_rr'] = self.data['whl_slip_rr'] * self.data['v_mps']
                logging.info('Calculated longitudinal slip velocities for all wheels')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Did not calculate longitudinal slip velocities because of data column(s): ({issues})')

        # Calc lat slip velocity
        check_list = ['v_mps', 'alpha_fl_rad', 'alpha_fr_rad', 'alpha_rl_rad', 'alpha_rr_rad']
        check_not_list = ['lat_slip_velocity_fl', 'lat_slip_velocity_fr', 'lat_slip_velocity_rl', 'lat_slip_velocity_rr']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['lat_slip_velocity_fl'] = self.data['v_mps'] * np.tan(self.data['alpha_fl_rad'])
                self.data['lat_slip_velocity_fr'] = self.data['v_mps'] * np.tan(self.data['alpha_fr_rad'])
                self.data['lat_slip_velocity_rl'] = self.data['v_mps'] * np.tan(self.data['alpha_rl_rad'])
                self.data['lat_slip_velocity_rr'] = self.data['v_mps'] * np.tan(self.data['alpha_rr_rad'])
                logging.info('Calculated lateral slip velocities for all wheels')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Did not calculate lateral slip velocities because of data column(s): ({issues})')

        # Calc combined slip velocity
        check_list = [
            'whl_slip_combined_fl',
            'whl_slip_combined_fr',
            'whl_slip_combined_rl',
            'whl_slip_combined_rr',
            'v_mps'
        ]
        check_not_list = [
            'combined_slip_velocity_fl',
            'combined_slip_velocity_fr',
            'combined_slip_velocity_rl',
            'combined_slip_velocity_rr'
        ]
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['combined_slip_velocity_fl'] = self.data['whl_slip_combined_fl'] * self.data['v_mps']
                self.data['combined_slip_velocity_fr'] = self.data['whl_slip_combined_fr'] * self.data['v_mps']
                self.data['combined_slip_velocity_rl'] = self.data['whl_slip_combined_rl'] * self.data['v_mps']
                self.data['combined_slip_velocity_rr'] = self.data['whl_slip_combined_rr'] * self.data['v_mps']
                logging.info('Calculated combined slip velocities for all wheels using combined slip signal')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Did not calculate combined slip velocity because of data column(s): ({issues})')

        # Calc estimated wheel radius with zero slip assumption and vehicle speed as reference
        check_list = ['whl_fl_radps', 'whl_fr_radps', 'whl_rl_radps', 'whl_rr_radps', 'v_mps']
        check_not_list = ['whl_fl_radius_m', 'whl_fr_radius_m', 'whl_rl_radius_m', 'whl_rr_radius_m']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['whl_fl_radius_m'] = np.where(self.data['v_mps'] >= v_min, self.data['v_mps'] / self.data['whl_fl_radps'], 0)
                self.data['whl_fr_radius_m'] = np.where(self.data['v_mps'] >= v_min, self.data['v_mps'] / self.data['whl_fr_radps'], 0)
                self.data['whl_rl_radius_m'] = np.where(self.data['v_mps'] >= v_min, self.data['v_mps'] / self.data['whl_rl_radps'], 0)
                self.data['whl_rr_radius_m'] = np.where(self.data['v_mps'] >= v_min, self.data['v_mps'] / self.data['whl_rr_radps'], 0)
                self.data['whl_f_radius_m'] = (self.data['whl_fl_radius_m'] + self.data['whl_fr_radius_m']) / 2
                self.data['whl_r_radius_m'] = (self.data['whl_rl_radius_m'] + self.data['whl_rr_radius_m']) / 2
                logging.info(' Calculated wheel radius')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate wheel radius because of data column(s): ({issues})')

        # Calc alpha diff
        check_list = ['alpha_f_rad', 'alpha_r_rad', 'curvature_smoothed']
        check_not_list = ['alpha_diff_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['alpha_diff_rad'] = (self.data['alpha_f_rad'] - self.data['alpha_r_rad']) * np.sign(self.data['curvature_smoothed'])
                logging.info(' Calculated alpha diff')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate alpha diff because of data column(s): ({issues})')

        # Calc understeer gradient as in "Rennwagentechnik-Praxislehrgang"
        check_list = ['ay_mps2', 'delta_f_rad', 'v_mps']
        check_not_list = ['understeer_gradient']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                ay_min_understeer = 1.0  # m/s^2
                valid = (
                    (self.data['v_mps'] > v_min)
                    & (self.data['ay_mps2'].abs() >= ay_min_understeer)
                )
                self.data['understeer_gradient'] = np.nan
                self.data.loc[valid, 'understeer_gradient'] = (
                    self.data.loc[valid, 'delta_f_rad'] / self.data.loc[valid, 'ay_mps2']
                    - self.vehicle_handler.vehicle().dimension.wheelbase
                    / self.data.loc[valid, 'v_mps'] ** 2
                )
                logging.info(' Calculated understeer gradient')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate understeer gradient because of data column(s): ({issues})')

        # Calc excess steering angle
        """
        Excess Steering Angle as defined in the Kegelman Thesis.

        Adjusted to different sign convention
        ours: counterclockwise is positive 
        Kegelman: other way round
        """
        check_list = ['curvature_smoothed']
        check_not_list = ['delta_f_kinematic_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['delta_f_kinematic_rad'] = self.vehicle_handler.vehicle().dimension.wheelbase * self.data['curvature_smoothed']
                logging.info(' Calculated kinematic steering reference')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate kinematic steering reference because of data column(s): ({issues})')

        check_list = ['delta_f_rad', 'delta_f_kinematic_rad', 'curvature_smoothed']
        check_not_list = ['excess_steering_angle_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['excess_steering_angle_rad'] = (self.data['delta_f_rad'] - self.data['delta_f_kinematic_rad']) * np.sign(self.data['curvature_smoothed'])
                logging.info(' Calculated excess steering angle')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate excess steering angle because of data column(s): ({issues})')

        # Calc yaw rate difference from the neutral-steer kinematic reference
        check_list = ['delta_f_rad', 'v_mps']
        check_not_list = ['yaw_rate_kinematic_radps']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                wheelbase = self.vehicle_handler.vehicle().dimension.wheelbase
                self.data['yaw_rate_kinematic_radps'] = np.where(
                    self.data['v_mps'] > v_min,
                    self.data['v_mps'] * self.data['delta_f_rad'] / wheelbase,
                    0,
                )
                logging.info(' Calculated kinematic yaw-rate reference')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate kinematic yaw-rate reference because of data column(s): ({issues})')

        check_list = ['yaw_rate_kinematic_radps', 'yaw_rate_radps', 'curvature_smoothed']
        check_not_list = ['yaw_rate_diff_kinematic_radps']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['yaw_rate_diff_kinematic_radps'] = (
                    self.data['yaw_rate_kinematic_radps'] - self.data['yaw_rate_radps']
                ) * np.sign(self.data['curvature_smoothed'])
                logging.info(' Calculated yaw rate difference from kinematic reference')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate yaw rate difference from kinematic reference because of data column(s): ({issues})')

        # Calc rCosPhi
        check_list = ['ax_mps2', 'ay_mps2']
        check_not_list = ['r_cos_phi']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['r_cos_phi'] = self.data['ax_mps2'] / np.hypot(self.data['ax_mps2'], self.data['ay_mps2'])
                logging.info(' Calculated rCosPhi')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate rCosPhi because of data column(s): ({issues})')

        # Calc yaw acceleration
        check_list = ['yaw_rate_radps', 'time_s']
        check_not_list = ['yaw_accel_radps2']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['yaw_accel_radps2'] = np.gradient(self.data['yaw_rate_radps'], self.data['time_s'].diff().median())
                logging.info(' Calculated yaw acceleration')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate yaw acceleration because of data column(s): ({issues})')

        # Calculate lateral forces
        check_list = ['ay_mps2', 'yaw_accel_radps2']
        check_not_list = ['fy_f_N', 'fy_r_N', 'fy_N']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                weight_dist = (self.vehicle_handler.vehicle().dimension.wheelbase - self.vehicle_handler.vehicle().dimension.distance_to_front_axle) / self.vehicle_handler.vehicle().dimension.wheelbase
                self.data['fy_f_N'] = self.vehicle_handler.vehicle().mass.total * self.data['ay_mps2'] * weight_dist + self.vehicle_handler.vehicle().inertia.yaw * self.data['yaw_accel_radps2'] * (1/self.vehicle_handler.vehicle().dimension.wheelbase)
                self.data['fy_r_N'] = self.vehicle_handler.vehicle().mass.total * self.data['ay_mps2'] - self.data['fy_f_N']
                self.data['fy_N'] = self.data['fy_f_N'] + self.data['fy_r_N']
                logging.info(' Calculated lateral forces')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate lateral forces because of data column(s): ({issues})')

        # Estimate Vertical Force per Axle and per Tire
        check_list = ['v_mps', 'ax_mps2', 'ay_mps2']  # Required input signals from self.data
        check_not_list = ['fz_f_N', 'fz_r_N', 'fz_fl_N', 'fz_fr_N', 'fz_rl_N', 'fz_rr_N']    # Signals to be calculated

        # Check for data signals
        conditions_met, issues = self.check_signals(check_list, check_not_list)

        if conditions_met:
            try:
                # Vehicle parameters
                g = 9.81  # m/s^2
                vehicle_params = self.vehicle_handler.vehicle()  # Get vehicle object

                m_total = vehicle_params.mass.total  # [kg]
                wb = vehicle_params.dimension.wheelbase  # [m]
                # l_f_cog is distance from front axle to CoG
                l_f_cog = vehicle_params.dimension.distance_to_front_axle  # [m]
                # l_r_cog is distance from CoG to rear axle
                l_r_cog = wb - l_f_cog  # [m]
                h_cog = vehicle_params.dimension.cog_height  # [m]

                rho = vehicle_params.aero.air_density  # [kg/m^3]
                cl = vehicle_params.aero.lift_coeff  # [-]
                a_aero = vehicle_params.aero.cross_track_area  # [m^2]
                x_cp_cog = vehicle_params.aero.diff_cog_x  # [m]

                # --- 1. Static load distribution (positive downwards) ---
                if abs(wb) < 1e-6:
                    logging.error("Wheelbase is zero, cannot calculate static loads.")
                    fz_static_f_N = np.full_like(self.data['v_mps'], np.nan)
                    fz_static_r_N = np.full_like(self.data['v_mps'], np.nan)
                else:
                    fz_static_f_N = m_total * g * l_r_cog / wb
                    fz_static_r_N = m_total * g * l_f_cog / wb

                # --- 2. Aerodynamic load component (positive for downforce) ---
                v_mps_sq = self.data['v_mps']**2
                fz_aero_total_N = -0.5 * rho * v_mps_sq * a_aero * cl

                if abs(wb) < 1e-6:
                    logging.error("Wheelbase is zero, cannot distribute aero loads.")
                    dfz_aero_f_N = np.full_like(self.data['v_mps'], np.nan)
                    dfz_aero_r_N = np.full_like(self.data['v_mps'], np.nan)
                else:
                    dfz_aero_f_N = fz_aero_total_N * (l_r_cog - x_cp_cog) / wb
                    dfz_aero_r_N = fz_aero_total_N * (l_f_cog + x_cp_cog) / wb

                # --- 3. Longitudinal load transfer component ---
                if abs(wb) < 1e-6:
                    logging.error("Wheelbase is zero, cannot calculate longitudinal load transfer.")
                    dfz_long_transfer_N = np.full_like(self.data['v_mps'], np.nan)
                else:
                    dfz_long_transfer_N = m_total * self.data['ax_mps2'] * h_cog / wb

                # --- Total estimated vertical loads per axle (positive downwards) ---
                self.data['fz_f_N'] = fz_static_f_N + dfz_aero_f_N - dfz_long_transfer_N
                self.data['fz_r_N'] = fz_static_r_N + dfz_aero_r_N + dfz_long_transfer_N
                
                logging.info('Calculated estimated vertical axle loads (fz_f_N, fz_r_N)')

                # --- 4. Estimate Vertical Force per Tire ---
                # Assume vehicle_params.dimension provides front and rear track widths

                front_track = vehicle_params.dimension.track_width_front  # [m]
                rear_track = vehicle_params.dimension.track_width_rear    # [m]

                if front_track is None or rear_track is None or front_track < 1e-6 or rear_track < 1e-6:
                    logging.error("Invalid track width values, cannot calculate tire loads.")
                    self.data['fz_fl_N'] = np.full_like(self.data['v_mps'], np.nan)
                    self.data['fz_fr_N'] = np.full_like(self.data['v_mps'], np.nan)
                    self.data['fz_rl_N'] = np.full_like(self.data['v_mps'], np.nan)
                    self.data['fz_rr_N'] = np.full_like(self.data['v_mps'], np.nan)
                else:
                    # Lateral load transfer for each axle due to lateral acceleration
                    lateral_transfer_front = m_total * self.data['ay_mps2'] * h_cog / front_track
                    lateral_transfer_rear = m_total * self.data['ay_mps2'] * h_cog / rear_track

                    # Distribute the axle load to left and right tires:
                    # Left tire load = half of axle load minus half of lateral load transfer
                    # Right tire load = half of axle load plus half of lateral load transfer
                    self.data['fz_fl_N'] = self.data['fz_f_N'] / 2 - lateral_transfer_front / 2
                    self.data['fz_fr_N'] = self.data['fz_f_N'] / 2 + lateral_transfer_front / 2
                    self.data['fz_rl_N'] = self.data['fz_r_N'] / 2 - lateral_transfer_rear / 2
                    self.data['fz_rr_N'] = self.data['fz_r_N'] / 2 + lateral_transfer_rear / 2

                    logging.info('Calculated estimated vertical tire loads (fz_fl_tire, fz_fr_tire, fz_rl_tire, fz_rr_tire)')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Did not calculate vertical loads because of data column(s): ({issues})')

        # Calculate normalized lateral forces by vertical forces
        check_list = ['fy_f_N', 'fz_f_N', 'fy_r_N', 'fz_r_N']
        check_not_list = ['fy_f_norm_N', 'fy_r_norm_N', 'fy_norm_N']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['fy_f_norm_N'] = self.data['fy_f_N'] / self.data['fz_f_N']
                self.data['fy_r_norm_N'] = self.data['fy_r_N'] / self.data['fz_r_N']
                self.data['fy_norm_N'] = (self.data['fy_f_N'] + self.data['fy_r_N']) / (self.data['fz_f_N'] + self.data['fz_r_N'])
                logging.info('Calculated normalized lateral forces: fy_f_norm_N, fy_r_norm_N, and combined fy_norm_N')
            except Exception as e:
                logging.error('Error calculating normalized lateral forces: %s', e)
        else:
            logging.info(f"Did not calculate normalized lateral forces because of data column(s): ({issues})")

        # Calculate effective mue
        check_list = ['a_total_mps2', 'fz_f_N', 'fz_r_N']
        check_not_list = ['mue_effective']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                total_vertical_load = self.data['fz_f_N'] + self.data['fz_r_N']
                vehicle_mass = self.vehicle_handler.vehicle().mass.total
                self.data['mue_effective'] = np.where(
                    total_vertical_load != 0,
                    (vehicle_mass * self.data['a_total_mps2']) / total_vertical_load,
                    0
                )
                logging.info('Calculated mue_effective')
            except Exception as e:
                logging.error('Error calculating mue_effective: %s', e)
        else:
            logging.info('Did not calculate mue_effective because of data column(s): (%s)', issues)

        # Calculate Weighted Slip Angles ---
        check_list = [
            'fz_f_N', 'fz_r_N', 'fz_fl_N', 'fz_fr_N',
            'alpha_fl_rad', 'alpha_fr_rad', 'fz_rl_N', 'fz_rr_N',
            'alpha_rl_rad', 'alpha_rr_rad'
        ]
        check_not_list = ['alpha_f_weighted_rad', 'alpha_r_weighted_rad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                # Compute weighted average slip angle for the front axle using the axle loads
                alpha_f_weighted = np.where(
                    self.data['fz_f_N'] != 0,
                    (self.data['alpha_fl_rad'] * self.data['fz_fl_N'] + self.data['alpha_fr_rad'] * self.data['fz_fr_N']) / self.data['fz_f_N'],
                    0
                )
                # Compute weighted average slip angle for the rear axle using the axle loads
                alpha_r_weighted = np.where(
                    self.data['fz_r_N'] != 0,
                    (self.data['alpha_rl_rad'] * self.data['fz_rl_N'] + self.data['alpha_rr_rad'] * self.data['fz_rr_N']) / self.data['fz_r_N'],
                    0
                )
                # Save weighted slip angles into the data dictionary
                self.data['alpha_f_weighted_rad'] = alpha_f_weighted
                self.data['alpha_r_weighted_rad'] = alpha_r_weighted
                logging.info('Calculated weighted slip angles')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Weighted slip angles not calculated because of data column(s): ({issues})')

        # Calculate Lateral Slip Angle Efficiency ---
        check_list = ['fy_f_N', 'fy_r_N', 'fz_f_N', 'fz_r_N', 'alpha_f_weighted_rad', 'alpha_r_weighted_rad']
        check_not_list = ['lateral_slip_efficiency_front', 'lateral_slip_efficiency_rear', 'lateral_slip_efficiency_vehicle']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                # Calculate lateral slip efficiency for the front axle only if |weighted slip angle| >= 0.01, else set to 0
                self.data['lateral_slip_efficiency_front'] = np.where(
                    np.abs(self.data['alpha_f_weighted_rad']) >= 0.01,
                    self.data['fy_f_N'] / self.data['alpha_f_weighted_rad'],
                    0
                )
                # Calculate lateral slip efficiency for the rear axle only if |weighted slip angle| >= 0.01, else set to 0
                self.data['lateral_slip_efficiency_rear'] = np.where(
                    np.abs(self.data['alpha_r_weighted_rad']) >= 0.01,
                    self.data['fy_r_N'] / self.data['alpha_r_weighted_rad'],
                    0
                )
                # Calculate vehicle lateral slip efficiency as weighted average based on axle vertical loads
                condition_vehicle = (
                    (self.data['lateral_slip_efficiency_front'] != 0) &
                    (self.data['lateral_slip_efficiency_rear'] != 0)
                )
                vehicle_efficiency = (
                    (self.data['lateral_slip_efficiency_front'] * self.data['fz_f_N'] +
                    self.data['lateral_slip_efficiency_rear'] * self.data['fz_r_N']) /
                    (self.data['fz_f_N'] + self.data['fz_r_N'])
                )
                self.data['lateral_slip_efficiency_vehicle'] = np.where(
                    condition_vehicle,
                    vehicle_efficiency,
                    0
                )
                logging.info('Calculated lateral slip efficiency')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Lateral slip efficiency not calculated because of data column(s): ({issues})')

        # Calculate Weighted Wheel Slip 
        check_list = [
            'fz_f_N', 'fz_r_N', 'fz_fl_N', 'fz_fr_N',
            'whl_slip_fl', 'whl_slip_fr', 'fz_rl_N', 'fz_rr_N',
            'whl_slip_rl', 'whl_slip_rr'
        ]
        check_not_list = ['whl_slip_weighted_front', 'whl_slip_weighted_rear']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                whl_slip_weighted_front = np.where(
                    self.data['fz_f_N'] != 0,
                    (self.data['whl_slip_fl'] * self.data['fz_fl_N'] + self.data['whl_slip_fr'] * self.data['fz_fr_N']) / self.data['fz_f_N'],
                    0
                )
                whl_slip_weighted_rear = np.where(
                    self.data['fz_r_N'] != 0,
                    (self.data['whl_slip_rl'] * self.data['fz_rl_N'] + self.data['whl_slip_rr'] * self.data['fz_rr_N']) / self.data['fz_r_N'],
                    0
                )
                self.data['whl_slip_weighted_front'] = whl_slip_weighted_front
                self.data['whl_slip_weighted_rear'] = whl_slip_weighted_rear
                logging.info('Calculated weighted wheel slip')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Weighted wheel slip not calculated because of data column(s): ({issues})')
            
        # Calculate wheel slip efficiency
        check_list = ['whl_slip_weighted_front', 'whl_slip_weighted_rear', 'fy_f_N', 'fy_r_N', 'fz_f_N', 'fz_r_N']
        check_not_list = ['wheel_slip_efficiency_front', 'wheel_slip_efficiency_rear', 'wheel_slip_efficiency_vehicle']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['wheel_slip_efficiency_front'] = np.where(
                    np.abs(self.data['whl_slip_weighted_front']) >= 0.01,
                    self.data['fy_f_N'] / self.data['whl_slip_weighted_front'],
                    0
                )
                self.data['wheel_slip_efficiency_rear'] = np.where(
                    np.abs(self.data['whl_slip_weighted_rear']) >= 0.01,
                    self.data['fy_r_N'] / self.data['whl_slip_weighted_rear'],
                    0
                )
                condition_whl = (
                    (self.data['wheel_slip_efficiency_front'] != 0) &
                    (self.data['wheel_slip_efficiency_rear'] != 0)
                )
                wheel_slip_efficiency_vehicle = (
                    (self.data['wheel_slip_efficiency_front'] * self.data['fz_f_N'] +
                    self.data['wheel_slip_efficiency_rear'] * self.data['fz_r_N']) /
                    (self.data['fz_f_N'] + self.data['fz_r_N'])
                )
                self.data['wheel_slip_efficiency_vehicle'] = np.where(
                    condition_whl,
                    wheel_slip_efficiency_vehicle,
                    0
                )
                logging.info('Calculated wheel slip efficiency')
            except Exception as e:
                logging.error(e)
        else:
            logging.info(f'Wheel slip efficiency not calculated because of data column(s): ({issues})')

        # Calculate normalized Lateral Slip Angle Efficiency (lateral mue efficiency)
        check_list = ['lateral_slip_efficiency_front', 'lateral_slip_efficiency_rear', 'fz_f_N', 'fz_r_N']
        check_not_list = ['lateral_slip_efficiency_norm_front', 'lateral_slip_efficiency_norm_rear', 'lateral_slip_efficiency_norm_vehicle']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                self.data['lateral_slip_efficiency_norm_front'] = np.where(
                    self.data['fz_f_N'] != 0,
                    self.data['lateral_slip_efficiency_front'] / self.data['fz_f_N'],
                    0
                )
                self.data['lateral_slip_efficiency_norm_rear'] = np.where(
                    self.data['fz_r_N'] != 0,
                    self.data['lateral_slip_efficiency_rear'] / self.data['fz_r_N'],
                    0
                )
                # Compute weighted normalized efficiency for the vehicle
                total_axle_load = self.data['fz_f_N'] + self.data['fz_r_N']
                efficiency_norm_vehicle = (
                    self.data['lateral_slip_efficiency_norm_front'] * self.data['fz_f_N'] +
                    self.data['lateral_slip_efficiency_norm_rear'] * self.data['fz_r_N']
                ) / total_axle_load
                self.data['lateral_slip_efficiency_norm_vehicle'] = np.where(
                    total_axle_load != 0,
                    efficiency_norm_vehicle,
                    0
                )
                logging.info('Calculated normalized lateral slip efficiency (lateral mue efficiency)')
            except Exception as e:
                logging.error('Error calculating normalized lateral slip efficiency: %s', e)
        else:
            logging.info(f"Did not calculate normalized lateral slip efficiency because of data column(s): ({issues})")
        

    def calc_instantaneous_cornering_stiffness(self) -> None:
        front_estimator = None
        rear_estimator = None

        # Estimate instantaneous cornering stiffnesses
        check_list = ['alpha_f_rad', 'alpha_r_rad', 'fy_f_N', 'fy_r_N', 'time_s']
        check_not_list = ['cs_f_Nprad', 'cs_r_Nprad']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                logging.info(' Started estimating instantaneous cornering stiffnesses')
                from .cornering_stiffness_estimator import (
                    InstantaneousCorneringStiffnessEstimator,
                    close_cornering_stiffness_progress_bars,
                    create_cornering_stiffness_progress_bars,
                    wait_for_futures_with_progress,
                )
                mp_context = multiprocessing.get_context()
                with mp_context.Manager() as manager:
                    progress_queue = manager.Queue()
                    front_estimator = InstantaneousCorneringStiffnessEstimator(self.data['alpha_f_rad'], self.data['fy_f_N'], sampling_frequency=self.sample_frequency, axle='Front', progress_bar_position=0)
                    rear_estimator = InstantaneousCorneringStiffnessEstimator(self.data['alpha_r_rad'], self.data['fy_r_N'], sampling_frequency=self.sample_frequency, axle='Rear', progress_bar_position=3)
                    progress_bars = create_cornering_stiffness_progress_bars(
                        front_estimator.progress_bar_specifications()
                        + rear_estimator.progress_bar_specifications()
                    )
                    try:
                        with ProcessPoolExecutor(
                            max_workers=2,
                            mp_context=mp_context,
                        ) as executor:
                            futures = {
                                'front': executor.submit(
                                    front_estimator.estimate_cornering_stiffness,
                                    progress_queue,
                                ),
                                'rear': executor.submit(
                                    rear_estimator.estimate_cornering_stiffness,
                                    progress_queue,
                                ),
                            }
                            results = wait_for_futures_with_progress(
                                futures,
                                progress_queue,
                                progress_bars,
                            )
                    finally:
                        close_cornering_stiffness_progress_bars(progress_bars)

                front_cornering_stiffness = results['front']
                rear_cornering_stiffness = results['rear']

                self.data['cs_f_Nprad'] = front_cornering_stiffness
                self.data['cs_r_Nprad'] = rear_cornering_stiffness

                logging.info(' Estimated instantaneous cornering stiffnesses')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not estimate instantaneous cornering stiffnesses because of data column(s): ({issues})')

        # Calculate cornering stiffness ratios
        check_list = [
            'alpha_f_rad', 'alpha_r_rad', 'fy_f_N', 'fy_r_N',
            'cs_f_Nprad', 'cs_r_Nprad',
        ]
        check_not_list = ['cs_ratio_f', 'cs_ratio_r']
        conditions_met, issues = self.check_signals(check_list, check_not_list)
        if conditions_met:
            try:
                if front_estimator is None or rear_estimator is None:
                    from .cornering_stiffness_estimator import InstantaneousCorneringStiffnessEstimator
                    front_estimator = InstantaneousCorneringStiffnessEstimator(self.data['alpha_f_rad'], self.data['fy_f_N'], sampling_frequency=self.sample_frequency)
                    rear_estimator = InstantaneousCorneringStiffnessEstimator(self.data['alpha_r_rad'], self.data['fy_r_N'], sampling_frequency=self.sample_frequency)

                self.data['cs_ratio_f'] = front_estimator.calculate_cornering_stiffness_ratio(self.data['cs_f_Nprad'])
                self.data['cs_ratio_r'] = rear_estimator.calculate_cornering_stiffness_ratio(self.data['cs_r_Nprad'])
                logging.info(' Calculated cornering stiffness ratios')
            except Exception as e: logging.error(e)
        else:
            logging.info(f'Did not calculate cornering stiffness ratios because of data column(s): ({issues})')

    def calc_value(self, variable, model, scaler) -> None:
        X = self.data[['az_mps2', 'ax_mps2', 'v_mps', 'beta_rad', 'delta_f_rad']].values
        if scaler is not None:
            X_scaled = scaler.transform(X)
            self.data[variable] = model.predict(X_scaled)
        else:
            self.data[variable] = model.predict(X)

    def smooth_signals(self) -> None:
        """Smoothing of signals."""
        logging.info("Starting signal smoothing process.")

        # Calculate window size for each signal
        self.filter_list['window_size'] = (
            self.filter_list['window_length_s'] * self.sample_frequency
        ).round().astype(int)
        logging.debug('Calculated window sizes (in samples):\n%s', self.filter_list['window_size'])

        # Defrag Data Frame 
        self.data = self.data.copy()

        # Iterate through each signal in the filter list
        for idx, row in self.filter_list.iterrows():
            signal = row['signal_name']
            window_size = row['window_size']

            # Validate window size
            if window_size < 1:
                logging.debug(
                    f"Smoothing {idx} / {len(self.filter_list)}: Window size for signal '{signal}' is less than 1 (window_size={window_size}). "
                    "Skipping smoothing for this signal."
                )
                continue  # Skip to the next signal

            if signal in self.data.columns:
                # Apply moving average using rolling window
                smoothed_signal = self.data[signal].rolling(
                    window=window_size, center=True, min_periods=1
                ).mean()
                # Create a new column with the smoothed signal
                smoothed_column_name = f"{signal}_smoothed"
                self.data[smoothed_column_name] = smoothed_signal
                logging.debug(
                    f"Smoothing {idx} / {len(self.filter_list)}: Applied moving average filter to '{signal}' with window size {window_size} samples."
                )
            else:
                logging.debug(
                    f"Smoothing {idx} / {len(self.filter_list)}: Signal '{signal}' not found in data. Skipping smoothing for this signal."
                )

        logging.debug("Signal smoothing completed successfully.")

    def characterise_corners(self) -> None:
        from .corner_characterisation import CornerCharacterisation
        """Add corner channels to data."""
        try:
            cc = CornerCharacterisation.from_default_channels(self.data)
            cc.characterise_corners()
        except ValueError as e:
            logging.error("Error in corner characterization: %s", e)

    def write(self) -> None:
        """Writing of unified data into output folder."""
        logging.info(' Saving %s', self.file_name)
        base_name = self.file_path.stem

        # Save full log
        save_name = base_name + ".csv"
        save_path = self.folder_path / 'unified_format' / save_name
        self.data.to_csv(save_path, index=False, escapechar='\\', float_format='%.16f')

        # save laps
        for lap, lap_df in self.data.groupby('n_lap'):
            lap_time = round(lap_df['time_s'].iloc[-1] - lap_df['time_s'].iloc[0], 2)
            laps_file_name = f'{base_name}_lap_{int(lap)}_{lap_time}s.csv'
            laps_path = self.folder_path / 'unified_format' / 'laps'/ laps_file_name
            # Drop the first and last row, write only the middle part
            if len(lap_df) > 2:
                lap_df.iloc[1:-1].to_csv(laps_path, index=False, escapechar='\\', float_format='%.16f')
            else:
                # If there are not enough rows, skip writing this lap
                lap_df.to_csv(laps_path, index=False, escapechar='\\', float_format='%.16f')

    def lap_slice(self) -> None:

        """Splitting of data into different laps. Files of each lap get saved into "laps" folder."""
        if 's_m' in self.data.columns or 's_norm_m' in self.data.columns:
            MAX_JUMP = 300
            CHECK_LENGTH = 100
            if 's_norm_m' in self.data.columns:
                s_jump = self.data['s_norm_m'].diff() < -MAX_JUMP  # Check for negative s jumps
            else:
                s_jump = self.data['s_m'].diff() < -MAX_JUMP  # Check for negative s jumps
                
            if any(s_jump):
                self.lap_change_idx_raw = np.where(np.array(s_jump.values))[0]
                lap_change_idx_tmp = np.array([self.lap_change_idx_raw[i] for i in range(self.lap_change_idx_raw.shape[0]-1) if self.lap_change_idx_raw[i+1] - self.lap_change_idx_raw[i] >= CHECK_LENGTH])
                # Ensure lap_change_idx is an integer array
                self.lap_change_idx = np.concatenate(
                    (np.array([0]), lap_change_idx_tmp, np.array([self.lap_change_idx_raw[-1]]))
                ).astype(int)
            else:
                self.lap_change_idx = np.array([0])
            
            # Add lap identifier channel
            lap_flags = np.zeros(len(self.data))
            lap_flags[self.lap_change_idx] = 1
            n_lap = lap_flags.cumsum().astype(int)
            self.data['n_lap'] = n_lap
            
        else:
            self.lap_change_idx = np.array([0])
            self.data['n_lap'] = np.ones(len(self.data), dtype=int)

    def get_s_norm_ref(self, s_offset=0) -> KNeighborsRegressor:
        # Instantiate the classifier
        knn = KNeighborsRegressor(n_neighbors=3)

        # Calculate new s array with offset and modulo with max s value
        s_array = self.data['s_m'].values
        s_max = np.max(s_array)
        s_offset_array = (s_array + s_offset) % s_max

        # Train the classifier
        knn.fit(self.data[['pos_x_m', 'pos_y_m']].values, s_offset_array)

        # Copy s_m to s_norm_ref
        self.data['s_norm_m'] = s_offset_array

        return knn
    
    def calc_s_norm(self, knn: KNeighborsRegressor) -> None:
        # Predict s_norm
        self.data['s_norm_m'] = knn.predict(self.data[['pos_x_m', 'pos_y_m']].values)


    def select_parser(self) -> None:
        """
        Finds the conversion config column that matches the header of the data file.

        Goes through each name space column in the data conversion config file. If all names present
        in this column are found in the data header, it is a match. As config columns with few
        entries (e.g. the "trackbounds" config contains only "x" and "y" as variables) can match
        multiple data headers, the user needs to choose the correct config for the respective data
        file from the list of matching configurations if multiple are found.
        """
        matching_config_columns = []  # indices of columns where variable names match data headers
        for j in range(1, self.parser_config.shape[1], 2):  # Loop through columns in parser configs
            logging.debug(' Checking parser config %s', self.parser_config.columns[j])
            # Loop through one column and check if same as data header
            for i in range(self.parser_config.shape[0]):
                # check if entry is not nan and if there is a match in the data header
                if (
                    pd.notna(self.parser_config.iloc[i, j])
                    and self.parser_config.iloc[i, j] not in self.data.columns
                ):
                    logging.debug(' No match at parser entry: %s', self.parser_config.iloc[i, j])
                    break  # continue with next column of parser config
                if i == self.parser_config.shape[0] - 1:
                    parser_config_select = self.parser_config.columns[j]
                    matching_config_columns.append(parser_config_select)
                    logging.info(' Found Parser config "%s"', parser_config_select)

        if len(matching_config_columns) > 0:
            config_name = matching_config_columns[0]
            index = self.parser_config.columns.get_loc(config_name)
            if isinstance(index, (slice, np.ndarray)):
                error_msg = 'Multiple matching data conversion configurations have the same name'
                raise ValueError(error_msg)
            self.parser_config_idx = index
            logging.info('Selected data conversion config: %s', config_name)
        elif len(matching_config_columns) == 0:
            logging.error('No matching data conversion config found')

    
    def get_time_elapsed(self) -> float:
        """Returns the time elapsed in seconds."""
        time_elapsed = self.data["time_s"].iloc[-1] - self.data["time_s"].iloc[0]
        return time_elapsed
    def unify_signal_names(self) -> None:
        # Check if any column is sparse
        if any(isinstance(dtype, pd.SparseDtype) for dtype in self.data.dtypes):
            logging.info("Sparse data detected; converting to dense format.")
            self.data = self.data.sparse.to_dense()
        """Renames data headers according to parser config column (index)."""
        if self.parser_config_idx is None:
            error_msg = 'No conversion config selected.'
            raise ValueError(error_msg)
        j = self.parser_config_idx  # Get Parser config column index
        logging.debug('Unifying signal names')
        # Loop through parser config and replace the headers
        for i in range(self.parser_config.shape[0]):
            if pd.notna(self.parser_config.iloc[i, j]):
                original_name = self.parser_config.iloc[i, j]
                logging.debug(' Unifying signal: %s', original_name)
                if original_name not in self.data.columns:
                    error_msg = f' {original_name} found in conversion config not in data header'
                    raise ValueError(error_msg)
                target_name = self.parser_config.iloc[i, 0]
                self.data.rename(columns={original_name: target_name}, inplace=True)
                # apply conversion to values
                if pd.notna(self.parser_config.iloc[i, j + 1]):
                    conv_factor_scalar = self.parser_config.iloc[i, j + 1]
                    if isinstance(conv_factor_scalar, np.generic):
                        conv_factor = float(conv_factor_scalar)
                    else:
                        error_msg = f'Expected a Number, but received {type(conv_factor_scalar)}.'
                        raise TypeError(error_msg)
                    self.data[target_name] *= conv_factor

    def resample_and_interpolate(self, sampling_rate=100, shift_time_to_zero=True, time_offset=0) -> None:
        """
        Resamples and interpolates the time series data from a CSV file to a specified sampling rate.

        Parameters:
        -----------

        sampling_rate : int or float, optional, default=100
            The desired sampling rate in Hertz (Hz) for the resampled data. For example, a sampling rate of 100 Hz
            means that the data will be resampled to have 100 points per second.

        shift_time_to_zero : bool, optional, default=False
            If True, the time column in the resampled data will be shifted to start at 0. If False, the time column
            will retain its original starting value unless a specific time offset is provided.

        time_offset : int or float, optional, default=0
            An optional time offset to adjust the starting point of the resampled time column. This value is added
            to the resampled time column, allowing for the starting time to be set at a specific value. If
            `shift_time_to_zero` is True, this offset will be applied after shifting the time to start at 0.

        """
        # Create a copy of the original data
        if self.data.isnull().values.any():
            logging.warning('NaN values present in data, Data will be interpolated')
            # Set float format
            pd.set_option('float_format', '{:f}'.format)

            # # Ensure the time column is sorted
            self.data = self.data.sort_values('time_s')

            # # Convert all columns except '__time' to numeric, coerce errors (convert to NaN if conversion fails)
            for col in self.data.columns:
                if col != '__time':
                    self.data[col] = pd.to_numeric(self.data[col], errors='coerce')

            # Define the new time points with the desired sampling rate (in Hz)
            original_time = self.data['time_s'].values
            new_time_points = np.arange(original_time.min(), original_time.max(), 1 / sampling_rate)

            # Create a dictionary to hold the interpolated columns
            interpolated_data = {'time_s': new_time_points}

            # Interpolate the other columns to match the new time points
            for col in self.data.columns:
                if col != 'time_s':
                    df_interpolate = pd.DataFrame({'time_s': original_time})
                    df_interpolate = df_interpolate.join(self.data[col])
                    df_interpolate = df_interpolate.dropna(axis=0)
                    try:
                        interpolated_data[col] = np.interp(new_time_points, df_interpolate['time_s'], df_interpolate[col])
                        logging.debug("%s interpolation complete", col)
                    except (TypeError, ValueError):
                        logging.exception("Could not interpolate %s", col)

            # Create the new DataFrame from the dictionary
            new_df = pd.DataFrame(interpolated_data)

            if shift_time_to_zero:
                # Adjust the time column to start at 0
                new_df['time_s'] = new_df['time_s'] - new_df['time_s'].min()

            # Apply any additional time offset
            if time_offset != 0:
                new_df['time_s'] = new_df['time_s'] + time_offset

            # Replace the original DataFrame with the new DataFrame
            self.data = new_df.copy()

            # Fix s_coordinate at lap beginning
            if hasattr(self.data, 's_m'):
                self.data['s_m'] = self.data['s_m'].where(self.data['s_m'].diff() >= -10, 0)

    def run_plot_creator(self):
        root = tk.Tk()
        root.withdraw()
        try:
            open_plot_creator = messagebox.askyesno(
                'Open Plot Creator', 'Would you like to open the Plot Creator?'
            )
        finally:
            root.destroy()

        if open_plot_creator:
            script_path = PROJECT_ROOT / 'tools' / 'plot_creator.py'
            command = [sys.executable, str(script_path)]
            unified_folder_path = self.folder_path / 'unified_format'
            if unified_folder_path.is_dir():
                csv_files = list(unified_folder_path.glob('*.csv'))
                if csv_files:
                    latest_csv = max(csv_files, key=lambda path: path.stat().st_mtime)
                    command.append(str(latest_csv.resolve()))
            subprocess.run(
                command,
                check=False,
                cwd=str(self.folder_path.resolve()),
            )

def create_file_objects(folder_path) -> list[DataFile]:
    """
    Returns a list of DataFile instances of each data file located at the folder path.

    :param folder_path: path to the files folder
    :return: data_file_list
    """
    file_list = list(Path(folder_path).iterdir())
    data_files_list = [
        file_name for file_name in file_list
        if file_name.suffix in {'.csv', '.mat', '.mcap', '.log', '.candump'}
    ]

    if not data_files_list:
        logging.error('No supported .csv, .mat, .mcap, .log, or .candump files found.')
        return []

    from natsort import natsorted
    data_files_list_sorted = natsorted(data_files_list)

    file_objects = []
    # Loop through the files and save path in path list
    for file_path in data_files_list_sorted:
        file_objects.append(DataFile(file_path))
        logging.info('Found: %s', file_path.name)
    return file_objects



def select_data_folder():
    """
    Opens a dialog to select a folder.

    :return: folder_path
    """
    root = tk.Tk()
    root.withdraw()  # Hide the main window

    folder_path = _read_cached_path()
    
    # Open a directory dialog to select a directory
    try:
        folder_path = filedialog.askdirectory(
            title='Select the Logs Directory', initialdir=folder_path
        )
    finally:
        root.destroy()
    if not folder_path:
        error_msg = 'No directory selected'
        raise ValueError(error_msg)
    logging.info(' Selected Logs File Path: %s', folder_path)
    
    _write_cached_path(Path(folder_path))

    return Path(folder_path)


def select_can_definition_file(initial_directory: Path | str) -> Path:
    """Prompt for the JSON signal definition used to read one CAN log."""
    root = tk.Tk()
    root.withdraw()
    try:
        selected_path = filedialog.askopenfilename(
            title='Select CAN Signal Definition',
            initialdir=initial_directory,
            filetypes=(('JSON definitions', '*.json'), ('All files', '*.*')),
        )
    finally:
        root.destroy()
    if not selected_path:
        raise ValueError('No CAN signal definition selected')
    logging.info(' Selected CAN Signal Definition: %s', selected_path)
    return Path(selected_path)


def select_start_finish_line(
    x_values: np.ndarray,
    y_values: np.ndarray,
    file_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Display a trajectory and let the user select two line endpoints."""
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Button

    stride = max(1, len(x_values) // 20_000)
    figure = plt.figure(figsize=(10, 7.5))
    axis = figure.add_subplot()
    figure.subplots_adjust(left=0.10, right=0.96, top=0.84, bottom=0.23)
    if figure.canvas.manager is not None:
        figure.canvas.manager.set_window_title(
            'Select Start/Finish Line for Lap Slicing'
        )
    axis.plot(x_values[::stride], y_values[::stride], color='black', linewidth=1)
    axis.set_aspect('equal', adjustable='datalim')
    axis.set_xlabel('pos_x_m')
    axis.set_ylabel('pos_y_m')
    figure.suptitle(file_name, y=0.97)
    axis.set_title('Click two endpoints, then confirm or redraw', pad=12)
    axis.grid(True, alpha=0.25)

    confirm_axis = figure.add_axes(
        [0.23, 0.055, 0.16, 0.085], label='confirm_start_finish'
    )
    redraw_axis = figure.add_axes(
        [0.42, 0.055, 0.16, 0.085], label='redraw_start_finish'
    )
    cancel_axis = figure.add_axes(
        [0.61, 0.055, 0.16, 0.085], label='cancel_start_finish'
    )
    confirm_button = Button(confirm_axis, 'Confirm')
    redraw_button = Button(redraw_axis, 'Redraw')
    cancel_button = Button(cancel_axis, 'Cancel')
    confirm_button.set_active(False)

    selected_points: list[np.ndarray] = []
    point_artists = []
    line_artist = None
    selection_result: list[tuple[np.ndarray, np.ndarray]] = []

    def redraw_selection(_event=None):
        nonlocal line_artist
        for artist in point_artists:
            artist.remove()
        point_artists.clear()
        if line_artist is not None:
            line_artist.remove()
            line_artist = None
        selected_points.clear()
        confirm_button.set_active(False)
        figure.canvas.draw_idle()

    def select_point(event):
        nonlocal line_artist
        if (
            event.inaxes is not axis
            or event.button != 1
            or event.xdata is None
            or event.ydata is None
            or len(selected_points) >= 2
        ):
            return
        point = np.array([event.xdata, event.ydata])
        selected_points.append(point)
        point_artists.extend(axis.plot(point[0], point[1], 'ro'))
        if len(selected_points) == 2:
            line_artist = axis.plot(
                [selected_points[0][0], selected_points[1][0]],
                [selected_points[0][1], selected_points[1][1]],
                color='red',
                linewidth=2.5,
            )[0]
            confirm_button.set_active(True)
        figure.canvas.draw_idle()

    def confirm_selection(_event):
        if len(selected_points) != 2:
            return
        selection_result.append(
            (selected_points[0].copy(), selected_points[1].copy())
        )
        plt.close(figure)

    def cancel_selection(_event):
        plt.close(figure)

    click_connection = figure.canvas.mpl_connect('button_press_event', select_point)
    confirm_button.on_clicked(confirm_selection)
    redraw_button.on_clicked(redraw_selection)
    cancel_button.on_clicked(cancel_selection)
    try:
        plt.show(block=True)
    finally:
        figure.canvas.mpl_disconnect(click_connection)
        plt.close(figure)
    if not selection_result:
        raise ValueError('Start/finish line selection was cancelled')
    return selection_result[0]


def select_data_file():
    """
    Opens a dialog to select a folder.

    :return: folder_path
    """
    root = tk.Tk()
    root.withdraw()  # Hide the main window

    folder_path = _read_cached_path()
    
    # Open a directory dialog to select a directory
    try:
        selected_path = filedialog.askopenfilename(
            title='Select the Logs File', initialdir=folder_path
        )
    finally:
        root.destroy()
    if not selected_path:
        error_msg = 'No file selected'
        raise ValueError(error_msg)
    file_path = Path(selected_path)
    logging.info(' Selected Logs File: %s', file_path)
    
    _write_cached_path(file_path.parent)

    return Path(file_path)


def drop_down_gui(
    question: str,
    options: list[str],
    *,
    checkbox_label: str | None = None,
) -> tuple[str, bool]:
    """
    Opens drop down GUI for user to choose correct value from list.

    :param question: Question displayed for the user on what to choose
    :param options: List of possible answers displayed in the drop down menu
    :param checkbox_label: Optional label for an unchecked checkbox
    :return: Chosen option and checkbox state
    """
    if not options:
        raise ValueError("At least one dropdown option is required.")

    root = tk.Tk()
    root.withdraw()
    dialog = tk.Toplevel(root)
    dialog.title(question)
    dialog.resizable(False, False)
    label = tk.Label(dialog, text=question, wraplength=500, justify='center')
    label.pack(padx=20, pady=(20, 10))
    selected_config_var = tk.StringVar(root, options[0])
    checkbox_var = tk.BooleanVar(master=root, value=False)

    # Create dropdown menu
    dropdown = tk.OptionMenu(dialog, selected_config_var, *options)
    dropdown.pack(padx=20, pady=5)

    if checkbox_label:
        tk.Checkbutton(
            dialog,
            text=checkbox_label,
            variable=checkbox_var,
        ).pack(padx=20, pady=5)

    ok_button = tk.Button(dialog, text='OK', command=dialog.destroy)
    ok_button.pack(padx=20, pady=(10, 20))

    # Wait for the dialog to be closed
    try:
        root.wait_window(dialog)
        return selected_config_var.get(), checkbox_var.get()
    finally:
        root.destroy()

def select_data_config(matching_configs: list[str]) -> str:
    """Drop down menu for user to choose if multiple matching configs are found."""
    question = (
        'Multiple data conversion configurations match the data header. '
        'Choose which one you want to use.'
    )
    data_config_name, _ = drop_down_gui(question, matching_configs)
    logging.info(' Selected Data Conversion Configuration: %s', data_config_name)
    return data_config_name


def _read_cached_path() -> Path:
    try:
        cached_path = Path(CACHE_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        return Path.cwd()
    return cached_path if cached_path.is_dir() else Path.cwd()


def _write_cached_path(path: Path) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(str(path), encoding="utf-8")
    except OSError:
        logging.warning("Could not update data-path cache at %s", CACHE_FILE)


def create_directories(file_path: Path, *, overwrite: bool = False) -> Path:
    """Create a fresh output tree, requiring explicit consent to replace one."""
    source_path = Path(file_path).expanduser().resolve()
    if not source_path.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {source_path}")

    target_path = source_path / 'unified_format'
    if target_path.is_symlink():
        raise ValueError(f"Refusing to replace symlinked output directory: {target_path}")
    if target_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Output directory already exists: {target_path}. "
                "Pass overwrite=True only after receiving user confirmation."
            )
        shutil.rmtree(target_path)

    (target_path / 'laps').mkdir(parents=True)
    logging.info("Created output directory '%s'.", target_path)
    return target_path



def plot_metric_function(
    data: pd.DataFrame,
    metric_names: list,
    output_folder: Path,
    file_name: str,
    color_scale_limits: tuple[float, float] | None = None,
) -> None:
    """
    Plots the results of a metric function.

    Plots are saved in the 'plots' folder. The corresponding metric needs
    to be added to the data before plotting the metric
    :param data: data of the track and metrics to be displayed
    :param metric_names: name of the metric in the data 
    :param output_folder: folder where the plots are saved
    :param file_name: file name of the original data file to name plot accordingly
    :param color_scale_limits: limits for the color scale of the plot
    """
    if not isinstance(metric_names[0], str):
        logging.error('Metric names passed to plot function are not of type string.')
        return
    metric_variable_name = metric_names[0]
    if metric_variable_name not in data.columns:
        logging.error(
            '%s is not in the data. Please add metric first or check variable name',
            metric_variable_name,
        )
        return

    try:
        # Create subfolder for the metric (if it does not exist yet)
        metric_folder = output_folder / metric_variable_name
        if not metric_folder.exists():
            metric_folder.mkdir(parents=True)

        custom_color_scale = [
            [0.0, 'darkblue'],
            [0.25, 'lightblue'],
            [0.5, 'gray'],
            [0.75, 'lightgreen'],
            [1.0, 'darkgreen'],
        ]

        fig = px.scatter(
            data,
            x='pos_x_m',
            y='pos_y_m',
            color=metric_variable_name,
            color_continuous_scale=custom_color_scale,
            range_color=color_scale_limits,
            title=metric_variable_name,
            template='plotly_white',
            hover_data=[metric_variable_name],
        )

        # Set the same scale for both x and y axes
        fig.update_xaxes(scaleanchor='y', scaleratio=1)
        fig.update_yaxes(scaleanchor='x', scaleratio=1)

        # Save interactive HTML plot
        html_file_path = metric_folder / f'{file_name}_metric_plot.html'
        fig.write_html(html_file_path)
        logging.info(' Metric %s plot saved.', metric_variable_name)

    except Exception:
        logging.exception('While trying to plot the metric for %s.', metric_variable_name)
