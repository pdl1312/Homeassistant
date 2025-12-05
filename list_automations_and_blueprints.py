#!/usr/bin/env python
"""
List all Home Assistant automations with active/inactive status and blueprint info.

How it works:
- Connects to Home Assistant via WebSocket and fetches all entity states.
- Filters entities starting with 'automation.'.
- Uses each automation's attributes['id'] (internal id).
- Reads Home Assistant config files:
    - <CONFIG_DIR>/automations.yaml
    - <CONFIG_DIR>/.storage/automation
  and tries to match by internal id to discover which blueprint (if any) is used.

Requirements:
    pip install websockets pyyaml

Configuration:
    1) Set HASS_URL and LONG_LIVED_TOKEN below.
    2) Set CONFIG_DIR to the path of your HA config folder
       (for example, a Samba share mapped on Windows).
"""

import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import websockets
import yaml

# === CONFIGURE THESE ===
HASS_URL = "ws://homeassistant.local:8123/api/websocket"  # or ws://<ip>:8123/api/websocket
LONG_LIVED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiI4YTE1MDQ5Mzc0NGI0YmVjYWE5ZmQyNmExNGU3MDU1YyIsImlhdCI6MTc2MTg1NjY1OCwiZXhwIjoyMDc3MjE2NjU4fQ.QwnBSufOzxeQ5UL8DVpu_VDTUzRTOjunlPnZHu2ih4Q"
# Path to your Home Assistant /config folder (adjust for your setup!)
# Examples:
#   r"Z:\\homeassistant\\config"
#   r"\\HOMEASSISTANT\\config"
#   r"/config"
CONFIG_DIR = r"\\192.168.1.110\config"
# =======================



# ---------------------------------------------------------------------------
# CONFIG FILE SCANNING
# ---------------------------------------------------------------------------

def _safe_load_yaml(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"[WARN] Failed to read YAML {path}: {e}")
        return None


def discover_yaml_automation_files(config_dir: str) -> List[str]:
    """Discover automation YAML files: automations.yaml and automations/*.yaml."""
    files: List[str] = []

    main = os.path.join(config_dir, "automations.yaml")
    if os.path.isfile(main):
        files.append(main)

    autos_dir = os.path.join(config_dir, "automations")
    if os.path.isdir(autos_dir):
        for name in os.listdir(autos_dir):
            if not name.lower().endswith((".yaml", ".yml")):
                continue
            files.append(os.path.join(autos_dir, name))

    return files


