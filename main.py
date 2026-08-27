"""
Main file for unifying different data sources and postprocessing.

Different data sources log their data with different variable names and units in their output files.
This script unifies the variable names and units. Additionally calculated signals get added to the file.
"""

import logging
import tkinter as tk
from tkinter import messagebox

LOG_LEVEL = logging.DEBUG

# Prompt user with Tkinter
def ask_user_to_generate_report():
    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno(
            "Generate Run Report",
            "Would you like to generate the run report?",
        )
    finally:
        root.destroy()


def confirm_output_replacement(folder_path):
    """Ask before replacing an existing unified output directory."""
    output_path = folder_path / "unified_format"
    if not output_path.exists():
        return False

    root = tk.Tk()
    root.withdraw()
    try:
        confirmed = messagebox.askyesno(
            "Replace Existing Output",
            f"{output_path} already exists. Replace it and all of its contents?",
        )
    finally:
        root.destroy()
    if not confirmed:
        raise RuntimeError("Output replacement cancelled by user.")
    return True


def main():
    """Convert selected data files and optionally launch reporting tools."""
    from src.data_handler import (
        create_directories,
        create_file_objects,
        select_data_folder,
    )
    from src.run_report import run_report_generator

    logging.basicConfig(level=LOG_LEVEL)

    try:
        folder_path = select_data_folder()
    except ValueError:
        logging.info("No data directory selected; exiting.")
        return

    file_path_list = create_file_objects(folder_path)
    if not file_path_list:
        logging.info("No supported data files found; exiting.")
        return

    try:
        overwrite_output = confirm_output_replacement(folder_path)
    except RuntimeError:
        logging.info("Output replacement cancelled; exiting.")
        return

    for data_file in file_path_list:
        data_file.select_vehicle()

    folders_created = False
    s_norm_ref = False
    knn = None
    last_converted_file = None

    for data_file in file_path_list:
        data_file.read()
        if not data_file.read_success:
            continue

        data_file.select_parser()
        if data_file.parser_config_idx is None:
            continue

        data_file.unify_signal_names()
        data_file.resample_and_interpolate()
        data_file.calc_signals()

        # Use the first file with position data as the common distance reference.
        position_columns = {'s_m', 'pos_x_m', 'pos_y_m'}
        if not s_norm_ref and position_columns.issubset(data_file.data.columns):
            knn = data_file.get_s_norm_ref()
            s_norm_ref = True
        elif s_norm_ref:
            data_file.calc_s_norm(knn)

        data_file.lap_slice()
        data_file.smooth_signals()
        data_file.characterise_corners()
        if data_file.cornering_stiffness_estimation_enabled:
            data_file.calc_instantaneous_cornering_stiffness()

        if not folders_created:
            create_directories(folder_path, overwrite=overwrite_output)
            folders_created = True
        data_file.write()
        last_converted_file = data_file

    if last_converted_file is None:
        logging.warning("No files were converted; skipping report and plot tools.")
        return

    converted_folder_path = folder_path / 'unified_format'
    if ask_user_to_generate_report():
        run_report_generator(
            last_converted_file.file_name,
            converted_folder_path,
        )
    else:
        print("Run report generation skipped.")

    last_converted_file.run_plot_creator()


if __name__ == "__main__":
    main()
