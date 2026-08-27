# Synthetic ROS 2 Example

`synthetic_rosbag/synthetic_vehicle.mcap` is a deterministic three-lap vehicle
dataset that can be processed without private ROS message packages. Every signal
uses the standard `std_msgs/msg/Float64` ROS 2 schema (`float64 data`) and CDR
encoding.

The vehicle follows a 60 m radius circuit at 10 Hz. Its speed varies smoothly,
the path coordinate wraps to zero on each lap, and position, body velocities,
accelerations, steering, yaw rate, and individual wheel speeds share one
kinematic model. MCAP log timestamps provide `time_s` through the reader's
`__time` field, so every sparse topic row has a usable interpolation timestamp.

## Topic Contract

All message payloads expose a single `data` field. Consequently, a topic such as
`/synthetic/vx_mps` is flattened to `/synthetic/vx_mps/data`, as listed
in `config/parser_config.csv`. The allowed topics are listed in
`config/topic_list.csv`.
