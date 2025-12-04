#!/usr/bin/env python3
"""
rename_zha_device.py — Rename ZHA device (and optionally its battery entity)

- Sets the device's custom name (Device Registry: name_by_user)
- Optionally sets a friendly "name" on the battery sensor entity (not the entity_id by default)
- You can target by IEEE (recommended) or by device_id

Examples:
  python3 rename_zha_device.py --ieee 00:12:4b:00:aa:bb:cc:dd --name "Hall Motion"
  python3 rename_zha_device.py --device-id 1234567890abcdef --name "Kitchen Switch"
  # Also label the battery sensor entity:
  python3 rename_zha_device.py --ieee 00:12:... --name "Door Sensor" --set-battery-entity-name "Door Sensor battery"
"""

import os, sys, json, argparse, asyncio

# ---------- EDIT THESE ----------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# --------------------------------

try:
    import websockets  # pip install websockets
except ImportError:
    print("Missing dependency: websockets. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


class HAWS:
    def __init__(self, uri, token):
        self.uri = uri
        self.token = token
        self.ws = None
        self._id = 0

    async def __aenter__(self):
        self.ws = await websockets.connect(self.uri, max_size=20_000_000)
        _ = await self.ws.recv()  # auth_required
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


async def main():
    ap = argparse.ArgumentParser(description="Rename ZHA device (and optionally its battery entity).")
    ap.add_argument("--url", default=HA_URL_DEFAULT, help="HA base URL (ws:// or wss://)")
    ap.add_argument("--token", default=None, help="Long-lived token (overrides hardcoded)")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--ieee", help="Device IEEE address (e.g., 00:12:4b:...)")
    group.add_argument("--device-id", help="HA device_id (from device registry)")
    ap.add_argument("--name", required=True, help="New device custom name (name_by_user)")
    ap.add_argument("--set-battery-entity-name", help="Also set 'name' for battery sensor entity (friendly name)")
    ap.add_argument("--rename-entity-id", action="store_true",
                    help="Also change battery entity_id slug to match device name (careful: may break dashboards)")
    args = ap.parse_args()

    token = args.token or os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: Set HARD_CODED_TOKEN or pass --token/HASS_TOKEN.", file=sys.stderr)
        sys.exit(2)

    uri = args.url.rstrip("/") + "/api/websocket"
    async with HAWS(uri, token) as ha:
        # Pull ZHA devices + registries
        zha_devices = await ha.call("zha/devices")
        devreg = await ha.call("config/device_registry/list")
        entreg = await ha.call("config/entity_registry/list")

        devreg_by_id = {d["id"]: d for d in devreg}
        devreg_by_ieee = {}
        for d in devreg:
            for ident in d.get("identifiers", []):
                if isinstance(ident, (list, tuple)) and len(ident) == 2 and str(ident[0]).lower() == "zha":
                    devreg_by_ieee[str(ident[1]).lower()] = d

        # Resolve device_id
        target_device_id = None
        target_ieee = None

        if args.device_id:
            target_device_id = args.device_id
            target_ieee = None
            # sanity: ensure it exists
            if target_device_id not in devreg_by_id:
                raise SystemExit(f"Device ID not found in registry: {target_device_id}")
        else:
            target_ieee = args.ieee.lower()
            # try map IEEE -> device_id
            dev = devreg_by_ieee.get(target_ieee)
            if dev:
                target_device_id = dev["id"]
            else:
                # If it isn't in device registry, try to match ZHA list by IEEE and then its device_reg_id
                z = next((x for x in zha_devices if str(x.get("ieee","")).lower() == target_ieee), None)
                if not z:
                    raise SystemExit(f"IEEE not found in ZHA list: {target_ieee}")
                target_device_id = z.get("device_reg_id")
                if not target_device_id or target_device_id not in devreg_by_id:
                    raise SystemExit("Found in ZHA but not in Device Registry (ghost). Pair it properly first.")

        # 1) Rename at the Device Registry (sets the UI name)
        await ha.call("config/device_registry/update",
                      device_id=target_device_id,
                      name_by_user=args.name)
        print(f"✓ Device renamed: device_id={target_device_id}  →  '{args.name}'")

        # 2) Optionally set friendly name (and/or entity_id) for the battery sensor
        if args.set_battery_entity_name or args.rename_entity_id:
            ents = [e for e in entreg if e.get("device_id") == target_device_id]
            # Find a likely battery entity
            battery_entity = None
            for e in ents:
                eid = e.get("entity_id", "")
                if ":battery_" in eid or eid.endswith("_battery"):
                    battery_entity = e
                    break
                if eid.split(".", 1)[-1].endswith("battery"):
                    battery_entity = e
                    break
                # fallback: any sensor containing 'battery'
                if e.get("domain") == "sensor" and "battery" in eid:
                    battery_entity = e
                    break

            if battery_entity:
                update_payload = {"entity_id": battery_entity["entity_id"]}
                if args.set_battery_entity_name:
                    update_payload["name"] = args.set_battery_entity_name
                if args.rename_entity_id:
                    # Make a neat slug like sensor.<slug>_battery
                    base_slug = args.name.lower().strip().replace(" ", "_")
                    update_payload["new_entity_id"] = f"sensor.{base_slug}_battery"
                res = await ha.call("config/entity_registry/update", **update_payload)
                print(f"✓ Battery entity updated: {battery_entity['entity_id']} → {res.get('entity_id')}")
            else:
                print("! No battery entity found for this device (skipped)")

if __name__ == "__main__":
    asyncio.run(main())
