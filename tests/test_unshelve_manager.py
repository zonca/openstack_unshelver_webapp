import asyncio

import httpx
import pytest
import pytest_asyncio

from openstack_unshelver_webapp.config import AppSettings, ButtonSettings
from openstack_unshelver_webapp.openstack_client import InstanceEndpoint
from openstack_unshelver_webapp.unshelve_manager import ButtonStatus, InstanceActionManager


class DummyServer:
    def __init__(self, server_id: str, status: str, addresses=None):
        self.id = server_id
        self.status = status
        self.addresses = addresses or {}


class DummyClient:
    def __init__(self):
        self.unshelve_calls = 0
        self._get_calls = 0
        self._active_server = DummyServer(
            "server-1",
            "ACTIVE",
            addresses={
                "public": [
                    {
                        "addr": "1.2.3.4",
                        "version": 4,
                    }
                ]
            },
        )

    def find_server(self, instance_name):
        if instance_name != "instance-one":
            return None
        return DummyServer("server-1", "SHELVED")

    def unshelve_server(self, server_id):
        self.unshelve_calls += 1

    def get_server(self, server_id):
        self._get_calls += 1
        if self._get_calls == 1:
            return DummyServer("server-1", "SHELVED")
        return self._active_server

    def build_endpoint(self, server, button):
        return InstanceEndpoint(
            address="1.2.3.4",
            scheme=button.url_scheme,
            port=button.port,
            launch_path=button.launch_path or "/",
            healthcheck_path=button.healthcheck_path,
            verify_tls=button.verify_tls,
        )


class SuccessResponse:
    def __init__(self):
        self.status_code = 200
        self.text = "OK"

    def json(self):  # pragma: no cover - compatibility only
        return {"status": "ok"}


class StubHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url):
        return SuccessResponse()


@pytest_asyncio.fixture
async def manager(monkeypatch):
    app_settings = AppSettings(
        title="Test",
        secret_key="1234567890abcdef",
        poll_interval_seconds=1,
        http_probe_timeout=1,
        http_probe_attempts=1,
    )
    button = ButtonSettings(
        id="button-one",
        label="Button",
        instance_name="instance-one",
        url_scheme="http",
        healthcheck_path="/health",
    )
    client = DummyClient()
    mgr = InstanceActionManager(app_settings, {button.id: button}, client)

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def fast_sleep(_: float):
        return None

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubHttpClient())

    return mgr


@pytest.mark.asyncio
async def test_unshelve_workflow(manager):
    status = await manager.start_unshelve("button-one")
    assert status.running

    task = manager._tasks["button-one"]
    await task
    await asyncio.sleep(0)

    final_status = manager.get_status("button-one")
    assert final_status.state == "ready"
    assert final_status.http_ready is True
    assert final_status.url.endswith("/")
    remaining = manager._tasks.get("button-one")
    if remaining is not None:
        assert remaining.done()


@pytest.mark.asyncio
async def test_start_unshelve_ignores_duplicate_requests(manager):
    status = await manager.start_unshelve("button-one")
    task = manager._tasks["button-one"]

    second_status = await manager.start_unshelve("button-one")
    assert second_status.running
    assert manager._tasks["button-one"] is task

    await task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unknown_button(manager):
    with pytest.raises(KeyError):
        await manager.start_unshelve("missing")

    with pytest.raises(KeyError):
        manager.get_status("missing")
