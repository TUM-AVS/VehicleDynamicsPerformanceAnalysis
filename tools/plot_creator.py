import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pandas as pd
from plotly.offline import plot as save_plot


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    project_root = str(PROJECT_ROOT)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.cornering_stiffness_estimator import InstantaneousCorneringStiffnessEstimator as icse
from src.plotting_methods import PlotCreator

PLOT_OPTIONS = [
    "Overlay",
    "Track Map",
    "Scatter",
    "CS Evaluator"
]

GROUP_OPTIONS = {
    "Lap": "n_lap",
    "Corner Name": "corner_name",
    "Corner Speed": "corner_speed",
    "Corner Phase": "corner_phase"
}

DEFAULT_OVERLAY_CHANNELS = [
    "v_mps",
    "ax_mps2_smoothed",
    "ay_mps2_smoothed",
    "r_cos_phi_smoothed"
]

COLORSCALE_OPTIONS = [
    "RdBu",
    "RdBu_r",
    "Hot",
    "Hot_r",
    "Viridis",
    "Viridis_r"
]

# Tkinter variables (global)
root = None
input_file_var = None
destination_folder_var = None
plot_option_var = None
color_option_var = None
open_plot_var = None
selected_channels = {}

scatter_x_channel_var = None
scatter_y_channel_var = None
scatter_color_channel_var = None
scatter_colorscale_var = None

gating_mode_var = None
gating_channel_var = None
gating_for_var = None

cs_axle = None
cs_lap = None
cs_location = None

# Tkinter widgets (global)
x_channel_dropdown = None
y_channel_dropdown = None
color_channel_dropdown = None
scrollable_frame = None
gating_channel_dropdown = None
gating_for_dropdown = None
cs_lap_options_dropdown = None
location_input = None
range_label = None

def create_plot(input_file, destination_folder, plot_selection, group_selection, open_plot, selected_channels, cs_axle, cs_lap, cs_location):
    """
    Is executed when 'Create Plot' button is clicked and creates and opens selected plots.
    """
    try:
        df = pd.read_csv(input_file)

        # Apply gating filter if enabled
        if plot_selection == "CS Evaluator":
            if cs_axle == "Front":
                cs_estimator = icse(df.alpha_f_rad, df.fy_f_N, sampling_frequency=1 / np.average(np.diff(df.time_s)))
            elif cs_axle == "Rear":
                cs_estimator = icse(df.alpha_r_rad, df.fy_r_N, sampling_frequency=1 / np.average(np.diff(df.time_s)))
            fig = cs_estimator.create_evaluation_plot(cs_location, df, cs_lap, cs_axle)

        elif gating_mode_var.get() == "On" and gating_channel_var.get() and gating_for_var.get():
            if gating_channel_var.get() == 'Lap':
                df = df[df[GROUP_OPTIONS[gating_channel_var.get()]] == int(gating_for_var.get())]
            else:
                df = df[df[GROUP_OPTIONS[gating_channel_var.get()]] == str(gating_for_var.get())]

        if plot_selection == "Overlay":
            if "v_mps" in selected_channels:
                selected_channels.remove("v_mps")
                selected_channels.insert(0, "v_mps")
            fig = PlotCreator.create_overlay(
                df, channels=selected_channels, group_by=GROUP_OPTIONS[group_selection]
            )

        elif plot_selection == "Track Map":
            fig = PlotCreator.create_track_map(df, group_by=GROUP_OPTIONS[group_selection])

        elif plot_selection == "Scatter":
            x_channel = scatter_x_channel_var.get()
            y_channel = scatter_y_channel_var.get()
            color_channel = scatter_color_channel_var.get()
            color_channel = None if color_channel == "None" else color_channel
            colorscale = scatter_colorscale_var.get()

            if not x_channel or not y_channel:
                messagebox.showerror("Error", "Please select both X and Y channels for Scatter plot.")
                return

            fig = PlotCreator.create_scatter(
                df, x_channel=x_channel, y_channel=y_channel, group_by=GROUP_OPTIONS[group_selection], color_channel=color_channel, colorscale=colorscale
            )

        output_path = Path(destination_folder) / f"{plot_selection}_{Path(input_file).name}.html"
        save_plot(fig, filename=str(output_path), auto_open=open_plot)
    except Exception as e:
        messagebox.showerror("Error", f"An error occurred: {e}")

