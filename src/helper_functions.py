import os
import re

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.collections import LineCollection
import tkinter as tk
from tkinter import simpledialog



def extract_date_and_time(folder_with_csv):
    date_pattern = r'\d{2}\.\d{2}\.\d{4}'
    time_pattern = r'\d{2}\.\d{2}\.\d{2}'

    try:
        date_match = re.search(date_pattern, folder_with_csv.split('_')[0])
        if date_match:
            date_found = date_match.group()
        else:
            date_found= '--'
    except Exception:
        date_found= '--'

    try:
        time_match = re.search(time_pattern, folder_with_csv.split('_')[1])
        if time_match:
            time_found = time_match.group()
            time_found = time_found.rsplit('.', 1)[0].replace(".", ":")
        else:
            time_found = "--"
    except Exception:
        time_found = "--"

    return time_found, date_found



def get_run_session(time_found):
    if str(time_found)== '--':
        return '--'
    try:
        run_session_hour = float(time_found.replace(":", "."))
        if 6 <= run_session_hour < 12:
            return 'Morning'
        elif 12 <= run_session_hour < 15:
            return 'Noon'
        elif 15 <= run_session_hour < 18:
            return 'Evening'
        elif 18 <= run_session_hour < 24:
            return 'Night'
        else:
            return '--'
    except (ValueError, TypeError):
        return '--'



def get_run_time(df):
    try:
        run_time = round((df['time_s'].max()) / 60, 2)
        run_time_str = str(run_time) + ' min'
        return run_time_str
    except KeyError:
        return '--'



def get_track_length(df):
    try:
        track_length = round(df['s_m'].max() / 1000, 3)
        track_length_str = str(track_length) + ' km'
        return track_length_str
    except KeyError:
        return '--'


def transform_time_s(value):
    if value < 10 and value == int(value):
        return value * 0.01
    elif value >= 10 and value == int(value):
        return value * 0.001
    else:
        return value


def load_data_from_folder(directory):
    """
    Loads all lap CSVs from a directory into a dictionary, preserving original lap numbers.
    Performs initial validation to discard fundamentally broken laps.
    """
    try:
        if not directory.endswith('/'):
            directory += '/'
        directory += 'laps/'
        
        csv_files = [filename for filename in os.listdir(directory) if filename.endswith(".csv")]

        def natural_sort_key(s):
            return list(map(int, re.findall(r'\d+', s)))
        sorted_csv_files = sorted(csv_files, key=natural_sort_key)
      
        if not csv_files:
            raise FileNotFoundError("no csv files found")

        all_laps_dict = {}
        
        for i, filename in enumerate(sorted_csv_files):
            lap_num = i + 1
            file_path = os.path.join(directory, filename)
            df = pd.read_csv(file_path, delimiter=",")
            
            if df.empty:
                print(f"INFO: Skipping empty file for Lap {lap_num}: {filename}")
                continue

            # --- DATA NORMALIZATION ---
            initial_time = df['time_s'].iloc[0]
            df['time_s'] = df['time_s'] - initial_time

            all_laps_dict[lap_num] = df
            
        # --- VALIDATION STEP ---
        print("\n--- Validating all loaded laps ---")
        valid_laps_dict = {}
        for lap_num, df in all_laps_dict.items():
            print(f"Validating Lap {lap_num}...")
            if is_lap_valid(df):
                valid_laps_dict[lap_num] = df
                print(f" -> Lap {lap_num} is VALID.")
            else:
                print(f" -> Lap {lap_num} is INVALID and will be discarded.")
        
        print("---------------------------------")
        laps_kept = len(valid_laps_dict)
        laps_removed = len(all_laps_dict) - laps_kept
        print(f"Initial validation complete. Kept {laps_kept} laps, removed {laps_removed}.")

        return valid_laps_dict

    except (FileNotFoundError, pd.errors.EmptyDataError) as e:
        print(f"Error loading data: {e}")
        return {}


