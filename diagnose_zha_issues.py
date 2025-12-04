#!/usr/bin/env python3
# diagnose_zha_issues.py
#
# Quick ZHA health report:
# - Offline/unavailable devices
# - Unknown manufacturer/model
# - Devices with no entities
# - Entities with 'unavailable' / 'unknown' state
# - Low battery devices (battery < BATTERY_THRESHOLD)
#
# Usage:
#   python diagnose_zha_issues.py
#
# Optional:
#   python diagnose_zha_issues.py --url ws://homeassistant.local:8123 --token YOUR_TOKEN

import os
import sys
import json
import asyncio
import argparse
from datetime import datetime

# -------- EDIT THESE DEFAULTS --------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
BATTERY_THRESHOLD = 25.0  # % - tweak if you want
# ------------------------------------


try:
    import websockets  # pip install websockets
except ImportError:
    print("Missing dependency: websockets. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


class HAWS:
    """Minimal Home Assistant WebSocket helper (same style as your other scripts)."""
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri, max_size=20_000_000)
        # auth_required
        await self.ws.recv()
        # send auth
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
            raw = await self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._id and data.get("type") == "result":
                if not data.get("success", False):
                    raise RuntimeError(f"{typ} failed: {data}")
                return data["result"]


async def collect_data(ha: HAWS):
    """Fetch ZHA + registries + states."""
    print("Fetching ZHA devices…")
    zha_devices = await ha.call("zha/devices")

    print("Fetching device registry…")
    devreg = await ha.call("config/device_registry/list")

    print("Fetching entity registry…")
    entreg = await ha.call("config/entity_registry/list")

    print("Fetching states…")
    states = await ha.call("get_states")

    return zha_devices, devreg, entreg, states


def build_indexes(zha_devices, devreg, entreg, states):
    """Build helper maps for diagnostics."""
    # device_id -> device registry record
    dev_id_to_dev = {d["id"]: d for d in devreg}

    # ieee -> device_id (from device_registry identifiers)
    ieee_to_dev_id = {}
    for d in devreg:
        for ident in d.get("identifiers", []):
            if (
                isinstance(ident, (list, tuple))
                and len(ident) == 2
                and str(ident[0]).lower() == "zha"
            ):
                ieee_to_dev_id[str(ident[1]).lower()] = d["id"]

    # device_id -> list of entity registry records
    dev_id_to_entities = {}
    for e in entreg:
        dev_id = e.get("device_id")
        if not dev_id:
            continue
        dev_id_to_entities.setdefault(dev_id, []).append(e)

    # entity_id -> state object
    entity_id_to_state = {s["entity_id"]: s for s in states}

    # ieee -> zha_device record
    ieee_to_zha = {(d.get("ieee") or "").lower(): d for d in zha_devices}

    return ieee_to_dev_id, dev_id_to_dev, dev_id_to_entities, entity_id_to_state, ieee_to_zha