def browse_file():
    file_path = filedialog.askopenfilename(
        filetypes=[("CSV files", "*.csv")], title="Select a CSV File"
    )
    if file_path:
        input_file_var.set(file_path)
        load_columns(file_path)
        update_cs_lap_options()
        validate_location_input()

def browse_folder():
    folder_path = filedialog.askdirectory(title="Select Destination Folder")
    if folder_path:
        destination_folder_var.set(folder_path)

def load_columns(file_path):
    try:
        df = pd.read_csv(file_path)
        numeric_columns = [col for col in df.columns if pd.api.types.is_numeric_dtype(df[col])]

        scatter_x_channel_var.set("")
        scatter_y_channel_var.set("")
        scatter_color_channel_var.set("None")
        scatter_colorscale_var.set("RdBu")
        x_channel_dropdown["values"] = numeric_columns
        y_channel_dropdown["values"] = numeric_columns
        color_channel_dropdown["values"] = ["None"] + numeric_columns

        gating_channel_var.set("")
        gating_for_var.set("")
        gating_channel_dropdown["values"] = list(GROUP_OPTIONS.keys())

        for widget in scrollable_frame.winfo_children():
            widget.destroy()

        selected_channels.clear()
        for col in df.columns:
            var = tk.BooleanVar(value=(col in DEFAULT_OVERLAY_CHANNELS))
            tk.Checkbutton(
                scrollable_frame, text=col, variable=var, anchor="w", width=40
            ).pack(anchor="center", pady=2)
            selected_channels[col] = var
    except Exception as e:
        messagebox.showerror("Error", f"Unable to read the CSV file: {e}")

def update_gating_for(*args):
    try:
        if input_file_var.get() and gating_channel_var.get():
            df = pd.read_csv(input_file_var.get())
            channel_key = GROUP_OPTIONS[gating_channel_var.get()]
            unique_values = [str(val) for val in df[channel_key].dropna().unique()]
            gating_for_dropdown["values"] = unique_values
            gating_for_var.set("")
    except Exception as e:
        messagebox.showerror("Error", f"Unable to update gating options: {e}")

def update_cs_lap_options(*args):
    try:
        if input_file_var.get():
            df = pd.read_csv(input_file_var.get())
            lap_options = list(df.n_lap.unique())
            cs_lap_options_dropdown["values"] = lap_options
            cs_lap.set(lap_options[0])
    except Exception as e:
        messagebox.showerror("Error", f"Unable to update lap options: {e}")

def validate_location_input(*args):
    """Validates the Location input field based on min and max s_m in the selected lap."""
    try:
        if input_file_var.get() and cs_lap.get():
            df = pd.read_csv(input_file_var.get())
            selected_lap_data = df.query(f"n_lap == {cs_lap.get()}")
            min_s_m, max_s_m = selected_lap_data["s_m"].min(), selected_lap_data["s_m"].max()
            range_label["text"]=f"(Allowed Range: {round(min_s_m, )} - {round(max_s_m, 0)}m)"
    except Exception as e:
        messagebox.showerror("Error", f"Could not validate Location input: {e}")

def get_selected_channels():
    return [name for name, var in selected_channels.items() if var.get()]

