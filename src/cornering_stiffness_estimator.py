import multiprocessing
import time
from queue import Empty

import numpy as np
from collections import deque
from tqdm import tqdm
from functools import partial
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from scipy.signal import butter, filtfilt
from scipy.optimize import minimize
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd


class _ProgressBatch:
    """Batch worker progress updates to limit manager-queue overhead."""

    def __init__(self, progress_queue, key, batch_size=64, interval_s=0.1):
        self.progress_queue = progress_queue
        self.key = key
        self.batch_size = batch_size
        self.interval_s = interval_s
        self.pending = 0
        self.last_flush = time.monotonic()

    def advance(self):
        if self.progress_queue is None:
            return

        self.pending += 1
        now = time.monotonic()
        if self.pending >= self.batch_size or now - self.last_flush >= self.interval_s:
            self.flush(now)

    def flush(self, now=None):
        if self.progress_queue is None or self.pending == 0:
            return

        self.progress_queue.put((self.key, self.pending))
        self.pending = 0
        self.last_flush = time.monotonic() if now is None else now


def create_cornering_stiffness_progress_bars(specifications):
    """Create all progress bars in the process that owns the terminal."""
    return {
        key: tqdm(
            total=total,
            desc=description,
            position=position,
            leave=False,
        )
        for key, description, total, position in specifications
    }


def wait_for_futures_with_progress(futures, progress_queue, progress_bars):
    """Update parent-owned bars until all supplied futures finish."""
    def apply_update(update):
        key, amount = update
        bar = progress_bars[key]
        remaining = bar.total - bar.n
        if remaining > 0:
            bar.update(min(amount, remaining))

    while not all(future.done() for future in futures.values()):
        try:
            apply_update(progress_queue.get(timeout=0.1))
        except Empty:
            pass

        while True:
            try:
                apply_update(progress_queue.get_nowait())
            except Empty:
                break

    while True:
        try:
            apply_update(progress_queue.get_nowait())
        except Empty:
            break

    results = {name: future.result() for name, future in futures.items()}
    for bar in progress_bars.values():
        if bar.n < bar.total:
            bar.update(bar.total - bar.n)
    return results


def close_cornering_stiffness_progress_bars(progress_bars):
    """Clear live rows and retain one ordered snapshot of each final bar."""
    bars = tuple(progress_bars.values())
    final_lines = tuple(str(bar) for bar in bars)
    for bar in reversed(bars):
        bar.close()
    if bars:
        for line in final_lines:
            tqdm.write(line, file=bars[0].fp)


