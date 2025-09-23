import os

import pytest
from pytest import CaptureFixture
from openstack.exceptions import SDKException

from openstack_unshelver_webapp.config import ConfigurationError, load_settings
from openstack_unshelver_webapp.openstack_client import OpenStackClient

pytestmark = pytest.mark.skipif(
    os.environ.get("CI") or os.environ.get("PYTEST_SKIP_OPENSTACK_LIVE"),
    reason="OpenStack live credential check runs only on developer machines",
)


def test_openstack_credentials_authorize_and_list_servers(capsys: CaptureFixture[str]) -> None:
    """Ensure local OpenStack credentials in config.yaml are valid."""

    try:
        settings = load_settings()
    except ConfigurationError as exc:
        pytest.skip(f"Skipping live credential check: {exc}")

    client = OpenStackClient(settings.openstack)
    conn = client.create_connection()
    try:
        token = conn.authorize()
        assert token, "OpenStack authorization returned an empty token"

        # Peek at a small slice of instances so we don't follow pagination links.
        running: list[str] = []
        shelved: list[str] = []
        others: list[tuple[str, str]] = []
        for index, server in enumerate(conn.compute.servers(limit=20)):
            if index >= 20:  # Safety guard in case the SDK ignores the limit.
                break
            name = getattr(server, "name", server.id)
            status = getattr(server, "status", "UNKNOWN") or "UNKNOWN"
            if status == "ACTIVE":
                running.append(name)
            elif status in {"SHELVED", "SHELVED_OFFLOADED"}:
                shelved.append(name)
            else:
                others.append((name, status))

        with capsys.disabled():
            print("Running instances:", ", ".join(sorted(running)) or "<none>")
            print("Shelved instances:", ", ".join(sorted(shelved)) or "<none>")
            if others:
                details = ", ".join(f"{name} ({status})" for name, status in sorted(others))
                print("Other statuses:", details)
    except SDKException as exc:
        pytest.fail(f"OpenStack live credential check failed: {exc}")
    finally:
        conn.close()
