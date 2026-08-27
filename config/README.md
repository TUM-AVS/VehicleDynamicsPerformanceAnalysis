# Configuration

This directory contains the default parser, topic, smoothing, and vehicle
configuration used by Vehicle Dynamics Performance Analysis.

## Files

| Path | Purpose |
| --- | --- |
| `parser_config.csv` | Maps source channels to the common signal names and optional unit-conversion factors. |
| `topic_list.csv` | Lists the ROS 2 topics decoded from MCAP input. |
| `filter_config.csv` | Defines smoothing-window lengths in seconds. |
| `vehicle_handler/` | Contains vehicle and tire parameters used by the signal calculations. |
| `plotjuggler_base_layout.xml` | Optional starting layout for viewing converted data in PlotJuggler; it is not loaded by the analyzer. |

## Parser Profiles

`parser_config.csv` begins with `target_name`, followed by pairs of profile and
conversion columns:

```csv
target_name,profile_name_1,conversion,profile_name_2,conversion
time_s,source_timestamp,0.001,Time,
vx_mps,source_vehicle_speed,,v_meas,0.277777778
```

Each non-empty entry in a profile column names a required source channel. A
profile matches only when all of those channels are present in the input. The
first matching profile is selected.

The adjacent `conversion` column contains an optional scalar multiplier. Leave
it empty when the source channel already uses the target unit. For decoded MCAP
data, flattened field names use the form `/topic/field.

## MCAP Topics

`topic_list.csv` contains one ROS 2 topic per line and has no header. Only those
topics are decoded from an MCAP file. Every source topic used by an MCAP parser
profile must therefore be represented in this list.

## Signal Smoothing

`filter_config.csv` has two columns:

```csv
signal_name,window_length_s
curvature,1.0
```

The window length is expressed in seconds and converted to samples using the
measured input frequency. When the named signal is available, the analyzer
writes a centered moving average as `<signal_name>_smoothed`. Smoothing is
applied to the complete log rather than separately per lap, uses
`min_periods=1` at the edges, and can therefore blend samples across a lap
boundary.

## Vehicle Configuration

Every child directory under `vehicle_handler/` represents one selectable
vehicle and must contain both files:

```text
vehicle_handler/<VehicleName>/
|-- tire_config.yaml
`-- vehicle_config.yaml
```

`GenericVehicle` is synthetic and non-calibrated. It is intended only for the
included synthetic example and tests. Do not use it to interpret real vehicle
data or make engineering or safety decisions.

## External Configuration

Keep private or vehicle-specific configuration outside this repository and
select it with environment variables:

```bash
export VDPA_CONFIG_DIR=/absolute/path/to/analysis-config
export VDPA_VEHICLE_CONFIG_DIR=/absolute/path/to/vehicle-configs
```

`VDPA_CONFIG_DIR` must contain `parser_config.csv`, `topic_list.csv`, and
`filter_config.csv`. Each child directory of `VDPA_VEHICLE_CONFIG_DIR` must
contain a complete `vehicle_config.yaml` and `tire_config.yaml` pair.

See the root [README](../README.md#configuration-overrides) for the complete
runtime setup and override documentation.