def get_true_track_length(laps_dict):
    """
    Calculates the true track length from a dictionary of lap dataframes.
    """
    if not laps_dict:
        return 0

    distances_traveled = []
    for df in laps_dict.values():
        if not df.empty and 's_m' in df.columns:
            distance = df['s_m'].max() - df['s_m'].min()
            distances_traveled.append(distance)

    return max(distances_traveled) if distances_traveled else 0



def get_sector_boundaries(df, track_length):
    """
    Determines the start and end integer positions for three track sectors.
    """
    if track_length == 0:
        return [(0, 0), (0, 0), (0, 0)]

    third = track_length / 3.0
    boundaries_dist = [third, 2 * third, track_length]

    sector_boundaries = []
    last_position = 0
    last_available_position = df.shape[0] - 1

    for boundary_dist in boundaries_dist:
        matching_indices = df.index[df['s_m'] >= boundary_dist]
        
        if not matching_indices.empty:
            first_matching_index = matching_indices[0]
            current_position = df.index.get_loc(first_matching_index)
        else:
            current_position = last_available_position
        
        sector_boundaries.append((last_position, current_position))
        last_position = current_position

        if current_position == last_available_position:
            break

    while len(sector_boundaries) < 3:
        sector_boundaries.append((last_available_position, last_available_position))

    return sector_boundaries



