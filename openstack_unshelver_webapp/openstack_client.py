from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
from typing import Any, Iterable, Optional

from openstack import connection
from openstack.compute.v2.server import Server
from openstack.exceptions import ResourceNotFound, SDKException

from .config import ButtonSettings, OpenStackSettings


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class InstanceEndpoint:
    address: str
    scheme: str
    port: Optional[int]
    launch_path: str
    healthcheck_path: str
    verify_tls: bool

    @property
    def base_url(self) -> str:
        host = format_host(self.address)
        default_port = 80 if self.scheme == "http" else 443 if self.scheme == "https" else None
        port_part = "" if self.port in (None, default_port) else f":{self.port}"
        return f"{self.scheme}://{host}{port_part}"

    @property
    def launch_url(self) -> str:
        return f"{self.base_url}{self.launch_path}"

    @property
    def healthcheck_url(self) -> str:
        return f"{self.base_url}{self.healthcheck_path}"


class OpenStackClient:
    """Synchronous helper that wraps openstacksdk operations."""

    def __init__(self, settings: OpenStackSettings) -> None:
        self._settings = settings
        self._dns_cache: dict[str, Optional[str]] = {}

    def create_connection(self) -> connection.Connection:
        payload = self._settings.model_dump(exclude_none=True, mode="json")
        return connection.Connection(**payload)

    def find_server(self, instance_name: str) -> Optional[Server]:
        conn = self.create_connection()
        try:
            return conn.compute.find_server(instance_name, ignore_missing=True)
        finally:
            conn.close()

    def unshelve_server(self, server_id: str) -> None:
        conn = self.create_connection()
        try:
            server = conn.compute.get_server(server_id)
            conn.compute.unshelve_server(server)
        finally:
            conn.close()

    def shelve_server(self, server_id: str) -> None:
        conn = self.create_connection()
        try:
            server = conn.compute.get_server(server_id)
            conn.compute.shelve_server(server)
        finally:
            conn.close()

    def get_server(self, server_id: str) -> Server:
        conn = self.create_connection()
        try:
            server = conn.compute.get_server(server_id)
            if server is None:
                raise ResourceNotFound(f"Server {server_id} not found")
            return server
        finally:
            conn.close()

    def build_endpoint(self, server: Server, button: ButtonSettings) -> Optional[InstanceEndpoint]:
        address = select_address(server, button.preferred_networks)
        if not address:
            return None
        hostname = self._resolve_dns_name(address) or address
        return InstanceEndpoint(
            address=hostname,
            scheme=button.url_scheme,
            port=button.port,
            launch_path=button.launch_path or "/",
            healthcheck_path=button.healthcheck_path or "/",
            verify_tls=button.verify_tls,
        )

    def _resolve_dns_name(self, address: str) -> Optional[str]:
        """Look up the DNS name for an IP address via Designate if possible."""

        try:
            ipaddress.ip_address(address)
        except ValueError:
            # Already a hostname
            return address

        if address in self._dns_cache:
            return self._dns_cache[address]

        result: Optional[str] = None
        conn = self.create_connection()
        try:
            result = self._lookup_designate_record(conn, address)
        except SDKException as exc:  # pragma: no cover - requires live OpenStack
            _LOGGER.debug("Designate lookup failed for %s: %s", address, exc, exc_info=True)
        finally:
            conn.close()

        if result:
            result = result.rstrip(".")
        self._dns_cache[address] = result
        return result

    @staticmethod
    def _lookup_designate_record(conn: connection.Connection, address: str) -> Optional[str]:
        for zone in conn.dns.zones():  # pragma: no cover - requires live OpenStack
            zone_id = _extract(zone, "id")
            if not zone_id:
                continue
            for recordset in conn.dns.recordsets(zone_id):
                record_type = _extract(recordset, "type")
                if (record_type or "").upper() != "A":
                    continue
                records = _extract(recordset, "records") or []
                if address in records:
                    name = _extract(recordset, "name")
                    if name:
                        return name
        return None


def select_address(server: Server, preferred_networks: Optional[Iterable[str]] = None) -> Optional[str]:
    """Given a server, pick the best IP address to contact it."""

    addresses = getattr(server, "addresses", {}) or {}
    # Preferred networks override all other logic
    if preferred_networks:
        for network in preferred_networks:
            ip = _first_address(addresses.get(network, []))
            if ip:
                return ip
    # Floating IPs are the next best option
    for candidates in addresses.values():
        ip = _first_address(candidates, preferred_type="floating")
        if ip:
            return ip
    # Fall back to IPv4 addresses
    for candidates in addresses.values():
        ip = _first_address(candidates, prefer_ipv4=True)
        if ip:
            return ip
    # Any other address
    for candidates in addresses.values():
        ip = _first_address(candidates, prefer_ipv4=False)
        if ip:
            return ip
    # Final fallback: accessIPv4/accessIPv6 fields
    access_v4 = getattr(server, "accessIPv4", None)
    if access_v4:
        return access_v4
    access_v6 = getattr(server, "accessIPv6", None)
    if access_v6:
        return access_v6
    return None


def _first_address(candidates: Optional[Iterable[dict]], preferred_type: Optional[str] = None, prefer_ipv4: bool = True) -> Optional[str]:
    if not candidates:
        return None
    entries = [c for c in candidates if isinstance(c, dict) and c.get("addr")]
    if preferred_type:
        for entry in entries:
            if entry.get("OS-EXT-IPS:type") == preferred_type:
                return entry["addr"]
    if prefer_ipv4:
        for entry in entries:
            if entry.get("version") == 4:
                return entry["addr"]
    for entry in entries:
        return entry["addr"]
    return None


def format_host(address: str) -> str:
    """Wrap IPv6 addresses in square brackets for URLs."""

    if ":" in address and not address.startswith("["):
        return f"[{address}]"
    return address


def _extract(item: Any, attr: str) -> Any:
    if hasattr(item, attr):
        return getattr(item, attr)
    if isinstance(item, dict):
        return item.get(attr)
    return None
