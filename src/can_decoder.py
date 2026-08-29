"""Reusable candump parsing, CAN signal decoding, and tabular conversion."""

from __future__ import annotations

import csv
import json
import math
import os
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Iterator, Mapping, Sequence, TextIO

if TYPE_CHECKING:
    import pandas as pd


MAX_CAN_ID = 0x1FFFFFFF
MAX_CAN_PAYLOAD_BYTES = 64


class CanDecoderError(Exception):
    """Base exception for CAN conversion failures."""


class DefinitionError(CanDecoderError, ValueError):
    """Raised when a source definition is invalid."""


class CandumpParseError(CanDecoderError, ValueError):
    """Raised when a candump line cannot be parsed."""


class DecodeError(CanDecoderError, ValueError):
    """Raised when a frame cannot satisfy its signal definition."""


class CsvError(CanDecoderError, ValueError):
    """Raised when decoded records cannot be represented as requested."""


@dataclass(frozen=True)
class SignalDefinition:
    name: str
    start_bit: int
    bit_length: int
    is_little_endian: bool
    is_signed: bool
    factor: float
    offset: float
    unit: str


@dataclass(frozen=True)
class MessageDefinition:
    can_id: int
    name: str
    signals: tuple[SignalDefinition, ...]
    is_extended: bool = False


@dataclass(frozen=True)
class CanDatabase:
    messages: tuple[MessageDefinition, ...]
    _messages_by_id: Mapping[tuple[int, bool], MessageDefinition] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_messages_by_id",
            MappingProxyType(
                {
                    (message.can_id, message.is_extended): message
                    for message in self.messages
                }
            ),
        )

    def message_for_id(
        self, can_id: int, is_extended: bool | None = None
    ) -> MessageDefinition | None:
        if is_extended is None:
            is_extended = can_id > 0x7FF
        return self._messages_by_id.get((can_id, is_extended))


@dataclass(frozen=True)
class CanFrame:
    timestamp: float | None
    interface: str | None
    can_id: int
    data: bytes
    is_extended: bool = False
    is_fd: bool = False
    fd_flags: int | None = None


@dataclass(frozen=True)
class DecodedValue:
    signal_name: str
    value: float
    unit: str


@dataclass(frozen=True)
class DecodedEvent:
    timestamp: float | None
    interface: str | None
    can_id: int
    is_extended: bool
    message_name: str
    values: tuple[DecodedValue, ...]


_TIMESTAMP_RE = re.compile(
    r"^\((?P<timestamp>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\)\s*(?P<body>.*)$"
)
_COMPACT_FRAME_RE = re.compile(
    r"^(?:(?P<interface>\S+)\s+)?"
    r"(?P<can_id>[0-9A-Fa-f]{1,8})(?P<separator>##|#)(?P<body>\S*)$"
)
_BRACKET_FRAME_RE = re.compile(
    r"^(?:(?P<interface>\S+)\s+)?"
    r"(?P<can_id>[0-9A-Fa-f]{1,8})\s+"
    r"\[(?P<dlc>\d{1,2})\](?:\s+(?P<data>.*))?$"
)


def _definition_error(location: str, message: str) -> DefinitionError:
    return DefinitionError(f"Invalid source definition at {location}: {message}")


def _required(mapping: Mapping[str, object], key: str, location: str) -> object:
    if key not in mapping:
        raise _definition_error(location, f"missing required field {key!r}")
    return mapping[key]


def _as_mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _definition_error(location, "expected an object")
    return value


def _as_nonempty_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _definition_error(location, "expected a non-empty string")
    return value


def _as_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _definition_error(location, "expected an integer")
    return value