def lap_time_format(lap_time_in_sec):
    if lap_time_in_sec is None or pd.isna(lap_time_in_sec) or lap_time_in_sec < 0:
        return "--"
    minutes = int(lap_time_in_sec // 60)
    seconds = int(lap_time_in_sec % 60)
    tenths_of_second = int(round((lap_time_in_sec - int(lap_time_in_sec)) * 10))
    
    return f"{minutes}:{seconds:02d}.{tenths_of_second}"



def calculate_sector_times_numerical(df, sector_boundaries, track_length, completion_threshold=0.95):
    """
    Calculates sector times, validating against distance.
    """
    sector_times = []
    ideal_sector_length = track_length / 3.0

    for start, end in sector_boundaries:
        final_sector_time = np.nan
        try:
            if start < end:
                start_distance = df['s_m'].iloc[start]
                end_distance = df['s_m'].iloc[end]
                actual_sector_distance = end_distance - start_distance
                
                if actual_sector_distance >= (ideal_sector_length * completion_threshold):
                    sector_start_time = df['time_s'].iloc[start]
                    sector_end_time = df['time_s'].iloc[end]
                    calculated_time = round(sector_end_time - sector_start_time, 1)
                    if calculated_time > 0:
                        final_sector_time = calculated_time
        except (IndexError, KeyError):
            pass
        sector_times.append(final_sector_time)
        
    return sector_times

def find_minimum_sector_times(laps_dict, sector_boundaries_dict, track_length):
    """
    Finds the best time for each sector across all laps.
    """
    all_sector_times_dict = {
        lap_num: calculate_sector_times_numerical(df, sector_boundaries_dict[lap_num], track_length)
        for lap_num, df in laps_dict.items()
    }
    
    print("DEBUG: Calculated Sector Times for all laps:")
    for lap_num, times in all_sector_times_dict.items():
        print(f"  Lap {lap_num}: {times}")
                        
    min_sector_times = {}
    for sector_index in range(3):
        sector_key = f"Sector {sector_index + 1}"
        best_time = float('inf')
        best_lap = '--'

        for lap_num, lap_times in all_sector_times_dict.items():
            if len(lap_times) > sector_index and not np.isnan(lap_times[sector_index]):
                if lap_times[sector_index] < best_time:
                    best_time = lap_times[sector_index]
                    best_lap = lap_num
        
        if best_time != float('inf'):
            min_sector_times[sector_key] = (lap_time_format(best_time), best_lap)
        else:
            min_sector_times[sector_key] = ('--', '--')
            
    return min_sector_times, all_sector_times_dict



def plot_car_trajectory(laps_dict, sector_boundaries_dict, true_track_length, lap_colors, track_file_path, completion_threshold=0.98):
    """
    Plots the car's trajectory for the most representative lap.
    """
    best_lap_num = -1

    # 1. Search for the first lap that is nearly complete
    for lap_num, df in laps_dict.items():
        driven_distance = df['s_m'].max() - df['s_m'].min()
        if driven_distance >= (true_track_length * completion_threshold):
            if best_lap_num == -1:
                best_lap_num = lap_num  # first complete lap
            else:
                best_lap_num = lap_num  # second complete lap
                print(f"INFO: Using Lap {lap_num} for trajectory plot (second nearly complete lap, driven length: {driven_distance:.2f} m).")
                break

    # 2. Fallback: If no complete laps, find the longest driven lap
    if best_lap_num == -1 and laps_dict:
        max_driven_dist = -1
        for lap_num, df in laps_dict.items():
            current_driven_dist = df['s_m'].max() - df['s_m'].min()
            if current_driven_dist > max_driven_dist:
                max_driven_dist = current_driven_dist
                best_lap_num = lap_num
        if best_lap_num != -1:
            print(f"INFO: Using Lap {best_lap_num} for trajectory plot (longest lap fallback, driven length: {max_driven_dist:.2f} m).")

    # If no usable lap data exists, save a blank plot
    if best_lap_num == -1:
        plt.figure(figsize=(6, 6), facecolor='white')
        plt.text(0.5, 0.5, 'No valid lap data to plot track.', ha='center', va='center')
        plt.axis('off')
        plt.savefig(track_file_path, format='png', dpi=200, transparent=True, bbox_inches='tight', pad_inches=0)
        plt.close()
        print("WARNING: No valid lap data available for trajectory plot. Saved blank image.")
        return

    df_to_plot = laps_dict[best_lap_num]
    boundaries_to_plot = sector_boundaries_dict[best_lap_num]

    try:
        if 'pos_x_m' not in df_to_plot.columns or 'pos_y_m' not in df_to_plot.columns:
            raise KeyError("Missing 'pos_x_m' or 'pos_y_m' column")

        plt.figure(figsize=(6, 6), facecolor='none')
        for i, (start, end) in enumerate(boundaries_to_plot):
            if start < end:
                x_data = df_to_plot['pos_x_m'].iloc[start:end+1]
                y_data = df_to_plot['pos_y_m'].iloc[start:end+1]
                plt.plot(x_data, y_data, marker='o', linestyle='-', color=lap_colors[i], markersize=12)
        
        plt.axis('equal')
        plt.axis('off')
        print(f"INFO: Successfully plotted trajectory for Lap {best_lap_num}.")
        
    except (KeyError, IndexError) as e:
        print(f"Error plotting car trajectory for Lap {best_lap_num}: {e}")
        plt.figure(figsize=(6, 6), facecolor='white')
        plt.axis('off')

    plt.savefig(track_file_path, format='png', dpi=200, transparent=True, bbox_inches='tight', pad_inches=0)
    plt.close()


def plot_velocity_trends(laps_dict, colors, speed_vs_dist_path):
    try:
        for df in laps_dict.values():
            if 's_m' not in df.columns or 'vx_mps' not in df.columns:
                raise KeyError("Missing 's_m' or 'vx_mps' column")

        plt.figure(figsize=(19, 6))
        font_color = (75/255, 75/255, 75/255)

        for i, (lap_num, df) in enumerate(laps_dict.items()):
            plt.plot(df['s_m'], df['vx_mps'], label=f'Lap {lap_num}', color=colors[i])

        plt.xlabel('Distance (m)', fontsize=16, color=font_color)
        plt.ylabel('Velocity (m/s)', fontsize=16, color=font_color)
        plt.title('Velocity per Lap', fontsize=18, color=font_color)
        plt.legend(loc='upper right', fontsize=12, labelcolor=font_color)
        plt.grid(which='major', linestyle='-', linewidth=0.5)
        plt.grid(which='minor', linestyle='-', linewidth=0.2)
        plt.minorticks_on()
        plt.xticks(fontsize=14, color=font_color)
        plt.yticks(fontsize=14, color=font_color)

    except KeyError as e:
        print(e)
        plt.figure(figsize=(16, 6))
        plt.text(0.5, 0.5, 'Required data is missing', fontsize=18, ha='center', va='center')
        plt.axis('off')

    plt.savefig(speed_vs_dist_path,  transparent=True, format='png', dpi=200)
    plt.close()



def rolling_mean(series, window):
    return series.rolling(window=window, min_periods=1).mean()

def ensure_kpi_columns(df, kpi_columns, default_value=-4700):
    for col in kpi_columns:
        if col not in df.columns:
            df[col] = default_value
    return df

def replace_negative_values_with_na(dictionary, negative_value=-4700, replacement='--'):
    for key, value in dictionary.items():
        if isinstance(value, dict):
            replace_negative_values_with_na(value, negative_value, replacement)
        elif isinstance(value, (int, float)) and value <= negative_value:
            dictionary[key] = replacement


def calculate_lap_times_and_kpis(laps_dict, sector_boundaries_dict, sector_times_dict, rolling_window_size=5):
    lap_kpis_dict = {}
    kpi_columns = ['vx_mps', 'ay_mps2', 'engine_trq_req_nm', 'a_total_mps2', 'ax_mps2']

    for lap_num, df_lap in laps_dict.items():
        df_lap = ensure_kpi_columns(df_lap.copy(), kpi_columns)
        
        lap_sector_times = sector_times_dict.get(lap_num, [np.nan, np.nan, np.nan])
        
        current_lap_kpis = {
            'Max Speed': round(rolling_mean(df_lap['vx_mps'], rolling_window_size).max(), 2),
            'Max Lateral Acc.': round(rolling_mean(df_lap['ay_mps2'].abs(), rolling_window_size).max(), 2),
            'Max Engine Torque': round(rolling_mean(df_lap['engine_trq_req_nm'], rolling_window_size).max(), 2),
            'Max Longitudinal Acc.': round(rolling_mean(df_lap['ax_mps2'], rolling_window_size).max(), 2),
            'Min Longitudinal Acc.': round(rolling_mean(df_lap['ax_mps2'], rolling_window_size).min(), 2),
            'Max Combined Acc.': round(rolling_mean(df_lap['a_total_mps2'], rolling_window_size).max(), 2)
        }
        
        sector_boundaries = sector_boundaries_dict[lap_num]
        sectors = {
            "Sector 1": df_lap.iloc[sector_boundaries[0][0]:sector_boundaries[0][1]],
            "Sector 2": df_lap.iloc[sector_boundaries[1][0]:sector_boundaries[1][1]],
            "Sector 3": df_lap.iloc[sector_boundaries[2][0]:-1]
        }
        
        sector_kpis = {}
        for sector_name, sector_df in sectors.items():
            sector_df = ensure_kpi_columns(sector_df.copy(), kpi_columns)
            if not sector_df.empty:
                each_sector_kpis = {
                    'Max Speed': round(rolling_mean(sector_df['vx_mps'], rolling_window_size).max(), 2),
                    'Max Lateral Acc.': round(rolling_mean(sector_df['ay_mps2'].abs(), rolling_window_size).max(), 2),
                    'Max Engine Torque': round(rolling_mean(sector_df['engine_trq_req_nm'], rolling_window_size).max(), 2),
                    'Max Longitudinal Acc.': round(rolling_mean(sector_df['ax_mps2'], rolling_window_size).max(), 2),
                    'Min Longitudinal Acc.': round(rolling_mean(sector_df['ax_mps2'], rolling_window_size).min(), 2),
                    'Max Combined Acc.': round(rolling_mean(sector_df['a_total_mps2'], rolling_window_size).max(), 2)
                }
            else:
                 each_sector_kpis = {kpi: -4700 for kpi in current_lap_kpis.keys()}
            sector_kpis[sector_name] = each_sector_kpis
        
        lap_kpis_dict[f"Lap {lap_num}"] = {
            "current_lap_time": lap_time_format(df_lap['time_s'].max()),
            "sector1_time": lap_time_format(lap_sector_times[0]),
            "sector2_time": lap_time_format(lap_sector_times[1]),
            "sector3_time": lap_time_format(lap_sector_times[2]),
            "Current Lap Stats": current_lap_kpis,
            "Sector KPIs": sector_kpis
        }
        
    replace_negative_values_with_na(lap_kpis_dict)
    return lap_kpis_dict


def save_empty_ggplot(file_path):
    plt.figure(figsize=(10, 10))
    plt.text(0.5, 0.5, 'Required data is missing', fontsize=28, ha='center', va='center')
    plt.axis('on')
    plt.savefig(file_path, dpi=200, format='png')
    plt.close()

def ggplot_corr(df, file_path, window_size, font_color, ax_max, ax_min, ay_max, ay_min):
    font_color = (75/255, 75/255, 75/255)
    df['ay_smoothed'] = df['ay_mps2'].rolling(window=window_size).mean()
    df['ax_smoothed'] = df['ax_mps2'].rolling(window=window_size).mean()
    plt.figure(figsize=(20, 10))
    sc = plt.scatter(
        df['ay_smoothed'], df['ax_smoothed'],
        c=df['v_mps'], cmap='CMRmap',
        vmin=0, vmax=df['v_mps'].max()
    )
    cbar = plt.colorbar(sc)
    cbar.set_label('Velocity (m/s)', fontsize=24, color=font_color)
    cbar.ax.tick_params(labelsize=20, colors=font_color)
    plt.xlabel("Lateral Acc. (m/s$^2$)", fontsize=24, color=font_color)
    plt.ylabel("Longitudinal Acc. (m/s$^2$)", fontsize=24, color=font_color)
    plt.title("GG-Diagram", fontsize=28, color=font_color)
    plt.xticks(fontsize=22, color=font_color)
    plt.yticks(fontsize=22, color=font_color)
    plt.ylim(ax_min, ax_max)
    plt.xlim(ay_min, ay_max)
    plt.grid("both", which='major', linestyle='-', linewidth=0.5)
    plt.grid("both", which='minor', linestyle='--', linewidth=0.2)
    plt.minorticks_on()
    plt.savefig(file_path, dpi=200, format='png')
    plt.close()



def save_empty_comb_acc_plot(file_path):
    plt.figure(figsize=(16, 6))
    plt.text(0.5, 0.5, 'Required data is missing', fontsize=28, ha='center', va='center')
    plt.axis('on')
    plt.savefig(file_path, dpi=200, format='png')
    plt.close()

def comb_acc_pattern_plot(laps_dict, lap_colors, file_path, window_size, font_color):
    plt.figure(figsize=(19, 6))
    for i, (lap_num, df) in enumerate(laps_dict.items()):
        df['a_total_mps2_smoothed'] = df['a_total_mps2'].rolling(window=window_size).mean()
        plt.plot(df['s_m'], df['a_total_mps2_smoothed'], label=f'Lap {lap_num}', color=lap_colors[i])

    plt.xlabel('Distance (m)', fontsize=16, color=font_color)  
    plt.ylabel('Combined Acc. (m/s$^2$)', fontsize=16, color=font_color) 
    plt.title('Combined Acceleration per Lap', fontsize=18, color=font_color)
    plt.legend(loc='upper right', fontsize=12, labelcolor=font_color)
    plt.xticks(fontsize=14, color=font_color)
    plt.yticks(fontsize=14, color=font_color)
    plt.minorticks_on()
    plt.grid(which='major', linestyle='-', linewidth=0.5)
    plt.grid(which='minor', linestyle='-', linewidth=0.2)
    plt.savefig(file_path,  transparent=True, format='png', dpi=200)
    plt.close()




def get_weather(city_name):
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        print("INFO: Weather lookup skipped; OPENWEATHER_API_KEY is not set.")
        return 0, 0, "Not Found", "Not Available"

    try:
        response = requests.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"appid": api_key, "q": city_name, "units": "metric"},
            timeout=5,
        )
        response.raise_for_status()
        data = response.json()
        if str(data.get("cod")) == "200":
            forecasts = data.get("list", [])
            if not forecasts:
                return 0, 0, "Not Found", "Not Found"
            weather_main = forecasts[0]["weather"][0]['main']
            weather_description = forecasts[0]["weather"][0]["description"].capitalize()
            min_temp = min(f["main"]["temp_min"] for f in forecasts[:8])
            max_temp = max(f["main"]["temp_max"] for f in forecasts[:8])
            return int(min_temp), int(max_temp), weather_main, weather_description
        else:
            print("City not found or weather API error.")
            return 0, 0, "Not Found", "Not Found"
    except (requests.exceptions.RequestException, KeyError, TypeError, ValueError):
        # Request exceptions may include the credential-bearing URL in their text.
        print("Weather request failed.")
        return 0, 0, "Not Found", "Connection Error"



