import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.express as px
import itertools


COLOR_DICT = {
    'Braking': '#E6194B',  # Red
    'Entry': '#F58231',  # Orange
    'Mid': '#FFE119',  # Yellow
    'Exit': '#BFEF45',  # Lime

    'Low Speed': '#BFEF45',  # Lime
    'Medium Speed': '#F58231',  # Orange
    'High Speed': '#E6194B',  # Red
}

COLOR_LIST = [
    '#E6194B',  # Red
    '#F58231',  # Orange
    '#FFE119',  # Yellow
    '#BFEF45',  # Lime
    '#3CB44B',  # Green
    '#42D4F4',  # Cyan
    '#4363D8',  # Blue
    '#911EB4',  # Purple
    '#F032E6',  # Magenta
]

COLOR_KEYS = {
            'n_lap': 'Lap',
            'corner_name': 'Corner',
            'corner_speed': 'Corner Speed'
        }


class PlotCreator:
    """
    Collection of plottin methods.
    """

    @staticmethod
    def create_overlay(df, channels, group_by='n_lap', x_channel='s_m'):
        """
        Method for creating interactive overlay for analysing time series data

        Attributes
        -------
        df: pd.DataFrame
            DataFrame containing data to plot
        channels: list-like
            channels ot plot in overlay
        color_by (optional): str
            channel used for coloring traces, must be either 'n_lap' or 'corner_name' or 'corner_speed'
        x_channel (optional): str 
            x plotting channel
        """

        #prepare data
        df = df.copy()
        df['group'] = (df[group_by] != df[group_by].shift()).cumsum()

        #get color mapping
        if group_by in ['corner_speed','corner_phase']:
            colors = COLOR_DICT
        else:
            colors = dict(zip(df.query(f'{group_by} != "-"')[group_by].unique(), itertools.cycle(COLOR_LIST)))
        
        #create figure
        fig = make_subplots(cols=1, rows=len(channels), shared_xaxes=True, vertical_spacing=0.02)

        group_names = []

        for idx, channel in enumerate(channels):
            for _, group_df in df.groupby('group'):
                group_name=group_df.iloc[0][group_by]
                group_df = group_df.iloc[:-10]
                fig.add_trace(
                    go.Scatter(
                        mode='lines',
                        x=group_df[x_channel],
                        y=group_df[channel],
                        name=str(group_name),
                        legendgroup=str(group_name),
                        showlegend=bool(
                            group_name not in group_names and group_name != '-'
                        ),
                        line=dict(
                            color=colors[group_name] if group_name!='-' else 'lightgrey',
                            width=3
                        ),
                        hovertemplate=f'{COLOR_KEYS.get(group_by,group_by)} {group_name}: %{{y}}<extra></extra>',
                        connectgaps=False
                    ), row=idx+1, col=1
                )

                if group_name != '-':
                    group_names.append(group_name)

            #update layout
            fig.update_xaxes(
                showgrid=True, 
                gridcolor='lightgrey',
                showline=True,
                linecolor='black',
                mirror=True,
                spikemode  = 'across',
                row=idx+1)
            fig.update_yaxes(
                title=channel,
                showgrid=True, 
                gridcolor='lightgrey',
                showline=True,
                linecolor='black',
                zeroline=True,
                zerolinecolor='grey',
                mirror=True,
                row=idx+1)

        fig.update_layout(
            height=len(channels)*250,
            legend=dict(
                title=COLOR_KEYS.get(group_by,group_by)
            ),
            paper_bgcolor='white',
            plot_bgcolor='white',
            hovermode='x unified'
        )

        return fig


    @staticmethod
    def create_single_window_overlay(df, channels, x_channel='s_m'):
        """
        Method for creating interactive overlay for analysing time series data

        Attributes
        -------
        df: pd.DataFrame
            DataFrame containing data to plot
        channels: list-like
            channels ot plot in overlay
        x_channel (optional): str 
            x plotting channel
        """

        #prepare data
        colors = dict(zip(channels, itertools.cycle(COLOR_LIST)))
        
        #create figure
        fig = make_subplots(shared_xaxes=True)

        for channel in channels:
            fig.add_trace(
                go.Scatter(
                    mode='lines',
                    x=df[x_channel],
                    y=df[channel],
                    name=str(channel),
                    legendgroup=str(channel),
                    showlegend=True,
                    line=dict(
                        color=colors[channel],
                        width=3
                    ),
                    hovertemplate=f'{channel}: %{{y}}<extra></extra>',
                    connectgaps=False
                )
            )

        #update layout
        fig.update_xaxes(
            showgrid=True, 
            gridcolor='lightgrey',
            showline=True,
            linecolor='black',
            mirror=True,
            spikemode  = 'across',
        )
        fig.update_yaxes(
            title=channel,
            showgrid=True, 
            gridcolor='lightgrey',
            showline=True,
            linecolor='black',
            zeroline=True,
            zerolinecolor='grey',
            mirror=True,
        )

        fig.update_layout(
            height=500,
            legend=dict(
                title='Channels'
            ),
            paper_bgcolor='white',
            plot_bgcolor='white',
            hovermode='x unified'
        )

        return fig


    @staticmethod
    def create_track_map(df, group_by='corner_name'):
        """
        Method for creating interactive circuit map.

        Attributes
        -------
        df: pd.DataFrame
            DataFrame containing data to plot
        group_by (optional): str
            channel used for coloring traces, must be either 'n_lap' or 'corner_name' or 'corner_speed' or 'corner_phase'
        """

        #prepare data
        df = df.copy()
        df['group'] = (df[group_by] != df[group_by].shift()).cumsum()

        #get color mapping
        if group_by in ['corner_speed','corner_phase']:
            colors = COLOR_DICT
        else:
            colors = dict(zip(df.query(f'{group_by} != "-"')[group_by].unique(), itertools.cycle(COLOR_LIST)))

        #create figure
        fig = go.Figure()

        color_idx = 0

        group_names = []

        for _, group_df in df.groupby('group'):
            group_name=group_df.iloc[0][group_by]
            fig.add_trace(
                go.Scatter(
                    mode='lines',
                    x=group_df['pos_x_m'],
                    y=group_df['pos_y_m'],
                    name=str(group_name),
                    legendgroup=str(group_name),
                    showlegend=bool(
                        group_name not in group_names and group_name != '-'
                    ),
                    line=dict(
                        color=colors.get(group_name, 'lightgrey'),
                        width=5
                    ),
                    customdata=group_df['s_m'],
                    hovertemplate=f'Corner {group_name}: %{{customdata:.0f}}<extra></extra>'
                )
            )

            if group_name != '-':
                fig.add_trace(
                    go.Scatter(
                        mode='text',
                        x=[group_df['pos_x_m'].mean()],
                        y=[group_df['pos_y_m'].mean()],
                        text=str(group_name),
                        name='Corner Labels',
                        legendgroup='Corner Labels',
                        showlegend=False
                    )
                )

                color_idx += 1
                group_names.append(group_name)

        #update layout
        fig.update_layout(
            height=1000, 
            width=1100,
            legend=dict(
                title=COLOR_KEYS.get(group_by,group_by)
            ),
            paper_bgcolor='white',
            plot_bgcolor='white'
        )

        fig.update_xaxes(
            title='',
            showgrid=False, 
            showline=True,
            linecolor='black',
            mirror=True,
            zeroline=False,
            showticklabels=False
        )
        fig.update_yaxes(
            title='',
            showgrid=False, 
            showline=True,
            linecolor='black',
            mirror=True,
            zeroline=False,
            showticklabels=False,
            scaleanchor="x",
            scaleratio=1,
        )

        return fig
    

    @staticmethod
    def create_scatter(df, x_channel, y_channel, group_by, color_channel=None, colorscale='RdBu', cmin=None, cmax=None, cmid=None, equal_axis_scaling=False):
        """
        Method for creating interactive scatter plots.

        Attributes
        -------
        df: pd.DataFrame
            DataFrame containing data to plot
        group_by: str
            channel used for grouping traces, must be either 'n_lap' or 'corner_name' or 'corner_speed' or 'corner_phase'
        x_channel: str 
            x plotting channel
        y_channel: str 
            y plotting channel
        color_channel (optional): str 
            color plotting channel
        """
            
        #prepare data
        df = df.copy()
        df['group'] = (df[group_by] != df[group_by].shift()).cumsum()

        #get color mapping
        if group_by in ['corner_speed','corner_phase']:
            colors = COLOR_DICT
        elif group_by in ['n_lap', 'corner_name']:
            colors = dict(zip(df.query(f'{group_by} != "-"')[group_by].unique(), itertools.cycle(COLOR_LIST)))

        #create figure
        fig = go.Figure()

        group_names = []
        
        for _, group_df in df.groupby('group'):
            group_name=group_df.iloc[0][group_by]

            fig.add_trace(
                go.Scatter(
                    x=group_df[x_channel],
                    y=group_df[y_channel],
                    mode='markers',
                    marker=dict(
                        color=colors.get(group_name, 'lightgrey') if color_channel == None else group_df[color_channel],
                        colorscale=colorscale if color_channel else None,
                        cmin=cmin,
                        cmax=cmax,
                        cmid=cmid,
                        coloraxis='coloraxis',
                        showscale=False
                    ),
                    customdata=None if color_channel == None else group_df[color_channel],
                    hovertemplate=f'{COLOR_KEYS.get(group_by,group_by)} {group_name}<br>x: %{{x}}<br>y: %{{y}}<br>z: %{{customdata}}<extra></extra>',
                    name=str(group_name),
                    legendgroup=str(group_name),
                    showlegend=group_name not in group_names,
                )
            )
            
            group_names.append(group_name)

        #update layout
        fig.update_layout(
            height=1000, 
            width=1200,
            legend=dict(
                title=COLOR_KEYS.get(group_by,group_by),
                x=1.2
                ),
            paper_bgcolor='white',
            plot_bgcolor='white',
            coloraxis = {'colorscale':colorscale}
        )

        fig.update_xaxes(
            title=x_channel,
            showgrid=True, 
            gridcolor='lightgrey',
            showline=True,
            linecolor='black',
            mirror=True,
            zeroline=True,
            zerolinecolor='grey'
        )
        fig.update_yaxes(
            title=y_channel,
            showgrid=True, 
            gridcolor='lightgrey',
            showline=True,
            linecolor='black',
            mirror=True,
            zeroline=True,
            zerolinecolor='grey'
        )

        if equal_axis_scaling:
            fig.update_yaxes(
                scaleanchor="x",
                scaleratio=1,
            )
        
        if color_channel:
            fig.update_coloraxes(
                cmax=cmax, 
                cmin=cmin, 
                cmid=cmid, 
                colorbar_title=color_channel)

        return fig
