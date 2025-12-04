#!/usr/bin/env python3
# export_zha_devices_to_csv.py
#
# Export all ZHA devices and their entities to a semicolon-separated CSV.
# Includes an empty "custom_name" column you can fill and later feed into
# apply_zha_names_from_csv.py (which expects ieee;custom_name).
#
# Usage:
#   python export_zha_devices_to_csv.py --csv zigbee_devices.csv
#
# If --csv is omitted, it writes ./zigbee_devices.csv next to this script.

import os
import sys
import csv
import json
import argparse
import asyncio
from pathlib import Path

# -------- SAME DEFAULTS AS YOUR WORKING SCRIPT --------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# ------------------------------------------------------


try:
    import websockets  # pip install websockets
except ImportError:
    print("Missing dependency: websockets. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


class HAWS:
    """Minimal Home Assistant WebSocket helper."""
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri, max_size=20_000_000)
        # first message: auth_required
        await self.ws.recv()
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_ok = await self.ws.recv()
        if '"auth_ok"' not in auth_ok:
            raise RuntimeError(f"Auth failed: {auth_ok}")
        return self

    async def __aexit__(self, *exc):
        if self.ws:
            await self.ws.close()

    async def call(self, typ, **kwargs):
        self._id += 1
        msg = {"id": self._id, "type": typ}
        msg.update(kwargs)
        await self.ws.send(json.dumps(msg))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == self._id and data.get("type") == "result":
                if not data.get("success", False):
                    raise RuntimeError(f"{typ} failed: {data}")
                return data["result"]


async def fetch_zha_devices(ha_url, token):
    """Fetch ZHA devices (each includes its entities list)."""
    uri = ha_url.rstrip("/") + "/api/websocket"
    async with HAWS(uri, token) as ha:
        devices = await ha.call("zha/devices")
    return devices


def nwk_to_str(nwk):
    if isinstance(nwk, int):
        return f"0x{nwk:04X}"
    return str(nwk) if nwk is not None else ""


def write_csv(devices, csv_path: Path):
    fieldnames = [
        "ieee",
        "nwk",
        "model",
        "device_name",
        "entity_id",
        "entity_name",
        "domain",
        "custom_name",  # empty; you can fill this and feed into apply_zha_names_from_csv.py
    ]

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for dev in devices:
            ieee = (dev.get("ieee") or "").lower()
            nwk = nwk_to_str(dev.get("nwk"))
            model = dev.get("model") or ""
            dev_name = dev.get("user_given_name") or dev.get("name") or ""

            # Skip coordinator (nwk 0x0000) – you rarely want to rename it
            if dev.get("nwk") in (0, "0", "0000"):
                continue

            ent_list = dev.get("entities") or []
            if not ent_list:
                # device with no entities
                writer.writerow({
                    "ieee": ieee,
                    "nwk": nwk,
                    "model": model,
                    "device_name": dev_name,
                    "entity_id": "",
                    "entity_name": "",
                    "domain": "",
                    "custom_name": "",
                })
            else:
                for ent in ent_list:
                    entity_id = ent.get("entity_id") or ""
                    entity_name = ent.get("name") or ""
                    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
                    writer.writerow({
                        "ieee": ieee,
                        "nwk": nwk,
                        "model": model,
                        "device_name": dev_name,
                        "entity_id": entity_id,
                        "entity_name": entity_name,
                        "domain": domain,
                        "custom_name": "",
                    })


async def async_main(args):
    token = args.token or os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: Set HARD_CODED_TOKEN or pass --token / HASS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    script_dir = Path(__file__).resolve().parent
    csv_path = Path(args.csv) if args.csv else (script_dir / "zigbee_devices.csv")

    print(f"Connecting to Home Assistant at {args.url} ...")
    devices = await fetch_zha_devices(args.url, token)
    print(f"Fetched {len(devices)} ZHA devices.")

    print(f"Writing CSV to: {csv_path}")
    write_csv(devices, csv_path)
    print("Done.")


def main():
    ap = argparse.ArgumentParser(
        description="Export ZHA devices + entities to a semicolon-separated CSV."
    )
    ap.add_argument("--csv", help="Path to output CSV. Defaults to ./zigbee_devices.csv next to this script.")
    ap.add_argument("--url", default=HA_URL_DEFAULT, help="Home Assistant base URL (ws:// or wss://)")
    ap.add_argument("--token", default=None, help="Long-lived token (overrides hardcoded)")
    args = ap.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
