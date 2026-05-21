import asyncio

import httpx
import pytest
import pytest_asyncio

from openstack_unshelver_webapp.config import AppSettings, ButtonSettings
from openstack_unshelver_webapp.openstack_client import InstanceEndpoint
from openstack_unshelver_webapp.unshelve_manager import ButtonStatus, InstanceActionManager, _utcnow
from dataclasses import replace


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
        control_token="abcdef0123456789",
        manual_shelve_path="/admin-shelve",
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


class AlwaysActiveClient(DummyClient):
    def __init__(self):
        super().__init__()
        self._server = DummyServer(
            "server-active",
            "ACTIVE",
            addresses={
                "public": [
                    {
                        "addr": "203.0.113.10",
                        "version": 4,
                        "OS-EXT-IPS:type": "floating",
                    },
                    {
                        "addr": "10.0.0.5",
                        "version": 4,
                        "OS-EXT-IPS:type": "fixed",
                    },
                ]
            },
        )

    def find_server(self, instance_name):
        if instance_name != "instance-one":
            return None
        return self._server

    def get_server(self, server_id):
        return self._server

    def build_endpoint(self, server, button):
        return InstanceEndpoint(
            address="203.0.113.10",
            scheme=button.url_scheme,
            port=button.port,
            launch_path=button.launch_path or "/",
            healthcheck_path=button.healthcheck_path,
            verify_tls=button.verify_tls,
        )


@pytest_asyncio.fixture
async def active_manager(monkeypatch):
    app_settings = AppSettings(
        title="Test Active",
        secret_key="fedcba0987654321",
        poll_interval_seconds=1,
        http_probe_timeout=1,
        http_probe_attempts=1,
        control_token="abcdef0123456789",
        manual_shelve_path="/admin-shelve",
    )
    button = ButtonSettings(
        id="button-one",
        label="Button",
        instance_name="instance-one",
        url_scheme="http",
        port=8080,
        healthcheck_path="/health",
    )
    client = AlwaysActiveClient()
    mgr = InstanceActionManager(app_settings, {button.id: button}, client)

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    return mgr


@pytest.mark.asyncio
async def test_unshelve_workflow(manager):
    status = await manager.start_unshelve("button-one", actor="tester")
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
async def test_public_base_url_overrides_endpoint(monkeypatch):
    app_settings = AppSettings(
        title="Test Public URL",
        secret_key="0123456789abcdef",
        poll_interval_seconds=1,
        http_probe_timeout=1,
        http_probe_attempts=1,
        control_token="abcdef0123456789",
        manual_shelve_path="/admin-shelve",
    )
    button = ButtonSettings(
        id="button-one",
        label="Button",
        instance_name="instance-one",
        url_scheme="http",
        healthcheck_path="/health",
        public_base_url="https://chat.example.com",
    )
    client = AlwaysActiveClient()
    mgr = InstanceActionManager(app_settings, {button.id: button}, client)

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    status = await mgr.refresh_openstack_status("button-one")
    assert status.url == "https://chat.example.com/"


@pytest.mark.asyncio
async def test_start_unshelve_ignores_duplicate_requests(manager):
    status = await manager.start_unshelve("button-one", actor="tester")
    task = manager._tasks["button-one"]

    second_status = await manager.start_unshelve("button-one", actor="tester")
    assert second_status.running
    assert manager._tasks["button-one"] is task

    await task
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_unknown_button(manager):
    with pytest.raises(KeyError):
        await manager.start_unshelve("missing", actor="tester")

    with pytest.raises(KeyError):
        manager.get_status("missing")


@pytest.mark.asyncio
async def test_initial_active_status_exposes_launch_link(active_manager):
    status = active_manager.get_status("button-one")
    assert status.state == "active"
    assert status.url == "http://203.0.113.10:8080/"
    assert status.message.startswith("Instance status: Active")


@pytest.mark.asyncio
async def test_refresh_status_keeps_launch_link_when_active(active_manager):
    refreshed = await active_manager.refresh_openstack_status("button-one")
    assert refreshed.state == "active"
    assert refreshed.url == "http://203.0.113.10:8080/"
    assert "Instance status" in refreshed.message


@pytest.mark.asyncio
async def test_stale_running_flag_recovered(manager):
    """When running=True but the task is done, refresh should clear the stale flag."""
    # Manually set running=True without a live task
    manager._statuses["button-one"] = replace(
        manager._statuses["button-one"],
        running=True,
        state="unshelving",
        message="Working…",
    )
    # No task in _tasks dict — simulates the stuck state from the bug
    status = await manager.refresh_openstack_status("button-one")
    # The stale running flag should have been cleared
    assert status.running is False


