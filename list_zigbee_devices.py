#!/usr/bin/env python3
"""
list_zigbee_devices.py — prints all Zigbee devices from Home Assistant (ZHA)

Hardcoded token version.

Usage examples:
  python3 list_zigbee_devices.py
  python3 list_zigbee_devices.py --url ws://homeassistant.local:8123
  python3 list_zigbee_devices.py --json
  # You can still override:
  python3 list_zigbee_devices.py --token OVERRIDE_TOKEN
  HASS_TOKEN=OVERRIDE_TOKEN python3 list_zigbee_devices.py
"""

import os
import sys
import json
import csv
import argparse
import asyncio
import datetime

# ------------------ HARD-CODED SETTINGS (edit these) ------------------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# ----------------------------------------------------------------------
try:
    import websockets  # pip install websockets
except ImportError:
    print("Missing dependency: websockets. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


def human_dt(ts):
    if not ts:
        return "-"
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        try:
            return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)


class HAWS:
    """Tiny helper for HA WebSocket calls."""

    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self._id = 1
        self.ws = None

    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri, max_size=20_000_000)
        _ = await self.ws.recv()  # auth_required
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_ok = await self.ws.recv()
        if '"auth_ok"' not in auth_ok:
            raise RuntimeError(f"Auth failed: {auth_ok}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
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
            # ignore other events


async def fetch_all(url, token):
    uri = url.rstrip("/") + "/api/websocket"
    async with HAWS(uri, token) as ha:
        # 1) ZHA devices (includes ieee, nwk, manufacturer, model, power_source, last_seen, device_reg_id)
        zha_devices = await ha.call("zha/devices")

        # 2) Device Registry (to get custom name / user name, etc.)
        devreg = await ha.call("config/device_registry/list")
        devreg_by_id = {d["id"]: d for d in devreg}

        # Map IEEE -> device registry entry (identifiers usually like ["zha", "<ieee>"])
        devreg_by_ieee = {}
        for d in devreg:
            for ident in d.get("identifiers", []):
                if isinstance(ident, (list, tuple)) and len(ident) == 2 and str(ident[0]).lower() == "zha":
                    devreg_by_ieee[str(ident[1]).lower()] = d

        # 3) Entity Registry (to find battery entities tied to device_id)
        entreg = await ha.call("config/entity_registry/list")
        ents_by_device = {}
        for e in entreg:
            ents_by_device.setdefault(e.get("device_id"), []).append(e)

        # 4) Live states (to read battery % now)
        states = await ha.call("get_states")
        states_by_entity_id = {s.get("entity_id"): s for s in states}

    # Merge data
    enriched = []
    for d in zha_devices:
        ieee = (d.get("ieee") or "").lower()
        dev_id = d.get("device_reg_id")

        # find device registry entry
        dev_entry = devreg_by_id.get(dev_id) or devreg_by_ieee.get(ieee)

        # derive custom name
        custom_name = "-"
        if dev_entry:
            custom_name = dev_entry.get("name_by_user") or dev_entry.get("name") or "-"

        # find a battery entity for this device
        battery = "-"
        battery_entity_id = "-"
        if dev_entry:
            for ent in ents_by_device.get(dev_entry["id"], []):
                ent_id = ent.get("entity_id") or f"{ent.get('domain')}.{ent.get('unique_id')}"
                st = states_by_entity_id.get(ent_id)
                if not st:
                    continue
                attrs = st.get("attributes", {})
                device_class = (attrs.get("device_class") or ent.get("device_class") or ent.get(
                    "original_device_class") or "").lower()
                unit = (attrs.get("unit_of_measurement") or "").lower()
                # Prefer device_class=battery with % state
                if device_class == "battery" and st.get("state") not in (None, "unknown", "unavailable"):
                    if unit in ("%", "percent") or st.get("state").isdigit():
                        battery = f"{st.get('state')}{attrs.get('unit_of_measurement', '')}"
                        battery_entity_id = ent_id
                        break
                # Fallback: entity_id contains 'battery' with % unit
                if "battery" in ent.get("entity_id", "") and st.get("state") not in (
                None, "unknown", "unavailable") and unit in ("%", "percent"):
                    battery = f"{st.get('state')}{attrs.get('unit_of_measurement', '')}"
                    battery_entity_id = ent_id
                    break

        enriched.append({
            "name": d.get("name") or d.get("user_given_name") or "-",
            "custom_name": custom_name,
            "ieee": d.get("ieee", "-"),
            "nwk": d.get("nwk"),
            "manufacturer": d.get("manufacturer", "-"),
            "model": d.get("model", "-"),
            "power_source": d.get("power_source", "-"),
            "last_seen": d.get("last_seen"),
            "battery": battery,
            "battery_entity_id": battery_entity_id,
        })
    return enriched


def print_table(devs):
    headers = ["Name", "Custom name", "IEEE", "NWK", "Manufacturer", "Model", "Power", "Battery", "Last seen"]
    rows = []
    for d in devs:
        nwk_val = d.get("nwk")
        nwk = (hex(nwk_val)[2:].zfill(4) if isinstance(nwk_val, int) else (nwk_val or "-"))
        rows.append([
            d.get("name", "-"),
            d.get("custom_name", "-"),
            d.get("ieee", "-"),
            nwk,
            d.get("manufacturer", "-"),
            d.get("model", "-"),
            d.get("power_source", "-"),
            d.get("battery", "-"),
            human_dt(d.get("last_seen")),
        ])
    widths = [max(len(str(x)) for x in [h] + [r[i] for r in rows]) for i, h in enumerate(headers)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for r in rows:
        print(fmt.format(*r))
    print(f"\nTotal: {len(rows)} device(s)")


def write_csv_semicolon(devs, path=None):
    """Write UTF-8 CSV with semicolon delimiter for Excel-friendly import in EU locales."""
    if not path or path is True:  # True when user passed --csv without value
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        path = f"zigbee_devices_{ts}.csv"

    headers = ["name", "custom_name", "ieee", "nwk", "manufacturer", "model", "power_source", "battery",
               "battery_entity_id", "last_seen"]
    # Use utf-8-sig to include BOM so Excel picks UTF-8 correctly on Windows
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f, delimiter=";")
        wr.writerow(headers)
        for d in devs:
            nwk_val = d.get("nwk")
            nwk = (hex(nwk_val)[2:].zfill(4) if isinstance(nwk_val, int) else (nwk_val or "-"))
            wr.writerow([
                d.get("name", "-"),
                d.get("custom_name", "-"),
                d.get("ieee", "-"),
                nwk,
                d.get("manufacturer", "-"),
                d.get("model", "-"),
                d.get("power_source", "-"),
                d.get("battery", "-"),
                d.get("battery_entity_id", "-"),
                human_dt(d.get("last_seen")),
            ])
    return path


def main():
    p = argparse.ArgumentParser(description="List Zigbee devices (ZHA) with custom name, battery info, and CSV export.")
    p.add_argument("--url", default=HA_URL_DEFAULT, help="HA base URL (ws:// or wss://)")
    p.add_argument("--token", default=None, help="Long-lived access token (overrides hard-coded)")
    p.add_argument("--json", action="store_true", help="Output raw JSON instead of table")
    # --csv optional arg: if provided without value → default timestamped file; with value → that path
    p.add_argument("--csv", nargs="?", const=True,
                   help="Write a semicolon-separated CSV (optional: provide output path)")
    args = p.parse_args()

    token = args.token or os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token.strip() == "" or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: Set HARD_CODED_TOKEN in the script or pass --token / HASS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    try:
        devices = asyncio.run(fetch_all(args.url, token))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(devices, ensure_ascii=False, indent=2))
    else:
        print_table(devices)

    if args.csv is not None:
        out_path = write_csv_semicolon(devices, path=args.csv)
        print(f"\nCSV written: {out_path}  (delimiter=';')")


if __name__ == "__main__":
    main()