def diagnose(zha_devices, ieee_to_dev_id, dev_id_to_dev, dev_id_to_entities, entity_id_to_state):
    """Run various checks and print report."""
    offline_devices = []
    unknown_model_devices = []
    devices_without_entities = []
    entity_unavailable = []
    low_battery = []

    print("\n=== ZHA Diagnostics per device ===\n")

    for z in zha_devices:
        ieee = (z.get("ieee") or "").lower()
        manuf = (z.get("manufacturer") or "").strip()
        model = (z.get("model") or "").strip()
        nwk = z.get("nwk")
        # available flag is often present for ZHA devices; if not, we skip
        available = z.get("available", True)

        dev_id = ieee_to_dev_id.get(ieee)
        devreg_dev = dev_id_to_dev.get(dev_id) if dev_id else None
        dev_name = devreg_dev.get("name_by_user") or devreg_dev.get("name") if devreg_dev else ""
        if not dev_name:
            dev_name = z.get("name") or ""

        ent_list = dev_id_to_entities.get(dev_id, [])

        problems = []

        # Offline?
        if available is False:
            problems.append("UNAVAILABLE (offline?)")
            offline_devices.append((ieee, dev_name or model or manuf))

        # Unknown manufacturer/model?
        low_manuf = manuf.lower()
        low_model = model.lower()
        if (
            not manuf
            or not model
            or low_manuf.startswith("unk")
            or low_model.startswith("unk")
        ):
            problems.append("Unknown manufacturer/model")
            unknown_model_devices.append((ieee, manuf, model))

        # No entities?
        if not ent_list:
            problems.append("No entities in entity_registry")
            devices_without_entities.append((ieee, dev_name or model or manuf))

        # Battery + unavailable entities
        dev_low_battery = []
        dev_unavail_ents = []

        for e in ent_list:
            eid = e.get("entity_id")
            st = entity_id_to_state.get(eid)
            if not st:
                continue

            state_val = st.get("state")
            attrs = st.get("attributes", {})

            if state_val in ("unavailable", "unknown"):
                dev_unavail_ents.append(eid)
                entity_unavailable.append((eid, state_val))

            # battery detection: look for 'battery' in entity_id or device_class
            eid_lower = eid.lower()
            dev_cls = str(attrs.get("device_class") or "").lower()
            if "battery" in eid_lower or dev_cls == "battery":
                try:
                    batt = float(state_val)
                except (TypeError, ValueError):
                    continue
                if batt < BATTERY_THRESHOLD:
                    dev_low_battery.append((eid, batt))
                    low_battery.append((ieee, dev_name or model or manuf, eid, batt))

        if dev_unavail_ents:
            problems.append(f"{len(dev_unavail_ents)} entity(ies) unavailable/unknown")
        if dev_low_battery:
            problems.append(f"{len(dev_low_battery)} low-battery entity(ies)")

        # Print per-device header
        title = dev_name or f"{manuf} {model}".strip() or "(unnamed device)"
        print(f"IEEE: {ieee}  NWK: {nwk}  Name: {title}")
        print(f"  Manufacturer: {manuf or '-'}")
        print(f"  Model       : {model or '-'}")

        if problems:
            print("  ⚠ Issues:")
            for p in problems:
                print(f"    - {p}")
        else:
            print("  ✓ No obvious issues")

        # Optionally print detailed low-battery / unavailable entities for this device
        if dev_unavail_ents:
            print("    Unavailable entities:")
            for eid in dev_unavail_ents:
                print(f"      - {eid}")
        if dev_low_battery:
            print("    Low-battery entities:")
            for eid, batt in dev_low_battery:
                print(f"      - {eid}: {batt}%")

        print()

    # Summary section
    print("\n=== Summary ===\n")

    print(f"Total ZHA devices: {len(zha_devices)}\n")

    print(f"Offline/unavailable devices: {len(offline_devices)}")
    for ieee, name in offline_devices:
        print(f"  - {ieee}  ({name})")
    print()

    print(f"Devices with unknown manufacturer/model: {len(unknown_model_devices)}")
    for ieee, manuf, model in unknown_model_devices:
        print(f"  - {ieee}  manuf='{manuf}'  model='{model}'")
    print()

    print(f"Devices with NO entities: {len(devices_without_entities)}")
    for ieee, name in devices_without_entities:
        print(f"  - {ieee}  ({name})")
    print()

    print(f"Entities with state 'unavailable'/'unknown': {len(entity_unavailable)}")
    for eid, st in entity_unavailable[:30]:
        print(f"  - {eid}: {st}")
    if len(entity_unavailable) > 30:
        print(f"  ... and {len(entity_unavailable) - 30} more")
    print()

    print(f"Low-battery devices (threshold {BATTERY_THRESHOLD}%): {len(low_battery)}")
    for ieee, name, eid, batt in low_battery:
        print(f"  - {ieee} ({name})  {eid}: {batt}%")
    print()


async def async_main(args):
    token = args.token or os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: Set HARD_CODED_TOKEN or pass --token / HASS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    uri = args.url.rstrip("/") + "/api/websocket"
    print(f"Connecting to Home Assistant at {uri} ...")

    async with HAWS(uri, token) as ha:
        zha_devices, devreg, entreg, states = await collect_data(ha)

    ieee_to_dev_id, dev_id_to_dev, dev_id_to_entities, entity_id_to_state, ieee_to_zha = \
        build_indexes(zha_devices, devreg, entreg, states)

    diagnose(zha_devices, ieee_to_dev_id, dev_id_to_dev, dev_id_to_entities, entity_id_to_state)


def main():
    ap = argparse.ArgumentParser(
        description="Diagnose common ZHA issues: offline devices, unknown models, no entities, low battery, unavailable entities."
    )
    ap.add_argument("--url", default=HA_URL_DEFAULT, help="Home Assistant base URL (ws:// or wss://)")
    ap.add_argument("--token", default=None, help="Long-lived token (overrides hardcoded)")
    args = ap.parse_args()

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
