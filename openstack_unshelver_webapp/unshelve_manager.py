from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from openstack.compute.v2.server import Server
from openstack.exceptions import ResourceNotFound, SDKException

from .config import AppSettings, ButtonSettings
from .event_logger import EventLogger
from .openstack_client import InstanceEndpoint, OpenStackClient


_LOGGER = logging.getLogger(__name__)
_UNSET = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_openstack_status(status: str) -> str:
    cleaned = (status or "UNKNOWN").replace("_", " ")
    return cleaned.strip().title() or "Unknown"


@dataclass(slots=True)
class ButtonStatus:
    button_id: str
    instance_name: str
    state: str
    message: str
    running: bool
    last_updated: datetime
    url: Optional[str] = None
    http_ready: bool = False
    error: Optional[str] = None

    def serialise(self) -> dict[str, Optional[str]]:
        return {
            "button_id": self.button_id,
            "instance_name": self.instance_name,
            "state": self.state,
            "message": self.message,
            "running": self.running,
            "last_updated": self.last_updated.isoformat(),
            "url": self.url,
            "http_ready": self.http_ready,
            "error": self.error,
        }


class InstanceActionManager:
    """Coordinates unshelve requests and status tracking."""

    def __init__(
        self,
        app_settings: AppSettings,
        buttons: Dict[str, ButtonSettings],
        client: OpenStackClient,
        event_logger: Optional[EventLogger] = None,
    ) -> None:
        self._app_settings = app_settings
        self._buttons = buttons
        self._client = client
        self._event_logger = event_logger
        self._statuses: Dict[str, ButtonStatus] = {
            button_id: ButtonStatus(
                button_id=button_id,
                instance_name=button.instance_name,
                state="idle",
                message="Fetching OpenStack status…",
                running=False,
                last_updated=_utcnow(),
            )
            for button_id, button in buttons.items()
        }
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._prime_initial_statuses()

    async def log_event(self, action: str, *, actor: str, instance_name: str, detail: Optional[str] = None) -> None:
        if not self._event_logger:
            return
        await self._event_logger.log(action, actor=actor, instance_name=instance_name, detail=detail)

    def _prime_initial_statuses(self) -> None:
        for button_id, button in self._buttons.items():
            try:
                server = self._client.find_server(button.instance_name)
            except SDKException as exc:
                _LOGGER.debug("Initial status refresh failed for %s: %s", button.instance_name, exc, exc_info=True)
                message = "Unable to query OpenStack status right now."
            else:
                if not server:
                    message = "Instance not found in OpenStack."
                else:
                    raw_status = (getattr(server, "status", None) or "").upper() or "UNKNOWN"
                    display_status = _format_openstack_status(raw_status)
                    message = f"Instance status: {display_status}."
            self._statuses[button_id] = replace(
                self._statuses[button_id],
                message=message,
            )

    def get_status(self, button_id: str) -> ButtonStatus:
        status = self._statuses.get(button_id)
        if not status:
            raise KeyError(f"Unknown button id '{button_id}'")
        return status

    async def refresh_openstack_status(self, button_id: str) -> ButtonStatus:
        button = self._buttons.get(button_id)
        if not button:
            raise KeyError(f"Unknown button id '{button_id}'")

        status = self._statuses[button_id]
        if status.running:
            return status

        try:
            server = await asyncio.to_thread(self._client.find_server, button.instance_name)
        except SDKException as exc:
            _LOGGER.debug("Failed to refresh status for %s: %s", button.instance_name, exc, exc_info=True)
            return await self._update_status(
                button_id,
                message="Unable to query OpenStack status right now.",
            )

        if not server:
            return await self._update_status(
                button_id,
                message="Instance not found in OpenStack.",
            )

        raw_status = (getattr(server, "status", None) or "").upper() or "UNKNOWN"
        display_status = _format_openstack_status(raw_status)
        return await self._update_status(
            button_id,
            message=f"Instance status: {display_status}.",
        )

    async def start_unshelve(self, button_id: str, *, actor: str, reason: Optional[str] = None) -> ButtonStatus:
        button = self._buttons.get(button_id)
        if not button:
            raise KeyError(f"Unknown button id '{button_id}'")

        async with self._lock:
            task = self._tasks.get(button_id)
            current = self._statuses[button_id]
            if task and not task.done():
                return current
            updated = replace(
                current,
                state="unshelving",
                message="Starting unshelve workflow…",
                running=True,
                http_ready=False,
                error=None,
                url=None,
                last_updated=_utcnow(),
            )
            self._statuses[button_id] = updated
            job = asyncio.create_task(self._run_unshelve(button, actor=actor, reason=reason))
            self._tasks[button_id] = job
            job.add_done_callback(lambda t: asyncio.create_task(self._clear_task(button_id, t)))
            await self.log_event("unshelve_requested", actor=actor, instance_name=button.instance_name, detail=reason)
            return updated

    async def start_shelve(self, button_id: str, *, actor: str, reason: Optional[str] = None) -> ButtonStatus:
        button = self._buttons.get(button_id)
        if not button:
            raise KeyError(f"Unknown button id '{button_id}'")

        async with self._lock:
            task = self._tasks.get(button_id)
            current = self._statuses[button_id]
            if task and not task.done():
                return current
            updated = replace(
                current,
                state="shelving",
                message="Starting shelve workflow…",
                running=True,
                http_ready=False,
                error=None,
                url=current.url,
                last_updated=_utcnow(),
            )
            self._statuses[button_id] = updated
            job = asyncio.create_task(self._run_shelve(button, actor=actor, reason=reason))
            self._tasks[button_id] = job
            job.add_done_callback(lambda t: asyncio.create_task(self._clear_task(button_id, t)))
            await self.log_event("shelve_requested", actor=actor, instance_name=button.instance_name, detail=reason)
            return updated

    async def _clear_task(self, button_id: str, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
            if exc:
                _LOGGER.exception("Unshelve task for %s raised an exception", button_id, exc_info=exc)
        except asyncio.CancelledError:
            _LOGGER.warning("Unshelve task for %s was cancelled", button_id)
        async with self._lock:
            self._tasks.pop(button_id, None)

    async def _update_status(
        self,
        button_id: str,
        *,
        state: Optional[str] = None,
        message: Optional[str] = None,
        running: Optional[bool] = None,
        url: Any = _UNSET,
        http_ready: Optional[bool] = None,
        error: Any = _UNSET,
    ) -> ButtonStatus:
        async with self._lock:
            status = self._statuses[button_id]
            new_status = replace(
                status,
                state=state or status.state,
                message=message or status.message,
                running=status.running if running is None else running,
                url=status.url if url is _UNSET else url,
                http_ready=status.http_ready if http_ready is None else http_ready,
                error=status.error if error is _UNSET else error,
                last_updated=_utcnow(),
            )
            self._statuses[button_id] = new_status
            return new_status

    async def _run_unshelve(self, button: ButtonSettings, *, actor: str, reason: Optional[str]) -> None:
        button_id = button.id
        try:
            server = await asyncio.to_thread(self._client.find_server, button.instance_name)
            if not server:
                raise ResourceNotFound(f"Instance '{button.instance_name}' not found")

            status = (server.status or "").upper()
            await self._update_status(
                button_id,
                message=f"Current OpenStack status: {_format_openstack_status(status or 'UNKNOWN')}.",
                state="unshelving" if status in {"SHELVED", "SHELVED_OFFLOADED"} else "booting",
            )

            if status in {"SHELVED", "SHELVED_OFFLOADED"}:
                await self._update_status(button_id, message="Requesting unshelve from OpenStack…")
                try:
                    await asyncio.to_thread(self._client.unshelve_server, server.id)
                except SDKException as exc:
                    raise RuntimeError(f"Failed to unshelve instance: {exc}") from exc
            else:
                _LOGGER.info(
                    "Instance %s has status %s; skipping unshelve request and monitoring until ACTIVE",
                    button.instance_name,
                    status,
                )

            server = await self._wait_until_active(button_id, server.id, initial=server)
            endpoint = self._client.build_endpoint(server, button)
            if not endpoint:
                await self._update_status(
                    button_id,
                    state="active",
                    message="Instance ACTIVE but no reachable address was found.",
                    url=None,
                    http_ready=False,
                )
                return

            await self._update_status(
                button_id,
                state="active",
                message="Instance ACTIVE. Checking application availability…",
                url=endpoint.launch_url,
                http_ready=False,
            )

            ready, detail = await self._probe_http(button_id, endpoint, button)
            if ready:
                await self._update_status(
                    button_id,
                    state="ready",
                    message="Instance is ready.",
                    url=endpoint.launch_url,
                    http_ready=True,
                    error=None,
                )
                await self.log_event("unshelve_complete", actor=actor, instance_name=button.instance_name, detail=reason)
            else:
                await self._update_status(
                    button_id,
                    state="active",
                    message="Instance ACTIVE but application is not responding.",
                    url=endpoint.launch_url,
                    http_ready=False,
                    error=detail,
                )
                await self.log_event(
                    "unshelve_incomplete",
                    actor=actor,
                    instance_name=button.instance_name,
                    detail=detail or "http probe failed",
                )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Unshelve workflow for %s failed", button.instance_name, exc_info=exc)
            await self._update_status(
                button_id,
                state="error",
                message="Unshelve workflow failed.",
                http_ready=False,
                error=str(exc),
            )
            await self.log_event("workflow_failed", actor=actor, instance_name=button.instance_name, detail=str(exc))
        finally:
            await self._update_status(button_id, running=False)

    async def _run_shelve(self, button: ButtonSettings, *, actor: str, reason: Optional[str]) -> None:
        button_id = button.id
        try:
            server = await asyncio.to_thread(self._client.find_server, button.instance_name)
            if not server:
                raise ResourceNotFound(f"Instance '{button.instance_name}' not found")

            status = (server.status or "").upper()
            if status in {"SHELVED", "SHELVED_OFFLOADED"}:
                await self._update_status(
                    button_id,
                    state="shelved",
                    message="Instance already shelved.",
                    http_ready=False,
                    url=None,
                )
                await self.log_event("shelve_complete", actor=actor, instance_name=button.instance_name, detail="already shelved")
                return

            await self._update_status(
                button_id,
                state="shelving",
                message="Requesting shelve from OpenStack…",
            )
            try:
                await asyncio.to_thread(self._client.shelve_server, server.id)
            except SDKException as exc:
                raise RuntimeError(f"Failed to shelve instance: {exc}") from exc

            await self._wait_until_shelved(button_id, server.id)
            await self._update_status(
                button_id,
                state="shelved",
                message="Instance is shelved and ready for the next wake-up.",
                url=None,
                http_ready=False,
            )
            await self.log_event("shelve_complete", actor=actor, instance_name=button.instance_name, detail=reason)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Shelve workflow for %s failed", button.instance_name, exc_info=exc)
            await self._update_status(
                button_id,
                state="error",
                message="Shelve workflow failed.",
                http_ready=False,
                error=str(exc),
            )
            await self.log_event("workflow_failed", actor=actor, instance_name=button.instance_name, detail=str(exc))
        finally:
            await self._update_status(button_id, running=False)

    async def _wait_until_active(
        self,
        button_id: str,
        server_id: str,
        *,
        initial: Optional[Server] = None,
    ) -> Server:
        poll = self._app_settings.poll_interval_seconds
        server = initial
        while True:
            if server is None:
                server = await asyncio.to_thread(self._client.get_server, server_id)
            status = (getattr(server, "status", None) or "").upper()
            if status == "ACTIVE":
                return server
            if status in {"ERROR", "UNKNOWN"}:
                raise RuntimeError(f"Instance entered {status} state")
            await self._update_status(
                button_id,
                state="booting",
                message=f"Instance status: {status or 'UNKNOWN'}. Re-checking in {poll}s…",
            )
            await asyncio.sleep(poll)
            server = await asyncio.to_thread(self._client.get_server, server_id)

    async def _wait_until_shelved(
        self,
        button_id: str,
        server_id: str,
    ) -> None:
        poll = self._app_settings.poll_interval_seconds
        while True:
            server = await asyncio.to_thread(self._client.get_server, server_id)
            status = (getattr(server, "status", None) or "").upper()
            if status in {"SHELVED", "SHELVED_OFFLOADED"}:
                return
            await self._update_status(
                button_id,
                state="shelving",
                message=f"Shelve request in progress (status {status or 'UNKNOWN'}). Re-checking in {poll}s…",
            )
            await asyncio.sleep(poll)

    async def _probe_http(
        self,
        button_id: str,
        endpoint: InstanceEndpoint,
        button: ButtonSettings,
    ) -> tuple[bool, Optional[str]]:
        attempts = button.http_probe_attempts or self._app_settings.http_probe_attempts
        interval = button.http_probe_interval_seconds or self._app_settings.poll_interval_seconds
        timeout = self._app_settings.http_probe_timeout
        last_detail: Optional[str] = None

        async with httpx.AsyncClient(timeout=timeout, verify=endpoint.verify_tls, follow_redirects=True) as client:
            for attempt in range(1, attempts + 1):
                await self._update_status(
                    button_id,
                    state="checking_http",
                    message=f"Checking service availability ({attempt}/{attempts})…",
                )
                try:
                    response = await client.get(endpoint.healthcheck_url)
                    if response.status_code < 400:
                        return True, None
                    last_detail = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_detail = str(exc)
                if attempt < attempts:
                    await asyncio.sleep(interval)

        return False, last_detail
