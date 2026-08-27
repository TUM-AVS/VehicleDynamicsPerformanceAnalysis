import pandas as pd
import numpy as np
from scipy.signal import find_peaks
import warnings
import logging



class CornerCharacterisation:
    """
    Class containing methods for characterising corners in dataset containing track or simulation data of a race car.
    """

    def __init__(self, data, n_lap_channel, s_coord_channel, curvature_channel, lat_accel_channel, velocity_channel, r_cos_phi_channel):
        """
        Class for charactrising corners in dataset. For initialising data and required channel names need to be passed.

        Attributes
        ------
        data: pandas DataFrame
            DataFrame containing track or simulation data of race car.
        curvature_channel: str
            curvature channel for apex detection
        lat_accel_channel: str
            lateral acceleration channel for corner start and end detection
        s_coord_channel: str
            s coordinate channel distance checks between characteristic points
        velocity_channel: str
            velocity channel for corner speed detection
        n_lap_channel: str
            n lap channel for individual corner characterisation per lap
        r_cos_phi_channel: str
            rCosPhi channel for corner phase characterisation
        """
        self.data = data

        self.n_lap_channel = n_lap_channel
        self.s_coord_channel = s_coord_channel 
        self.curvature_channel = curvature_channel
        self.lat_accel_channel = lat_accel_channel
        self.velocity_channel = velocity_channel
        self.r_cos_phi_channel = r_cos_phi_channel


    @classmethod
    def from_default_channels(cls, data):
        """
        Initialises class by checking if default channels exist in dataset.
        """
        default_channels = {
            'n_lap_channel': 'n_lap',
            's_coord_channel': 's_m', 
            'curvature_channel': 'curvature_smoothed', 
            'lat_accel_channel': 'ay_mps2_smoothed', 
            'velocity_channel': 'v_mps', 
            'r_cos_phi_channel': 'r_cos_phi_smoothed'
        }

        missing_channels = [channel for channel in default_channels.values() if channel not in data.columns]

        if len(missing_channels) > 0:
            raise ValueError(f'Following channels are missing in dataset to execute Corner Characterisation: {missing_channels}')
        else:
            return cls(data, **default_channels)
        


    def characterise_corners(self):
        """
        This algorithm characterises corners in the provided dataset. Corners are identified using characteristic points derived from the following signals:

        - curvature
        - longitudinal velocity
        - lateral acceleration
        - s coordinate
        - rCosPhi

        The method adds the following corner-related channels to the input dataframe:

        - **corner_name**:  
        Corners are labeled sequentially in ascending order as they appear along the track, starting from 1 (e.g., T1, T2, ...).

        - **corner_speed**:  
        Corners are classified based on their apex speed into three categories: Low Speed, Medium Speed, and High Speed.

        - **corner_phase**:  
        Each corner is divided into distinct phases based on rCosPhi: Braking, Entry, Mid, and Exit.
        """
        # Copy required channels from input dataset and create default corner channels
        df = self.data[self._get_required_channels()].copy()
        final_df = pd.DataFrame()

        # Process each lap individually
        for _, lap_df in df.groupby(self.n_lap_channel):

            # Corner characterisation is build around apex detection
            apices = self._find_apices(lap_df)

            # Only exectute corner characterisation if at least one apex was detected
            if len(apices) > 0:

                # Find characteristic points for constructing corners
                decel_points = self._find_deceleration_points(lap_df)
                lat_accel_change_points = self._find_lat_accel_sign_changes(lap_df)
                low_lat_accel_points = self._find_low_lat_accel_points(lap_df)

                # Construct DataFrame containing all characteristic points for detecting corners
                characteristic_points = self._construct_characteristic_points(
                    apices, decel_points, lat_accel_change_points, low_lat_accel_points
                )

                # Create corner info DataFrame containing information about each corner
                corner_info = self._create_corner_info(characteristic_points, lap_df)

                # Add corner name and corner speed channels
                lap_df['corner_name'] = self._create_corner_channel(corner_info, 'corner_name', len(lap_df))
                lap_df['corner_speed'] = self._create_corner_channel(corner_info, 'corner_speed', len(lap_df))

                # Add corner phases
                lap_df['corner_phase'] = '-'
                for corner, corner_df in lap_df.query('corner_name != "-"').groupby('corner_name'):
                    lap_df.loc[lap_df['corner_name'] == corner, 'corner_phase'] = self._create_corner_phase_channel(corner_df)

            else:
                # Set corner channels to default if no corners were detected
                lap_df.loc[:,['corner_name','corner_speed','corner_phase']] = ['-','-','-']

            final_df = pd.concat([final_df, lap_df[['corner_name','corner_speed','corner_phase']]])

        # Add corner channels to input dataset
        self.data[['corner_name','corner_speed','corner_phase']] = final_df[['corner_name','corner_speed','corner_phase']]

        logging.info('Corner Characterisation ran successfully.')
        

    def _create_corner_phase_channel(self, corner_df, entry=-0.97, mid=-0.3, exit=0.1):
        """
        Corner phase characterisation based on rCosPhi.
        """

        def find_first_occurence(seq, val, start_point):
            hits = np.where(seq.iloc[start_point:] < val)[0]
            if len(hits) > 0: 
                return hits[-1]
            else:
                return 0

        corner_df = corner_df.copy().reset_index(drop=True)     
        
        entry_start = find_first_occurence(corner_df[self.r_cos_phi_channel], entry, 0)
        mid_start = find_first_occurence(corner_df[self.r_cos_phi_channel], mid, entry_start) + entry_start
        exit_start = find_first_occurence(corner_df[self.r_cos_phi_channel], exit, mid_start) + mid_start

        n_phase = np.zeros(len(corner_df))

        n_phase[[entry_start,mid_start,exit_start]] += 1
        n_phase = n_phase.cumsum()

        phase_mapping = {
            0: 'Braking',
            1: 'Entry',
            2: 'Mid',
            3: 'Exit'
        }

        phase_channel = np.vectorize(phase_mapping.get)(n_phase)
                
        return phase_channel
        

    def _create_corner_channel(self, corner_info, channel_name, length):
        """
        Construct corner channel based on corner start and end points.
        """
        corner_name_channel = ['-'] * length

        for _, single_corner_info in corner_info.iterrows():
            point = single_corner_info['start_point']

            while True:
                corner_name_channel[point] = single_corner_info[channel_name]
                point = (point+1) % length
                if point == single_corner_info['end_point']:
                    break

        return corner_name_channel

    
    def _create_corner_info(self, characteristic_points, df):
        """
        Each corner's start and end point is defined based on characteristic points.
        """
        corner_info = pd.DataFrame()

        corner_info['corner_name'] = [f'T{corner_number}' for corner_number in range(1,len(characteristic_points.query('type == "apex"'))+1)]

        corner_info['apex_index'] = characteristic_points.query('type == "apex"').index
        corner_info['start_index'] = corner_info.apply(lambda corner_data: self._search_corner_start(characteristic_points, corner_data['apex_index'], corner_data['apex_index']), axis=1)
        corner_info['end_index'] = corner_info.apply(lambda corner_data: self._search_corner_end(characteristic_points, corner_data['apex_index'], corner_data['apex_index']), axis=1)

        corner_info['apex_point'] = corner_info['apex_index'].apply(lambda index: int(characteristic_points.iloc[index]['point'].mean())).values
        corner_info['start_point'] = corner_info['start_index'].apply(lambda index: int(characteristic_points.iloc[index]['point'].mean())).values
        corner_info['end_point'] = corner_info['end_index'].apply(lambda index: int(characteristic_points.iloc[index]['point'].mean())).values

        corner_info['corner_speed'] = corner_info.apply(lambda corner_data: self._get_corner_speed(df.iloc[corner_data['apex_point']][self.velocity_channel]), axis=1)

        return corner_info
    

    def _get_corner_speed(self, vx, ms_threshold=25, hs_threshold=40):
        if vx < ms_threshold:
            return 'Low Speed'
        elif vx < hs_threshold:
            return 'Medium Speed'
        else: 
            return 'High Speed'


    def _construct_characteristic_points(self, apices, decel_points, lat_accel_change_points, low_lat_accel_points):
        """
        Construct single DataFrame containing all charactristic points for corner characterisation.
        """
        apex_df = pd.DataFrame()
        apex_df['point'] = apices
        apex_df['type'] = 'apex'

        decel_df = pd.DataFrame()
        decel_df['point'] = decel_points
        decel_df['type'] = 'deceleration'

        lat_accel_df = pd.DataFrame()
        lat_accel_df['point'] = lat_accel_change_points
        lat_accel_df['type'] = 'lat_accel_change'

        low_accel_df = pd.DataFrame()
        low_accel_df['point'] = low_lat_accel_points
        low_accel_df['type'] = 'low_lat_accel'

        characteristic_points = pd.concat([apex_df, decel_df, lat_accel_df, low_accel_df])
        characteristic_points = characteristic_points.sort_values('point').reset_index(drop=True)
        characteristic_points['point'] = characteristic_points['point'].astype(int)

        #remove low lateral acceleration points if they are close to lateral acceleration change points
        mask = characteristic_points.apply(
            lambda row: 
                not (row['type'] == 'low_lat_accel' and 
                any(abs(row['point'] - lat_point) <= 30 for lat_point in lat_accel_change_points)
        ),axis=1)
        characteristic_points = characteristic_points[mask].reset_index(drop=True)

        return characteristic_points


    def _get_required_channels(self):
        required_channels = [
            self.n_lap_channel, 
            self.s_coord_channel, 
            self.curvature_channel, 
            self.lat_accel_channel, 
            self.velocity_channel, 
            self.r_cos_phi_channel
        ]
        return required_channels


    def _find_apices(self, df):
        """
        Method for detecting apices as points with locally max abs curvature.
        """
        potential_apex_indices, _ = find_peaks(abs(df[self.curvature_channel]), distance=100, height=0.002, width=50, prominence=0.001)
        apex_indices = potential_apex_indices[df[self.lat_accel_channel].iloc[potential_apex_indices].abs() > 5]

        return apex_indices
    

    def _find_lat_accel_sign_changes(self, df, min_distance=50):
        """
        Detect points where sign of lateral acceleration changes curvature.
        """
        #first look for all points where sign flips in lateral accel signal
        potential_acceleration_changes = np.where(np.diff(np.sign(df[self.lat_accel_channel])))[0]

        if len(potential_acceleration_changes) == 0:
            return []

        #second group points that are close together (sign changes due to noise)
        accel_change_groups = []

        accel_change_group = [potential_acceleration_changes[0]]
        
        for idx in range(len(potential_acceleration_changes) - 1):
            if (df[self.s_coord_channel].iloc[potential_acceleration_changes[idx+1]] - df[self.s_coord_channel].iloc[potential_acceleration_changes[idx]]) < min_distance:
                accel_change_group.append(int(potential_acceleration_changes[idx+1]))
            else:
                accel_change_groups.append(accel_change_group)
                accel_change_group = [int(potential_acceleration_changes[idx+1])]
        accel_change_groups.append(accel_change_group)

        return [int(np.median(accel_change_group)) for accel_change_group in accel_change_groups]
    

    def _find_low_lat_accel_points(self, df, min_distance=20, accel_threshold=2.5):
        """
        Detect points where absolute lateral acceleration goes below threshold.
        """
        #first look for all points that satisfy low lateral accel criteria
        potential_low_acceleration1 = np.where(np.diff(np.sign(df[self.lat_accel_channel]-accel_threshold)) & (df[self.curvature_channel].iloc[1:].abs() < 0.002))[0]
        potential_low_acceleration2 = np.where(np.diff(np.sign(df[self.lat_accel_channel]+accel_threshold)) & (df[self.curvature_channel].iloc[1:].abs() < 0.002))[0]
        potential_low_acceleration = np.sort(np.concat([potential_low_acceleration1,potential_low_acceleration2]))
        
        #second group points that are close together (due to noise)
        low_accel_groups = []

        if len(potential_low_acceleration) > 0:
            low_accel_group = [potential_low_acceleration[0]]
            
            for idx in range(len(potential_low_acceleration) - 1):
                if (df[self.s_coord_channel].iloc[potential_low_acceleration[idx+1]] - df[self.s_coord_channel].iloc[potential_low_acceleration[idx]]) < min_distance:
                    low_accel_group.append(int(potential_low_acceleration[idx+1]))
                else:
                    low_accel_groups.append(low_accel_group)
                    low_accel_group = [int(potential_low_acceleration[idx+1])]
            low_accel_groups.append(low_accel_group)

            return [int(np.median(low_accel_group)) for low_accel_group in low_accel_groups]
        
        else:
            return []
        

    def _find_deceleration_points(self, df, min_distance=50):
        """
        Detect points where longitudinal acceleration goes below threshold.
        """
        is_at_threshold = np.gradient(np.sign(df[self.r_cos_phi_channel]+0.15)) != 0
        has_negative_gradient = pd.Series(np.gradient(df[self.r_cos_phi_channel]))[::-1].rolling(20, min_periods=1).quantile(0.75)[::-1] < 0
        potential_deceleration = np.where(is_at_threshold & has_negative_gradient)[0]

        #second group points that are close together (due to noise)
        deceleration_groups = []

        if len(potential_deceleration) > 0:
            decel_group = [potential_deceleration[0]]
            
            for idx in range(len(potential_deceleration) - 1):
                if (df[self.s_coord_channel].iloc[potential_deceleration[idx+1]] - df[self.s_coord_channel].iloc[potential_deceleration[idx]]) < min_distance:
                    decel_group.append(int(potential_deceleration[idx+1]))
                else:
                    deceleration_groups.append(decel_group)
                    decel_group = [int(potential_deceleration[idx+1])]
            deceleration_groups.append(decel_group)

            return [decel_group[-1] for decel_group in deceleration_groups]
        
        else:
            return []
    

    def _search_corner_start(self, characteristic_points, index, apex_index, accel_index=None):
        """
        Start of corner is found by starting from corner apex and checking previous characteristic points.
        """
        preceding_index = index-1
        preceding_type = characteristic_points.iloc[preceding_index]['type']
        
        if preceding_type == 'deceleration':
            return [preceding_index]
        elif preceding_type in ['lat_accel_change','low_lat_accel']:
            potential_start = preceding_index if accel_index == None else accel_index
            return self._search_corner_start(characteristic_points, preceding_index, apex_index, potential_start)
        elif preceding_type == 'apex':
            if accel_index != None:
                return [accel_index]
            else:
                
                return [index, apex_index]

 
    def _search_corner_end(self, characteristic_points, index, apex_index):
        """
        End of corner is found by starting from corner apex and checking following characteristic points.
        """
        proceding_index = index+1
        if proceding_index > len(characteristic_points)-1:
            proceding_index = 0

        proceding_type = characteristic_points.iloc[proceding_index]['type']
        if proceding_type == 'apex':
            return [index, apex_index]
        else:
            return [proceding_index]
