#!/usr/bin/env python3
# apply_zha_names_from_csv.py
#
# Update Home Assistant ZHA device custom names (name_by_user) from a semicolon-separated CSV.
# Interactive: shows what will change, asks for confirmation, then applies.
#
# CSV must contain at least: ieee;custom_name
#
# Usage:
#   python apply_zha_names_from_csv.py --csv path\zigbee_devices.csv
#
# Notes:
# - Matches by IEEE via Device Registry ("zha", "<ieee>").
# - Only updates device "custom name" (name_by_user).
# - Does NOT touch entity_ids, so automations and dashboards should keep working.

import os
import sys
import csv
import json
import argparse
import asyncio
from pathlib import Path

# -------- EDIT THESE DEFAULTS --------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# ------------------------------------


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
        await self.ws.recv()  # auth_required
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


def read_semicolon_csv(path: Path):
    """Read CSV with delimiter=';' and UTF-8 (BOM ok). Returns list of dicts with lowercase keys."""
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f, delimiter=";")
        rows = list(rdr)
    norm = []
    for r in rows:
        norm.append({(k.lower() if isinstance(k, str) else k): v for k, v in r.items()})
    return norm


async def load_device_maps(ha: HAWS):
    """
    Build helper structures:
    - ieee_to_dev_id: ieee -> device_id (from device registry)
    - dev_id_to_names: device_id -> (current custom name, discovered name)
    - coordinator_ieee: ieee of coordinator (nwk 0x0000)
    """
    devreg = await ha.call("config/device_registry/list")
    zha_devices = await ha.call("zha/devices")

    ieee_to_dev_id = {}
    for d in devreg:
        for ident in d.get("identifiers", []):
            if (
                isinstance(ident, (list, tuple))
                and len(ident) == 2
                and str(ident[0]).lower() == "zha"
            ):
                ieee_to_dev_id[str(ident[1]).lower()] = d["id"]

    dev_id_to_names = {
        d["id"]: (d.get("name_by_user"), d.get("name"))
        for d in devreg
    }

    coordinator_ieee = None
    for zd in zha_devices:
        nwk = zd.get("nwk")
        if (isinstance(nwk, int) and nwk == 0) or str(nwk) in ("0", "0000"):
            coordinator_ieee = (zd.get("ieee") or "").lower()
            break

    return ieee_to_dev_id, dev_id_to_names, coordinator_ieee


async def plan_changes(csv_rows, ha_url, token):
    """
    Figure out what would change, but don't apply it yet.
    Returns:
      - changes: list of dicts {ieee, dev_id, old_name, new_name}
      - stats   various counters for skipped reasons
    """
    uri = ha_url.rstrip("/") + "/api/websocket"
    changes = []
    skipped_missing = 0
    skipped_empty = 0
    skipped_same = 0
    skipped_coordinator = 0
    skipped_no_ieee = 0

    async with HAWS(uri, token) as ha:
        ieee_to_dev_id, dev_id_to_names, coordinator_ieee = await load_device_maps(ha)

    for row in csv_rows:
        ieee = (row.get("ieee") or "").strip().lower()
        new_name = (row.get("custom_name") or "").strip()

        if not ieee:
            skipped_no_ieee += 1
            continue

        # don't rename coordinator
        if coordinator_ieee and ieee == coordinator_ieee:
            skipped_coordinator += 1
            continue

        if not new_name or new_name == "-":
            skipped_empty += 1
            continue

        dev_id = ieee_to_dev_id.get(ieee)
        if not dev_id:
            # ghost / not in registry / typo
            skipped_missing += 1
            continue

        old_name_by_user, old_discovered = dev_id_to_names.get(dev_id, (None, None))
        old_custom = old_name_by_user or ""
        if new_name == old_custom:
            skipped_same += 1
            continue

        changes.append({
            "ieee": ieee,
            "dev_id": dev_id,
            "old_name": old_custom,
            "new_name": new_name,
        })

    stats = {
        "skipped_missing": skipped_missing,
        "skipped_empty": skipped_empty,
        "skipped_same": skipped_same,
        "skipped_coordinator": skipped_coordinator,
        "skipped_no_ieee": skipped_no_ieee,
    }

    return changes, stats


