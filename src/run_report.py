from pathlib import Path
from statistics import mean
import tkinter as tk

import matplotlib.pyplot as plt
import pandas as pd

from .helper_functions import (
    acc_grad_color_encoded_plot,
    calculate_lap_times_and_kpis,
    comb_acc_pattern_plot,
    find_minimum_sector_times,
    get_run_time,
    get_sector_boundaries,
    get_true_track_length,
    get_weather,
    ggplot_corr,
    lap_time_format,
    load_data_from_folder,
    plot_car_trajectory,
    plot_velocity_trends,
    save_empty_acc_grad_plot,
    save_empty_comb_acc_plot,
    save_empty_ggplot,
    transform_time_s,
)


SOURCE_DIR = Path(__file__).resolve().parent
GRAPH_IMAGES_DIR = SOURCE_DIR / "graph_imgs_src"
WEATHER_IMAGES_DIR = SOURCE_DIR / "weather_imgs_src"
WEATHER_IMAGE_NAMES = {"Clear", "Clouds", "Rain", "Sunny", "Wind"}

def get_run_info_from_gui():
    """Creates a robust Tkinter pop-up to get all required run info from the user."""
    root = tk.Tk()
    root.title("Enter Run Information")
    # Increased window height to fit all fields and the button comfortably
    root.geometry("300x350")

    # Dictionary to hold the entered values
    results = {}

    def on_submit():
        # Store values from entries into the dictionary
        results['date'] = date_entry.get()
        results['session'] = session_entry.get()
        results['start_time'] = time_entry.get()
        results['track'] = track_entry.get()
        results['city'] = city_entry.get()
        # End the mainloop, allowing the script to continue
        root.quit()

    # Create a frame for padding
    frame = tk.Frame(root, padx=10, pady=10)
    frame.pack(expand=True, fill=tk.BOTH)

    # Add widgets to the frame
    tk.Label(frame, text="Date (YYYY-MM-DD):").pack(pady=2)
    date_entry = tk.Entry(frame)
    date_entry.pack()

    tk.Label(frame, text="Session Number:").pack(pady=2)
    session_entry = tk.Entry(frame)
    session_entry.pack()

    tk.Label(frame, text="Start Time (HH:MM:SS):").pack(pady=2)
    time_entry = tk.Entry(frame)
    time_entry.pack()

    tk.Label(frame, text="Track Name:").pack(pady=2)
    track_entry = tk.Entry(frame)
    track_entry.pack()

    tk.Label(frame, text="City:").pack(pady=2)
    city_entry = tk.Entry(frame)
    city_entry.pack()

    submit_button = tk.Button(frame, text="Submit", command=on_submit)
    submit_button.pack(pady=10)

    # Set a protocol to handle the window close button
    root.protocol("WM_DELETE_WINDOW", root.quit)

    # Start the GUI event loop. This call will block until root.quit() is called.
    root.mainloop()

    # The window is gone, but the root object still exists. Destroy it.
    root.destroy()

    # Return the values that were captured in the results dictionary
    return (results.get('date', ''),
            results.get('session', ''),
            results.get('start_time', ''),
            results.get('track', ''),
            results.get('city', ''))


