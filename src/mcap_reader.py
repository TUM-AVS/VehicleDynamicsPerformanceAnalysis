"""Parallel extraction of numeric ROS 2 data from MCAP files."""

from __future__ import annotations

import gc
import logging
import math
import multiprocessing
import os
import shutil
import struct
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

import pandas as pd
from mcap.data_stream import ReadDataStream
from mcap.opcode import Opcode
from mcap.reader import FOOTER_SIZE, make_reader
from mcap.records import Channel, Chunk, ChunkIndex, Footer, Schema
from mcap.stream_reader import MAGIC_SIZE, StreamReader, get_chunk_data_stream
from mcap_ros2.decoder import DecoderFactory


_TARGET_TASK_BYTES = 32 * 1024 * 1024
# Each worker builds a dense frame before Parquet staging, so bound aggregate RSS.
_MAX_WORKERS = 64
_STREAM_BATCH_SIZE = 5000
_ORDER_COLUMNS = (
    "__mcap_log_time_ns",
    "__mcap_chunk_offset",
    "__mcap_message_offset",
)
_UINT16 = struct.Struct("<H")
_UINT64 = struct.Struct("<Q")

_worker_file: BinaryIO | None = None
_worker_channels: dict[int, Channel] = {}
_worker_schemas: dict[int, Schema] = {}
_worker_factory: DecoderFactory | None = None
_worker_decoders: dict[int, Callable[[bytes], object] | None] = {}


@dataclass(frozen=True)
class ChunkDescriptor:
    """The compact part of a chunk index needed by a worker."""

    start_offset: int
    chunk_length: int
    message_index_start: int
    message_index_length: int
    uncompressed_size: int
    has_message_indexes: bool
    message_start_time: int
    message_end_time: int


def _read_indexed_plan(
    stream: BinaryIO,
    topics: list[str],
) -> tuple[dict[int, Channel], dict[int, Schema], list[ChunkDescriptor]] | None:
    # Stream the summary instead of retaining every ChunkIndex mapping in memory.
    stream.seek(-(FOOTER_SIZE + MAGIC_SIZE), os.SEEK_END)
    footer = next(StreamReader(stream, skip_magic=True).records)
    if not isinstance(footer, Footer):
        raise ValueError(f"Expected MCAP footer, found {type(footer).__name__}")
    if footer.summary_start == 0:
        return None

    channels = {}
    schemas = {}
    stream.seek(footer.summary_start)
    for record in StreamReader(stream, skip_magic=True).records:
        if isinstance(record, Channel):
            channels[record.id] = record
        elif isinstance(record, Schema):
            schemas[record.id] = record
        elif isinstance(record, Footer):
            break

    selected_channels = {
        channel_id: channel
        for channel_id, channel in channels.items()
        if channel.topic in topics
    }
    if not selected_channels:
        raise ValueError("No configured topics are present in the MCAP file")

    selected_channel_ids = set(selected_channels)
    selected_schema_ids = {
        channel.schema_id
        for channel in selected_channels.values()
        if channel.schema_id != 0
    }
    selected_schemas = {
        schema_id: schemas[schema_id]
        for schema_id in selected_schema_ids
    }

    descriptors = []
    stream.seek(footer.summary_start)
    for record in StreamReader(stream, skip_magic=True).records:
        if isinstance(record, ChunkIndex):
            index_offsets = record.message_index_offsets
            if index_offsets and selected_channel_ids.isdisjoint(index_offsets):
                continue
            descriptors.append(
                ChunkDescriptor(
                    start_offset=record.chunk_start_offset,
                    chunk_length=record.chunk_length,
                    message_index_start=min(index_offsets.values(), default=0),
                    message_index_length=record.message_index_length,
                    uncompressed_size=record.uncompressed_size,
                    has_message_indexes=bool(index_offsets),
                    message_start_time=record.message_start_time,
                    message_end_time=record.message_end_time,
                )
            )
        elif isinstance(record, Footer):
            break
    if not descriptors:
        return None
    return selected_channels, selected_schemas, descriptors