def main(input_file=None):
    global root
    global input_file_var, destination_folder_var, plot_option_var, color_option_var, open_plot_var
    global scatter_x_channel_var, scatter_y_channel_var, scatter_color_channel_var, scatter_colorscale_var
    global gating_mode_var, gating_channel_var, gating_for_var
    global cs_axle, cs_lap, cs_location
    global x_channel_dropdown, y_channel_dropdown, color_channel_dropdown, scrollable_frame
    global gating_channel_dropdown, gating_for_dropdown, cs_lap_options_dropdown
    global location_input, range_label

    root = tk.Tk(className="PlotCreator")
    input_file_var = tk.StringVar(master=root)
    destination_folder_var = tk.StringVar(master=root, value=str(Path.cwd()))
    plot_option_var = tk.StringVar(master=root, value=PLOT_OPTIONS[0])
    color_option_var = tk.StringVar(master=root, value=list(GROUP_OPTIONS.keys())[0])
    open_plot_var = tk.BooleanVar(master=root, value=False)

    scatter_x_channel_var = tk.StringVar(master=root)
    scatter_y_channel_var = tk.StringVar(master=root)
    scatter_color_channel_var = tk.StringVar(master=root, value="None")
    scatter_colorscale_var = tk.StringVar(master=root, value="RdBu")

    gating_mode_var = tk.StringVar(master=root, value="Off")
    gating_channel_var = tk.StringVar(master=root)
    gating_for_var = tk.StringVar(master=root)

    cs_axle = tk.StringVar(master=root, value='Front')
    cs_lap = tk.IntVar(master=root)
    cs_location = tk.DoubleVar(master=root)
    selected_channels.clear()

    root.title("Plot Creator")
    root.geometry("650x250")
    root.resizable(False, False)

    icon_image = None
    icon_path = PROJECT_ROOT / 'src' / 'logo_imgs_src' / 'TAM_logo.png'
    if icon_path.is_file():
        try:
            icon_image = tk.PhotoImage(master=root, file=str(icon_path))
            root.iconphoto(False, icon_image)
        except (OSError, tk.TclError):
            pass

    notebook = ttk.Notebook(root)
    notebook.pack(expand=True, fill="both")

    main_page = ttk.Frame(notebook)
    notebook.add(main_page, text="Main Page")

    tk.Label(main_page, text="Input File:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
    tk.Entry(main_page, textvariable=input_file_var, width=50).grid(row=0, column=1, padx=5, pady=5)
    tk.Button(main_page, text="Browse", command=browse_file).grid(row=0, column=2, padx=5, pady=5)

    tk.Label(main_page, text="Destination Folder:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
    tk.Entry(main_page, textvariable=destination_folder_var, width=50).grid(row=1, column=1, padx=5, pady=5)
    tk.Button(main_page, text="Browse", command=browse_folder).grid(row=1, column=2, padx=5, pady=5)

    options_frame = tk.Frame(main_page)
    options_frame.grid(row=2, column=0, columnspan=3, pady=10)

    tk.Label(options_frame, text="Plot:").pack(side="left", padx=5)
    ttk.Combobox(
        options_frame, textvariable=plot_option_var, values=PLOT_OPTIONS, state="readonly", width=20
    ).pack(side="left", padx=5)

    tk.Label(options_frame, text="Group By:").pack(side="left", padx=5)
    ttk.Combobox(
        options_frame, textvariable=color_option_var, values=list(GROUP_OPTIONS.keys()), state="readonly", width=20
    ).pack(side="left", padx=5)

    checkbox_frame = tk.Frame(main_page)
    checkbox_frame.grid(row=3, column=0, columnspan=3, pady=10)
    tk.Checkbutton(checkbox_frame, text="Open Plot", variable=open_plot_var).pack(side="left", padx=10)

    tk.Button(
        main_page,
        text="Save Plot",
        command=lambda: create_plot(
            input_file_var.get(),
            destination_folder_var.get(),
            plot_option_var.get(),
            color_option_var.get(),
            open_plot_var.get(),
            get_selected_channels(),
            cs_axle.get(),
            cs_lap.get(),
            cs_location.get()
        ),
        bg="lightgrey",
    ).grid(row=4, column=0, columnspan=3, pady=10)

    column_page = ttk.Frame(notebook)
    notebook.add(column_page, text="Overlay Channels")

    canvas = tk.Canvas(column_page)
    scrollbar = ttk.Scrollbar(column_page, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)

    scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    scatter_page = ttk.Frame(notebook)
    notebook.add(scatter_page, text="Scatter Channels")

    tk.Label(scatter_page, text="X Channel:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
    x_channel_dropdown = ttk.Combobox(scatter_page, textvariable=scatter_x_channel_var, state="readonly", width=30)
    x_channel_dropdown.grid(row=0, column=1, padx=5, pady=5)

    tk.Label(scatter_page, text="Y Channel:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
    y_channel_dropdown = ttk.Combobox(scatter_page, textvariable=scatter_y_channel_var, state="readonly", width=30)
    y_channel_dropdown.grid(row=1, column=1, padx=5, pady=5)

    tk.Label(scatter_page, text="Color Channel:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
    color_channel_dropdown = ttk.Combobox(scatter_page, textvariable=scatter_color_channel_var, state="readonly", width=30)
    color_channel_dropdown.grid(row=2, column=1, padx=5, pady=5)

    tk.Label(scatter_page, text="Colorscale:").grid(row=3, column=0, padx=5, pady=5, sticky="e")
    colorscale_dropdown = ttk.Combobox(scatter_page, textvariable=scatter_colorscale_var, state="readonly", width=30)
    colorscale_dropdown.grid(row=3, column=1, padx=5, pady=5)
    colorscale_dropdown["values"] = COLORSCALE_OPTIONS

    gating_page = ttk.Frame(notebook)
    notebook.add(gating_page, text="Gating")

    tk.Label(gating_page, text="Gating Mode:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
    ttk.Combobox(gating_page, textvariable=gating_mode_var, values=["On", "Off"], state="readonly", width=30).grid(row=0, column=1, padx=5, pady=5)

    tk.Label(gating_page, text="Gating Channel:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
    gating_channel_dropdown = ttk.Combobox(gating_page, textvariable=gating_channel_var, state="readonly", width=30)
    gating_channel_dropdown.grid(row=1, column=1, padx=5, pady=5)
    gating_channel_dropdown.bind("<<ComboboxSelected>>", update_gating_for)

    tk.Label(gating_page, text="Gate For:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
    gating_for_dropdown = ttk.Combobox(gating_page, textvariable=gating_for_var, state="readonly", width=30)
    gating_for_dropdown.grid(row=2, column=1, padx=5, pady=5)

    cs_eval_page = ttk.Frame(notebook)
    notebook.add(cs_eval_page, text="Cornering Stiffness")

    tk.Label(cs_eval_page, text="Axle:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
    ttk.Combobox(cs_eval_page, textvariable=cs_axle, values=["Front", "Rear"], state="readonly", width=30).grid(row=0, column=1, padx=5, pady=5)

    tk.Label(cs_eval_page, text="Lap:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
    cs_lap_options_dropdown = ttk.Combobox(cs_eval_page, textvariable=cs_lap, state="readonly", width=30)
    cs_lap_options_dropdown.grid(row=1, column=1, padx=5, pady=5)
    cs_lap_options_dropdown.bind("<<ComboboxSelected>>", validate_location_input)
    
    tk.Label(cs_eval_page, text="Location:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
    location_input = ttk.Entry(cs_eval_page, textvariable=cs_location, width=30)
    location_input.grid(row=2, column=1, padx=5, pady=5)
    range_label = tk.Label(cs_eval_page, text="(Allowed Range: )")
    range_label.grid(row=2, column=3, padx=5, pady=5, sticky="e")

    if input_file:
        input_file_var.set(input_file)
        load_columns(input_file)  # Automatically load columns when input file is provided
        update_cs_lap_options()
        validate_location_input()

    root.mainloop()


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else None
    main(input_file)