class InstantaneousCorneringStiffnessEstimator:
    """
    Estimates instantaneous cornering stiffness by linear fit over variable window.
    """

    def __init__(self, slip_angle, lateral_force, sampling_frequency=100, cut_off_frequency=2, min_slip_angle_interval=0.02, weighting_function_order_for_overall_estimation=1, weighting_function_order_for_section_wise_estimation=4, axle=None, progress_bar_position=0):
        """
        Settings for instantaneous cornering stiffness estimation algorithm.

        For the online approximation, the instantaneous cornering stiffness at each instance is estimated using a left-sided window, which relies on past values.
        The size of this window varies dynamically, ensuring that a minimum slip angle interval is available for the estimation at every instance. 
        The algorithm is consists out of two separate estimation schemes that are combined based on their estimation quality.
            - Window estimation: A least square fit is used to estimate cornering stiffness for the overall window.
            - Section estimation: Each window es split up into sections based on sign changes in the growing rate of the slip angle. For each of those sections a least square fit is performed. Based on the slip angle interval for each section the section wise estimation is calculated using a weighted mean.
            - Overall estimation: Based on R2 (coefficient of determination) of the window estimation both estimation schemes are combined using a weighed mean. 

            
        Input Signals (required):
        - slip_angle: np.array
            tyre slip angle in rad
        - lateral_force: np.array
            lateral axle force in N

        Settings (optional):
        - sampling_frequency: float
            Sampling freuqency of input signals.
        - cut_off_frequency: float
            Cut off frequency for filtering input signals.
        - min_slip_angle_interval: float
            Minimum required slip angle interval for each window.
        - weighting_function_order_for_overall_estimation: int
            Weighting function order that is used to asign weights for overall estimation based on R2 (coefficient of determination) of window estimation.
        - weighting_function_order_for_section_wise_estimation: int
            Weighting function order that is used to asign weights for section estimation based on section slip angle interval.
        - axle: str
            Axle description used as description to follow which estimation process is running when estimating in parallel.
        - progress_bar_position: int
            Position of first progress bar. Can be used for multi processing to not overwrite progress bars of differnet processes.
        """
        # Settings 
        self.sampling_frequency = sampling_frequency
        self.cut_off_frequency = cut_off_frequency
        self.min_slip_angle_interval = min_slip_angle_interval
        self.weighting_function_order_for_final_estimation = weighting_function_order_for_overall_estimation
        self.weighting_function_order_for_section_wise_estimation = weighting_function_order_for_section_wise_estimation
        self.axle = axle
        self.progress_bar_position = progress_bar_position

        # Weighting functions
        self.section_weighting_function = partial(self._weighting_function, lower_bound=0, upper_bound=min_slip_angle_interval, order=weighting_function_order_for_section_wise_estimation)
        self.overall_weighting_function = partial(self._weighting_function, lower_bound=0, upper_bound=1, order=weighting_function_order_for_overall_estimation)

        # Signals
        if len(slip_angle) != len(lateral_force):
            raise ValueError('Input singals do not match.')
        else:
            self.slip_angle = self._low_pass_filter(slip_angle, self.cut_off_frequency, self.sampling_frequency)
            self.lateral_force = self._low_pass_filter(lateral_force, self.cut_off_frequency, self.sampling_frequency)

        # Windows and sections
        self.window_start_indices = self._find_window_start_indices(self.slip_angle, self.min_slip_angle_interval)
        self.segmentation = self._find_segmentation(self.slip_angle)
        self.section_start_end_index_tuples = self._get_section_start_end_index_tuples()


    def _progress_key(self, stage):
        axle = self.axle or "Cornering"
        return f"{axle.lower()}.{stage}"


    def progress_bar_specifications(self):
        """Return parent-rendered progress-bar metadata for this estimator."""
        axle = self.axle or "Cornering"
        return (
            (
                self._progress_key("window"),
                f"{axle} Window Estimation",
                len(self.window_start_indices),
                self.progress_bar_position,
            ),
            (
                self._progress_key("section"),
                f"{axle} Section Estimation",
                len(self.section_start_end_index_tuples),
                self.progress_bar_position + 1,
            ),
            (
                self._progress_key("combine"),
                f"{axle} Combine Sections",
                len(self.window_start_indices),
                self.progress_bar_position + 2,
            ),
        )



    @staticmethod
    def _low_pass_filter(signal, cutoff_freq, sampling_freq):
        """
        Applies a low-pass filter to the input signal.

        Parameters:
        - signal: array-like
            The input signal to be filtered.
        - cutoff_freq: float
            The cutoff frequency of the low-pass filter (Hz).
        - sample_freq: float
            The sampling frequency of the signal (Hz).

        Returns:
        - filtered_signal: array-like
            The filtered signal.
        """
        nyquist_freq = sampling_freq / 2.0
        normalized_cutoff = cutoff_freq / nyquist_freq
        
        # Design a Butterworth low-pass filter
        b, a = butter(N=4, Wn=normalized_cutoff, btype='low', analog=False)
        
        return filtfilt(b, a, signal)
    

    @staticmethod
    def _weighting_function(value, lower_bound, upper_bound, order):
        """
        Point-symmetric weighting function with smooth transition at midpoint.
        """
        midpoint = (lower_bound + upper_bound) / 2
        weights = np.zeros_like(value)

        # Left side of the midpoint
        left_mask = value <= midpoint
        weights[left_mask] = (2*(value[left_mask] - lower_bound) / (upper_bound - lower_bound))**order / 2

        # Right side of the midpoint
        right_mask = value > midpoint
        weights[right_mask] = 1 - (2*(upper_bound - value[right_mask]) / (upper_bound - lower_bound))**order / 2

        return weights


    @staticmethod
    def _weighted_mean(values, weights):
        """
        Computes the weighted mean of values, ignoring rows where NaN occurs in either array.
        """  
        if len(values) > 0:      
            # Create a mask where both arrays are not NaN
            valid_mask = ~np.isnan(values) & ~np.isnan(weights)
            
            # Apply the mask
            filtered_values = values[valid_mask]
            filtered_weights = weights[valid_mask]
            
            # Ensure there are valid weights to avoid division by zero
            total_weight = np.sum(filtered_weights)
            if total_weight == 0:
                return np.nan
            else:
                return np.dot(filtered_values, filtered_weights) / total_weight
        else:
            return np.nan
        

    @staticmethod
    def _get_interval(arr):
        """
        Calculate the interval (range) of a given array.
        """
        return np.max(arr) - np.min(arr)


    @staticmethod
    def _find_window_start_indices(arr, threshold):
        """
        Find the starting indices of windows in the array where the difference between 
        the maximum and minimum values is just below a given threshold.

        This function slides a window across the array and computes the starting index of 
        each window such that the difference between the maximum and minimum value in 
        the window is just less than the specified threshold. The window is dynamically adjusted 
        by maintaining deques for the minimum and maximum values within the window.
        """
        n = len(arr)
        result = np.zeros(n, dtype=np.int32)

        min_deque, max_deque = deque(), deque()
        left = 0  # Left pointer for the sliding window

        for right in range(n):
            # Maintain min deque
            while min_deque and arr[min_deque[-1]] > arr[right]:
                min_deque.pop()
            min_deque.append(right)

            # Maintain max deque
            while max_deque and arr[max_deque[-1]] < arr[right]:
                max_deque.pop()
            max_deque.append(right)

            # Shrink window from the left if interval exceeds threshold
            while arr[max_deque[0]] - arr[min_deque[0]] >= threshold:
                left += 1
                if min_deque[0] < left:
                    min_deque.popleft()
                if max_deque[0] < left:
                    max_deque.popleft()

            result[right] = left  # Store the valid left boundary index

        return result


    @staticmethod
    def _find_segmentation(arr):
        """
        Segment the array based on sign changes in the differences between consecutive elements.

        This function computes the difference between consecutive elements in the input array, 
        determines where the sign changes (from positive to negative or vice versa), and assigns 
        a unique label to each segment based on these sign changes.
        """
        diff = np.diff(arr)
        sign = np.sign(diff)
        sign_change = np.insert(sign[1:] != sign[:-1], 0, False)
        segment_labels = np.cumsum(sign_change, dtype=np.int32)
        return np.concatenate((np.array([0], dtype=np.int32), segment_labels))
    

    @staticmethod
    def find_section_start_indices(arr):
        """
        Finds the index of the first occurence of item in array, which is associated to the start of the section.
        """
        return np.concat([np.array([0]), np.where(np.diff(arr) == 1)[0] + 1])
    

    def _get_section_start_end_index_tuples(self):
        """
        Returns unique start, end index tuples for all relevant subsections.
        """
        start_end_index_tuples = set()
        for window_end_index, window_start_index in enumerate(self.window_start_indices):
            section_start_indices = self.find_section_start_indices(self.segmentation[window_start_index:window_end_index+1]) + window_start_index
            section_end_indices = np.concat([section_start_indices[1:] - 1, np.array([window_end_index])])
            start_end_index_tuples.update(zip(section_start_indices, section_end_indices))

        return list(start_end_index_tuples)
    

    def _compute_slope(self, start_index, end_index):
        """
        Least square method to compute slope.
        """
        if end_index - start_index > 1:
            x_mean = np.mean(self.slip_angle[start_index:end_index+1])
            y_mean = np.mean(self.lateral_force[start_index:end_index+1])
            slope = np.sum((self.slip_angle[start_index:end_index+1] - x_mean) * (self.lateral_force[start_index:end_index+1] - y_mean)) / np.sum((self.slip_angle[start_index:end_index+1] - x_mean) ** 2)
            return slope
        else:
            return np.nan
    

    def _window_cornering_stiffness_estimation(self, progress_queue=None):
        """
        Least square estimation of cornering stiffness for each window.
        """
        slopes = np.full_like(self.slip_angle, np.nan, dtype=np.float64)
        r2_values = np.full_like(self.slip_angle, np.nan, dtype=np.float64)

        def compute_slope_and_r2_vectorized(index):
            """
            Helper function to compute slope and R2 for a given index.
            """
            start = self.window_start_indices[index]
            if index - start > 1:
                end = index + 1
                X = self.slip_angle[start:end]
                Y = self.lateral_force[start:end]

                # Vectorized least squares fit
                A = np.vstack([X, np.ones_like(X)]).T
                m, c = np.linalg.lstsq(A, Y, rcond=None)[0]  # Solve Ax = B

                # Compute R2 in vectorized form
                y_pred = m * X + c
                ss_total = np.sum((Y - np.mean(Y)) ** 2)
                ss_residual = np.sum((Y - y_pred) ** 2)
                r2 = 1 - (ss_residual / ss_total)

                return index, m, r2
            else:
                return index, np.nan, np.nan

        progress = _ProgressBatch(progress_queue, self._progress_key("window"))
        try:
            # Using ThreadPoolExecutor for parallelism
            with ThreadPoolExecutor() as executor:
                # Submit tasks to thread pool
                futures = {executor.submit(compute_slope_and_r2_vectorized, index): index for index in range(len(self.window_start_indices))}

                # Process results as they complete
                for future in as_completed(futures):
                    index, m, r2 = future.result()
                    slopes[index] = m
                    r2_values[index] = r2
                    progress.advance()
        finally:
            progress.flush()

        return slopes, r2_values
    

    def _unique_section_cornering_stiffness_estimation(self, progress_queue=None):
        """
        Least square estimation of cornering stiffness for each unique section.
        """
        slopes = np.empty(len(self.section_start_end_index_tuples), dtype=np.float64)

        def compute_slope_vectorized(index):
            """
            Helper function to compute slope and R2 for a given index.
            """
            start, end = self.section_start_end_index_tuples[index]
            if end - start > 1:
                end = end + 1
                X = self.slip_angle[start:end]
                Y = self.lateral_force[start:end]

                # Vectorized least squares fit
                A = np.vstack([X, np.ones_like(X)]).T
                m, c = np.linalg.lstsq(A, Y, rcond=None)[0]  # Solve Ax = B

                return index, m
            else:
                return index, np.nan

        progress = _ProgressBatch(progress_queue, self._progress_key("section"))
        try:
            # Using ThreadPoolExecutor for parallelism
            with ThreadPoolExecutor() as executor:
                # Submit tasks to thread pool
                futures = {executor.submit(compute_slope_vectorized, index): index for index in range(len(self.section_start_end_index_tuples))}

                # Process results as they complete
                for future in as_completed(futures):
                    index, m = future.result()
                    slopes[index] = m
                    progress.advance()
        finally:
            progress.flush()

        return dict(zip(self.section_start_end_index_tuples, slopes))

        
    def _section_cornering_stiffness_estimation(self, progress_queue=None):
        """
        Combined section wise cornering stiffness estimation per window.
        """
        section_cornering_stiffness_lookup = self._unique_section_cornering_stiffness_estimation(progress_queue)
        slopes = np.full_like(self.slip_angle, np.nan, dtype=np.float64)

        def process_window(window_end_index):
            window_start_index = self.window_start_indices[window_end_index]
            segmentation_slice = self.segmentation[window_start_index:window_end_index + 1]
            
            section_start_indices = self.find_section_start_indices(segmentation_slice) + window_start_index
            section_end_indices = np.append(section_start_indices[1:] - 1, window_end_index)
            
            # Vectorized computation of slip angle intervals
            slip_angle_slices = [self.slip_angle[start:end + 1] for start, end in zip(section_start_indices, section_end_indices)]
            slip_angle_intervals = np.array(list(map(self._get_interval, slip_angle_slices)))
            
            # Vectorized lookup for section slopes
            individual_section_slopes = np.array([section_cornering_stiffness_lookup[(start, end)] for start, end in zip(section_start_indices, section_end_indices)])
            
            # Compute section weights in a vectorized manner
            section_weights = self.section_weighting_function(slip_angle_intervals)
            
            return window_end_index, self._weighted_mean(individual_section_slopes, section_weights)

        progress = _ProgressBatch(progress_queue, self._progress_key("combine"))
        try:
            # Use ThreadPoolExecutor to process windows in parallel
            with ThreadPoolExecutor() as executor:
                # Submit tasks to thread pool
                futures = {executor.submit(process_window, index): index for index in range(len(self.window_start_indices))}

                # Process results as they complete
                for future in as_completed(futures):
                    index, m = future.result()
                    slopes[index] = m
                    progress.advance()
        finally:
            progress.flush()

        return slopes


    def estimate_cornering_stiffness(self, progress_queue=None):
        """
        Full cornering stiffness estimation using multiprocessing
        """
        mp_context = multiprocessing.get_context()

        def submit_estimations(executor, queue):
            return {
                "window": executor.submit(
                    self._window_cornering_stiffness_estimation,
                    queue,
                ),
                "section": executor.submit(
                    self._section_cornering_stiffness_estimation,
                    queue,
                ),
            }

        if progress_queue is None:
            with mp_context.Manager() as manager:
                local_progress_queue = manager.Queue()
                progress_bars = create_cornering_stiffness_progress_bars(
                    self.progress_bar_specifications()
                )
                try:
                    with ProcessPoolExecutor(
                        max_workers=2,
                        mp_context=mp_context,
                    ) as executor:
                        results = wait_for_futures_with_progress(
                            submit_estimations(executor, local_progress_queue),
                            local_progress_queue,
                            progress_bars,
                        )
                finally:
                    close_cornering_stiffness_progress_bars(progress_bars)
        else:
            with ProcessPoolExecutor(
                max_workers=2,
                mp_context=mp_context,
            ) as executor:
                futures = submit_estimations(executor, progress_queue)
                results = {
                    name: future.result() for name, future in futures.items()
                }

        window_cornering_stiffness, window_r2_values = results["window"]
        section_cornering_stiffness = results["section"]

        # Compute weights
        window_weights = self.overall_weighting_function(window_r2_values)

        # Compute final cornering stiffness
        cornering_stiffness = window_weights * window_cornering_stiffness + (1 - window_weights) * section_cornering_stiffness

        return cornering_stiffness
    

    def calculate_cornering_stiffness_ratio(self, cornering_stiffness, linear_slip_threshold=0.021):
        """
        Computes the cornering stiffness ratio, which indicates the saturation level of an axle.  
        A ratio of:
        - **1** signifies the tyre is in the linear region.
        - **0** indicates full saturation at the peak.
        - **< 0** means the tyre is beyond its lateral load peak.

        The linear region is determined using a slip angle threshold. If all slip angles  
        within the estimation window remain below this threshold, the corresponding stiffness  
        value is considered part of the linear region.

        Parameters:
            cornering_stiffness : array-like
                An array of instantaneous cornering stiffness values.
            linear_slip_threshold : float, optional (default=0.021)
                The slip angle threshold defining the linear region.

        Returns:
            cornering_stiffness_ratio : array-like
                The ratio of instantaneous cornering stiffness to the stiffness in the linear region.  
                Values are clipped at a maximum of 1.
        """
        linear_cs = np.empty_like(self.slip_angle)
        previous_cs = np.nan

        for i in range(len(self.slip_angle)):
            if np.max(np.abs(self.slip_angle[self.window_start_indices[i]: i+1])) < linear_slip_threshold:
                linear_cs[i] = cornering_stiffness[i]
                previous_cs = cornering_stiffness[i]
            else:
                linear_cs[i] = previous_cs

        return (cornering_stiffness / linear_cs).clip(upper=1)


    def create_evaluation_plot(self, s_m_location, df, lap_number, axle):
        """
        Plot for evaluating instantaneous cornering stiffness estimation.

        Parameters:
        - s_m_location: float
            Location for evaluating cornering stiffness estimation.
        - df: pd.DataFrame
            Complete unified data.
        - lap_number: int
            Lap number for evaluating cornering stiffness estimation.
        - axle: str
            Axle to evaluate cornering stiffness estimation (Front or Rear).

        Returns:
        - fig: plotly.fig
            Figure for checking correct estimation of cornering stiffness.
        """
        # --- Required Methods --- #
        def lateral_force_model(alpha, B, C, D, E):
            """
            Pacejka magic formula.
            
            Parameters:
                alpha (float or np.array): Slip angle in radians
                B (float): Stiffness factor
                C (float): Shape factor
                D (float): Peak factor
                E (float): Curvature factor
                

            Returns:
                Fy (float): lateral tyre force
            """
            Fy = D * np.sin(C * np.arctan(B * alpha - E * (B * alpha - np.arctan(B * alpha))))
            return Fy

        def cornering_stiffness_model(alpha, B, C, D, E):
            """Cornering stiffness as derivatvie of Pacejka magic formula."""
            theta = B * alpha - E * (B * alpha - np.arctan(B * alpha))
            d_theta_d_alpha = B * (1 - E) + (E * B) / (1 + (B * alpha) ** 2)

            return D * C * np.cos(C * np.arctan(theta)) * d_theta_d_alpha / (1 + theta ** 2)


        def get_cornering_stiffness_model(alpha, fy):

            def objective(params):
                distances = lateral_force_model(alpha=alpha, B=params[0], C=params[1], D=params[2], E=params[3]) - fy
                return np.dot(distances, distances)
            
            result = minimize(objective, x0=[12,1.9,8000,0.97], method='Powell')
            param = result.x

            return partial(lateral_force_model, B=param[0], C=param[1], D=param[2], E=param[3]), partial(cornering_stiffness_model, B=param[0], C=param[1], D=param[2], E=param[3])

        def find_closest_index(df, column_name, target_value):
            closest_index = (df[column_name] - target_value).abs().idxmin()
            return closest_index

        def get_yintercept(slope, x, y):
            return y - slope * x

        def find_corner_start_and_end_index(df, index):
            current_corner = df.loc[index].corner_name
            # Start
            for idx in range(index, df.head(1).index[0]-1, -1):
                if df.loc[idx].corner_name != current_corner:
                    start_index = idx + 1
                    break
            else:
                start_index = idx
            # End
            for idx in range(index, df.tail(1).index[0]+1):
                if df.loc[idx].corner_name != current_corner:
                    end_index = idx - 1
                    break
            else:
                end_index = idx

            return start_index, end_index

        def axes_base_layout(fig, row, col, x_title=None, y_title=None):

            fig.update_xaxes(
                title=x_title,
                showline=True,
                linecolor='black',
                mirror=True,
                showgrid=True,
                gridcolor='lightgrey',
                zeroline=True,
                zerolinecolor='grey',
                row=row, col=col)
            
            fig.update_yaxes(
                title=y_title,
                showline=True,
                linecolor='black',
                mirror=True,
                showgrid=True,
                gridcolor='lightgrey',
                zeroline=True,
                zerolinecolor='grey',
                row=row, col=col)


        # --- Data Processing --- #
        n = len(df)

        # Masks
        lap_start_index = df.query(f'n_lap == {lap_number}').index[0]
        lap_end_index = df.query(f'n_lap == {lap_number}').index[-1]
        lap_mask = np.zeros(n, dtype=bool)
        lap_mask[lap_start_index:lap_end_index+1] = True

        index = find_closest_index(df[lap_mask], 's_m', s_m_location)

        current_corner = df.loc[index].corner_name if df.loc[index].corner_name.startswith('T') else 'Straight'
        corner_start_index, corner_end_index = find_corner_start_and_end_index(df, index)
        corner_mask = np.zeros(n, dtype=bool)
        corner_mask[corner_start_index:corner_end_index+1] = True

        window_mask = np.zeros(n, dtype=bool)
        window_mask[self.window_start_indices[index]:index+1] = True

        if axle == 'Front':
            cs_channel = 'cs_f_Nprad'
            fy_channel = 'fy_f_N'
            alpha_channel = 'alpha_f_rad'

        elif axle == 'Rear':
            cs_channel = 'cs_r_Nprad'
            fy_channel = 'fy_r_N'
            alpha_channel = 'alpha_r_rad'

        else:
            raise ValueError("axle must be either 'Front' or 'Rear'")
        
        # Cornering Stiffness
        fy_model, cs_model = get_cornering_stiffness_model(df[lap_mask][alpha_channel], df[lap_mask][fy_channel])
        equally_spaced_slip_angle = np.linspace(np.min(self.slip_angle[lap_mask]), np.max(self.slip_angle[lap_mask]), 100)

        # --- Plotting --- #
        fig = make_subplots(
            rows=3, cols=2,
            specs=[
                [{'colspan': 2}, None],
                [{'colspan': 2}, None],
                [{}, {}]
            ],
            column_widths=[1,4],
            row_heights=[1,1,4]
        )

        # vCar Overlay (1)
        fig.add_trace(
            go.Scatter(
                mode='lines',
                x=df[lap_mask].s_m,
                y=df[lap_mask].v_mps,
                showlegend=False,
                line=dict(
                    color='grey'
                ),
                hovertemplate='Distance: %{x:.0f}m<br>Velocity: %{y:.2f}mps<extra></extra>',
                name='velocity'
            ), row=1, col=1
        )

        fig.add_vline(
            x=df.loc[index].s_m,
            line_dash='dash',
            line_color='red',
            row=1,
            col=1
        )

        axes_base_layout(fig, 1, 1, y_title='Velocity (mps)')


        # Instantanous cornering stiffness overlay (2)
        fig.add_trace(
            go.Scatter(
                mode='lines',
                x=df[lap_mask].s_m,
                y=df[lap_mask][cs_channel],
                showlegend=True,
                name='Online Estimation',
                legendgroup='Online Estimation',
                hovertemplate='Distance: %{x:.0f}m<br>Cornering Stiffness: %{y}N/rad',
                line=dict(
                    color='blue'
                )
            ), row=2, col=1
        )

        fig.add_trace(
            go.Scatter(
                mode='lines',
                x=df[lap_mask].s_m,
                y=cs_model(self.slip_angle[lap_mask]),
                showlegend=True,
                name='Reference Model',
                legendgroup='Reference Model',
                hovertemplate='Distance: %{x:.0f}m<br>Cornering Stiffness: %{y}N/rad',
                line=dict(
                    color='green'
                )
            ), row=2, col=1
        )

        fig.add_vline(
            x=df.loc[index].s_m,
            line_dash='dash',
            line_color='red',
            row=2,
            col=1
        )

        axes_base_layout(fig, 2, 1, y_title=f'{axle} CS (F/rad)')
        fig.update_xaxes(row=2, col=1, matches='x')


        # Track Map (3)
        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=df[lap_mask].pos_x_m,
                y=df[lap_mask].pos_y_m,
                showlegend=False,
                marker=dict(
                    color='lightgrey',
                    size=2
                ),
                customdata=df[lap_mask].s_m,
                name='Track',
                hovertemplate='Distance: %{customdata:.0f}m'
            ), row=3, col=1
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=df[corner_mask].pos_x_m,
                y=df[corner_mask].pos_y_m,
                showlegend=False,
                marker=dict(
                    color='grey',
                    size=3
                ),
                customdata=df[corner_mask].s_m,
                name=current_corner,
                hovertemplate='Distance: %{customdata:.0f}m'
            ), row=3, col=1
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=df[window_mask].pos_x_m,
                y=df[window_mask].pos_y_m,
                showlegend=False,
                marker=dict(
                    color='black',
                    size=3
                ),
                customdata=df[window_mask].s_m,
                name='Estimation Window',
                hovertemplate='Distance: %{customdata:.0f}m'
            ), row=3, col=1
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=[df.loc[index].pos_x_m],
                y=[df.loc[index].pos_y_m],
                showlegend=False,
                marker=dict(
                    color='red',
                    size=5
                ),
                customdata=[df.loc[index].s_m],
                name='Selected Location',
                hovertemplate='Distance: %{customdata:.0f}m'
            ), row=3, col=1
        )

        fig.update_xaxes(
            showticklabels=False,
            row=3, col=1)

        fig.update_yaxes(
            showticklabels=False,
            scaleanchor='x3', 
            scaleratio=1,
            row=3, col=1)


        # Scatter Plot (4)
        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=self.slip_angle[lap_mask],
                y=self.lateral_force[lap_mask],
                showlegend=False,
                marker=dict(
                    color='lightgrey',
                    size=2
                ),
                hoverinfo='skip'
            ), row=3, col=2
        )

        fig.add_trace(
            go.Scatter(
                mode='lines',
                x=equally_spaced_slip_angle,
                y=fy_model(equally_spaced_slip_angle),
                showlegend=False,
                line=dict(
                    color='green',
                    width=2
                ),
                legendgroup='Reference Model',
                name='Reference Model',
                hoverinfo='skip'
            ), row=3, col=2
        )

        fig.add_trace(
            go.Scatter(
                mode='lines',
                x=equally_spaced_slip_angle,
                y=df.loc[index][cs_channel] * equally_spaced_slip_angle + get_yintercept(df.loc[index][cs_channel], self.slip_angle[index], self.lateral_force[index]),
                showlegend=False,
                line=dict(
                    color='blue',
                    width=2
                ),
                legendgroup='Online Estimation',
                name='Online Estimation',
                hoverinfo='skip'
            ), row=3, col=2
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=self.slip_angle[corner_mask],
                y=self.lateral_force[corner_mask],
                showlegend=True,
                name=current_corner,
                marker=dict(
                    color='grey',
                    size=4
                ),
                customdata=df[corner_mask].s_m,
                hovertemplate='Distance: %{customdata:.0f}m<br>Slip Angle: %{x:.4f}rad<br>Lateral Force: %{y:.1f}N'
            ), row=3, col=2
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=self.slip_angle[window_mask],
                y=self.lateral_force[window_mask],
                showlegend=True,
                name='Estimation Window',
                marker=dict(
                    color='black',
                    size=4
                ),
                customdata=df[window_mask].s_m,
                hovertemplate='Distance: %{customdata:.0f}m<br>Slip Angle: %{x:.4f}rad<br>Lateral Force: %{y:.1f}N'
            ), row=3, col=2
        )

        fig.add_trace(
            go.Scatter(
                mode='markers',
                x=self.slip_angle[[index]],
                y=self.lateral_force[[index]],
                showlegend=True,
                marker=dict(
                    color='red',
                    size=8
                ),
                name='Selected Location',
                customdata=[df.loc[index].s_m],
                hovertemplate='Distance: %{customdata:.0f}m<br>Slip Angle: %{x:.4f}rad<br>Lateral Force: %{y:.1f}N'
            ), row=3, col=2
        )

        axes_base_layout(fig, 3, 2, x_title=f'{axle} Slip Angle (rad)', y_title=f'{axle} Lateral Axle Force (N)')


        # Overal Layout
        fig.update_layout(
                height=1200,
                width=1300,
                plot_bgcolor='white',
                title=dict(text=f'<b>{axle} Cornering Stiffness Estimation', x=0.5, font_size=22)
            )


        return fig