def read_mcap(file_path: Path, topics: list[str]) -> pd.DataFrame:
    """Extract configured topics from an MCAP into the legacy sparse layout."""
    output_directory = Path(tempfile.mkdtemp(prefix="vdpa_batches_"))
    try:
        with file_path.open("rb") as stream:
            indexed_plan = _read_indexed_plan(stream, topics)
            if indexed_plan is None:
                stream.seek(0)
                reader = make_reader(stream)
                return _read_streamed_mcap(reader, topics, output_directory)
            selected_channels, selected_schemas, descriptors = indexed_plan

        if not descriptors:
            raise ValueError("No messages matched the configured MCAP topics")

        groups = _group_chunks(descriptors)
        max_workers = min(len(groups), _MAX_WORKERS, os.cpu_count() or 1)
        logging.info(
            "Reading MCAP file: Processing %d indexed chunks in %d tasks with %d workers",
            len(descriptors),
            len(groups),
            max_workers,
        )

        processing_started = time.perf_counter()
        results = []
        with ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_initialize_chunk_worker,
            initargs=(file_path, selected_channels, selected_schemas),
        ) as executor:
            futures = {
                executor.submit(
                    _process_chunk_group,
                    group_number,
                    group,
                    output_directory / f"batch_{group_number}.parquet",
                ): group_number
                for group_number, group in enumerate(groups)
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                logging.debug(
                    "MCAP task %d processed %d messages",
                    result[0],
                    result[2],
                )

        logging.info(
            "Reading MCAP file: Parallel chunk processing completed in %.2f seconds",
            time.perf_counter() - processing_started,
        )

        batch_files = [
            batch_file
            for _, batch_file, result_count in sorted(results)
            if result_count
        ]
        combine_started = time.perf_counter()
        data = _combine_indexed_batches(batch_files)
        logging.info(
            "Reading MCAP file: Batch assembly completed in %.2f seconds",
            time.perf_counter() - combine_started,
        )
        return data
    finally:
        shutil.rmtree(output_directory, ignore_errors=True)


def _group_chunks(
    descriptors: list[ChunkDescriptor],
) -> list[tuple[ChunkDescriptor, ...]]:
    groups = []
    group = []
    group_size = 0
    group_end_time = 0
    ordered_descriptors = sorted(
        descriptors,
        key=lambda descriptor: (
            descriptor.message_start_time,
            descriptor.start_offset,
        ),
    )
    for descriptor in ordered_descriptors:
        # Split only between non-overlapping time ranges so ordered task output can
        # be concatenated without another full-frame source-order sort.
        if (
            group
            and group_size >= _TARGET_TASK_BYTES
            and descriptor.message_start_time > group_end_time
        ):
            groups.append(tuple(group))
            group = []
            group_size = 0
            group_end_time = 0
        group.append(descriptor)
        group_size += descriptor.uncompressed_size
        group_end_time = max(group_end_time, descriptor.message_end_time)
    if group:
        groups.append(tuple(group))
    return groups


def _initialize_chunk_worker(
    file_path: Path,
    channels: dict[int, Channel],
    schemas: dict[int, Schema],
) -> None:
    global _worker_channels
    global _worker_decoders
    global _worker_factory
    global _worker_file
    global _worker_schemas

    _worker_file = file_path.open("rb")
    _worker_channels = channels
    _worker_schemas = schemas
    _worker_factory = DecoderFactory()
    _worker_decoders = {}


def _process_chunk_group(
    group_number: int,
    descriptors: tuple[ChunkDescriptor, ...],
    batch_file: Path,
) -> tuple[int, Path, int]:
    if _worker_file is None:
        raise RuntimeError("MCAP worker was not initialized")

    results = []
    for descriptor in descriptors:
        _worker_file.seek(descriptor.start_offset + 9)
        chunk = Chunk.read(ReadDataStream(_worker_file))
        chunk_stream, chunk_length = get_chunk_data_stream(chunk)
        chunk_data = chunk_stream.read(chunk_length)

        if descriptor.has_message_indexes:
            message_offsets = _read_selected_message_offsets(
                _worker_file,
                descriptor,
            )
        else:
            message_offsets = _scan_selected_message_offsets(chunk_data)

        for message_offset in message_offsets:
            decoded = _decode_message_at_offset(
                chunk_data,
                descriptor.start_offset,
                message_offset,
            )
            if decoded is not None:
                results.append(decoded)

    result_count = len(results)
    if result_count:
        frame = pd.DataFrame(results)
        frame = frame.sort_values(list(_ORDER_COLUMNS), kind="stable")
        frame = frame.drop(columns=list(_ORDER_COLUMNS))
        frame.to_parquet(batch_file)
        del frame
    del results
    gc.collect()
    return group_number, batch_file, result_count


def _read_selected_message_offsets(
    stream: BinaryIO,
    descriptor: ChunkDescriptor,
) -> list[int]:
    stream.seek(descriptor.message_index_start)
    index_data = stream.read(descriptor.message_index_length)
    selected_channel_ids = _worker_channels.keys()
    offsets = []
    position = 0

    while position < len(index_data):
        opcode = index_data[position]
        record_length = _UINT64.unpack_from(index_data, position + 1)[0]
        body_start = position + 9
        record_end = body_start + record_length
        if opcode == Opcode.MESSAGE_INDEX:
            channel_id = _UINT16.unpack_from(index_data, body_start)[0]
            if channel_id in selected_channel_ids:
                records_length = struct.unpack_from("<I", index_data, body_start + 2)[0]
                records_start = body_start + 6
                records_end = records_start + records_length
                for entry_position in range(records_start, records_end, 16):
                    offsets.append(_UINT64.unpack_from(index_data, entry_position + 8)[0])
        position = record_end

    offsets.sort()
    return offsets


def _scan_selected_message_offsets(chunk_data: bytes) -> list[int]:
    offsets = []
    position = 0
    while position < len(chunk_data):
        opcode = chunk_data[position]
        record_length = _UINT64.unpack_from(chunk_data, position + 1)[0]
        if opcode == Opcode.MESSAGE:
            channel_id = _UINT16.unpack_from(chunk_data, position + 9)[0]
            if channel_id in _worker_channels:
                offsets.append(position)
        position += 9 + record_length
    return offsets


def _decode_message_at_offset(
    chunk_data: bytes,
    chunk_offset: int,
    message_offset: int,
) -> dict | None:
    body_start = message_offset + 9
    record_length = _UINT64.unpack_from(chunk_data, message_offset + 1)[0]
    channel_id = _UINT16.unpack_from(chunk_data, body_start)[0]
    log_time = _UINT64.unpack_from(chunk_data, body_start + 6)[0]
    channel = _worker_channels[channel_id]

    try:
        if channel_id not in _worker_decoders:
            if _worker_factory is None:
                raise RuntimeError("MCAP decoder factory was not initialized")
            schema = _worker_schemas.get(channel.schema_id)
            _worker_decoders[channel_id] = _worker_factory.decoder_for(
                channel.message_encoding,
                schema,
            )
        decoder = _worker_decoders[channel_id]
        if decoder is None:
            logging.warning(
                "No decoder available for encoding '%s' on topic '%s'. Skipping message.",
                channel.message_encoding,
                channel.topic,
            )
            return None

        payload_start = body_start + 22
        payload_end = body_start + record_length
        ros_msg = decoder(chunk_data[payload_start:payload_end])
        msg_dict = flatten_ros_msg(ros_msg, channel.topic)
        msg_dict["__time"] = float(log_time) / 1e9
        msg_dict[_ORDER_COLUMNS[0]] = log_time
        msg_dict[_ORDER_COLUMNS[1]] = chunk_offset
        msg_dict[_ORDER_COLUMNS[2]] = message_offset
        return msg_dict
    except Exception:
        logging.exception("Error decoding ROS message on topic '%s'", channel.topic)
        return None


def _combine_indexed_batches(batch_files: list[Path]) -> pd.DataFrame:
    if not batch_files:
        raise ValueError("No MCAP messages were decoded")

    dataframes = []
    for batch_file in batch_files:
        batch = pd.read_parquet(batch_file)
        values = batch.astype(
            pd.SparseDtype("float", float("nan"))
        )
        dataframes.append(values)

    logging.info("Reading MCAP file: Concatenating DataFrames")
    data = pd.concat(dataframes, ignore_index=True, sort=True)
    return _finalize_column_order(data)


def _read_streamed_mcap(reader, topics: list[str], output_directory: Path) -> pd.DataFrame:
    logging.info("Reading MCAP file without a chunk index: Processing message batches")
    futures = []
    batch = []
    batch_number = 0
    with ProcessPoolExecutor(
        mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        for schema, channel, message in reader.iter_messages(topics=topics):
            batch.append((schema, channel, message))
            if len(batch) >= _STREAM_BATCH_SIZE:
                batch_file = output_directory / f"batch_{batch_number}.parquet"
                futures.append(
                    executor.submit(_process_stream_batch, batch, batch_file)
                )
                batch_number += 1
                batch = []
        if batch:
            batch_file = output_directory / f"batch_{batch_number}.parquet"
            futures.append(executor.submit(_process_stream_batch, batch, batch_file))
        for future in as_completed(futures):
            future.result()

    batch_files = sorted(
        output_directory.glob("batch_*.parquet"),
        key=lambda path: int(path.stem.removeprefix("batch_")),
    )
    dataframes = [
        pd.read_parquet(batch_file).astype(
            pd.SparseDtype("float", float("nan"))
        )
        for batch_file in batch_files
    ]
    if not dataframes:
        raise ValueError("No MCAP messages were decoded")
    return _finalize_column_order(
        pd.concat(dataframes, ignore_index=True, sort=True)
    )


def _process_stream_batch(batch, batch_file: Path) -> int:
    results = []
    factory = DecoderFactory()
    decoders = {}
    for schema, channel, message in batch:
        try:
            decoder = decoders.get(channel.id)
            if channel.id not in decoders:
                decoder = factory.decoder_for(channel.message_encoding, schema)
                decoders[channel.id] = decoder
            if decoder is None:
                logging.warning(
                    "No decoder available for encoding '%s' on topic '%s'. Skipping message.",
                    channel.message_encoding,
                    channel.topic,
                )
                continue
            msg_dict = flatten_ros_msg(decoder(message.data), channel.topic)
            msg_dict["__time"] = float(message.log_time) / 1e9
            results.append(msg_dict)
        except Exception:
            logging.exception("Error decoding ROS message on topic '%s'", channel.topic)
    if results:
        pd.DataFrame(results).to_parquet(batch_file)
    return len(results)


def _finalize_column_order(data: pd.DataFrame) -> pd.DataFrame:
    columns = data.columns.tolist()
    if "__time" in columns:
        columns.insert(0, columns.pop(columns.index("__time")))
        data = data[columns]
        data = data.sort_values(by="__time").reset_index(drop=True)
    return data


def flatten_ros_msg(msg, base_path: str) -> dict:
    """Flatten the numeric leaves of a dynamically decoded ROS message."""
    stack = [(base_path, msg, 0)]
    flat_dict = {}
    while stack:
        current_path, current_msg, depth = stack.pop()
        if depth > 6:
            logging.warning("Maximum depth exceeded at %s", current_path)
            continue
        if isinstance(current_msg, (int, float)):
            flat_dict[current_path] = current_msg
        elif isinstance(current_msg, (list, tuple)):
            if len(current_msg) > 1000:
                logging.warning("Large array skipped at %s", current_path)
                continue
            for idx, item in enumerate(current_msg):
                stack.append((f"{current_path}/{idx}", item, depth + 1))
        elif (
            hasattr(current_msg, "x")
            and hasattr(current_msg, "y")
            and hasattr(current_msg, "z")
            and hasattr(current_msg, "w")
        ):
            x = current_msg.x
            y = current_msg.y
            z = current_msg.z
            w = current_msg.w
            flat_dict[f"{current_path}/x"] = x
            flat_dict[f"{current_path}/y"] = y
            flat_dict[f"{current_path}/z"] = z
            flat_dict[f"{current_path}/w"] = w
            roll, pitch, yaw = quaternion_to_euler(x, y, z, w)
            flat_dict[f"{current_path}/roll"] = roll
            flat_dict[f"{current_path}/pitch"] = pitch
            flat_dict[f"{current_path}/yaw"] = yaw
        else:
            slots = getattr(type(current_msg), "__slots__", None)
            if slots is None:
                attributes = [
                    attr
                    for attr in dir(current_msg)
                    if not attr.startswith("_")
                    and not callable(getattr(current_msg, attr))
                    and attr.lower() != "covariance"
                ]
            else:
                attributes = [
                    attr
                    for attr in slots
                    if not attr.startswith("_") and attr.lower() != "covariance"
                ]
            for attr in attributes:
                if attr in ("timestamp", "stamp"):
                    continue
                stack.append(
                    (f"{current_path}/{attr}", getattr(current_msg, attr), depth + 1)
                )
    if len(flat_dict) > 1000:
        logging.warning(
            "Message at %s resulted in a large number of fields: %d",
            base_path,
            len(flat_dict),
        )
    return flat_dict


def quaternion_to_euler(x, y, z, w):
    """Convert a quaternion to roll, pitch, and yaw."""
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    x /= norm
    y /= norm
    z /= norm
    w /= norm

    t0 = 2.0 * (w * x + y * z)
    t1 = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(t0, t1)

    t2 = 2.0 * (w * y - z * x)
    t2 = max(-1.0, min(1.0, t2))
    pitch = math.asin(t2)

    t3 = 2.0 * (w * z + x * y)
    t4 = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(t3, t4)
    return roll, pitch, yaw
