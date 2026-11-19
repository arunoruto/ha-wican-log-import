"""Manual check against a real WiCAN, without Home Assistant.

Lists the logs, downloads one, and prints the schema and parsed rows. Writes
nothing anywhere except the downloaded file.

    python3 scripts/manual_check.py 192.168.1.118            # list + newest closed log
    python3 scripts/manual_check.py 192.168.1.118 FILE.db    # a specific log
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "wican_log_import"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"wli_{name}", PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


api = _load("api")
importer = _load("importer")


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


async def main(address: str, filename: str | None) -> None:
    async with aiohttp.ClientSession() as session:
        client = api.WiCanClient(session, address)
        try:
            status = await client.async_get_status()
        except api.WiCanConnectionError as err:
            print(f"WiCAN not reachable (asleep?): {err}")
            return
        print("check_status:", status)
        listing = await client.async_list_logs()
        print(f"\ncurrent_db: {listing.current_db}")
        for db in listing.databases:
            mark = " (active)" if db.filename == listing.current_db else ""
            print(f"  {db.filename:30} {db.created:22} {db.size:>10} {db.status}{mark}")
        if filename is None:
            closed = [d for d in listing.databases if d.filename != listing.current_db]
            pool = closed or listing.databases
            if not pool:
                print("No logs on the SD card.")
                return
            filename = max(pool, key=lambda d: (d.created, d.filename)).filename
        print(f"\nDownloading {filename} ...")
        started = time.monotonic()
        data = await client.async_download_log(filename)
        print(f"{len(data)} bytes in {time.monotonic() - started:.1f} s")

    out = Path(tempfile.mkdtemp(prefix="wican_")) / filename
    out.write_bytes(data)
    result = importer.read_log_database(out)
    print(f"\nSaved to {out}")
    print("Tables/columns:", result.schema.tables)
    print(
        f"Rows: {result.rows_total}, numeric: {len(result.readings)}, "
        f"non-numeric: {result.rows_non_numeric}, fallback used: {result.used_fallback}"
    )
    valid, discarded = importer.filter_timestamps(result.readings, time.time())
    print(f"Valid timestamps: {len(valid)}, discarded: {discarded}")
    if valid:
        print(f"Time range: {_fmt(valid[0].ts)} .. {_fmt(valid[-1].ts)}")
    counts = Counter(r.name for r in valid)
    print("\nParameters (rows):")
    for name, count in sorted(counts.items()):
        print(f"  {name:40} {count}")
    print("\nFirst 20 rows:")
    for r in valid[:20]:
        print(f"  {_fmt(r.ts)}  {r.name:35} {r.value}")
    buckets = importer.bucket_hourly(valid)
    print("\nHourly buckets (first 3 parameters):")
    for name in sorted(buckets)[:3]:
        for hour, agg in sorted(buckets[name].items()):
            print(
                f"  {name:35} {_fmt(hour)}  n={agg.count:4} mean={agg.mean:.2f} "
                f"min={agg.minimum} max={agg.maximum}"
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