def run_report_generator(converted_file_name, converted_folder_path):

    # --- Get User Input via GUI ---
    date_input, session_input, start_time_input, track_input, city_input = get_run_info_from_gui()
    if not all([date_input, session_input, start_time_input, track_input, city_input]):
        print("CRITICAL: All fields must be filled in. Aborting report generation.")
        return

    # Visualisation style
    plt.rcParams['font.family'] = 'DejaVu Sans'

    converted_folder_path = Path(converted_folder_path).expanduser().resolve()
    converted_file_path = converted_folder_path / f"{Path(converted_file_name).stem}.csv"
    df = pd.read_csv(converted_file_path, delimiter=",")
    GRAPH_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Use data from the GUI
    city_name = city_input
    track_name = track_input

    # time transformation
    df['time_s'] = df['time_s'].apply(transform_time_s)

    # Total time for the run
    run_time = get_run_time(df)

    # data loading for all the laps, now returns a dictionary
    laps_dict = load_data_from_folder(str(converted_folder_path))

    # If, after validation, no laps remain, we cannot continue.
    if not laps_dict:
        print("CRITICAL: No valid lap data found after initial validation. Aborting report generation.")
        return # Exit the function early

    # Use the new, robust function to get the true track length
    track_length = get_true_track_length(laps_dict)
    print(f"INFO: True track length determined to be {track_length:.2f} meters.")

    # --- Invalidate laps with less than 5% of the true track length ---
    if track_length > 0:
        min_lap_distance = 0.05 * track_length
        laps_to_remove = []
        for lap_num, df_lap in laps_dict.items():
            driven_distance = df_lap['s_m'].max() - df_lap['s_m'].min()
            if driven_distance < min_lap_distance:
                laps_to_remove.append(lap_num)

        if laps_to_remove:
            print(f"INFO: Invalidating laps shorter than 5% threshold ({min_lap_distance:.2f} m): {', '.join(map(str, laps_to_remove))}")
            for lap_num in laps_to_remove:
                del laps_dict[lap_num]
    else:
        print("WARNING: Track length is zero. Cannot perform distance-based lap validation.")

    # If, after this final validation, no laps remain, we cannot continue.
    if not laps_dict:
        print("CRITICAL: No valid lap data remains after distance validation. Aborting report generation.")
        return # Exit the function early


    for df in laps_dict.values():
        df['time_s'] = df['time_s'].apply(transform_time_s)

    # calculating boundaries of each sector, stored in a dictionary
    sector_boundaries_dict = {lap_num: get_sector_boundaries(df, track_length) for lap_num, df in laps_dict.items()}

    for lap_num, boundaries in sector_boundaries_dict.items():
        print(f"Lap {lap_num} sector boundaries: {boundaries}")

    min_sector_times_list, sector_times_dict = find_minimum_sector_times(laps_dict, sector_boundaries_dict, track_length)


    # Best Lap Time - calculated from laps covering at least 99% of driven distance
    best_lap_time_sec = min((df['time_s'].max() for df in laps_dict.values() if (df['s_m'].max() - df['s_m'].min()) > 0.99 * track_length), default=None)
    best_lap_time = lap_time_format(best_lap_time_sec) if best_lap_time_sec is not None else "--"

    # Avg Lap Time - calculated from laps covering at least 95% of driven distance
    valid_lap_times = [df['time_s'].max() for df in laps_dict.values() if (df['s_m'].max() - df['s_m'].min()) > 0.95 * track_length]
    avg_lap_time_sec = mean(valid_lap_times) if valid_lap_times else 0
    avg_lap_time = lap_time_format(avg_lap_time_sec) if avg_lap_time_sec > 0 else "--"


    best_sector_times_by_lap = [
        f"{time} - Lap {lap}" for time, lap in zip(
            [min_sector_times_list['Sector 1'][0], min_sector_times_list['Sector 2'][0], min_sector_times_list['Sector 3'][0]],
            [min_sector_times_list['Sector 1'][1], min_sector_times_list['Sector 2'][1], min_sector_times_list['Sector 3'][1]]
        )
    ]

    # --- Build the data dictionaries for the PDF tables ---

    # Main "Run Information" table data using user input
    manual_details = {
        'Date': date_input,
        'Run': session_input,
        'Track': track_name,
        'Track Length': f"{track_length:.2f} m",
        'Start Time': start_time_input,
        'Run Time': run_time,
        'Laps': str(len(laps_dict)),
        'Best Lap Time': best_lap_time,
        'Avg Lap Time': avg_lap_time,
    }

    # "Performance Stats" table data
    performance_details = {}
    if laps_dict:
        all_laps_df = pd.concat(laps_dict.values())
        window_size = 30  # Use the same window size as elsewhere
        overall_top_speed = all_laps_df['v_mps'].rolling(window=window_size, min_periods=1).mean().max()
        overall_max_lat_acc = all_laps_df['ay_mps2'].rolling(window=window_size, min_periods=1).mean().max()
        overall_max_long_acc = all_laps_df['ax_mps2'].rolling(window=window_size, min_periods=1).mean().max()
        overall_min_long_acc = all_laps_df['ax_mps2'].rolling(window=window_size, min_periods=1).mean().min()
        overall_max_comb_acc = all_laps_df.get('a_total_mps2', pd.Series([0])).rolling(window=window_size, min_periods=1).mean().max()

        performance_details = {
            'Overall Best Stats': '', # Header
            'Top Speed (m/s)': f"{overall_top_speed:.2f}",
            'Max Lat Acc (m/s²)': f"{overall_max_lat_acc:.2f}",
            'Max Long Acc (m/s²)': f"{overall_max_long_acc:.2f}",
            'Max Braking (m/s²)': f"{overall_min_long_acc:.2f}",
            'Max Comb Acc (m/s²)': f"{overall_max_comb_acc:.2f}",
            'Best Sector Times': best_sector_times_by_lap
        }


    num_laps = len(laps_dict)
    if num_laps < 3:
        lap_colors = plt.get_cmap('tab10').colors[:3]
    else:
        cmap = plt.get_cmap('tab10').colors
        lap_colors = [cmap[i % len(cmap)] for i in range(num_laps)]

    # Race Track Car Trajectory Plot
    track_file_path = GRAPH_IMAGES_DIR / 'track_outline_plot.png'
    plot_car_trajectory(laps_dict, sector_boundaries_dict, track_length, lap_colors, track_file_path)

    # Velocity Trends Across Laps Plot
    speed_vs_dist_path = GRAPH_IMAGES_DIR / 'speed_vs_dist.png'
    plot_velocity_trends(laps_dict, lap_colors, speed_vs_dist_path)


    lap_times_dict = {}
    lap_times_dict = calculate_lap_times_and_kpis(laps_dict, sector_boundaries_dict, sector_times_dict)


    window_size = 30  # Define the window size for the moving average

    # Font color
    font_color = (75/255, 75/255, 75/255)

    #Find boundaries of accelerations
    ay_max, ay_min, ax_max, ax_min = 10, -10, 5, -10
    for df in laps_dict.values():
        ay_max = max(ay_max, df['ay_mps2'].rolling(window=window_size).mean().max())
        ay_min = min(ay_min, df['ay_mps2'].rolling(window=window_size).mean().min())
        ax_max = max(ax_max, df['ax_mps2'].rolling(window=window_size).mean().max())
        ax_min = min(ax_min, df['ax_mps2'].rolling(window=window_size).mean().min())


    for i, (lap_num, df) in enumerate(laps_dict.items()):
        ggplot_file_path = GRAPH_IMAGES_DIR / f'ggplot_ay_vs_ax_smoothed{lap_num}.png'

        try:
            # Check for missing columns
            if 'ay_mps2' not in df.columns or 'ax_mps2' not in df.columns:
                raise KeyError("Missing 'ay_mps2' or 'ax_mps2' column in DataFrame")

            ggplot_corr(df, ggplot_file_path, window_size, font_color, ax_max, ax_min, ay_max, ay_min)

        except KeyError as e:
            print(e)
            save_empty_ggplot(ggplot_file_path)


    dist_vs_acc_sma_path = GRAPH_IMAGES_DIR / 'dist_vs_acc_sma.png'

    try:
        # Check for missing columns in any dataframe
        for df in laps_dict.values():
            if 'a_total_mps2' not in df.columns or 's_m' not in df.columns:
                raise KeyError("Missing 'a_total_mps2' or 's_m' column in one or more DataFrames")

        comb_acc_pattern_plot(laps_dict, lap_colors, dist_vs_acc_sma_path, window_size, font_color)

    except KeyError as e:
        print(e)
        save_empty_comb_acc_plot(dist_vs_acc_sma_path)


    min_temp, max_temp, weather_main, weather_description = get_weather(city_name)

    weather_image_name = weather_main if weather_main in WEATHER_IMAGE_NAMES else "Clear"
    weather_img_path = WEATHER_IMAGES_DIR / f'{weather_image_name}.png'
    if not weather_img_path.is_file():
        weather_img_path = None


    for lap_num, df in laps_dict.items():
        color_encoded_plot_path = GRAPH_IMAGES_DIR / f'color_encoded_plot{lap_num}.png'

        try:
            if 's_m' not in df.columns or 'vx_mps' not in df.columns or 'a_total_mps2' not in df.columns:
                raise KeyError("Missing 's_m', 'vx_mps', or 'a_total_mps2' column in DataFrame")

            acc_grad_color_encoded_plot(df, color_encoded_plot_path)

        except KeyError as e:
            print(e)
            save_empty_acc_grad_plot(color_encoded_plot_path)


    from fpdf import FPDF

    class PDF(FPDF):
        def first_page_header(self):
            # Use absolute positioning for the header text
            self.set_xy(10, 15)
            self.set_font('helvetica', 'B', 20)
            self.set_text_color(100, 100, 100)
            self.cell(80, 10, 'Run Information', 0, 1, 'L')

        def fancy_table(self, data):
            # Use absolute positioning to place this table
            self.set_y(35)
            self.set_text_color(100, 100, 100)
            self.set_draw_color(169, 169, 169)
            self.set_line_width(0.3)
            self.set_font('helvetica', '', 8)
            cell_width = 40
            cell_height = 7
            start_y = self.get_y()

            for key, value in data.items():
                if key in ['Best Lap Time', 'Avg Lap Time']:
                    self.set_fill_color(211, 211, 211)
                    # Original two-row, two-column formatting
                    self.cell(cell_width, cell_height, key, border="TB", fill=True)
                    self.cell(cell_width, cell_height, '', border="TB", ln=1, fill=True)
                    self.cell(cell_width * 2, cell_height, str(value), border="B", fill=False, ln=1, align='R')
                else:
                    self.set_fill_color(255, 255, 255)
                    self.cell(cell_width, cell_height, key, border="TB", fill=True)
                    self.cell(cell_width, cell_height, str(value), border="TB", ln=1, fill=True, align='R')

            end_y = self.get_y()
            self.rect(10, start_y, 2 * cell_width, end_y - start_y)

        def performance_stats_table(self, perf_details, x, y):
            self.set_xy(x, y)
            self.set_text_color(100, 100, 100)
            cell_width = 40
            cell_height = 7

            self.set_font('helvetica', 'B', 10)
            self.cell(cell_width * 2, cell_height, 'Performance Stats', 0, 1, 'C')
            self.set_font('helvetica', '', 8)

            start_table_y = self.get_y()
            val_counter = 0
            color_counter = [[91, 90, 158], [212, 130, 97], [82, 158, 76]]

            for key, value in perf_details.items():
                self.set_x(x)
                if value == '':
                    self.set_fill_color(220, 220, 220)
                    self.cell(cell_width*2, cell_height, key, border="TB", ln=1, align='C', fill=True)
                elif key == 'Best Sector Times':
                    self.set_fill_color(220, 220, 220)
                    self.cell(cell_width*2, cell_height, key, border="TB", fill=True, ln=1, align='C')
                    for item in value:
                        self.set_x(x)
                        self.set_text_color(*color_counter[val_counter])
                        self.cell(cell_width * 2, cell_height, item, border="B", fill=False, ln=1, align='C')
                        val_counter += 1
                    self.set_text_color(100, 100, 100)
                else:
                    self.set_fill_color(255, 255, 255)
                    self.cell(cell_width, cell_height, key, border="TB", fill=True)
                    self.cell(cell_width, cell_height, str(value), border="TB", ln=1, fill=True, align='R')

            end_table_y = self.get_y()
            self.rect(x, start_table_y, 2 * cell_width, end_table_y - start_table_y)
            return end_table_y

        def add_track_image(self, image_path, x, y, w, h):
            self.image(str(image_path), x=x, y=y, w=w, h=h)
            self.set_xy(x, y + h)
            self.set_text_color(100, 100, 100)
            self.set_font('helvetica', '', 7)
            self.cell(w, 8, 'Track Segments', 0, 1, 'C')

        def add_weather_image(self, image_path, x, y, w, h, weather_info):
            if image_path is not None:
                self.image(str(image_path), x=x, y=y, w=w, h=h)
            # Set XY for text to be placed correctly under the image
            self.set_xy(x, y + h)
            self.set_font('helvetica', '', 7)
            self.set_text_color(130, 130, 130)
            # Use cell width `w` to center the text under the image
            self.cell(w, 4, weather_info[0], 0, 1, 'C')
            self.set_x(x) # Explicitly reset X position for the second line
            self.cell(w, 4, f"{weather_info[1]}°C / {weather_info[2]}°C", 0, 1, 'C')

        def add_dist_related_images(self, speed_vs_dist_path, dist_vs_acc_sma_path):
            image_width = self.w +20
            image_height = 75
            x = -10
            # Position graphs relative to the bottom of the page to avoid overlap
            y1 = self.h - 165
            y2 = y1 + image_height + 5
            self.image(str(speed_vs_dist_path), x=x, y=y1, w=image_width, h=image_height)
            self.image(str(dist_vs_acc_sma_path), x=x, y=y2, w=image_width, h=image_height)

        def add_page_outline(self, margin=4):
            self.set_draw_color(210, 210, 210)
            self.rect(margin, margin, self.w - 2 * margin, self.h - 2 * margin)

        def second_page_header(self, lap_no_str):
            self.set_font('helvetica', 'B', 20)
            self.set_text_color(100, 100, 100)
            self.cell(19, 18, lap_no_str, 0, 1, 'C')

        def timed_stats_table(self, lap_data, lap_no_str):
            self.add_page()
            self.add_page_outline(margin=4)
            self.second_page_header(lap_no_str)
            self.set_font('helvetica', '', 8)
            cell_width = 30
            cell_height = 7
            self.cell(cell_width * 2 + 20, cell_height, 'Timed Stats', 1, 1, 'L', fill=True)
            self.set_fill_color(211, 211, 211)
            lap_details = lap_data[lap_no_str]
            timed_stats = [
                ('Lap Time', lap_details['current_lap_time']),
                ('Sector 1 Time', lap_details['sector1_time']),
                ('Sector 2 Time', lap_details['sector2_time']),
                ('Sector 3 Time', lap_details['sector3_time']),
            ]
            for stat, value in timed_stats:
                self.cell(cell_width + 20, cell_height, stat, 1)
                self.cell(cell_width, cell_height, str(value), 1, 1, 'R')

        def current_lap_table(self, lap_data, start_x, start_y, lap_no_str):
            self.set_xy(start_x + 22, start_y + 25)
            self.set_font('helvetica', '', 8)
            cell_width = 34
            cell_height = 7
            self.cell(cell_width * 2 + 20, cell_height, 'Current Lap Stats', 1, 1, 'L', fill=True)
            self.set_fill_color(211, 211, 211)
            lap_details = lap_data[lap_no_str]['Current Lap Stats']
            for stat, value in lap_details.items():
                self.set_x(start_x + 22)
                self.cell(cell_width + 20, cell_height, stat, 1)
                self.cell(cell_width, cell_height, str(value), 1, 1, 'R')
            return self.get_y()

        def sector_kpis_table(self, lap_data):
            self.set_font('helvetica', '', 8)
            cell_width = 30
            cell_height = 7
            self.set_x(150)
            for lap, details in lap_data.items():
                self.cell(0, cell_height + 2, '', 0, 1, 'L')
                self.set_fill_color(211, 211, 211)
                self.set_font('helvetica', '', 8)
                self.set_x(35)
                self.cell(cell_width + 20, cell_height, 'Sector-wise Stats', 1, 0, 'L', fill=True)
                for sector in details['Sector KPIs'].keys():
                    self.cell(cell_width, cell_height, sector, 1, 0, 'R', 1)
                self.ln()
                for stat in details['Sector KPIs']['Sector 1'].keys():
                    self.set_x(35)
                    self.cell(cell_width + 20, cell_height, stat, 1, align='L')
                    for sector in details['Sector KPIs'].values():
                        self.cell(cell_width, cell_height, str(sector[stat]), 1, align='R')
                    self.ln()

    pdf = PDF()
    pdf.add_page()
    pdf.add_page_outline()

    # --- Draw all elements using absolute positioning ---

    # Right side plots (drawn first to be in the background)
    right_column_x = 120
    start_y = 8 # Move plots above the header
    weather_w, weather_h = 28, 21 # 30% smaller
    track_w, track_h = 25, 25   # 30% smaller

    weather_info_temp = [weather_description, min_temp, max_temp]
    pdf.add_weather_image(weather_img_path, right_column_x, start_y, w=weather_w, h=weather_h, weather_info=weather_info_temp)
    pdf.add_track_image(track_file_path, x=right_column_x + 40, y=start_y, w=track_w, h=track_h)

    # Header text
    pdf.first_page_header()

    # Main table on the left
    pdf.fancy_table(manual_details)

    # Performance Table below the plots on the right
    performance_table_y = start_y + weather_h + 8 + 5
    pdf.performance_stats_table(performance_details, x=right_column_x, y=performance_table_y)

    # --- Bottom of the page ---
    pdf.add_dist_related_images(speed_vs_dist_path, dist_vs_acc_sma_path)


    for lap_num in laps_dict.keys():

        lap_no_str = f'Lap {lap_num}'
        lap_data = {lap_no_str: lap_times_dict[lap_no_str]}


        pdf.timed_stats_table(lap_data, lap_no_str)
        start_x = pdf.get_x() + 80
        start_y = pdf.get_y() - 60
        pdf.current_lap_table(lap_data, start_x, start_y, lap_no_str)

        pdf.sector_kpis_table(lap_data)

        image_width = 160
        image_height = 75
        ggplot_image_x = (pdf.w - image_width) / 2
        ggplot_image_y = pdf.get_y() + 2
        ggplot_file_path = GRAPH_IMAGES_DIR / f'ggplot_ay_vs_ax_smoothed{lap_num}.png'
        pdf.image(str(ggplot_file_path), ggplot_image_x, ggplot_image_y, image_width, image_height)

        image_width = 238
        image_height = 75

        color_encoded_image_x = (pdf.w - image_width) / 2 + 10
        color_encoded_image_y = ggplot_image_y + 80
        color_encoded_plot_path = GRAPH_IMAGES_DIR / f'color_encoded_plot{lap_num}.png'
        pdf.image(str(color_encoded_plot_path), color_encoded_image_x, color_encoded_image_y, image_width, image_height)

    # Save the PDF
    # Compose the output filename with Date and Run number in the title
    output_filename = f"Run_Report_{manual_details['Date']}_Run_{manual_details['Run']}.pdf"
    output_path = converted_folder_path.parent / output_filename
    pdf.output(str(output_path))
    print(f'Run report generated at: {output_path}')