def _as_bool(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise _definition_error(location, "expected a boolean")
    return value


def _as_finite_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _definition_error(location, "expected a number")
    converted = float(value)
    if not math.isfinite(converted):
        raise _definition_error(location, "expected a finite number")
    return converted


def _parse_can_id(value: object, location: str) -> int:
    if isinstance(value, bool):
        raise _definition_error(location, "expected an integer or numeric string")
    if isinstance(value, int):
        can_id = value
    elif isinstance(value, str):
        try:
            can_id = int(value, 0)
        except ValueError as exc:
            raise _definition_error(
                location, "expected a decimal or 0x-prefixed CAN ID"
            ) from exc
    else:
        raise _definition_error(location, "expected an integer or numeric string")
    if not 0 <= can_id <= MAX_CAN_ID:
        raise _definition_error(
            location, f"CAN ID must be between 0 and 0x{MAX_CAN_ID:X}"
        )
    return can_id


def _motorola_bit_positions(start_bit: int, bit_length: int) -> tuple[int, ...]:
    positions: list[int] = []
    position = start_bit
    for _ in range(bit_length):
        positions.append(position)
        position = position - 1 if position % 8 else position + 15
    return tuple(positions)


def parse_definitions(document: object) -> CanDatabase:
    """Validate a decoded JSON value using the custom ``params`` schema."""
    root = _as_mapping(document, "root")
    params = _required(root, "params", "root")
    if not isinstance(params, list):
        raise _definition_error("params", "expected an array")

    messages: list[MessageDefinition] = []
    seen_ids: set[tuple[int, bool]] = set()
    seen_message_names: set[str] = set()
    for message_index, raw_message in enumerate(params):
        message_location = f"params[{message_index}]"
        message_data = _as_mapping(raw_message, message_location)
        can_id = _parse_can_id(
            _required(message_data, "canId", message_location),
            f"{message_location}.canId",
        )
        name = _as_nonempty_string(
            _required(message_data, "name", message_location),
            f"{message_location}.name",
        )
        is_extended = _as_bool(
            message_data.get("isExtendedFrame", can_id > 0x7FF),
            f"{message_location}.isExtendedFrame",
        )
        if not is_extended and can_id > 0x7FF:
            raise _definition_error(
                f"{message_location}.canId",
                "standard-frame CAN IDs must not exceed 0x7FF",
            )
        message_key = (can_id, is_extended)
        if message_key in seen_ids:
            frame_type = "extended" if is_extended else "standard"
            raise _definition_error(
                f"{message_location}.canId",
                f"duplicate CAN ID 0x{can_id:X} for a {frame_type} frame",
            )
        if name in seen_message_names:
            raise _definition_error(
                f"{message_location}.name", f"duplicate message name {name!r}"
            )

        raw_signals = _required(message_data, "signals", message_location)
        if not isinstance(raw_signals, list):
            raise _definition_error(f"{message_location}.signals", "expected an array")
        signals: list[SignalDefinition] = []
        seen_signal_names: set[str] = set()
        for signal_index, raw_signal in enumerate(raw_signals):
            signal_location = f"{message_location}.signals[{signal_index}]"
            signal_data = _as_mapping(raw_signal, signal_location)
            signal_name = _as_nonempty_string(
                _required(signal_data, "name", signal_location),
                f"{signal_location}.name",
            )
            if signal_name in seen_signal_names:
                raise _definition_error(
                    f"{signal_location}.name",
                    f"duplicate signal name {signal_name!r} in message {name!r}",
                )
            start_bit = _as_int(
                _required(signal_data, "startBit", signal_location),
                f"{signal_location}.startBit",
            )
            bit_length = _as_int(
                _required(signal_data, "bitLength", signal_location),
                f"{signal_location}.bitLength",
            )
            is_little_endian = _as_bool(
                signal_data.get("isLittleEndian", False),
                f"{signal_location}.isLittleEndian",
            )
            is_signed = _as_bool(
                signal_data.get("isSigned", False),
                f"{signal_location}.isSigned",
            )
            if start_bit < 0:
                raise _definition_error(
                    f"{signal_location}.startBit", "must be non-negative"
                )
            if bit_length <= 0:
                raise _definition_error(
                    f"{signal_location}.bitLength", "must be greater than zero"
                )
            if start_bit >= MAX_CAN_PAYLOAD_BYTES * 8:
                raise _definition_error(
                    f"{signal_location}.startBit",
                    f"must be below {MAX_CAN_PAYLOAD_BYTES * 8}",
                )
            if bit_length > MAX_CAN_PAYLOAD_BYTES * 8:
                raise _definition_error(
                    f"{signal_location}.bitLength",
                    f"must not exceed {MAX_CAN_PAYLOAD_BYTES * 8}",
                )
            if is_little_endian:
                positions = range(start_bit, start_bit + bit_length)
            else:
                positions = _motorola_bit_positions(start_bit, bit_length)
            if max(positions) >= MAX_CAN_PAYLOAD_BYTES * 8:
                raise _definition_error(
                    signal_location,
                    f"bit range exceeds the {MAX_CAN_PAYLOAD_BYTES}-byte CAN FD payload",
                )

            factor = _as_finite_number(
                signal_data.get("factor", 1), f"{signal_location}.factor"
            )
            offset = _as_finite_number(
                signal_data.get("offset", 0), f"{signal_location}.offset"
            )
            unit_value = signal_data.get("postfixMetric", "")
            if not isinstance(unit_value, str):
                raise _definition_error(
                    f"{signal_location}.postfixMetric", "expected a string"
                )
            signals.append(
                SignalDefinition(
                    name=signal_name,
                    start_bit=start_bit,
                    bit_length=bit_length,
                    is_little_endian=is_little_endian,
                    is_signed=is_signed,
                    factor=factor,
                    offset=offset,
                    unit=unit_value,
                )
            )
            seen_signal_names.add(signal_name)

        messages.append(MessageDefinition(can_id, name, tuple(signals), is_extended))
        seen_ids.add(message_key)
        seen_message_names.add(name)
    return CanDatabase(tuple(messages))


def load_definitions(path: str | Path) -> CanDatabase:
    """Read and validate a source definition JSON file."""
    source_path = Path(path)
    try:
        with source_path.open("r", encoding="utf-8") as source:
            document = json.load(source)
    except json.JSONDecodeError as exc:
        raise DefinitionError(
            f"Invalid JSON in {source_path} at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    return parse_definitions(document)


def _parse_timestamp(line: str) -> tuple[float | None, str]:
    if not line.startswith("("):
        return None, line
    match = _TIMESTAMP_RE.fullmatch(line)
    if match is None:
        raise CandumpParseError("invalid parenthesized timestamp")
    return float(match.group("timestamp")), match.group("body")


def _parse_hex_payload(data_text: str, *, is_fd: bool) -> bytes:
    if len(data_text) % 2:
        raise CandumpParseError("payload must contain complete hexadecimal bytes")
    try:
        payload = bytes.fromhex(data_text)
    except ValueError as exc:
        raise CandumpParseError("payload contains non-hexadecimal data") from exc
    maximum = MAX_CAN_PAYLOAD_BYTES if is_fd else 8
    if len(payload) > maximum:
        frame_type = "CAN FD" if is_fd else "classic CAN"
        raise CandumpParseError(
            f"{frame_type} payload has {len(payload)} bytes; maximum is {maximum}"
        )
    return payload


def _parse_frame_id(text: str) -> tuple[int, bool]:
    can_id = int(text, 16)
    if can_id > MAX_CAN_ID:
        raise CandumpParseError(f"CAN ID 0x{can_id:X} exceeds 29 bits")
    is_extended = len(text) > 3
    if not is_extended and can_id > 0x7FF:
        raise CandumpParseError(
            "standard CAN identifiers must not exceed 0x7FF; use eight hex digits "
            "for an extended frame"
        )
    return can_id, is_extended


def parse_candump_line(line: str, line_number: int | None = None) -> CanFrame:
    """Parse one compact or bracketed candump data line."""
    original = line.rstrip("\r\n")
    stripped = original.strip()
    prefix = f"line {line_number}: " if line_number is not None else ""
    if not stripped:
        raise CandumpParseError(f"{prefix}empty line")
    try:
        timestamp, body = _parse_timestamp(stripped)
        compact_match = _COMPACT_FRAME_RE.fullmatch(body)
        if compact_match is not None:
            can_id, is_extended = _parse_frame_id(compact_match.group("can_id"))
            is_fd = compact_match.group("separator") == "##"
            payload_text = compact_match.group("body")
            fd_flags: int | None = None
            if is_fd:
                if not payload_text:
                    raise CandumpParseError("CAN FD frame is missing its flags nibble")
                try:
                    fd_flags = int(payload_text[0], 16)
                except ValueError as exc:
                    raise CandumpParseError(
                        "CAN FD flags nibble is not hexadecimal"
                    ) from exc
                payload_text = payload_text[1:]
            payload = _parse_hex_payload(payload_text, is_fd=is_fd)
            return CanFrame(
                timestamp=timestamp,
                interface=compact_match.group("interface"),
                can_id=can_id,
                data=payload,
                is_extended=is_extended,
                is_fd=is_fd,
                fd_flags=fd_flags,
            )

        bracket_match = _BRACKET_FRAME_RE.fullmatch(body)
        if bracket_match is None:
            raise CandumpParseError("unsupported candump frame format")
        dlc_text = bracket_match.group("dlc")
        can_id, is_extended = _parse_frame_id(bracket_match.group("can_id"))
        dlc = int(dlc_text)
        # candump's long format zero-pads the length for CAN FD frames so that
        # short FD payloads remain distinguishable from classic CAN.
        is_fd = len(dlc_text) == 2
        if dlc > MAX_CAN_PAYLOAD_BYTES:
            raise CandumpParseError(
                f"payload length {dlc} exceeds {MAX_CAN_PAYLOAD_BYTES} bytes"
            )
        byte_tokens = (bracket_match.group("data") or "").split()
        if len(byte_tokens) != dlc:
            raise CandumpParseError(
                f"declared payload length is {dlc}, but found {len(byte_tokens)} bytes"
            )
        if any(len(token) != 2 for token in byte_tokens):
            raise CandumpParseError("bracketed payload bytes must use two hex digits")
        payload = _parse_hex_payload("".join(byte_tokens), is_fd=is_fd)
        return CanFrame(
            timestamp=timestamp,
            interface=bracket_match.group("interface"),
            can_id=can_id,
            data=payload,
            is_extended=is_extended,
            is_fd=is_fd,
        )
    except CandumpParseError as exc:
        if prefix:
            raise CandumpParseError(f"{prefix}{exc}") from exc
        raise


def iter_candump(lines: Iterable[str]) -> Iterator[CanFrame]:
    """Yield frames from candump text, ignoring only blank lines."""
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        yield parse_candump_line(line, line_number)


def read_candump(path: str | Path) -> Iterator[CanFrame]:
    """Read candump frames, ignoring an unterminated final line."""
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            if not line.endswith("\n"):
                return
            yield parse_candump_line(line, line_number)


def _extract_raw(payload: bytes, signal: SignalDefinition) -> int:
    if signal.is_little_endian:
        positions: Sequence[int] = range(
            signal.start_bit, signal.start_bit + signal.bit_length
        )
        if positions[-1] >= len(payload) * 8:
            raise DecodeError(
                f"signal {signal.name!r} needs bit {positions[-1]}, but payload has "
                f"only {len(payload) * 8} bits"
            )
        raw = 0
        for value_bit, payload_bit in enumerate(positions):
            raw |= ((payload[payload_bit // 8] >> (payload_bit % 8)) & 1) << value_bit
        return raw

    positions = _motorola_bit_positions(signal.start_bit, signal.bit_length)
    unavailable = next(
        (position for position in positions if position >= len(payload) * 8), None
    )
    if unavailable is not None:
        raise DecodeError(
            f"signal {signal.name!r} needs bit {unavailable}, but payload has only "
            f"{len(payload) * 8} bits"
        )
    raw = 0
    for payload_bit in positions:
        raw = (raw << 1) | (
            (payload[payload_bit // 8] >> (payload_bit % 8)) & 1
        )
    return raw


def decode_frame(
    frame: CanFrame, database: CanDatabase
) -> DecodedEvent | None:
    """Decode a frame, returning ``None`` when its CAN ID is not configured."""
    message = database.message_for_id(frame.can_id, frame.is_extended)
    if message is None:
        return None
    values: list[DecodedValue] = []
    for signal in message.signals:
        try:
            raw = _extract_raw(frame.data, signal)
        except DecodeError as exc:
            raise DecodeError(
                f"Cannot decode message {message.name!r} (0x{frame.can_id:X}): {exc}"
            ) from exc
        if signal.is_signed and raw & (1 << (signal.bit_length - 1)):
            raw -= 1 << signal.bit_length
        values.append(
            DecodedValue(
                signal_name=signal.name,
                value=raw * signal.factor + signal.offset,
                unit=signal.unit,
            )
        )
    return DecodedEvent(
        timestamp=frame.timestamp,
        interface=frame.interface,
        can_id=frame.can_id,
        is_extended=frame.is_extended,
        message_name=message.name,
        values=tuple(values),
    )


def decode_frames(
    frames: Iterable[CanFrame],
    database: CanDatabase,
    *,
    filter_duplicates: bool = False,
) -> Iterator[DecodedEvent]:
    """Decode known frames and optionally suppress unchanged per-bus payloads."""
    previous_payloads: dict[
        tuple[str | None, int, bool], tuple[bytes, bool, int | None]
    ] = {}
    for frame in frames:
        if database.message_for_id(frame.can_id, frame.is_extended) is None:
            continue
        if filter_duplicates:
            key = (frame.interface, frame.can_id, frame.is_extended)
            frame_content = (frame.data, frame.is_fd, frame.fd_flags)
            if previous_payloads.get(key) == frame_content:
                continue
            previous_payloads[key] = frame_content
        event = decode_frame(frame, database)
        if event is not None:
            yield event


def _wide_column_names(
    database: CanDatabase,
) -> tuple[list[str], dict[tuple[str, str], str]]:
    signal_counts = Counter(
        signal.name
        for message in database.messages
        for signal in message.signals
    )
    reserved_columns = {"timestamp", "interface", "can_id", "is_extended", "message"}
    columns: list[str] = []
    names: dict[tuple[str, str], str] = {}
    for message in database.messages:
        for signal in message.signals:
            column = (
                f"{message.name}.{signal.name}"
                if signal_counts[signal.name] > 1 or signal.name in reserved_columns
                else signal.name
            )
            key = (message.name, signal.name)
            if column in columns:
                raise CsvError(
                    f"wide CSV column name collision for {column!r}; rename the signal "
                    "or message in the source definition"
                )
            columns.append(column)
            names[key] = column
    return columns, names


def _base_row(event: DecodedEvent) -> dict[str, object]:
    return {
        "timestamp": "" if event.timestamp is None else event.timestamp,
        "interface": "" if event.interface is None else event.interface,
        "can_id": f"0x{event.can_id:X}",
        "is_extended": event.is_extended,
        "message": event.message_name,
    }


def events_to_dataframe(
    events: Iterable[DecodedEvent], database: CanDatabase
) -> pd.DataFrame:
    """Return decoded events in the sparse wide format used by the analyzer."""
    import pandas as pd

    signal_columns, column_names = _wide_column_names(database)
    rows: list[dict[str, float]] = []
    for event in events:
        if event.timestamp is None:
            raise DecodeError(
                "CAN logs analyzed directly must include a timestamp on every decoded frame"
            )
        row = {"timestamp": event.timestamp}
        for decoded_value in event.values:
            try:
                column = column_names[(event.message_name, decoded_value.signal_name)]
            except KeyError as exc:
                raise CsvError(
                    f"decoded value {event.message_name}.{decoded_value.signal_name} "
                    "is absent from the source definition"
                ) from exc
            row[column] = decoded_value.value
        rows.append(row)

    if not rows:
        raise DecodeError("No configured CAN frames were decoded from the log")

    data = pd.DataFrame.from_records(rows, columns=["timestamp", *signal_columns])
    return data.dropna(axis=1, how="all")


def read_can_log(
    input_path: str | Path,
    definition_path: str | Path,
    *,
    filter_duplicates: bool = True,
) -> pd.DataFrame:
    """Parse a candump log into a DataFrame, suppressing unchanged payloads by default."""
    database = load_definitions(definition_path)
    maximum_timestamp: float | None = None

    def tracked_frames() -> Iterator[CanFrame]:
        nonlocal maximum_timestamp
        for frame in read_candump(input_path):
            if database.message_for_id(frame.can_id, frame.is_extended) is not None:
                if frame.timestamp is None:
                    raise DecodeError(
                        "CAN logs analyzed directly must include a timestamp on every "
                        "decoded frame"
                    )
                maximum_timestamp = (
                    frame.timestamp
                    if maximum_timestamp is None
                    else max(maximum_timestamp, frame.timestamp)
                )
            yield frame

    events = decode_frames(
        tracked_frames(),
        database,
        filter_duplicates=filter_duplicates,
    )
    data = events_to_dataframe(events, database)
    if maximum_timestamp is not None and maximum_timestamp > data["timestamp"].max():
        data.loc[len(data), "timestamp"] = maximum_timestamp
    return data


def write_csv(
    events: Iterable[DecodedEvent],
    output: TextIO,
    database: CanDatabase,
    *,
    mode: str = "long",
) -> int:
    """Write decoded events to an already-open CSV text stream."""
    if mode not in {"long", "wide"}:
        raise CsvError("CSV mode must be 'long' or 'wide'")
    event_count = 0
    if mode == "long":
        fieldnames = [
            "timestamp",
            "interface",
            "can_id",
            "is_extended",
            "message",
            "signal",
            "value",
            "unit",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for event in events:
            base = _base_row(event)
            for decoded_value in event.values:
                writer.writerow(
                    {
                        **base,
                        "signal": decoded_value.signal_name,
                        "value": decoded_value.value,
                        "unit": decoded_value.unit,
                    }
                )
            event_count += 1
        return event_count

    signal_columns, column_names = _wide_column_names(database)
    fieldnames = [
        "timestamp",
        "interface",
        "can_id",
        "is_extended",
        "message",
        *signal_columns,
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for event in events:
        row = _base_row(event)
        for decoded_value in event.values:
            try:
                column = column_names[(event.message_name, decoded_value.signal_name)]
            except KeyError as exc:
                raise CsvError(
                    f"decoded value {event.message_name}.{decoded_value.signal_name} "
                    "is absent from the source definition"
                ) from exc
            row[column] = decoded_value.value
        writer.writerow(row)
        event_count += 1
    return event_count


def write_csv_file(
    events: Iterable[DecodedEvent],
    path: str | Path,
    database: CanDatabase,
    *,
    mode: str = "long",
) -> int:
    """Write decoded events to a CSV path and return the event count."""
    output_path = Path(path)
    temporary_path: Path | None = None
    replaced = False
    file_descriptor: int | None = None
    try:
        existing_mode = (
            output_path.stat().st_mode & 0o7777 if output_path.exists() else None
        )
        for _ in range(100):
            candidate = output_path.parent / (
                f".{output_path.name}.{secrets.token_hex(8)}.tmp"
            )
            try:
                file_descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    existing_mode if existing_mode is not None else 0o666,
                )
            except FileExistsError:
                continue
            temporary_path = candidate
            break
        else:
            raise OSError(f"Could not allocate a temporary output beside {output_path}")

        if existing_mode is not None:
            os.chmod(temporary_path, existing_mode)
        with os.fdopen(
            file_descriptor, "w", encoding="utf-8", newline=""
        ) as output:
            file_descriptor = None
            event_count = write_csv(events, output, database, mode=mode)
        temporary_path.replace(output_path)
        replaced = True
        return event_count
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None and not replaced:
            temporary_path.unlink(missing_ok=True)


def convert_log(
    input_path: str | Path,
    definition_path: str | Path,
    output_path: str | Path,
    *,
    mode: str = "long",
    filter_duplicates: bool = False,
) -> int:
    """Convert a candump file to CSV and return the decoded event count."""
    database = load_definitions(definition_path)
    events = decode_frames(
        read_candump(input_path),
        database,
        filter_duplicates=filter_duplicates,
    )
    return write_csv_file(events, output_path, database, mode=mode)
