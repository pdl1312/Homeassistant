#!/usr/bin/env python3
# make_rename_template_from_export.py
#
# Read zigbee_devices.csv (exported by export_zha_devices_to_csv.py)
# and create zigbee_devices_rename.csv with one row per device:
#   ieee;model;device_name;custom_name
#
# You can then edit the custom_name column and use that file
# with apply_zha_names_from_csv.py.

import csv
from pathlib import Path

INPUT_FILE = "zigbee_devices.csv"
OUTPUT_FILE = "zigbee_devices_rename.csv"

def main():
    script_dir = Path(__file__).resolve().parent
    src = script_dir / INPUT_FILE
    dst = script_dir / OUTPUT_FILE

    if not src.exists():
        print(f"Input file not found: {src}")
        return

    devices_by_ieee = {}

    with src.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ieee = (row.get("ieee") or "").strip().lower()
            if not ieee:
                continue
            if ieee in devices_by_ieee:
                # already have this ieee, skip extra entity rows
                continue

            devices_by_ieee[ieee] = {
                "ieee": ieee,
                "model": row.get("model") or "",
                "device_name": row.get("device_name") or "",
                "custom_name": "",  # leave empty for you to fill
            }

    # Write simplified rename template
    fieldnames = ["ieee", "model", "device_name", "custom_name"]
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for dev in devices_by_ieee.values():
            writer.writerow(dev)

    print(f"Wrote {len(devices_by_ieee)} devices to {dst}")

if __name__ == "__main__":
    main()
