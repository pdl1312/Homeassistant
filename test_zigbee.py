#!/usr/bin/env python3
# test_zigbee.py
#
# - Prints all ZHA devices + their entities
# - Then listens to:
#     * zha_event       (remote buttons etc.)
#     * state_changed   (all entities)
#
# No filters – everything is shown.
# Stop with Ctrl+C.

import os
import sys
import json
import asyncio
from datetime import datetime

# -------- SAME SETTINGS AS YOUR OTHER SCRIPT --------
HA_URL_DEFAULT = "ws://homeassistant.local:8123"
HARD_CODED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# ----------------------------------------------------

try:
    import websockets  # pip install websockets
except ImportError:
    print("Missing dependency: websockets. Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)


class HAWS:
    """Minimal helper for HA WebSocket 'result' calls (no events)."""
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


async def dump_all_devices(url, token):
    """Print all ZHA devices and their entities (entity_id + name)."""
    ws_url = url.rstrip("/") + "/api/websocket"
    async with HAWS(ws_url, token) as ha:
        devreg = await ha.call("config/device_registry/list")
        entreg = await ha.call("config/entity_registry/list")
        zha_devices = await ha.call("zha/devices")

    # map ieee -> device_id
    ieee_to_dev_id = {}
    for d in devreg:
        for ident in d.get("identifiers", []):
            if isinstance(ident, (list, tuple)) and len(ident) == 2 and str(ident[0]).lower() == "zha":
                ieee_to_dev_id[str(ident[1]).lower()] = d["id"]

    # entities grouped by device_id
    ents_by_dev = {}
    for e in entreg:
        dev_id = e.get("device_id")
        if not dev_id:
            continue
        ents_by_dev.setdefault(dev_id, []).append(e)

    print("=== ZHA devices and entities ===\n")
    for zd in sorted(zha_devices, key=lambda z: str(z.get("ieee", ""))):
        ieee = (zd.get("ieee") or "").lower()
        nwk = zd.get("nwk")
        model = zd.get("model")
        manufacturer = zd.get("manufacturer")
        dev_id = ieee_to_dev_id.get(ieee)
        dev_entities = ents_by_dev.get(dev_id, [])

        print(f"IEEE: {ieee}")
        print(f"  NWK : 0x{nwk:04X}" if isinstance(nwk, int) else f"  NWK : {nwk}")
        print(f"  Model: {manufacturer} {model}")
        if not dev_entities:
            print("  Entities: (none in registry)")
        else:
            print("  Entities:")
            for e in sorted(dev_entities, key=lambda x: x.get("entity_id", "")):
                en = e.get("entity_id", "")
                nm = e.get("name") or e.get("original_name") or ""
                print(f"    - {en}  ({nm})")
        print()

    print("=== end of list ===\n")


async def listen_all(url, token):
    """Subscribe to zha_event and state_changed, print everything."""
    ws_url = url.rstrip("/") + "/api/websocket"
    async with websockets.connect(ws_url, max_size=20_000_000) as ws:
        # auth_required
        hello = await ws.recv()
        print("Connected:", hello)

        # auth
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth_reply = json.loads(await ws.recv())
        if auth_reply.get("type") != "auth_ok":
            raise RuntimeError(f"Auth failed: {auth_reply}")
        print("Auth OK")

        msg_id = 0

        def next_id():
            nonlocal msg_id
            msg_id += 1
            return msg_id

        # subscribe to zha_event
        await ws.send(json.dumps({
            "id": next_id(),
            "type": "subscribe_events",
            "event_type": "zha_event",
        }))
        # subscribe to state_changed
        await ws.send(json.dumps({
            "id": next_id(),
            "type": "subscribe_events",
            "event_type": "state_changed",
        }))

        print("Subscribed to zha_event and state_changed")
        print("\nListening… (press buttons, toggle lights, etc.)  Ctrl+C to stop\n")

        while True:
            raw = await ws.recv()
            data = json.loads(raw)
            if data.get("type") != "event":
                continue

            event = data["event"]
            etype = event.get("event_type")
            now = datetime.now().strftime("%H:%M:%S")

            if etype == "zha_event":
                ed = event.get("data", {})
                ieee = (ed.get("device_ieee") or "").lower()
                cmd = ed.get("command")
                args = ed.get("args")
                print(f"[{now}] ZHA_EVENT  ieee={ieee}  cmd={cmd}  args={args}")

            elif etype == "state_changed":
                ed = event.get("data", {})
                entity_id = ed.get("entity_id", "")
                new_state = ed.get("new_state") or {}
                state = new_state.get("state")
                print(f"[{now}] STATE_CHANGED  {entity_id} -> {state}")


def main():
    url = HA_URL_DEFAULT
    token = os.environ.get("HASS_TOKEN") or HARD_CODED_TOKEN
    if not token or token == "PASTE_YOUR_LONG_LIVED_TOKEN_HERE":
        print("ERROR: set HARD_CODED_TOKEN or HASS_TOKEN", file=sys.stderr)
        sys.exit(2)

    # 1) print all devices + entities
    asyncio.run(dump_all_devices(url, token))

    # 2) then listen to all events
    try:
        asyncio.run(listen_all(url, token))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