def load_yaml_automations(config_dir: str) -> Tuple[
    Dict[str, Dict[str, Any]],  # by_id
    Dict[str, Dict[str, Any]]   # by_alias (lowercase)
]:
    """
    Load automations from YAML files.

    Returns:
        by_id:    internal_id (str) -> info
        by_alias: alias_lower (str) -> info

    info = {
        "alias": str,
        "blueprint": Optional[str],
        "source": "yaml:<filename>",
    }
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    by_alias: Dict[str, Dict[str, Any]] = {}

    for path in discover_yaml_automation_files(config_dir):
        raw = _safe_load_yaml(path)
        if raw is None:
            continue

        # Possible structures:
        # - list of automations
        # - dict with key 'automation': [ ... ]
        data = raw
        if isinstance(raw, dict) and "automation" in raw:
            data = raw["automation"]

        if not isinstance(data, list):
            continue

        for item in data:
            if not isinstance(item, dict):
                continue
            internal_id = item.get("id")
            alias = item.get("alias") or "<no alias>"
            alias_lower = alias.lower()

            ub = item.get("use_blueprint")
            blueprint = None
            if isinstance(ub, dict):
                blueprint = ub.get("path")

            info = {
                "alias": alias,
                "blueprint": blueprint,
                "source": f"yaml:{os.path.basename(path)}",
            }

            if internal_id:
                by_id[str(internal_id)] = info

            # Only set by_alias if not already present (first wins)
            if alias_lower and alias_lower not in by_alias:
                by_alias[alias_lower] = info

    return by_id, by_alias


def load_storage_automations(config_dir: str) -> Tuple[
    Dict[str, Dict[str, Any]],  # by_id
    Dict[str, Dict[str, Any]]   # by_alias (lowercase)
]:
    """
    Load automations from .storage/automation (UI-created automations).

    Returns:
        by_id:    internal_id (str) -> info
        by_alias: alias_lower (str) -> info

    info = {
        "alias": str,
        "blueprint": Optional[str],
        "source": "storage:automation",
    }
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    by_alias: Dict[str, Dict[str, Any]] = {}

    storage_path = os.path.join(config_dir, ".storage", "automation")
    if not os.path.exists(storage_path):
        return by_id, by_alias

    try:
        with open(storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to read {storage_path}: {e}")
        return by_id, by_alias

    items: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        data_obj = data.get("data")
        if isinstance(data_obj, dict) and isinstance(data_obj.get("items"), list):
            items = data_obj["items"]
        elif isinstance(data.get("items"), list):
            # Just in case it's a flatter structure
            items = data["items"]

    if not isinstance(items, list):
        print(f"[WARN] Unexpected structure inside {storage_path}.")
        return by_id, by_alias

    for item in items:
        if not isinstance(item, dict):
            continue

        internal_id = item.get("id")
        alias = item.get("alias") or "<no alias>"
        alias_lower = alias.lower()

        ub = item.get("use_blueprint")
        blueprint = None
        if isinstance(ub, dict):
            blueprint = ub.get("path")

        info = {
            "alias": alias,
            "blueprint": blueprint,
            "source": "storage:automation",
        }

        if internal_id:
            by_id[str(internal_id)] = info

        if alias_lower and alias_lower not in by_alias:
            by_alias[alias_lower] = info

    return by_id, by_alias


def load_all_config_automations(config_dir: str) -> Tuple[
    Dict[str, Dict[str, Any]],  # by_id
    Dict[str, Dict[str, Any]]   # by_alias
]:
    """
    Merge YAML and storage automations.

    Precedence:
      - YAML overrides storage when matching by internal id.
      - For aliases, first-come wins (storage, then YAML).
    """
    storage_by_id, storage_by_alias = load_storage_automations(config_dir)
    yaml_by_id, yaml_by_alias = load_yaml_automations(config_dir)

    by_id = dict(storage_by_id)
    by_alias = dict(storage_by_alias)

    # YAML overrides by id
    by_id.update(yaml_by_id)

    # For aliases, if YAML alias does not exist yet, add it
    for alias_lower, info in yaml_by_alias.items():
        if alias_lower not in by_alias:
            by_alias[alias_lower] = info

    print(f"[INFO] Loaded {len(storage_by_id)} automations from .storage/automation "
          f"and {len(yaml_by_id)} from YAML files.")
    return by_id, by_alias


# ---------------------------------------------------------------------------
# REST: FETCH STATES
# ---------------------------------------------------------------------------

def fetch_states() -> List[Dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {LONG_LIVED_TOKEN}",
        "Content-Type": "application/json",
    }
    url = f"{HASS_BASE_URL.rstrip('/')}/api/states"
    resp = requests.get(url, headers=headers, timeout=30)
    try:
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Failed to GET {url}: {resp.status_code} {resp.text}") from e
    return resp.json()


# ---------------------------------------------------------------------------
# MAIN LOGIC
# ---------------------------------------------------------------------------

def main():
    if not os.path.isdir(CONFIG_DIR):
        print(f"[WARN] CONFIG_DIR does not exist or is not a directory: {CONFIG_DIR}")
        print("       Blueprint info will likely be missing until you set the correct path.")
        config_by_id: Dict[str, Dict[str, Any]] = {}
        config_by_alias: Dict[str, Dict[str, Any]] = {}
    else:
        config_by_id, config_by_alias = load_all_config_automations(CONFIG_DIR)

    try:
        all_states = fetch_states()
    except Exception as e:
        print(f"Error fetching states from Home Assistant: {e}")
        return

    automations: List[Dict[str, Any]] = []

    for st in all_states:
        entity_id = st.get("entity_id", "")
        if not entity_id.startswith("automation."):
            continue

        attrs = st.get("attributes", {}) or {}
        internal_id = attrs.get("id")
        friendly_name = attrs.get("friendly_name") or entity_id
        state = st.get("state") or "<unknown>"

        cfg = None
        blueprint = None
        cfg_alias = None
        source = None

        # 1) Try match by internal id
        if internal_id is not None:
            cfg = config_by_id.get(str(internal_id))

        # 2) Fallback: match by alias / friendly_name
        if cfg is None:
            alias_lower = friendly_name.lower()
            cfg = config_by_alias.get(alias_lower)

        if cfg:
            blueprint = cfg.get("blueprint")
            cfg_alias = cfg.get("alias")
            source = cfg.get("source")

        # Use config alias if present, otherwise friendly_name
        alias = cfg_alias or friendly_name

        automations.append({
            "entity_id": entity_id,
            "alias": alias,
            "internal_id": str(internal_id) if internal_id else None,
            "state": state,
            "is_enabled": (state == "on"),
            "blueprint": blueprint,
            "config_source": source,
        })

    # Sort nicely
    automations.sort(key=lambda x: x["alias"].lower())

    active = [a for a in automations if a["is_enabled"]]
    inactive = [a for a in automations if not a["is_enabled"]]

    def print_section(title: str, items: List[Dict[str, Any]]) -> None:
        print()
        print("=" * 80)
        print(title)
        print("=" * 80)
        if not items:
            print("(none)")
            return

        for item in items:
            alias = item["alias"]
            entity_id = item["entity_id"]
            internal_id = item["internal_id"] or "<no internal id>"
            state = item["state"]
            blueprint = item["blueprint"]
            source = item["config_source"]

            bp_str = blueprint if blueprint else "— (no blueprint / unknown)"
            src_str = source if source else "unknown"

            print(f"- {alias}")
            print(f"  Entity ID    : {entity_id}")
            print(f"  Internal ID  : {internal_id}")
            print(f"  HA state     : {state}")
            print(f"  Config source: {src_str}")
            print(f"  Blueprint    : {bp_str}")
            print()

    print_section("ACTIVE (enabled) automations", active)
    print_section("INACTIVE (disabled) automations", inactive)


if __name__ == "__main__":
    main()