import math
import unittest
from pathlib import Path

import pandas as pd
from mcap.reader import make_reader
from mcap_ros2.decoder import DecoderFactory

from src.mcap_reader import (
    ChunkDescriptor,
    _group_chunks,
    flatten_ros_msg,
    read_mcap,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_MCAP = (
    PROJECT_ROOT / "examples" / "synthetic_rosbag" / "synthetic_vehicle.mcap"
)


def legacy_flatten_ros_msg(msg, base_path):
    stack = [(base_path, msg, 0)]
    flat_dict = {}
    while stack:
        current_path, current_msg, depth = stack.pop()
        if depth > 6:
            continue
        if isinstance(current_msg, (int, float)):
            flat_dict[current_path] = current_msg
        elif isinstance(current_msg, (list, tuple)):
            if len(current_msg) > 1000:
                continue
            for idx, item in enumerate(current_msg):
                stack.append((f"{current_path}/{idx}", item, depth + 1))
        elif all(hasattr(current_msg, attr) for attr in ("x", "y", "z", "w")):
            x = current_msg.x
            y = current_msg.y
            z = current_msg.z
            w = current_msg.w
            flat_dict[f"{current_path}/x"] = x
            flat_dict[f"{current_path}/y"] = y
            flat_dict[f"{current_path}/z"] = z
            flat_dict[f"{current_path}/w"] = w

            norm = math.sqrt(x * x + y * y + z * z + w * w)
            x /= norm
            y /= norm
            z /= norm
            w /= norm
            flat_dict[f"{current_path}/roll"] = math.atan2(
                2.0 * (w * x + y * z),
                1.0 - 2.0 * (x * x + y * y),
            )
            pitch_term = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
            flat_dict[f"{current_path}/pitch"] = math.asin(pitch_term)
            flat_dict[f"{current_path}/yaw"] = math.atan2(
                2.0 * (w * z + x * y),
                1.0 - 2.0 * (y * y + z * z),
            )
        else:
            attributes = [
                attr
                for attr in dir(current_msg)
                if not attr.startswith("_")
                and not callable(getattr(current_msg, attr))
                and attr.lower() != "covariance"
            ]
            for attr in attributes:
                if attr in ("timestamp", "stamp"):
                    continue
                stack.append(
                    (f"{current_path}/{attr}", getattr(current_msg, attr), depth + 1)
                )
    return flat_dict


def legacy_extract(file_path, topics):
    dataframes = []
    batch = []
    factory = DecoderFactory()
    with file_path.open("rb") as stream:
        reader = make_reader(stream)
        for schema, channel, message in reader.iter_messages(topics=topics):
            decoder = factory.decoder_for(channel.message_encoding, schema)
            if decoder is None:
                continue
            values = legacy_flatten_ros_msg(decoder(message.data), channel.topic)
            values["__time"] = float(message.log_time) / 1e9
            batch.append(values)
            if len(batch) == 5000:
                dataframes.append(
                    pd.DataFrame(batch).astype(pd.SparseDtype("float", float("nan")))
                )
                batch = []
    if batch:
        dataframes.append(
            pd.DataFrame(batch).astype(pd.SparseDtype("float", float("nan")))
        )

    data = pd.concat(dataframes, ignore_index=True, sort=True)
    columns = data.columns.tolist()
    columns.insert(0, columns.pop(columns.index("__time")))
    return data[columns].sort_values("__time").reset_index(drop=True)


class DynamicMessage:
    __slots__ = ("covariance", "nested", "stamp", "value")

    def __init__(self):
        self.covariance = 99.0
        self.nested: list[object] = [1.0, 2.0]
        self.stamp = 123
        self.value = 3.0


class Quaternion:
    __slots__ = ("w", "x", "y", "z")

    def __init__(self):
        self.w = 1.0
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0


class McapReaderTests(unittest.TestCase):
    def test_chunk_groups_split_only_at_non_overlapping_time_ranges(self):
        def descriptor(start, end):
            return ChunkDescriptor(
                start_offset=start,
                chunk_length=1,
                message_index_start=0,
                message_index_length=0,
                uncompressed_size=20 * 1024 * 1024,
                has_message_indexes=False,
                message_start_time=start,
                message_end_time=end,
            )

        groups = _group_chunks(
            [descriptor(0, 10), descriptor(10, 20), descriptor(30, 40)]
        )

        self.assertEqual([len(group) for group in groups], [2, 1])

    def test_slots_flattening_matches_legacy_reflection(self):
        message = DynamicMessage()
        message.nested.append(Quaternion())

        self.assertEqual(
            flatten_ros_msg(message, "/topic"),
            legacy_flatten_ros_msg(message, "/topic"),
        )

    def test_indexed_reader_matches_legacy_extraction(self):
        with SYNTHETIC_MCAP.open("rb") as stream:
            summary = make_reader(stream).get_summary()
            self.assertIsNotNone(summary)
            topics = [channel.topic for channel in summary.channels.values()]

        expected = legacy_extract(SYNTHETIC_MCAP, topics)
        actual = read_mcap(SYNTHETIC_MCAP, topics)

        pd.testing.assert_frame_equal(actual, expected, check_exact=True)

    def test_missing_topic_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "No configured topics"):
            read_mcap(SYNTHETIC_MCAP, ["/missing/topic"])


if __name__ == "__main__":
    unittest.main()