def save_empty_acc_grad_plot(file_path):
    plt.figure(figsize=(10, 6))
    plt.text(0.5, 0.5, 'Required data is missing', fontsize=28, ha='center', va='center')
    plt.axis('on')
    plt.savefig(file_path, dpi=200, format='png')
    plt.close()

def acc_grad_color_encoded_plot(df, file_path):
    font_color = (75/255, 75/255, 75/255)
    df_sorted = df.sort_values(by=['s_m'])
    plt.figure(figsize=(19, 6))
    points = np.array([df_sorted['s_m'], df_sorted['vx_mps']]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    norm = plt.Normalize(df_sorted['a_total_mps2'].min(), df_sorted['a_total_mps2'].max())
    lc = LineCollection(segments, cmap='turbo', norm=norm)
    lc.set_array(df_sorted['a_total_mps2'])
    lc.set_linewidth(3.5)
    line = plt.gca().add_collection(lc)
    cbar = plt.colorbar(line)
    cbar.set_label('Combined Acceleration (m/s$^2$)', size=16, color=font_color)
    plt.xlim(df_sorted['s_m'].min(), df_sorted['s_m'].max())
    plt.ylim(df_sorted['vx_mps'].min(), df_sorted['vx_mps'].max())
    plt.xlabel('Distance (m)', fontsize=16, color=font_color)
    plt.ylabel('Velocity (m/s)', fontsize=16, color=font_color)
    plt.title('Velocity and Combined Acceleration', fontsize=18, color=font_color)
    plt.xticks(fontsize=12, color=font_color)
    plt.yticks(fontsize=12, color=font_color)
    plt.minorticks_on()
    plt.grid(which='major', linestyle='-', linewidth=0.5)
    plt.grid(which='minor', linestyle='-', linewidth=0.2)
    plt.savefig(file_path, transparent=True, format='png', dpi=200)
    plt.close()




def prompt_user_for_missing_data(field_name):
    root = tk.Tk()
    root.withdraw()
    user_input = simpledialog.askstring(title="Missing Data", prompt=f"Please provide the data for {field_name}:")
    return user_input

def process_data(data):
    processed_data = {}
    for key, value in data.items():
        if value == '--':
            user_input = prompt_user_for_missing_data(key)
            processed_data[key] = user_input if user_input else '--'
        else:
            processed_data[key] = value
    return processed_data

def is_lap_valid(df, max_jump_m=100, min_duration_s=10):
    """
    Validates a single lap on multiple criteria.
    """
    if df.empty or 'time_s' not in df.columns or 's_m' not in df.columns:
        print("DEBUG: Lap rejected (empty or missing required columns).")
        return False
        
    lap_duration = df['time_s'].max() - df['time_s'].min()
    if lap_duration < min_duration_s:
        print(f"DEBUG: Lap rejected (duration {lap_duration:.1f}s < {min_duration_s}s).")
        return False

    distance_per_step = df['s_m'].diff()
    if not distance_per_step.empty and distance_per_step.max() > max_jump_m:
        print(f"DEBUG: Lap rejected (max jump of {distance_per_step.max():.2f}m > {max_jump_m}m).")
        return False
        
    return True