async def apply_changes(changes, ha_url, token):
    """Actually send config/device_registry/update for each change."""
    uri = ha_url.rstrip("/") + "/api/websocket"
    async with HAWS(uri, token) as ha:
        for ch in changes:
            await ha.call(
                "config/device_registry/update",
                device_id=ch["dev_id"],
                name_by_user=ch["new_name"],
            )
            print(f"✓ {ch['ieee']}  '{ch['old_name']}' -> '{ch['new_name']}'")


def main():
    ap = argparse.ArgumentParser(
        description="Interactively apply custom device names to ZHA from a ;-separated CSV (ieee;custom_name)."
    )
    # --csv is optional now; default to zigbee_devices.csv next to this script
    ap.add_argument("--csv", help="Path to semicolon-separated CSV. Defaults to ./zigbee_devices.csv next to this script.")
    ap.add_argument("--url", default=HA_URL_DEFAULT, help="Home Assistant base URL (ws:// or wss://)")
    ap.add_argument("--token", default=None, help="Long-lived token (overrides hardcoded)")
    args = ap.parse_args()

    token = args.token or os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: Set HARD_CODED_TOKEN or pass --token / HASS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    script_dir = Path(__file__).resolve().parent
    csv_path = Path(args.csv) if args.csv else (script_dir / "zigbee_devices.csv")

    if not csv_path.exists():
        print(f"ERROR: CSV not found at:\n  {csv_path}\n"
              f"Tip: export with --csv or place your edited zigbee_devices.csv next to this script.", file=sys.stderr)
        sys.exit(2)

    # 1. Read CSV
    csv_rows = read_semicolon_csv(csv_path)
    required_cols = {"ieee", "custom_name"}
    if not csv_rows or not required_cols.issubset(set(csv_rows[0].keys())):
        print(f"ERROR: CSV must have columns: ieee;custom_name (semicolon-separated). "
              f"Found columns: {list(csv_rows[0].keys()) if csv_rows else 'NONE'}", file=sys.stderr)
        sys.exit(2)

    # 2. Plan/preview
    changes, stats = asyncio.run(plan_changes(csv_rows, args.url, token))

    if not changes:
        print("Nothing to update.")
        print("Stats:")
        print(f"  Skipped (empty or '-')           : {stats['skipped_empty']}")
        print(f"  Skipped (same as current)        : {stats['skipped_same']}")
        print(f"  Skipped (not found / ghost)      : {stats['skipped_missing']}")
        print(f"  Skipped (coordinator)            : {stats['skipped_coordinator']}")
        print(f"  Skipped (no ieee in row)         : {stats['skipped_no_ieee']}")
        sys.exit(0)

    print(f"CSV: {csv_path}")
    print("\nThe following devices will be renamed:\n")
    for ch in changes:
        old_display = ch["old_name"] if ch["old_name"] else "(no custom name)"
        print(f"- {ch['ieee']}:  {old_display}  ->  {ch['new_name']}")
    print("\nStats:")
    print(f"  Will update                      : {len(changes)}")
    print(f"  Skipped (empty or '-')           : {stats['skipped_empty']}")
    print(f"  Skipped (same as current)        : {stats['skipped_same']}")
    print(f"  Skipped (not found / ghost)      : {stats['skipped_missing']}")
    print(f"  Skipped (coordinator)            : {stats['skipped_coordinator']}")
    print(f"  Skipped (no ieee in row)         : {stats['skipped_no_ieee']}")
    print()

    # 3. Ask user for confirmation
    try:
        answer = input("Apply these changes now? (y/n) ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)

    if answer not in ("y", "yes"):
        print("Aborted. No changes applied.")
        sys.exit(0)

    # 4. Apply for real
    asyncio.run(apply_changes(changes, args.url, token))
    print("\nDone.")


if __name__ == "__main__":
    main()