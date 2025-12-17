#!/usr/bin/env python3
"""
Ensure that a DNS A record exists inside the current OpenStack Designate zone.

Usage:
    uv run python scripts/ensure_dns_record.py cosmosage.example.org 203.0.113.10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

# Ensure the project root is on sys.path when run as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openstack import connection
from openstack.exceptions import SDKException

from openstack_unshelver_webapp.config import Settings, load_settings


def _build_connection(settings: Settings) -> connection.Connection:
    payload = settings.openstack.model_dump(exclude_none=True, mode="json")
    return connection.Connection(**payload)


def _find_zone_id(conn: connection.Connection, hostname: str) -> Optional[tuple[str, str]]:
    target = hostname.rstrip(".") + "."
    matches: list[tuple[int, str, str]] = []
    for zone in conn.dns.zones():
        name = getattr(zone, "name", "") or ""
        zone_id = getattr(zone, "id", "") or ""
        if not name or not zone_id:
            continue
        if target.endswith(name):
            matches.append((len(name), name, zone_id))
    if not matches:
        return None
    matches.sort(reverse=True)
    _, name, zone_id = matches[0]
    return name, zone_id


def _ensure_record(conn: connection.Connection, zone_id: str, fqdn: str, address: str, ttl: int) -> str:
    record_name = fqdn if fqdn.endswith(".") else f"{fqdn}."
    for recordset in conn.dns.recordsets(zone_id):
        record_type = (getattr(recordset, "type", "") or "").upper()
        name = getattr(recordset, "name", "") or ""
        recordset_id = getattr(recordset, "id", "") or ""
        if record_type != "A" or name != record_name or not recordset_id:
            continue
        records = list(getattr(recordset, "records", []) or [])
        if records == [address]:
            return f"Record {record_name} already points at {address}."
        conn.dns.update_recordset(zone_id, recordset_id, records=[address], ttl=ttl)
        return f"Updated {record_name} to {address}."

    conn.dns.create_recordset(zone_id, name=record_name, type="A", records=[address], ttl=ttl)
    return f"Created {record_name} -> {address}."


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure a Designate A record exists for the controller VM.")
    parser.add_argument("hostname", help="FQDN to manage (e.g. cosmosage.example.org)")
    parser.add_argument("address", help="IPv4 address for the record")
    parser.add_argument("--ttl", type=int, default=300, help="Record TTL in seconds (default: 300)")
    parser.add_argument("--config", default=None, help="Path to controller config.yaml (defaults to UNSHELVER_CONFIG)")
    args = parser.parse_args()

    settings = load_settings(args.config)
    conn = _build_connection(settings)

    try:
        zone = _find_zone_id(conn, args.hostname)
        if not zone:
            print(f"No matching Designate zone found for {args.hostname}", file=sys.stderr)
            return 1
        zone_name, zone_id = zone
        message = _ensure_record(conn, zone_id, args.hostname, args.address, args.ttl)
        print(f"{message} (zone {zone_name})")
        return 0
    except SDKException as exc:  # pragma: no cover - requires live OpenStack
        print(f"OpenStack error: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
