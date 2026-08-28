import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.can_decoder import (
    CandumpParseError,
    DecodeError,
    DefinitionError,
    decode_frame,
    decode_frames,
    iter_candump,
    parse_candump_line,
    parse_definitions,
    read_candump,
    write_csv,
    write_csv_file,
)
from tools.can_decoder import main as cli_main


def signal(
    name,
    start_bit,
    bit_length,
    *,
    little_endian=True,
    signed=False,
    factor: float = 1,
    offset: float = 0,
    unit="",
):
    return {
        "name": name,
        "startBit": start_bit,
        "bitLength": bit_length,
        "isLittleEndian": little_endian,
        "isSigned": signed,
        "factor": factor,
        "offset": offset,
        "postfixMetric": unit,
    }


def definitions(*messages):
    return parse_definitions({"params": list(messages)})


def message(can_id, name, *signals):
    return {"canId": can_id, "name": name, "signals": list(signals)}


class CandumpParsingTests(unittest.TestCase):
    def test_timestamped_classic_accepts_lowercase_hex(self):
        frame = parse_candump_line("(123.25) can0 1ab#0aBc")

        self.assertEqual(frame.timestamp, 123.25)
        self.assertEqual(frame.interface, "can0")
        self.assertEqual(frame.can_id, 0x1AB)
        self.assertEqual(frame.data, bytes.fromhex("0A BC"))
        self.assertFalse(frame.is_fd)

    def test_timestamp_free_classic_without_interface(self):
        frame = parse_candump_line("7FF#DEADBEEF")

        self.assertIsNone(frame.timestamp)
        self.assertIsNone(frame.interface)
        self.assertEqual(frame.can_id, 0x7FF)
        self.assertEqual(frame.data, bytes.fromhex("DE AD BE EF"))

    def test_standard_and_extended_ids_preserve_frame_format(self):
        standard = parse_candump_line("123#00")
        extended = parse_candump_line("00000123#00")

        self.assertFalse(standard.is_extended)
        self.assertTrue(extended.is_extended)
        self.assertEqual(standard.can_id, extended.can_id)

    def test_compact_can_fd_extracts_flags_and_mixed_case_payload(self):
        frame = parse_candump_line("can1 18DaF110##1aAbBcC")

        self.assertEqual(frame.can_id, 0x18DAF110)
        self.assertEqual(frame.fd_flags, 1)
        self.assertEqual(frame.data, bytes.fromhex("AA BB CC"))
        self.assertTrue(frame.is_fd)

    def test_bracketed_classic_and_fd(self):
        classic = parse_candump_line("can0 123 [2] 0a FF")
        full_classic = parse_candump_line("can0 123 [8] 00 01 02 03 04 05 06 07")
        short_fd = parse_candump_line("can0 123 [02] 00 01")
        fd = parse_candump_line("(1.0) can0 123 [09] 00 01 02 03 04 05 06 07 08")

        self.assertFalse(classic.is_fd)
        self.assertEqual(classic.data, b"\x0a\xff")
        self.assertFalse(full_classic.is_fd)
        self.assertTrue(short_fd.is_fd)
        self.assertTrue(fd.is_fd)
        self.assertEqual(len(fd.data), 9)

    def test_iter_ignores_blank_lines_and_reports_line_number(self):
        with self.assertRaisesRegex(CandumpParseError, r"line 3"):
            list(iter_candump(["\n", "can0 123#00\n", "not a frame\n"]))

    def test_rejects_invalid_payloads(self):
        cases = (
            "can0 123#A",
            "can0 123#GG",
            "can0 123#000000000000000000",
            "can0 123##",
            "can0 123 [2] 00",
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(CandumpParseError):
                parse_candump_line(case)


class DefinitionAndDecodeTests(unittest.TestCase):
    def test_signed_scaled_intel_signal(self):
        database = definitions(
            message(
                "0x123",
                "Powertrain",
                signal(
                    "torque",
                    0,
                    16,
                    signed=True,
                    factor=0.1,
                    offset=1,
                    unit="Nm",
                ),
            )
        )
        frame = parse_candump_line("123#9CFF")  # -100 before scaling

        event = decode_frame(frame, database)

        assert event is not None
        self.assertEqual(event.message_name, "Powertrain")
        self.assertAlmostEqual(event.values[0].value, -9.0)
        self.assertEqual(event.values[0].unit, "Nm")

    def test_endianness_and_signedness_default_to_false(self):
        database = parse_definitions(
            {
                "params": [
                    {
                        "canId": 1,
                        "name": "Status",
                        "signals": [
                            {"name": "value", "startBit": 7, "bitLength": 8}
                        ],
                    }
                ]
            }
        )

        event = decode_frame(parse_candump_line("1#2A"), database)

        assert event is not None
        self.assertEqual(event.values[0].value, 42)

    def test_standard_dbc_motorola_vectors(self):
        database = definitions(
            message(
                0x200,
                "Motorola",
                signal("word", 7, 16, little_endian=False),
                signal("cross_byte", 11, 12, little_endian=False),
            )
        )
        frame = parse_candump_line("200#123456")

        event = decode_frame(frame, database)

        assert event is not None
        self.assertEqual(event.values[0].value, 0x1234)
        self.assertEqual(event.values[1].value, 0x456)

    def test_unknown_can_id_is_not_an_event(self):
        database = definitions(message(1, "Known", signal("value", 0, 8)))
        self.assertIsNone(decode_frame(parse_candump_line("2#00"), database))

    def test_standard_and_extended_definitions_with_same_id_are_distinct(self):
        database = parse_definitions(
            {
                "params": [
                    {
                        **message(0x123, "Standard", signal("value", 0, 8)),
                        "isExtendedFrame": False,
                    },
                    {
                        **message(0x123, "Extended", signal("value", 0, 8)),
                        "isExtendedFrame": True,
                    },
                ]
            }
        )

        standard = decode_frame(parse_candump_line("123#01"), database)
        extended = decode_frame(parse_candump_line("00000123#02"), database)

        assert standard is not None and extended is not None
        self.assertEqual(standard.message_name, "Standard")
        self.assertEqual(extended.message_name, "Extended")

    def test_short_payload_reports_message_and_signal(self):
        database = definitions(message(1, "Status", signal("counter", 8, 8)))

        with self.assertRaisesRegex(
            DecodeError, r"Status.*counter.*payload has only 8 bits"
        ):
            decode_frame(parse_candump_line("1#00"), database)

    def test_invalid_definition_ranges_and_types_are_clear(self):
        invalid_documents = (
            ({}, r"root.*params"),
            ({"params": [{"canId": -1, "name": "Bad", "signals": []}]}, r"canId"),
            (
                {
                    "params": [
                        message(1, "Bad", signal("too_far", 511, 2))
                    ]
                },
                r"64-byte",
            ),
            (
                {
                    "params": [
                        message(
                            1,
                            "Bad",
                            {
                                **signal("bad_bool", 0, 1),
                                "isLittleEndian": 1,
                            },
                        )
                    ]
                },
                r"isLittleEndian.*boolean",
            ),
        )
        for document, pattern in invalid_documents:
            with self.subTest(pattern=pattern), self.assertRaisesRegex(
                DefinitionError, pattern
            ):
                parse_definitions(document)

    def test_duplicate_ids_and_same_message_signal_names_are_rejected(self):
        with self.assertRaisesRegex(DefinitionError, "duplicate CAN ID"):
            definitions(
                message(1, "First", signal("a", 0, 1)),
                message(1, "Second", signal("b", 0, 1)),
            )
        with self.assertRaisesRegex(DefinitionError, "duplicate signal name"):
            definitions(
                message(1, "First", signal("same", 0, 1), signal("same", 1, 1))
            )

    def test_duplicate_filter_tracks_each_interface_and_id(self):
        database = definitions(message(1, "Status", signal("value", 0, 8)))
        frames = iter_candump(
            [
                "can0 1#01",
                "can0 1#01",
                "can1 1#01",
                "can0 1#02",
                "can0 1#02",
            ]
        )

        events = list(decode_frames(frames, database, filter_duplicates=True))

        self.assertEqual([event.values[0].value for event in events], [1, 1, 2])


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.database = definitions(
            message(
                1,
                "Front",
                signal("speed", 0, 8, unit="km/h"),
                signal("temperature", 8, 8, offset=-40, unit="C"),
            ),
            message(2, "Rear", signal("speed", 0, 8, unit="km/h")),
        )
        self.events = list(
            decode_frames(
                iter_candump(["(1.5) can0 1#0A32", "can0 2#14"]),
                self.database,
            )
        )

    def test_long_csv_has_one_row_per_decoded_signal(self):
        output = io.StringIO()

        count = write_csv(self.events, output, self.database, mode="long")
        rows = list(csv.DictReader(io.StringIO(output.getvalue())))

        self.assertEqual(count, 2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["timestamp"], "1.5")
        self.assertEqual(rows[0]["can_id"], "0x1")
        self.assertEqual(rows[0]["signal"], "speed")
        self.assertEqual(rows[1]["value"], "10.0")
        self.assertEqual(rows[2]["timestamp"], "")

    def test_sparse_wide_csv_qualifies_duplicate_signal_names(self):
        output = io.StringIO()

        write_csv(self.events, output, self.database, mode="wide")
        reader = csv.DictReader(io.StringIO(output.getvalue()))
        rows = list(reader)

        self.assertEqual(
            reader.fieldnames,
            [
                "timestamp",
                "interface",
                "can_id",
                "is_extended",
                "message",
                "Front.speed",
                "temperature",
                "Rear.speed",
            ],
        )
        self.assertEqual(rows[0]["Front.speed"], "10.0")
        self.assertEqual(rows[0]["Rear.speed"], "")
        self.assertEqual(rows[1]["Front.speed"], "")
        self.assertEqual(rows[1]["Rear.speed"], "20.0")

    def test_wide_csv_qualifies_reserved_signal_names(self):
        database = definitions(
            message(3, "Clock", signal("timestamp", 0, 8))
        )
        events = list(decode_frames(iter_candump(["(4.5) 3#07"]), database))
        output = io.StringIO()

        write_csv(events, output, database, mode="wide")
        row = next(csv.DictReader(io.StringIO(output.getvalue())))

        self.assertEqual(row["timestamp"], "4.5")
        self.assertEqual(row["Clock.timestamp"], "7.0")

    def test_file_output_is_atomic_when_late_input_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "input.log"
            output_path = directory / "output.csv"
            input_path.write_text("1#0100\ninvalid frame\n", encoding="utf-8")
            output_path.write_text("existing output\n", encoding="utf-8")

            with self.assertRaises(CandumpParseError):
                write_csv_file(
                    decode_frames(read_candump(input_path), self.database),
                    output_path,
                    self.database,
                    mode="wide",
                )

            self.assertEqual(output_path.read_text(encoding="utf-8"), "existing output\n")

    def test_atomic_output_preserves_existing_permissions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "output.csv"
            output_path.write_text("existing output\n", encoding="utf-8")
            output_path.chmod(0o640)

            write_csv_file(self.events, output_path, self.database, mode="wide")

            self.assertEqual(os.stat(output_path).st_mode & 0o777, 0o640)

    def test_cli_is_quiet_unless_verbose(self):
        source = {
            "params": [message(1, "Status", signal("value", 0, 8))]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            input_path = directory / "input.log"
            definition_path = directory / "source.json"
            output_path = directory / "output.csv"
            input_path.write_text("1#2A\n", encoding="utf-8")
            definition_path.write_text(json.dumps(source), encoding="utf-8")

            quiet_output = io.StringIO()
            with redirect_stdout(quiet_output):
                result = cli_main(
                    [str(input_path), str(definition_path), str(output_path)]
                )
            self.assertEqual(result, 0)
            self.assertEqual(quiet_output.getvalue(), "")
            self.assertTrue(output_path.read_text(encoding="utf-8").startswith("timestamp,"))

            verbose_output = io.StringIO()
            with redirect_stdout(verbose_output):
                cli_main(
                    [
                        str(input_path),
                        str(definition_path),
                        str(output_path),
                        "--format",
                        "wide",
                        "--verbose",
                    ]
                )
            self.assertIn("Wrote 1 decoded message events", verbose_output.getvalue())


if __name__ == "__main__":
    unittest.main()