@pytest.mark.asyncio
async def test_stale_running_flag_with_done_task(manager):
    """When running=True but the task is already done, refresh should clear the stale flag."""
    # Start an unshelve, let it complete, then manually corrupt the running flag
    status = await manager.start_unshelve("button-one", actor="tester")
    task = manager._tasks["button-one"]
    await task
    await asyncio.sleep(0)

    # Simulate the running flag getting stuck
    manager._statuses["button-one"] = replace(
        manager._statuses["button-one"],
        running=True,
    )

    refreshed = await manager.refresh_openstack_status("button-one")
    assert refreshed.running is False


@pytest.mark.asyncio
async def test_unshelve_timeout(monkeypatch):
    """The unshelve workflow should fail if the instance never becomes ACTIVE within the timeout."""
    app_settings = AppSettings(
        title="Test Timeout",
        secret_key="1234567890abcdef",
        poll_interval_seconds=1,
        http_probe_timeout=1,
        http_probe_attempts=1,
        unshelve_timeout_minutes=1,
        api_retry_attempts=1,
        control_token="abcdef0123456789",
        manual_shelve_path="/admin-shelve",
    )
    button = ButtonSettings(
        id="button-one",
        label="Button",
        instance_name="instance-one",
        url_scheme="http",
        healthcheck_path="/health",
    )

    class StuckClient(DummyClient):
        """Server that stays SHELVED forever (never becomes ACTIVE)."""
        def get_server(self, server_id):
            return DummyServer("server-1", "SHELVED")

    client = StuckClient()
    mgr = InstanceActionManager(app_settings, {button.id: button}, client)

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    # Advance time by 2 minutes each sleep call to exceed the 1-minute timeout
    async def advance_time_sleep(seconds):
        import openstack_unshelver_webapp.unshelve_manager as mgr_mod
        # Advance the _utcnow function by 2 minutes each time
        mgr_mod._utcnow = (lambda _original=mgr_mod._utcnow: _original() + timedelta(minutes=2))

    from datetime import timedelta
    import openstack_unshelver_webapp.unshelve_manager as mgr_mod
    original_utcnow = mgr_mod._utcnow

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(asyncio, "sleep", advance_time_sleep)

    status = await mgr.start_unshelve("button-one", actor="tester")
    assert status.running

    task = mgr._tasks["button-one"]
    await task
    await asyncio.sleep(0)

    # Restore _utcnow
    mgr_mod._utcnow = original_utcnow

    final = mgr.get_status("button-one")
    assert final.state == "error"
    assert "did not become ACTIVE" in (final.error or "")


@pytest.mark.asyncio
async def test_unshelve_scheduling_failure_detected(monkeypatch):
    """When OpenStack reports the unshelve action as Error (scheduling failure),
    the workflow should detect it and report a useful error message."""
    app_settings = AppSettings(
        title="Test Scheduling Failure",
        secret_key="1234567890abcdef",
        poll_interval_seconds=1,
        http_probe_timeout=1,
        http_probe_attempts=1,
        unshelve_timeout_minutes=30,
        api_retry_attempts=1,
        control_token="abcdef0123456789",
        manual_shelve_path="/admin-shelve",
    )
    button = ButtonSettings(
        id="button-one",
        label="Button",
        instance_name="instance-one",
        url_scheme="http",
        healthcheck_path="/health",
    )

    class StuckShelvedClient(DummyClient):
        """Server stays SHELVED_OFFLOADED with no task_state (scheduler failure)."""
        def __init__(self):
            super().__init__()
            self._action_checked = False

        def find_server(self, instance_name):
            if instance_name != "instance-one":
                return None
            srv = DummyServer("server-1", "SHELVED_OFFLOADED")
            return srv

        def get_server(self, server_id):
            srv = DummyServer("server-1", "SHELVED_OFFLOADED")
            # Simulate no task_state (scheduler hasn't picked it up)
            srv.__dict__["OS-EXT-STS:task_state"] = None
            return srv

        def get_last_instance_action(self, server_id, action):
            """Simulate OpenStack reporting the unshelve as Error."""
            return {
                "action": "unshelve",
                "message": "Error",
                "start_time": "2026-05-21T08:21:23.000000",
                "events": [
                    {
                        "event": "schedule_instances",
                        "start_time": "2026-05-21T08:21:24.000000",
                        "finish_time": "2026-05-21T08:21:27.000000",
                        "result": "Error",
                    }
                ],
            }

    client = StuckShelvedClient()
    mgr = InstanceActionManager(app_settings, {button.id: button}, client)

    async def immediate_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def fast_sleep(_: float):
        return None

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: StubHttpClient())

    status = await mgr.start_unshelve("button-one", actor="tester")
    assert status.running

    task = mgr._tasks["button-one"]
    await task
    await asyncio.sleep(0)

    final = mgr.get_status("button-one")
    assert final.state == "error"
    assert "failed to schedule" in (final.error or "").lower()
    assert "schedule_instances" in (final.error or "")
