"""Benchmark MCAP extraction and write an exact sparse-data fingerprint."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import resource
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(
    os.environ.get("VDPA_SOURCE_ROOT", Path(__file__).resolve().parents[1])
).resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data_handler import DataFile


def _update_hash(digest: Any, value: str | bytes) -> None:
    data = value.encode() if isinstance(value, str) else value
    digest.update(len(data).to_bytes(8, "little"))
    digest.update(data)


def fingerprint_dataframe(data: pd.DataFrame) -> dict:
    """Return hashes that capture column order, row order, values, and missingness."""
    frame_digest = hashlib.sha256()
    columns = []

    for name in data.columns:
        series = data[name]
        column_digest = hashlib.sha256()
        _update_hash(column_digest, str(name))
        _update_hash(column_digest, str(series.dtype))
        _update_hash(column_digest, str(len(series)))

        if isinstance(series.dtype, pd.SparseDtype):
            sparse_array = series.array
            indices = np.asarray(
                sparse_array.sp_index.to_int_index().indices,
                dtype="<i8",
            )
            values = np.asarray(sparse_array.sp_values)
            _update_hash(column_digest, indices.tobytes())
            _update_hash(column_digest, values.dtype.str)
            _update_hash(column_digest, values.tobytes())
            stored_values = len(values)
        else:
            values = pd.util.hash_pandas_object(series, index=False).to_numpy(
                dtype="<u8",
                copy=False,
            )
            _update_hash(column_digest, values.tobytes())
            stored_values = int(series.notna().sum())

        column_hash = column_digest.hexdigest()
        _update_hash(frame_digest, column_hash)
        columns.append(
            {
                "name": str(name),
                "dtype": str(series.dtype),
                "stored_values": stored_values,
                "sha256": column_hash,
            }
        )

    return {
        "shape": list(data.shape),
        "sha256": frame_digest.hexdigest(),
        "columns": columns,
    }


def _process_tree_rss_kib(root_pid: int) -> int:
    pending = [root_pid]
    seen = set()
    total = 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1])
                    break
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text()
            pending.extend(int(child) for child in children.split())
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total


def _monitor_memory(stop: threading.Event, result: list[int]) -> None:
    peak = 0
    while not stop.wait(0.05):
        peak = max(peak, _process_tree_rss_kib(os.getpid()))
    result.append(max(peak, _process_tree_rss_kib(os.getpid())))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mcap", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    data_file = DataFile(args.mcap, config_path=args.config)

    memory_stop = threading.Event()
    memory_result = []
    memory_thread = threading.Thread(
        target=_monitor_memory,
        args=(memory_stop, memory_result),
        daemon=True,
    )
    memory_thread.start()
    started = time.perf_counter()
    try:
        data_file.read()
        elapsed = time.perf_counter() - started
    finally:
        memory_stop.set()
        memory_thread.join()
    if not data_file.read_success:
        raise RuntimeError(f"Failed to read {args.mcap}")

    fingerprint_started = time.perf_counter()
    fingerprint = fingerprint_dataframe(data_file.data)
    fingerprint_elapsed = time.perf_counter() - fingerprint_started
    fingerprint.update(
        {
            "mcap": str(args.mcap.resolve()),
            "read_seconds": elapsed,
            "fingerprint_seconds": fingerprint_elapsed,
            "parent_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "children_max_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "process_tree_peak_rss_kib": memory_result[0],
        }
    )

    args.manifest.write_text(json.dumps(fingerprint, indent=2) + "\n")
    logging.info(
        "Read %d rows and %d columns in %.2f seconds; fingerprint %s",
        data_file.data.shape[0],
        data_file.data.shape[1],
        elapsed,
        fingerprint["sha256"],
    )


if __name__ == "__main__":
    main()
